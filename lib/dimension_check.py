"""dimension_check.py — the legibility gate for dimensioned renders (`3d dimension --check`).

WHAT: decide whether the dimension labels of an image written by `3d dimension` can be READ.
  A render without legible sizes does not let the owner confirm a model before a print, so
  this gate is what other tools (tg-cli's send gate) call before an image goes out.

HOW: the image is checked against its `<image>.dims.json` sidecar (written by
  dimension_render.py) AND against its own pixels:
    - sidecar present, parsable and bound to this exact image (sha256 + size);
    - at least MIN_DIMENSIONS labelled dimensions;
    - font size >= MIN_FONT_PX measured at a 1280 px wide image (a bigger image scales it),
      and the INK of the label really is that tall in the pixels (a lying sidecar fails);
    - every label box inside the image and inside its own view panel;
    - no two label boxes overlap;
    - a label does not cover the part's silhouette (more than MAX_GEOMETRY_OVERLAP of its box);
    - the text contrasts with the label background (WCAG ratio >= MIN_CONTRAST);
    - the label shows its value (text contains the sidecar's value_mm).

INVARIANTS: no top-level heavy import beyond numpy/PIL (this module is only loaded by the
  tool, never by command discovery); thresholds are module constants so tests and callers
  share one definition.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image

MIN_FONT_PX = 14.0  # em size at REFERENCE_WIDTH
REFERENCE_WIDTH = 1280
# Digit/cap height of the label font relative to its em; ink shorter than this at the
# minimum font means the text is smaller than the sidecar claims.
CAP_HEIGHT_RATIO = 0.70
MIN_DIMENSIONS = 3
MIN_CONTRAST = 4.5  # WCAG AA for normal text
MAX_GEOMETRY_OVERLAP = 0.10
MIN_INK_FRACTION = 0.01
INK_DISTANCE = (
    80  # per-channel distance from the label background that counts as text ink
)
VALUE_TOLERANCE_MM = 0.051  # the label shows one decimal; the sidecar keeps three
SIDECAR_SUFFIX = ".dims.json"


@dataclass(frozen=True)
class Offender:
    """One legibility failure: which label (or the image as a whole) and why."""

    label: str
    rule: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"label": self.label, "rule": self.rule, "detail": self.detail}


def sidecar_path(image: str) -> str:
    return image + SIDECAR_SUFFIX


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _luminance(rgb: np.ndarray) -> float:
    channels = rgb.astype(float) / 255.0
    lin = np.where(
        channels <= 0.03928, channels / 12.92, ((channels + 0.055) / 1.055) ** 2.4
    )
    return float(0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2])


def contrast_ratio(fg: np.ndarray, bg: np.ndarray) -> float:
    """WCAG 2 contrast ratio between two RGB colours (1.0 .. 21.0)."""
    lf, lb = _luminance(fg), _luminance(bg)
    hi, lo = max(lf, lb), min(lf, lb)
    return (hi + 0.05) / (lo + 0.05)


def rects_overlap(a: list[float], b: list[float]) -> bool:
    """Do two [x0, y0, x1, y1] rectangles share area?"""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _inside(inner: list[float], outer: list[float]) -> bool:
    return (
        inner[0] >= outer[0]
        and inner[1] >= outer[1]
        and inner[2] <= outer[2]
        and inner[3] <= outer[3]
    )


def _grid_overlap(box: list[float], grid: dict[str, Any]) -> float:
    """Fraction of `box` lying on silhouette cells of a sidecar silhouette grid."""
    cell = max(int(grid["cell"]), 1)
    gx0, gy0 = int(grid["x0"]), int(grid["y0"])
    rows: list[str] = grid["rows"]
    covered = 0
    total = 0
    for y in range(int(box[1]), int(np.ceil(box[3]))):
        for x in range(int(box[0]), int(np.ceil(box[2]))):
            total += 1
            cx, cy = (x - gx0) // cell, (y - gy0) // cell
            if 0 <= cy < len(rows) and 0 <= cx < len(rows[cy]) and rows[cy][cx] == "#":
                covered += 1
    return covered / total if total else 0.0


@dataclass(frozen=True)
class _Label:
    """One sidecar dimension entry, validated."""

    label: str
    view: str
    font_px: float
    box: list[float]
    text_box: list[float]
    value: float
    text: str

    @property
    def name(self) -> str:
        return f"{self.label} [{self.view}]"


def _number(raw: Any, what: str) -> float:
    """A finite float: JSON allows NaN/Infinity, a sidecar must not carry them."""
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"{what} is not finite")
    return value


def _rect(raw: Any, what: str) -> list[float]:
    rect = [_number(v, what) for v in raw]
    if len(rect) != 4:
        raise ValueError(f"{what} needs 4 numbers, got {len(rect)}")
    return rect


def _grid(raw: Any) -> dict[str, Any]:
    """A validated silhouette grid: positive finite cell, int origin, equal-length rows of '#'/'.'."""
    rows = raw["rows"]
    if not isinstance(rows, list) or not all(isinstance(r, str) for r in rows):
        raise ValueError("silhouette_grid rows must be a list of strings")
    if any(len(r) != len(rows[0]) or set(r) - {"#", "."} for r in rows):
        raise ValueError(
            "silhouette_grid rows must be equal-length strings of '#' and '.'"
        )
    cell = int(_number(raw["cell"], "silhouette_grid cell"))
    if cell < 1:
        raise ValueError("silhouette_grid cell must be at least 1")
    return {
        "cell": cell,
        "x0": int(_number(raw["x0"], "silhouette_grid x0")),
        "y0": int(_number(raw["y0"], "silhouette_grid y0")),
        "rows": rows,
    }


def _parse_label(raw: Any) -> _Label:
    label = _Label(
        label=str(raw["label"]),
        view=str(raw["view"]),
        font_px=_number(raw["font_px"], "font_px"),
        box=_rect(raw["box"], "box"),
        text_box=_rect(raw["text_box"], "text_box"),
        value=_number(raw["value_mm"], "value_mm"),
        text=str(raw["text"]),
    )
    if not _inside(label.text_box, label.box):
        raise ValueError(f"{label.name}: text_box is not inside box")
    return label


def _check_pixels(
    pixels: np.ndarray, dim: _Label, scale: float, min_font_px: float
) -> list[Offender]:
    """Contrast and real text height of one label, from the image's own pixels."""
    height, width = pixels.shape[:2]
    x0, y0, x1, y1 = (int(v) for v in dim.text_box)
    x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, width), min(y1, height)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return [
            Offender(
                dim.name, "outside-image", "label text has no pixels inside the image"
            )
        ]
    crop = pixels[y0:y1, x0:x1, :3]
    flat = crop.reshape(-1, 3)
    colors, counts = np.unique(flat, axis=0, return_counts=True)
    background = colors[int(np.argmax(counts))]
    distance = np.abs(flat.astype(int) - background.astype(int)).max(axis=1)
    ink = distance > INK_DISTANCE
    if ink.mean() < MIN_INK_FRACTION:
        return [
            Offender(
                dim.name, "no-text", "no readable text pixels inside the label box"
            )
        ]
    out: list[Offender] = []
    foreground = np.median(flat[distance >= np.percentile(distance[ink], 80)], axis=0)
    ratio = contrast_ratio(foreground, background)
    if ratio < MIN_CONTRAST:
        out.append(
            Offender(
                dim.name,
                "low-contrast",
                f"text/background contrast {ratio:.1f}:1 < {MIN_CONTRAST}:1",
            )
        )
    rows = np.where(ink.reshape(y1 - y0, x1 - x0).any(axis=1))[0]
    ink_height = float(rows.max() - rows.min() + 1)
    # Text grows with the image: a font of MIN_FONT_PX at 1280 px is taller on a wider image.
    need = CAP_HEIGHT_RATIO * min_font_px * scale
    if ink_height < need:
        out.append(
            Offender(
                dim.name,
                "text-too-short",
                f"text is {ink_height:.0f} px tall; needs >= {need:.0f} px at this image size",
            )
        )
    return out


def _shows_value(text: str, value: float) -> bool:
    """Does the label text end in `<number> mm` equal to `value` at its printed precision?"""
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*mm\s*$", text)
    return (
        match is not None and abs(float(match.group(1)) - value) <= VALUE_TOLERANCE_MM
    )


def _grid_matches_rect(grid: dict[str, Any], rect: list[float], view: str) -> None:
    """A silhouette grid must start at its view's origin and span the whole panel, else it hides collisions."""
    cell = grid["cell"]
    want_rows = math.ceil((rect[3] - rect[1]) / cell)
    want_cols = math.ceil((rect[2] - rect[0]) / cell)
    if (grid["x0"], grid["y0"]) != (int(rect[0]), int(rect[1])):
        raise ValueError(f"view {view}: silhouette_grid origin is not the view origin")
    if len(grid["rows"]) != want_rows or any(len(r) != want_cols for r in grid["rows"]):
        raise ValueError(f"view {view}: silhouette_grid does not cover the view")


def _load_sidecar(
    side: str,
) -> tuple[list[_Label], dict[str, dict[str, Any]], str, tuple[int, int]]:
    """Parse and validate the sidecar; ValueError/KeyError/TypeError/OSError when malformed."""
    with open(side, encoding="utf-8") as handle:
        data = json.load(handle)
    labels = [_parse_label(raw) for raw in data["dimensions"]]
    views: dict[str, dict[str, Any]] = {}
    for view, entry in data["views"].items():
        raw_grid = entry.get("silhouette_grid")
        rect = _rect(entry["rect"], f"view {view} rect")
        grid = None if raw_grid is None else _grid(raw_grid)
        if grid is not None:
            _grid_matches_rect(grid, rect, str(view))
        views[str(view)] = {"rect": rect, "grid": grid}
    image = data["image"]
    return (
        labels,
        views,
        str(image["sha256"]),
        (int(image["width"]), int(image["height"])),
    )


def _check_label(
    dim: _Label,
    views: dict[str, dict[str, Any]],
    pixels: np.ndarray,
    scale: float,
    min_font_px: float,
) -> list[Offender]:
    height, width = pixels.shape[:2]
    font_px = dim.font_px / scale
    out: list[Offender] = []
    if font_px < min_font_px:
        out.append(
            Offender(
                dim.name,
                "font-too-small",
                f"font {font_px:.1f} px at {REFERENCE_WIDTH} px width < {min_font_px:g} px",
            )
        )
    if not _inside(dim.box, [0.0, 0.0, float(width), float(height)]):
        out.append(
            Offender(
                dim.name,
                "outside-image",
                f"label box {dim.box} leaves the {width}x{height} image",
            )
        )
        return out
    view = views.get(dim.view)
    if view is None:
        out.append(
            Offender(
                dim.name, "unknown-view", f"view {dim.view!r} is not in the sidecar"
            )
        )
    else:
        if not _inside(dim.box, view["rect"]):
            out.append(
                Offender(
                    dim.name,
                    "outside-panel",
                    f"label box {dim.box} leaves its {dim.view} panel {view['rect']}",
                )
            )
        if view["grid"] is not None:
            share = _grid_overlap(dim.box, view["grid"])
            if share > MAX_GEOMETRY_OVERLAP:
                out.append(
                    Offender(
                        dim.name,
                        "covers-geometry",
                        f"label hides {share:.0%} of itself over the part silhouette",
                    )
                )
    out.extend(_check_pixels(pixels, dim, scale, min_font_px))
    if not _shows_value(dim.text, dim.value):
        out.append(
            Offender(
                dim.name,
                "value-not-shown",
                f"text {dim.text!r} does not end in {dim.value:.1f} mm",
            )
        )
    return out


def check_image(
    image: str,
    sidecar: str | None = None,
    *,
    min_font_px: float = MIN_FONT_PX,
    min_dimensions: int = MIN_DIMENSIONS,
    require_dims: bool = False,
) -> list[Offender]:
    """Return every legibility offender of `image` (empty list = legible)."""
    side = sidecar or sidecar_path(image)
    if not os.path.isfile(side):
        return [
            Offender(
                "(image)",
                "no-sidecar",
                f"{side} not found — render with `3d dimension`",
            )
        ]
    try:
        labels, views, recorded_sha, recorded_size = _load_sidecar(side)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return [
            Offender(
                "(image)",
                "bad-sidecar",
                f"{side} is not a valid dimension sidecar: {type(exc).__name__}: {exc}",
            )
        ]
    try:
        digest = sha256_file(image)
        with Image.open(image) as opened:
            pixels = np.asarray(opened.convert("RGB"))
    except OSError as exc:
        return [
            Offender(
                "(image)", "bad-image", f"{image} cannot be read as an image: {exc}"
            )
        ]
    height, width = pixels.shape[:2]
    if recorded_sha != digest or recorded_size != (width, height):
        return [
            Offender(
                "(image)",
                "sidecar-mismatch",
                "sidecar was written for a different image (sha256/size differ)",
            )
        ]
    offenders: list[Offender] = []
    if len(labels) < min_dimensions:
        offenders.append(
            Offender(
                "(image)",
                "too-few-dimensions",
                f"{len(labels)} dimension label(s); need >= {min_dimensions}",
            )
        )
    scale = width / REFERENCE_WIDTH
    for dim in labels:
        offenders.extend(_check_label(dim, views, pixels, scale, min_font_px))
    offenders.extend(
        Offender(first.name, "overlaps-label", f"overlaps {second.name}")
        for i, first in enumerate(labels)
        for second in labels[i + 1 :]
        if rects_overlap(first.box, second.box)
    )
    offenders.extend(_spanning_separated(side))
    if require_dims:
        offenders.extend(_missing_required(side, labels))
    return offenders


def _spanning_separated(side: str) -> list[Offender]:
    """Offenders for every dimension that measures across separate parts of an exploded / multi-part layout.

    `3d dimension` refuses to draw one and records `spans_separated` on each dimension; a sidecar that
    says so (a render by an old tool cannot, a hand-edited one is out of luck) fails the gate. The one
    allowed form is a mating gap declared `kind: gap` between two named parts.
    """
    try:
        with open(side, encoding="utf-8") as handle:
            raw = json.load(handle)["dimensions"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    seen: set[str] = set()
    out: list[Offender] = []
    for entry in raw:
        if not isinstance(entry, dict) or entry.get("spans_separated") is not True:
            continue
        if entry.get("kind") == "gap":
            continue
        label = str(entry.get("label"))
        if label in seen:
            continue
        seen.add(label)
        parts = entry.get("parts")
        named = (
            f" ({' / '.join(map(str, parts))})"
            if isinstance(parts, list) and parts
            else ""
        )
        out.append(
            Offender(
                label,
                "spans-separated-parts",
                f"measures across separate parts of an exploded / multi-part layout{named}; "
                "keep it on one part or declare a `kind: gap` between two named parts",
            )
        )
    return out


def _missing_required(side: str, labels: list[_Label]) -> list[Offender]:
    """Offenders for every measurement declared with `--dims-from` that is not drawn in the image.

    A sidecar without a `required_dims` list was not rendered with `--dims-from`: asking for the
    requirement then is itself the failure (nothing was declared, so nothing can be vouched for).
    """
    try:
        with open(side, encoding="utf-8") as handle:
            required = json.load(handle).get("required_dims")
    except (OSError, ValueError, AttributeError):
        required = None
    if not isinstance(required, list) or not all(isinstance(n, str) for n in required):
        return [
            Offender(
                "(image)",
                "no-required-dims",
                "the sidecar declares no required measurements — render with `--dims-from SPEC`",
            )
        ]
    drawn = {label.label for label in labels}
    return [
        Offender(
            name,
            "missing-required-dim",
            "declared with --dims-from but not drawn in any view",
        )
        for name in required
        if name not in drawn
    ]


def format_report(image: str, offenders: list[Offender]) -> str:
    if not offenders:
        return f"PASS: dimensions on {image} are legible"
    lines = [f"FAIL: {len(offenders)} legibility offender(s) on {image}"]
    lines += [f"  - {o.label}: {o.rule} — {o.detail}" for o in offenders]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """`3d dimension --check IMAGE [--json]`: exit 0 legible, 1 offenders."""
    ap = argparse.ArgumentParser(prog="dimension_check.py")
    ap.add_argument("image")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--require-dims", action="store_true")
    args = ap.parse_args(argv)
    offenders = check_image(args.image, require_dims=args.require_dims)
    if args.json:
        print(
            json.dumps(
                {
                    "image": args.image,
                    "legible": not offenders,
                    "offenders": [o.as_dict() for o in offenders],
                },
                indent=1,
            )
        )
    else:
        print(format_report(args.image, offenders))
    return 1 if offenders else 0


if __name__ == "__main__":
    sys.exit(main())
