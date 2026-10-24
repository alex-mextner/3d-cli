"""calib_underlay_map.py — reads the bare underlay of a calibration plate photo as the first-layer map.

WHAT: the plate manifest `calib-plate/1` lists the bare underlay (`underlay.bare`, rectangles in bed
  mm: the sheet between the tests, where no test stands) and the light to take the photo with
  (`underlay.read.light`: `contrast` on a multi-head printer — a light underlay under dark tests —
  `raking` on a single-head one, where the underlay and the tests are one filament). Given the
  rectified photo (calib_rectify: `rectified`, `area_mm`, `px_per_mm`) this module cuts the bare
  underlay into CELL_MM cells and measures each: mean brightness and texture (the energy of the
  pixel-to-pixel differences: closed even lines are smooth, gaps and ridges are not).
  It replaces the first-layer patches of the old dense plates (#320):
    first_layer_map   per cell: bbox in bed mm, mean, texture and a robust z-score of each
    uniformity        the spread of the cell means, and the plane they lie on (a brightness
                      gradient across the plate)
    hints             `bed_tilt` (a gradient over the plate), `z_offset` (cells whose texture is
                      an outlier: gaps = nozzle too high under contrast light, gaps or ridges to be
                      looked at under raking light), `patchy` (cells darker or brighter than the rest)
  Hints are advice for a person to confirm (plan §8.3 rule 6: the machine proposes), never applied.
WHY: tg#22330 — the underlay is a free full-plate first-layer and bed-flatness check.
RUN BY: `3d calibrate underlay-map` (numpy + Pillow only).
"""

from __future__ import annotations

import math
import sys
from collections.abc import Sequence
from typing import Any

import numpy as np

CELL_MM = 10.0  # side of a measuring cell
INSET_MM = 0.6  # cells keep this far from the edge of the bare rectangle (the foot's seam, the edge)
MIN_CELLS = 4  # fewer cells cannot tell a tilt from noise
OUTLIER_Z = 2.5  # robust z-score from which a cell stands out
TILT_REL = 0.06  # brightness change across the plate (relative to the mean) that is a tilt hint
MAD_TO_SIGMA = 1.4826
SIGMA_FLOOR = 0.03  # brightness (0..1): a cell must differ from the median by 2.5 x this (7.5 %) to stand out

LIGHTS = ("contrast", "raking")


Rect = tuple[float, float, float, float]


def _uncovered(r: Rect, rects: Sequence[Sequence[float]]) -> bool:
    """Part of `r` lies outside the union of `rects` (the union, not each rectangle: the bare
    sheet is decomposed into rectangles whose inner borders are not edges of anything)."""
    pieces = [r]
    for bx0, by0, bx1, by1 in rects:
        rest: list[Rect] = []
        for x0, y0, x1, y1 in pieces:
            if (
                min(x1, bx1) - max(x0, bx0) <= 1e-9
                or min(y1, by1) - max(y0, by0) <= 1e-9
            ):
                rest.append((x0, y0, x1, y1))
                continue
            if x0 < bx0:
                rest.append((x0, y0, bx0, y1))
            if x1 > bx1:
                rest.append((bx1, y0, x1, y1))
            lo, hi = max(x0, bx0), min(x1, bx1)
            if y0 < by0:
                rest.append((lo, y0, hi, by0))
            if y1 > by1:
                rest.append((lo, by1, hi, y1))
        pieces = rest
        if not pieces:
            return False
    return bool(pieces)


def _cells(bare: Sequence[Sequence[float]], cell: float) -> list[Rect]:
    """Whole cells of side `cell` on one lattice over the bare sheet; a cell counts when it, grown
    by INSET_MM, lies inside the union of the bare rectangles (so it keeps off the feet's seams)."""
    if not bare:
        return []
    ox = min(r[0] for r in bare) + INSET_MM
    oy = min(r[1] for r in bare) + INSET_MM
    nx = int((max(r[2] for r in bare) - INSET_MM - ox + 1e-9) // cell)
    ny = int((max(r[3] for r in bare) - INSET_MM - oy + 1e-9) // cell)
    out: list[Rect] = []
    for j in range(ny):
        for i in range(nx):
            c = (ox + i * cell, oy + j * cell, ox + (i + 1) * cell, oy + (j + 1) * cell)
            grown = (c[0] - INSET_MM, c[1] - INSET_MM, c[2] + INSET_MM, c[3] + INSET_MM)
            near = [
                r
                for r in bare
                if r[2] > grown[0]
                and r[0] < grown[2]
                and r[3] > grown[1]
                and r[1] < grown[3]
            ]
            if near and not _uncovered(grown, near):
                out.append(c)
    return out


def _robust_z(values: np.ndarray) -> np.ndarray:
    """Distance from the median in robust sigmas, the sigma never under SIGMA_FLOOR: a photo's
    noise (or a perfectly flat synthetic one) must not make a 0.1 % wobble an outlier."""
    med = float(np.median(values))
    sigma = max(float(np.median(np.abs(values - med))) * MAD_TO_SIGMA, SIGMA_FLOOR)
    return (values - med) / sigma


def _measure(patch: np.ndarray) -> tuple[float, float]:
    """(mean brightness 0..1, texture): the root of the mean squared neighbour differences."""
    dx = np.diff(patch, axis=1)
    dy = np.diff(patch, axis=0)
    return float(patch.mean()), float(math.sqrt(float((dx**2).mean() + (dy**2).mean())))


def analyze(
    image: np.ndarray,
    area_mm: Sequence[float],
    px_per_mm: float,
    underlay: dict[str, Any],
    *,
    cell_mm: float = CELL_MM,
) -> dict[str, Any]:
    """The first-layer map and the uniformity hints of a rectified photo (grey or colour, 8 bit or
    0..1) of a plate whose manifest carries `underlay`. Pixel (u, v) is the bed point
    (x0 + u / px_per_mm, y1 - v / px_per_mm), as calib_rectify writes it."""
    if px_per_mm <= 0 or cell_mm <= 0:
        raise ValueError("px_per_mm and cell_mm must be positive")
    light = underlay.get("read", {}).get("light")
    if light not in LIGHTS:
        raise ValueError(
            f"underlay.read.light must be one of {', '.join(LIGHTS)}, got {light!r}"
        )
    bare = underlay.get("bare") or []
    # Integer photos are 0..max of their type; a float photo is 0..1 unless it clearly is not.
    # Each cell is converted on its own: the whole photo never exists as a float array.
    scale = (
        float(np.iinfo(image.dtype).max)
        if np.issubdtype(image.dtype, np.integer)
        else (255.0 if image.max() > 1.5 else 1.0)
    )
    x0, _, _, y1 = area_mm
    h, w = image.shape[:2]
    cells: list[dict[str, Any]] = []
    for cx0, cy0, cx1, cy1 in _cells(bare, cell_mm):
        u0, u1 = round((cx0 - x0) * px_per_mm), round((cx1 - x0) * px_per_mm)
        v0, v1 = round((y1 - cy1) * px_per_mm), round((y1 - cy0) * px_per_mm)
        if u0 < 0 or v0 < 0 or u1 > w or v1 > h or u1 - u0 < 3 or v1 - v0 < 3:
            continue  # outside the photo
        patch = image[v0:v1, u0:u1].astype(np.float64) / scale
        if patch.ndim == 3:
            patch = patch.mean(axis=2)
        mean, texture = _measure(patch)
        cells.append(
            {
                "bbox_mm": [round(v, 3) for v in (cx0, cy0, cx1, cy1)],
                "mean": round(mean, 4),
                "texture": round(texture, 4),
            }
        )
    result: dict[str, Any] = {
        "schema": "calib-underlay-map/1",
        "light": light,
        "cell_mm": cell_mm,
        "cells_total": len(_cells(bare, cell_mm)),
        "cells_read": len(cells),
    }
    if len(cells) < MIN_CELLS:
        return {
            **result,
            "status": "needs_manual",
            "reason": f"only {len(cells)} whole cells of the bare underlay are in the photo (need {MIN_CELLS})",
            "first_layer_map": cells,
            "uniformity": None,
            "hints": [],
        }
    means = np.array([c["mean"] for c in cells])
    textures = np.array([c["texture"] for c in cells])
    zm, zt = _robust_z(means), _robust_z(textures)
    for c, a, b in zip(cells, zm, zt, strict=True):
        c["z_mean"], c["z_texture"] = round(float(a), 2), round(float(b), 2)
    centres = np.array(
        [
            [
                (c["bbox_mm"][0] + c["bbox_mm"][2]) / 2,
                (c["bbox_mm"][1] + c["bbox_mm"][3]) / 2,
            ]
            for c in cells
        ]
    )
    design = np.column_stack([centres, np.ones(len(cells))])
    (gx, gy, _), *_ = np.linalg.lstsq(design, means, rcond=None)
    span = np.ptp(centres, axis=0)
    rise = (abs(float(gx)) * float(span[0]) + abs(float(gy)) * float(span[1])) / max(
        float(means.mean()), 1e-9
    )
    cv = float(means.std() / max(float(means.mean()), 1e-9))
    uniformity = {
        "mean_cv": round(cv, 4),
        "texture_median": round(float(np.median(textures)), 4),
        "gradient_per_100mm": {
            "x": round(float(gx) * 100, 4),
            "y": round(float(gy) * 100, 4),
        },
        "rise_across_plate": round(rise, 4),
    }
    hints: list[dict[str, Any]] = []
    if rise >= TILT_REL:
        hints.append(
            {
                "kind": "bed_tilt",
                "note": f"the underlay's brightness changes by {rise:.0%} across the plate "
                f"(brighter towards {'+X' if gx > 0 else '-X'}, {'+Y' if gy > 0 else '-Y'}): the bed is "
                "tilted or the nozzle height changes over it; compare the bed mesh",
                "confidence": round(min(1.0, rise / (3 * TILT_REL)), 2),
            }
        )
    for c in cells:
        if c["z_texture"] >= OUTLIER_Z:
            gaps = light == "contrast" and c["z_mean"] <= -1.0
            hints.append(
                {
                    "kind": "z_offset",
                    "bbox_mm": c["bbox_mm"],
                    "note": "gaps between the lines (the bed shows through): nozzle too high here"
                    if gaps
                    else "rough: gaps (too high) or ridges (too low), look at this cell in the photo",
                    "confidence": round(min(1.0, c["z_texture"] / (2 * OUTLIER_Z)), 2),
                }
            )
        elif abs(c["z_mean"]) >= OUTLIER_Z:
            hints.append(
                {
                    "kind": "patchy",
                    "bbox_mm": c["bbox_mm"],
                    "note": f"{'darker' if c['z_mean'] < 0 else 'brighter'} than the rest of the underlay",
                    "confidence": round(
                        min(1.0, abs(c["z_mean"]) / (2 * OUTLIER_Z)), 2
                    ),
                }
            )
    return {
        **result,
        "status": "proposed",
        "first_layer_map": cells,
        "uniformity": uniformity,
        "hints": hints,
    }


def main(argv: list[str] | None = None) -> int:
    """`calib_underlay_map.py IMAGE --manifest PLATE.json --rectify R.json [--cell-mm N]` → JSON on
    stdout. Exit 0 = proposed, 1 = needs_manual (too few cells), 2 = unreadable input."""
    import argparse
    import json
    from pathlib import Path

    from PIL import Image

    ap = argparse.ArgumentParser(prog="calib_underlay_map", description=__doc__)
    ap.add_argument(
        "image", help="the rectified photo (calibrate rectify: `rectified`)"
    )
    ap.add_argument(
        "--manifest", required=True, help="plate manifest calib-plate/1 with `underlay`"
    )
    ap.add_argument(
        "--rectify", required=True, help="calib-rectify/1 JSON (area_mm, px_per_mm)"
    )
    ap.add_argument("--cell-mm", type=float, default=CELL_MM)
    a = ap.parse_args(argv)
    try:
        underlay = json.loads(Path(a.manifest).read_text(encoding="utf-8"))["underlay"]
        rect = json.loads(Path(a.rectify).read_text(encoding="utf-8"))
        with Image.open(a.image) as img:
            pixels = np.asarray(img.convert("L"))
        result = analyze(
            pixels,
            rect["area_mm"],
            float(rect["px_per_mm"]),
            underlay,
            cell_mm=a.cell_mm,
        )
    except (OSError, KeyError, TypeError, ValueError) as e:
        print(
            json.dumps(
                {"status": "failed", "reason": f"cannot read the underlay: {e!r}"}
            )
        )
        return 2
    print(json.dumps(result))
    return 0 if result["status"] == "proposed" else 1


if __name__ == "__main__":
    sys.exit(main())
