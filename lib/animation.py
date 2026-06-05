"""Deterministic animation frame planning for existing render workflows."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Sequence

from errors import InvalidArgument
from render import VIEW_NAMES


@dataclass(frozen=True)
class AnimationFrame:
    """One planned render frame."""

    index: int
    progress: float
    output: str
    view: str
    size: str
    defines: tuple[str, ...]
    render_argv: list[str]

    def to_json_data(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "progress": self.progress,
            "output": self.output,
            "view": self.view,
            "size": self.size,
            "defines": list(self.defines),
            "render_argv": list(self.render_argv),
        }


@dataclass(frozen=True)
class AnimationPlan:
    """A deterministic sequence of render frames for a model."""

    model: str
    outdir: str
    frames: tuple[AnimationFrame, ...]

    def to_json_data(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "outdir": self.outdir,
            "frames": [frame.to_json_data() for frame in self.frames],
        }


def _format_number(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.10g}"


def _interpolated_define(spec: str, progress: float) -> str:
    key, sep, value = spec.partition("=")
    if not sep or not key:
        raise InvalidArgument("-D", spec, ["name=value", "name=start:end"], command="animate")

    start_text, range_sep, end_text = value.partition(":")
    if not range_sep:
        return spec

    if not start_text or not end_text:
        raise InvalidArgument(
            "-D",
            spec,
            ["name=value", "name=<number>:<number>"],
            command="animate",
        ) from None
    try:
        start = float(start_text)
        end = float(end_text)
    except ValueError:
        if "?" in value or '"' in value or "'" in value or ("[" in value and "]" in value):
            return spec
        raise InvalidArgument(
            "-D",
            spec,
            ["name=value", "name=<number>:<number>"],
            command="animate",
        ) from None
    if not math.isfinite(start) or not math.isfinite(end):
        raise InvalidArgument("-D", spec, ["finite numeric ranges"], command="animate") from None
    value_at_frame = start + ((end - start) * progress)
    if not math.isfinite(value_at_frame):
        raise InvalidArgument("-D", spec, ["finite numeric ranges"], command="animate") from None
    return f"{key}={_format_number(value_at_frame)}"


def _render_argv(
    model: str,
    *,
    output: str,
    view: str,
    size: str,
    defines: Sequence[str],
) -> list[str]:
    argv = [model, "--view", view, "-o", output, "--size", size]
    for define in defines:
        argv += ["-D", define]
    return argv


def build_frame_plan(
    model: str,
    *,
    outdir: str,
    frames: int,
    view: str,
    size: str,
    defines: Sequence[str],
) -> AnimationPlan:
    """Build a deterministic render frame plan.

    Defines in ``name=start:end`` form are linearly interpolated from the first
    through the last frame. Defines in regular ``name=value`` form pass through.
    """
    if frames < 1:
        raise InvalidArgument("--frames", str(frames), ["a positive integer"], command="animate")
    if view not in VIEW_NAMES:
        raise InvalidArgument("--view", view, VIEW_NAMES, command="animate")

    planned: list[AnimationFrame] = []
    denom = max(frames - 1, 1)
    for index in range(frames):
        progress = index / denom if frames > 1 else 0.0
        output = os.path.join(outdir, f"frame_{index:04d}.png")
        frame_defines = tuple(_interpolated_define(spec, progress) for spec in defines)
        planned.append(
            AnimationFrame(
                index=index,
                progress=progress,
                output=output,
                view=view,
                size=size,
                defines=frame_defines,
                render_argv=_render_argv(
                    model,
                    output=output,
                    view=view,
                    size=size,
                    defines=frame_defines,
                ),
            )
        )
    return AnimationPlan(model=model, outdir=outdir, frames=tuple(planned))
