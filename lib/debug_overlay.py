"""Pure planning and advisory helpers for render/reference debug overlays."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Iterable, Sequence

from errors import InvalidArgument, UsageError

VALID_MODES = ("difference", "ghost", "edge")
MODE_ALIASES = {
    "diff": "difference",
    "difference": "difference",
    "ghost": "ghost",
    "blend": "ghost",
    "edge": "edge",
    "edges": "edge",
    "all": "all",
}


@dataclass(frozen=True)
class OverlayArtifact:
    kind: str
    path: str
    advice: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "path": self.path, "advice": self.advice}


@dataclass(frozen=True)
class OverlayPlan:
    render: str
    reference: str
    out_dir: str
    artifacts: tuple[OverlayArtifact, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "render": self.render,
            "reference": self.reference,
            "out_dir": self.out_dir,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }


@dataclass(frozen=True)
class OverlayAdvice:
    bucket: str
    ae: int
    mismatch_ratio: float
    next_steps: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "bucket": self.bucket,
            "ae": self.ae,
            "mismatch_ratio": self.mismatch_ratio,
            "next_steps": list(self.next_steps),
        }


_ADVICE_BY_MODE = {
    "difference": "Use for broad spatial error: bright regions mark changed pixels.",
    "ghost": "Use for camera/scale drift: the reference and render are blended 50/50.",
    "edge": "Use when silhouettes disagree: red=reference edges, cyan=render edges.",
}
_FILENAMES = {
    "difference": "overlay.png",
    "ghost": "ghost.png",
    "edge": "edge_overlay.png",
}


def normalize_modes(values: Iterable[str] | None = None) -> tuple[str, ...]:
    """Return normalized overlay modes, preserving declaration order."""
    if values is None:
        return VALID_MODES

    found: list[str] = []
    for raw in values:
        for token in raw.split(","):
            value = token.strip().lower()
            if not value:
                continue
            mode = MODE_ALIASES.get(value)
            if mode is None:
                raise InvalidArgument("--mode", token, (*VALID_MODES, "all"), command="overlay")
            if mode == "all":
                for default in VALID_MODES:
                    if default not in found:
                        found.append(default)
                continue
            if mode not in found:
                found.append(mode)
    return tuple(found) if found else VALID_MODES


def build_plan(
    render: str,
    reference: str,
    *,
    out_dir: str = "",
    modes: Sequence[str] | None = None,
) -> OverlayPlan:
    """Build the artifact plan without touching the filesystem or external tools."""
    resolved_out = out_dir or os.path.dirname(render) or "."
    artifacts = tuple(
        OverlayArtifact(
            kind=mode,
            path=os.path.join(resolved_out, _FILENAMES[mode]),
            advice=_ADVICE_BY_MODE[mode],
        )
        for mode in normalize_modes(modes)
    )
    return OverlayPlan(render=render, reference=reference, out_dir=resolved_out, artifacts=artifacts)


def summarize_advice(ae_raw: str, *, frame_pixels: int) -> OverlayAdvice:
    """Convert ImageMagick AE output into a small advisory bucket."""
    if frame_pixels <= 0:
        raise UsageError("frame has zero pixels", command="overlay")
    parts = ae_raw.split()
    token = parts[0] if parts else ""
    try:
        ae = int(round(float(token)))
    except ValueError:
        raise UsageError(f"could not parse AE mismatch count: {ae_raw}", command="overlay") from None

    ratio = ae / frame_pixels
    if ae == 0:
        return OverlayAdvice(
            bucket="identical",
            ae=ae,
            mismatch_ratio=ratio,
            next_steps=("The render and reference pixels match at the selected fuzz.",),
        )
    if ratio <= 0.05:
        return OverlayAdvice(
            bucket="minor",
            ae=ae,
            mismatch_ratio=ratio,
            next_steps=(
                "Generate an edge overlay if localized outline drift needs confirmation.",
                "Check small parameter changes before changing the camera.",
            ),
        )
    return OverlayAdvice(
        bucket="major",
        ae=ae,
        mismatch_ratio=ratio,
        next_steps=(
            "Check camera, scale, and subject isolation before tuning model parameters.",
            "Generate ghost or difference overlays if alignment remains unclear.",
        ),
    )


def plan_to_json(plan: OverlayPlan, advice: OverlayAdvice | None = None) -> str:
    payload = plan.to_dict()
    if advice is not None:
        payload["advice"] = advice.to_dict()
    return json.dumps(payload, sort_keys=True)


def format_plan(plan: OverlayPlan) -> str:
    lines = [
        f"[overlay] render={plan.render} ref={plan.reference}",
        f"[overlay] out={plan.out_dir}",
        "[overlay] planned artifacts:",
    ]
    for artifact in plan.artifacts:
        lines.append(f"  {artifact.kind:<10} {artifact.path}")
        lines.append(f"    {artifact.advice}")
    return "\n".join(lines)


def format_advice(advice: OverlayAdvice) -> str:
    lines = [
        "[overlay] advice:",
        f"  bucket={advice.bucket}",
        f"  AE={advice.ae}",
        f"  mismatch_ratio={advice.mismatch_ratio:.6f}",
    ]
    for step in advice.next_steps:
        lines.append(f"  - {step}")
    return "\n".join(lines)
