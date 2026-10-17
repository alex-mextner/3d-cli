"""topcam.focus — focus modes for a still, including a software focus sweep.

WHAT: `parse_focus("auto" | "N" | "sweep[:LO-HI[:STEP]]")` → FocusMode; `sweep(dev, ...)` steps
  `focus_absolute` over a range (coarse, then fine around the best), grabs one small luma frame
  per position and scores its sharpness (Brenner gradient, squared differences two pixels
  apart) inside a region of interest; the best position is used for the series.
WHY: the C920's continuous autofocus hunts and picks whatever has contrast, not the plate; a
  sweep finds the sharpest focus on the bed/markers region and keeps it for every frame of the
  series (the camera is reset to its defaults after the still, see v4l2.locked).
INVARIANTS: stdlib only — runs on the Pi Zero W node; the scoring works on a subsampled ROI so
  one position costs well under a second there.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from topcam import v4l2

DEFAULT_RANGE = (0, 250)
DEFAULT_STEP = 25
FINE_STEP = 5
DEFAULT_ROI = (0.25, 0.25, 0.75, 0.75)
SWEEP_RES = (1280, 720)


class FocusError(ValueError):
    """A focus spec that cannot be parsed or a sweep the camera cannot do."""


@dataclass(frozen=True)
class FocusMode:
    mode: str  # auto | manual | sweep | default (camera default, autofocus on for the C920)
    value: int | None = None
    lo: int = DEFAULT_RANGE[0]
    hi: int = DEFAULT_RANGE[1]
    step: int = DEFAULT_STEP

    def text(self) -> str:
        if self.mode == "manual":
            return str(self.value)
        if self.mode == "sweep":
            return f"sweep:{self.lo}-{self.hi}:{self.step}"
        return self.mode


def parse_focus(text: str | int | None) -> FocusMode:
    if text is None:
        return FocusMode("default")
    if isinstance(text, int) and not isinstance(text, bool):
        return FocusMode("manual", text)
    raw = str(text).strip().lower()
    if raw == "auto":
        return FocusMode("auto")
    if raw.lstrip("-").isdigit():
        return FocusMode("manual", int(raw))
    if raw.startswith("sweep"):
        parts = raw.split(":")
        lo, hi, step = DEFAULT_RANGE[0], DEFAULT_RANGE[1], DEFAULT_STEP
        try:
            if len(parts) > 1 and parts[1]:
                a, sep, b = parts[1].partition("-")
                if not sep:
                    raise ValueError
                lo, hi = int(a), int(b)
            if len(parts) > 2 and parts[2]:
                step = int(parts[2])
            if len(parts) > 3:
                raise ValueError
        except ValueError as exc:
            raise FocusError(
                f"focus {text!r}: sweep is sweep[:LO-HI[:STEP]], e.g. sweep:0-250:25"
            ) from exc
        if lo >= hi or step < 1:
            raise FocusError(f"focus {text!r}: need LO < HI and STEP >= 1")
        return FocusMode("sweep", None, lo, hi, step)
    raise FocusError(
        f"focus {text!r}: accepted auto | N (manual position) | sweep[:LO-HI[:STEP]]"
    )


def parse_roi(text: str | None) -> tuple[float, float, float, float]:
    if not text:
        return DEFAULT_ROI
    try:
        x0, y0, x1, y1 = (float(v) for v in text.split(","))
    except ValueError as exc:
        raise FocusError(
            f"focus ROI {text!r}: want X0,Y0,X1,Y1 as frame fractions 0..1"
        ) from exc
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise FocusError(
            f"focus ROI {text!r}: need 0 <= X0 < X1 <= 1 and 0 <= Y0 < Y1 <= 1"
        )
    return x0, y0, x1, y1


def sharpness(
    luma: bytes,
    width: int,
    height: int,
    roi: tuple[float, float, float, float] = DEFAULT_ROI,
    sub: int = 2,
) -> float:
    """Mean squared Brenner gradient (horizontal + vertical, 2 px apart) inside `roi`,
    sampling every `sub`-th row/column. Higher = sharper; comparable only between frames of
    the same scene and exposure."""
    x0, x1 = int(roi[0] * width), int(roi[2] * width)
    y0, y1 = int(roi[1] * height), int(roi[3] * height)
    total = 0
    count = 0
    for y in range(y0, y1 - 2, sub):
        row = luma[y * width + x0 : y * width + x1 : sub]
        below = luma[(y + 2) * width + x0 : (y + 2) * width + x1 : sub]
        dx = [a - b for a, b in zip(row[1:], row[:-1], strict=False)]
        dy = [a - b for a, b in zip(below, row, strict=False)]
        total += sum(d * d for d in dx) + sum(d * d for d in dy)
        count += len(dx) + len(dy)
    return total / count if count else 0.0


def _positions(lo: int, hi: int, step: int, ctrl_step: int) -> list[int]:
    step = max(step, ctrl_step)
    out = list(range(lo, hi + 1, step))
    if out[-1] != hi:
        out.append(hi)
    return [p - p % ctrl_step for p in out] if ctrl_step > 1 else out


def sweep(
    dev: v4l2.Device,
    mode: FocusMode,
    roi: tuple[float, float, float, float] = DEFAULT_ROI,
    res: tuple[int, int] = SWEEP_RES,
    settle_s: float = 0.25,
    skip: int = 2,
) -> dict[str, Any]:
    """Coarse pass over [lo, hi] at `step`, then a fine pass at the control's step (C920: 5)
    within one coarse step of the best. Autofocus must already be off (the caller's lock).
    Returns {"best", "scores": [[position, sharpness], ...], "roi", "res"}."""
    info = dev.resolve("focus")
    lo, hi = max(mode.lo, info.minimum), min(mode.hi, info.maximum)
    if lo >= hi:
        raise FocusError(
            f"focus sweep {lo}-{hi} is outside the camera's {info.minimum}..{info.maximum}"
        )
    ctrl_step = max(1, info.step)
    scores: dict[int, float] = {}

    def measure(pos: int) -> None:
        if pos in scores:
            return
        dev.set_controls({"focus": pos})
        time.sleep(settle_s)
        shot = dev.capture(res[0], res[1], 1, skip)
        scores[pos] = sharpness(v4l2.yuyv_luma(shot.frames[0]), res[0], res[1], roi)

    for pos in _positions(lo, hi, mode.step, ctrl_step):
        measure(pos)
    best = max(scores, key=lambda p: scores[p])
    fine = max(ctrl_step, FINE_STEP if FINE_STEP % ctrl_step == 0 else ctrl_step)
    if mode.step > fine:
        for pos in _positions(
            max(lo, best - mode.step), min(hi, best + mode.step), fine, ctrl_step
        ):
            measure(pos)
        best = max(scores, key=lambda p: scores[p])
    dev.set_controls({"focus": best})
    return {
        "best": best,
        "scores": [[p, round(scores[p], 3)] for p in sorted(scores)],
        "roi": list(roi),
        "res": list(res),
    }
