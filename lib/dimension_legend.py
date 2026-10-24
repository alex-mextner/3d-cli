"""dimension_legend.py — the colour -> part legend strip drawn under a multi-part render.

WHAT: `layout_legend` turns a `Model`'s parts into wrapped rows of swatch + name entries (and the strip
  height); `draw_legend` paints them under the views. Shared by `3d dimension` (dimension_render.py)
  and the cross-section renderer, so both name their parts the same way.

INVARIANTS: a model with a single part has no legend (height 0); parts are listed by name, not file
  order, so the same assembly always reads the same; a name wider than the image is cut with an
  ellipsis, never wrapped; more than MAX_LEGEND_PARTS parts end in a "+N more" line (no swatch) and the
  sidecar still lists every part.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, NamedTuple

from dimension_model import from_hex
from dimension_parts import Model, PartInfo
from PIL import Image, ImageDraw

ROW_PX = 34
PAD_PX = 14
SWATCH_PX = 22
GAP_PX = 30
TEXT_GAP_PX = 8
MAX_LEGEND_PARTS = 60
RULE_COLOR = (200, 205, 212)
OUTLINE_COLOR = (50, 60, 80)
TEXT_COLOR = (30, 30, 30)


class LegendEntry(NamedTuple):
    text: str
    color: str | None  # swatch colour; None draws no swatch (the "+N more" line)
    width: float  # px, swatch + text
    hatched: bool = False  # diagonal hatch over the swatch (a cut face)


def legend_text(part: PartInfo) -> str:
    """A part's legend entry: its name, plus its role when it is not a printed part.

    A part drawn in a palette colour although its file gave another is marked, so a name like
    "#1A1A1A" never sits beside a swatch of a different colour unexplained.
    """
    if part.role != "printed":
        return f"{part.name} ({part.role.replace('_', '-')})"
    if part.authored and part.color != part.authored:
        return f"{part.name} (recoloured)"
    return part.name


def _text_width(font: Any, text: str) -> float:
    left, _, right, _ = font.getbbox(text)
    return float(right - left)


def layout_legend(
    model: Model,
    font: Any,
    width: int,
    scale: float,
    extra: Sequence[tuple[str, str, bool]] = (),
) -> tuple[list[list[LegendEntry]], int]:
    """Legend rows and the strip height in px; none (height 0) for a single part with no `extra`.

    `extra` entries (text, colour, hatched) follow the parts, e.g. a section's "cut face".
    """
    if len(model.parts) + len(extra) < 2:
        return [], 0
    pad, swatch = PAD_PX * scale, SWATCH_PX * scale
    text_gap, gap = TEXT_GAP_PX * scale, GAP_PX * scale
    ordered = sorted(model.parts, key=lambda p: p.name)
    wanted: list[tuple[str, str | None, bool]] = [
        (legend_text(p), p.color, False) for p in ordered[:MAX_LEGEND_PARTS]
    ]
    if len(ordered) > MAX_LEGEND_PARTS:
        wanted.append(
            (f"+{len(ordered) - MAX_LEGEND_PARTS} more (see the sidecar)", None, False)
        )
    wanted += [(text, color, hatched) for text, color, hatched in extra]
    room = width - 2 * pad - swatch - text_gap
    rows: list[list[LegendEntry]] = [[]]
    x = pad
    for text, color, hatched in wanted:
        while len(text) > 1 and _text_width(font, text) > room:
            text = text[:-2] + "…"
        entry = LegendEntry(
            text,
            color,
            (swatch + text_gap if color else 0.0) + _text_width(font, text),
            hatched,
        )
        if rows[-1] and x + entry.width > width - pad:
            rows.append([])
            x = pad
        rows[-1].append(entry)
        x += entry.width + gap
    return rows, int(round(2 * pad + len(rows) * ROW_PX * scale))


def draw_legend(
    canvas: Image.Image,
    rows: list[list[LegendEntry]],
    top: int,
    font: Any,
    scale: float,
) -> dict[str, Any]:
    """Swatch + name per part in the strip starting at `top`; returns the sidecar `legend` entry."""
    draw = ImageDraw.Draw(canvas)
    pad, swatch = PAD_PX * scale, SWATCH_PX * scale
    text_gap, gap, row_h = TEXT_GAP_PX * scale, GAP_PX * scale, ROW_PX * scale
    draw.line([(0, top), (canvas.width, top)], fill=RULE_COLOR)
    y = top + pad
    for row in rows:
        x = pad
        for entry in row:
            text_x = x
            if entry.color:
                rgb = tuple(round(c * 255) for c in from_hex(entry.color))
                sy = y + (row_h - swatch) / 2
                draw.rectangle(
                    [x, sy, x + swatch, sy + swatch], fill=rgb, outline=OUTLINE_COLOR
                )
                if entry.hatched:  # the engineering-drawing mark of a cut face
                    ink = tuple(round(c * 0.55) for c in rgb)
                    for k in range(1, 4):
                        d = swatch * k / 4
                        draw.line(
                            [(x + d, sy + swatch), (x + swatch, sy + d)], fill=ink
                        )
                        draw.line([(x, sy + swatch - d), (x + d, sy)], fill=ink)
                text_x = x + swatch + text_gap
            left = font.getbbox(entry.text)[0]
            draw.text(
                (text_x - left, y + row_h / 2),
                entry.text,
                font=font,
                fill=TEXT_COLOR,
                anchor="lm",  # one baseline for every entry, whatever its glyphs
            )
            x += entry.width + gap
        y += row_h
    return {"box": [0, top, canvas.width, canvas.height], "rows": len(rows)}
