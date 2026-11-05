"""bed_refs.py — which empty-bed reference stands for a frame, how old it is, and the journal of automatic
refreshes (issue #723, item 3). Stdlib only; the pixels stay in bed_state.py.

A reference is identified by (kind, plate type, light state, Z):
  plate type  `watch.bed.plate` of the printer (default `default`): a smooth and a textured plate look
              different; a reference of another plate type is judged against only as a last resort and then
              never makes the verdict `occupied`.
  light state `on` / `off` / `unknown`: whether the printer's light was on when the reference was taken
              (the `light` report of the bed check). A frame is judged against a reference of its own light
              state first; another light state is a fallback that is marked `reference_light_mismatch` and
              judged by the lighting-normalised compare (bed_norm.py).
  age         `ts`; older than `watch.bed.reference_max_age_days` (default 30) the reference is stale: an
              `occupied` against it is `unknown` with the command that takes a new one.

Files: `<kind>-z<Z>.json` (the legacy name: plate `default`, light `unknown`) and
`<kind>-z<Z>[-<plate>][-<light>].json`, each with its `.jpg` / `.volume.png` / `.surface.png` next to it.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_PLATE = "default"
LIGHT_ON, LIGHT_OFF, LIGHT_UNKNOWN = "on", "off", "unknown"
LIGHT_STATES = (LIGHT_ON, LIGHT_OFF, LIGHT_UNKNOWN)
PLATE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
REFERENCE_MAX_AGE_DAYS = 30.0


def light_state(report: Mapping[str, Any] | None) -> str:
    """`on` | `off` | `unknown` from the `light` of a bed verdict (printer_ops.bed_light.LightReport.to_dict):
    was the bed lit when the frame was taken?"""
    lit = (report or {}).get("on_for_frame")
    return LIGHT_ON if lit is True else LIGHT_OFF if lit is False else LIGHT_UNKNOWN


def light_unavailable(report: Mapping[str, Any] | None) -> bool:
    """Is there no light to switch for this printer (none found, or turned off by its config)? Then a frame
    is as lit as it can be and `light on` cannot be asked for."""
    r = report or {}
    skipped = str(r.get("skipped") or "")
    return not r.get("error") and (
        skipped.startswith("off by config") or (not r.get("found") and not skipped)
    )


def stem(
    kind: str, z: float, plate: str = DEFAULT_PLATE, light: str = LIGHT_UNKNOWN
) -> str:
    """File stem of a reference; the legacy name for the default plate and an unknown light."""
    return (
        f"{kind}-z{z:.1f}"
        + ("" if plate == DEFAULT_PLATE else f"-{plate}")
        + ("" if light == LIGHT_UNKNOWN else f"-{light}")
    )


def rank(
    ref_plate: str, ref_light: str, plate: str | None, light: str | None
) -> tuple[int, int]:
    """Sort key of a candidate reference (smaller is better): the plate type first (0 same, 1 another), then the
    light state (0 same, 1 one of them unknown, 2 another known state). `None` asks for no preference."""
    p = 0 if plate is None or ref_plate == plate else 1
    if light is None or light == ref_light:
        lt = 0
    elif LIGHT_UNKNOWN in (light, ref_light):
        lt = 1
    else:
        lt = 2
    return p, lt


def light_differs(ref_light: str, light: str) -> bool:
    """Are both light states known and different (a reference taken lit, a frame taken dark, or the reverse)?"""
    return LIGHT_UNKNOWN not in (ref_light, light) and ref_light != light


WALL_CLOCK_FROM = 946684800.0  # 2000-01-01: an earlier `ts` is not a time of day (0 = never recorded, 1.0 = a test)


def age_days(ts: float, now: float) -> float | None:
    """Age of a reference in days; None when its time was never recorded (ts 0, or not a wall-clock time)."""
    return None if ts < WALL_CLOCK_FROM else max(0.0, (now - ts) / 86400.0)


def refresh_command(printer: str) -> str:
    return f"3d printer bed reference {printer} --refresh"


@dataclass(frozen=True)
class Tags:
    """What a stored reference records besides its pixels: the plate type, the light state, and the owner's
    reason when it was stored on their word (`--assume-empty`)."""

    light: str = LIGHT_UNKNOWN
    plate: str = DEFAULT_PLATE
    assumed: str | None = None


@dataclass(frozen=True)
class JudgeContext:
    """What a verdict needs to know about the frame beyond its pixels: its light state, the plate type in
    use, and when a reference counts as stale (`max_age_days` None: never)."""

    light: str = LIGHT_UNKNOWN
    plate: str = DEFAULT_PLATE
    max_age_days: float | None = None
    now: float = field(default_factory=time.time)


def describe(
    ref_ts: float, ref_light: str, ref_plate: str, ref_path: Path, ctx: JudgeContext
) -> dict[str, Any]:
    """The `reference` block of a verdict: which reference it was judged against, its age, and why it may be
    unfit (`unfit`: another plate type or older than the limit)."""
    age = age_days(ref_ts, ctx.now)
    stale = age is not None and ctx.max_age_days is not None and age > ctx.max_age_days
    return {
        "path": str(ref_path),
        "plate": ref_plate,
        "light": ref_light,
        "age_days": None if age is None else round(age, 1),
        "stale": stale,
        "other_plate": ref_plate != ctx.plate,
    }


def unfit_reason(
    info: Mapping[str, Any], ctx: JudgeContext, printer: str
) -> str | None:
    """Why `occupied` against this reference is not to be trusted (stale, or taken with another plate), or None."""
    why = []
    if info.get("stale"):
        why.append(
            f"the reference is {info['age_days']:.0f} days old (watch.bed.reference_max_age_days: "
            f"{ctx.max_age_days:g})"
        )
    if info.get("other_plate"):
        why.append(
            f"the reference is of plate type '{info['plate']}', this printer's plate is '{ctx.plate}'"
        )
    if not why:
        return None
    return f"{' and '.join(why)}; take a new one with the owner's confirmation: `{refresh_command(printer)}`"


# ---- may a frame become the reference? ------------------------------------------------------


def refresh_refusal(verdict: Mapping[str, Any], view_min: float) -> str | None:
    """Why the bed check `verdict` (a fresh frame, from `bed_state.status_view`) is not good enough to become
    the reference, or None: a CONFIDENT `clear` with the plate on, in a good view (`view_min` of the plate in
    the frame, `watch.bed.auto_view_min`), with the printer's light on (or no light to switch)."""
    if verdict.get("bed") != "clear" or verdict.get("plate") != "present":
        return f"the bed check says bed {verdict.get('bed')}, plate {verdict.get('plate')}, not clear with the plate on"
    metrics = verdict.get("metrics") or {}
    seen = metrics.get("bed_in_view")
    if isinstance(seen, (int, float)) and seen < view_min:
        return f"only {seen:.0%} of the plate is in the camera's view (needs {view_min:.0%})"
    light = verdict.get("light")
    if light_state(light) != LIGHT_ON and not light_unavailable(light):
        text = (light or {}).get("text") if isinstance(light, Mapping) else None
        return f"the frame was not taken with the light on ({text or 'light state unknown'})"
    return None


# ---- the journal of refreshes -----------------------------------------------------------------


def journal_path(printer: str) -> Path:
    from cli.paths import state_dir

    return state_dir() / "bed" / f"{printer}.refs.log"


def journal_refresh(
    printer: str,
    *,
    why: str,
    z: float | None,
    light: str,
    verdict: str,
    now: float | None = None,
    mode: str = "auto",
) -> Path:
    """Append one JSON line: when, why, at which Z, with which light, the verdict that allowed it."""
    path = journal_path(printer)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "ts": time.time() if now is None else now,
        "mode": mode,
        "why": why,
        "z": z,
        "light": light,
        "verdict": verdict,
    }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
    return path
