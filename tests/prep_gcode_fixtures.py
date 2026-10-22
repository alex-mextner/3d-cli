"""Synthetic Orca-style G-code for the prep G-code gate and view tests (no slicer needed).

One printed disc per layer: an outer wall loop, `loops - 1` inner wall loops, sparse infill as parallel chords
spaced `line_width / density` apart (so the true infill density is `density`), optional prime tower square,
tool changes `Tn`, M83 relative extrusion, header comments at the end like the slicer's config block.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path

LINE_W = 0.45
CENTRE = (135.0, 135.0)


def _loop(cx: float, cy: float, r: float, n: int = 72) -> list[tuple[float, float]]:
    return [
        (cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n))
        for i in range(n + 1)
    ]


def _extrude(pts: Sequence[tuple[float, float]]) -> list[str]:
    out = [f"G0 X{pts[0][0]:.3f} Y{pts[0][1]:.3f}"]
    out += [f"G1 X{x:.3f} Y{y:.3f} E0.05000" for x, y in pts[1:]]
    return out


def gcode_text(
    layers: Sequence[tuple[float, int]],
    *,
    loops: int = 4,
    density: float = 0.15,
    centre: tuple[float, float] = CENTRE,
    radius: float = 20.0,
    tower: bool = False,
    support_layer: int | None = None,
    travel_out: float | None = None,
    printable_area: str | None = None,
    grams: str = "1.5, 2.5",
    time_text: str = "1h 2m 3s",
) -> str:
    """`layers` = (thickness mm, tool) bottom to top. `travel_out` adds a non-extruding G0 to that X in layer 2."""
    cx, cy = centre
    lines = [
        "; synthetic test G-code",
        "M83",
        "T0",
        "G1 X5 Y5 E5 ; start purge, outside every layer",
    ]
    z = 0.0
    for k, (h, tool) in enumerate(layers):
        z += h
        lines += [";LAYER_CHANGE", f";Z:{z:.3f}", f";HEIGHT:{h:.3f}"]
        lines.append(f"T{tool}")
        lines.append(";TYPE:Outer wall")
        lines += _extrude(_loop(cx, cy, radius))
        if loops > 1:
            lines.append(";TYPE:Inner wall")
            for i in range(1, loops):
                lines += _extrude(_loop(cx, cy, radius - LINE_W * i))
        r_in = radius - LINE_W * max(loops - 1, 1)
        fill_r = r_in - LINE_W / 2 - 0.1
        spacing = LINE_W / density
        lines.append(";TYPE:Sparse infill")
        y = -fill_r + spacing / 2
        while y < fill_r:
            half = math.sqrt(fill_r**2 - y**2)
            lines += _extrude([(cx - half, cy + y), (cx + half, cy + y)])
            y += spacing
        if tower:
            lines.append(";TYPE:Prime tower")
            lines += _extrude([(20, 20), (30, 20), (30, 30), (20, 30), (20, 20)])
        if support_layer == k:
            lines.append(";TYPE:Support")
            lines += _extrude([(cx - 5, cy), (cx + 5, cy)])
        if travel_out is not None and k == 2:
            lines.append(f"G0 X{travel_out:.1f} Y{cy:.1f}")
    lines += [
        f"; filament used [g] = {grams}",
        f"; estimated printing time (normal mode) = {time_text}",
        f"; sparse_infill_line_width = {LINE_W}",
        f"; inner_wall_line_width = {LINE_W}",
        "; enable_support = 0",
    ]
    if printable_area:
        lines.append(f"; printable_area = {printable_area}")
    return "\n".join(lines) + "\n"


def write_gcode(path: Path, layers: Sequence[tuple[float, int]], **kw: object) -> Path:
    path.write_text(gcode_text(layers, **kw))  # type: ignore[arg-type]
    return path


EYE_BBOX = (100.0, 130.0, 110.0, 140.0)  # x0, y0, x1, y1: the "eye" part, beside the body disc (x 115..155)


def multipart_gcode(
    layers: Sequence[tuple[float, Sequence[int]]],
    *,
    outside_bbox: dict[int, int] | None = None,
) -> str:
    """A tool-0 body disc on every layer plus a small box at `EYE_BBOX` printed by the tools listed per layer.

    `layers` = (thickness mm, tools printing inside the eye bbox in that layer, in print order).
    `outside_bbox` = {layer index: tool}: that tool also prints a line on the body (outside the bbox) in that layer.
    """
    x0, y0, x1, y1 = EYE_BBOX
    lines = ["M83", "T0"]
    z = 0.0
    for k, (h, eye_tools) in enumerate(layers):
        z += h
        lines += [";LAYER_CHANGE", f";Z:{z:.3f}", f";HEIGHT:{h:.3f}", "T0", ";TYPE:Outer wall"]
        lines += _extrude(_loop(CENTRE[0], CENTRE[1], 20.0))
        if outside_bbox and k in outside_bbox:
            lines += [f"T{outside_bbox[k]}", ";TYPE:Inner wall", *_extrude([(125.0, 135.0), (145.0, 135.0)])]
        for t in eye_tools:
            lines += [f"T{t}", ";TYPE:Internal solid infill"]
            lines += _extrude([(x0 + 1, y0 + 1), (x1 - 1, y0 + 1), (x1 - 1, y1 - 1), (x0 + 1, y1 - 1)])
    return "\n".join(lines) + "\n"


def write_multipart(path: Path, layers: Sequence[tuple[float, Sequence[int]]], **kw: object) -> Path:
    path.write_text(multipart_gcode(layers, **kw))  # type: ignore[arg-type]
    return path
