"""ref_measure.py — `3d measure-ref`: measure a feature's shape on a reference image and on a render, report the delta.

ACCESSED VIA: `3d measure-ref <reference> <render> --spec FEATURES.json [options]` (lib/commands/measure_ref.py
launches this file through cli.pyrun, so the command module stays stdlib-only).

WHAT: for every feature named in the spec (an eye opening, a nostril, a pad ...) it builds a boolean shape from the
  reference and from the render, measures the same numbers on both and gates the difference:
    corner_len  corner-to-corner length (the longest chord of the shape, or the two corners given explicitly)
    thick       extent perpendicular to the corner line
    ar_axis     corner_len / thick
    tilt        angle of the corner line in degrees, outer corner higher = +  (needs `inner_side`)
    pca_tilt    same for the principal axis of the filled shape; pca_ar the sqrt of its eigenvalue ratio
    W, H, area  x extent, y extent, filled area
    <part>_W / <part>_H   extents of every named part (iris, pupil ...)
    cover_top / cover_bot share of the `iris` radius covered by the upper / lower lid, from the `pupil` centre
  Lengths are in reference pixels (render lengths are multiplied by --cand-scale, areas by its square).

WHY: iterating a figurine's eyes against a reference by eye is slow and unrepeatable. The numbers (tilt 19 deg vs 12,
  aspect 2.4 vs 1.8, iris 24 px vs 22) say what to change, and the gate says whether the render is close enough.
  Extracted from the Chekhov cat's eye_measure.py: hand-picked reference outlines vs painter-rasterised labels.

SPEC (JSON): {"tolerance": {metric: tol}, "features": {name: feature}}
  feature: {"inner_side": "left"|"right",       image side of the corner towards the nose bridge (sign of tilt)
            "box": [x0, y0, x1, y1],            region of interest in the reference (pixels)
            "render_box": [...],                region in the render (default: box)
            "reference": SOURCE, "render": SOURCE,
            "corners": {"reference": {"inner": [x, y], "outer": [x, y]}, "render": {...}},   optional
            "parts": {"iris": {"reference": SOURCE, "render": SOURCE}, "pupil": {...}},
            "tolerance": {metric: tol}}
  SOURCE (exactly one of): {"polygon": [[x, y], ...]}          absolute image pixels
                           {"ellipse": [cx, cy, rx, ry, deg]}  deg counter-clockwise on screen (optional, default 0)
                           {"mask": "mask.png"}                 white = in (same size as the image), cropped to the box
                           {"colour": "#rrggbb" | [...], "tol": 30, "close": 0}   pixels within RGB distance `tol` of any colour,
                                in the box; `close` N joins notches up to 2*N px wide (disc closing)
                           {"alpha": true}                      pixels with alpha > 127, in the box
  tol: a number (absolute: degrees for tilts, pixels for lengths) | "15%" (relative to the reference) | "off".
  Defaults: tilt 3, pca_tilt 4, ar_axis 15%, pca_ar 15%, iris_W 15% (ungated metrics are only reported).

Image-derived shapes keep their largest connected component; an empty reference shape is an input error (exit 2), an
empty render shape is a FAIL (the feature is missing). Exit: 0 PASS, 1 gate FAIL, 2 usage / input error.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

_LIB = os.path.dirname(os.path.abspath(__file__))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import numpy as np  # noqa: E402  (lib/ must be on sys.path first for `errors`)
from errors import GateFailure, InputNotFound, ThreeDError, UsageError  # noqa: E402
from numpy.typing import NDArray  # noqa: E402

PROG = "measure-ref"
ANALYTIC_SCALE = 8  # raster pixels per image pixel for polygon / ellipse sources
TIP_SHARE = 0.15  # end regions (share of the extent along the major axis) whose centroids are the corners
LENGTH_KEYS = ("corner_len", "thick", "W", "H")
DEFAULT_TOLERANCE: dict[str, str] = {
    "tilt": "3",
    "pca_tilt": "4",
    "ar_axis": "15%",
    "pca_ar": "15%",
    "iris_W": "15%",
}
FEATURE_KEYS = {
    "inner_side",
    "box",
    "render_box",
    "reference",
    "render",
    "corners",
    "parts",
    "tolerance",
}
SOURCE_KEYS = {"polygon", "ellipse", "mask", "colour", "tol", "alpha", "close"}
BoolArray = NDArray[np.bool_]
Point = tuple[float, float]


def _usage(
    message: str, *, hint: str = "see `3d measure-ref --help` for the spec format"
) -> UsageError:
    return UsageError(message, command=PROG, remediation=[hint])


# ---------------------------------------------------------------- shapes


@dataclass
class Shape:
    """A boolean raster whose pixel (ix, iy) covers image pixels [origin + i/scale, origin + (i+1)/scale)."""

    mask: BoolArray
    scale: int
    origin: Point

    def empty(self) -> bool:
        return not bool(self.mask.any())


@dataclass
class Image:
    path: str
    rgba: NDArray[np.uint8]
    size: tuple[int, int] = field(init=False)

    def __post_init__(self) -> None:
        self.size = (int(self.rgba.shape[1]), int(self.rgba.shape[0]))


def load_image(path: str) -> Image:
    if not os.path.isfile(path):
        raise InputNotFound(path, command=PROG)
    from PIL import Image as PilImage

    try:
        with PilImage.open(path) as im:
            return Image(path, np.asarray(im.convert("RGBA")).copy())
    except OSError as exc:
        raise _usage(
            f"{path}: not a readable image ({exc})", hint="pass a PNG or JPEG file"
        ) from exc


def _hex_colour(value: Any, where: str) -> tuple[int, int, int]:
    if isinstance(value, str) and len(value) == 7 and value.startswith("#"):
        try:
            return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)
        except ValueError:
            pass
    if isinstance(value, Sequence) and not isinstance(value, str) and len(value) == 3:
        return int(value[0]), int(value[1]), int(value[2])
    raise _usage(f"{where}: colour must be '#rrggbb' or [r, g, b], got {value!r}")


def _box(value: Any, where: str, size: tuple[int, int]) -> tuple[int, int, int, int]:
    if not (isinstance(value, Sequence) and len(value) == 4):
        raise _usage(f"{where}: box must be [x0, y0, x1, y1], got {value!r}")
    x0, y0, x1, y1 = (int(math.floor(float(v))) for v in value)
    x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, size[0]), min(y1, size[1])
    if x1 <= x0 or y1 <= y0:
        raise _usage(
            f"{where}: box {list(value)} is empty inside the {size[0]}x{size[1]} image"
        )
    return x0, y0, x1, y1


def _close(mask: BoolArray, radius: int) -> BoolArray:
    """Binary closing with a disc: joins a feature split by a thin notch (a pupil touching the lid colour in a flat render)."""
    from scipy import ndimage as ndi

    yy, xx = np.mgrid[-radius : radius + 1, -radius : radius + 1]
    padded = np.pad(mask, radius + 1)
    closed = ndi.binary_closing(
        padded, structure=(xx * xx + yy * yy) <= radius * radius
    )
    return np.asarray(closed[radius + 1 : -radius - 1, radius + 1 : -radius - 1])


def _largest_component(mask: BoolArray) -> BoolArray:
    from scipy import ndimage as ndi

    labels, n = ndi.label(mask)
    if n <= 1:
        return mask
    counts = np.bincount(labels.ravel())
    counts[0] = 0
    return np.asarray(labels == int(counts.argmax()))


def _rasterise(draw_fn: Any, bbox: tuple[float, float, float, float]) -> Shape:
    from PIL import Image as PilImage
    from PIL import ImageDraw

    s = ANALYTIC_SCALE
    x0, y0 = math.floor(bbox[0]) - 1, math.floor(bbox[1]) - 1
    x1, y1 = math.ceil(bbox[2]) + 1, math.ceil(bbox[3]) + 1
    canvas = PilImage.new("L", ((x1 - x0) * s, (y1 - y0) * s), 0)
    draw_fn(ImageDraw.Draw(canvas), lambda x, y: ((x - x0) * s, (y - y0) * s))
    return Shape(np.asarray(canvas) > 0, s, (float(x0), float(y0)))


def _polygon_shape(points: Any, where: str) -> Shape:
    if not (
        isinstance(points, Sequence)
        and len(points) >= 3
        and all(isinstance(p, Sequence) and len(p) == 2 for p in points)
    ):
        raise _usage(f"{where}: polygon needs at least 3 [x, y] points")
    pts = [(float(p[0]), float(p[1])) for p in points]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    # pixel (i, j) of the image covers [i, i+1): an outline picked on pixel centres is drawn at +0.5
    return _rasterise(
        lambda d, to: d.polygon([to(x + 0.5, y + 0.5) for x, y in pts], fill=1),
        (min(xs), min(ys), max(xs) + 1, max(ys) + 1),
    )


def _ellipse_shape(spec: Any, where: str) -> Shape:
    if not (isinstance(spec, Sequence) and len(spec) in (4, 5)):
        raise _usage(
            f"{where}: ellipse must be [cx, cy, rx, ry] or [cx, cy, rx, ry, degrees]"
        )
    cx, cy, rx, ry = (float(v) for v in spec[:4])
    theta = math.radians(float(spec[4])) if len(spec) == 5 else 0.0
    if rx <= 0 or ry <= 0:
        raise _usage(f"{where}: ellipse radii must be positive, got rx={rx} ry={ry}")
    ts = np.linspace(0.0, 2 * math.pi, 361)[:-1]
    ex, ey = rx * np.cos(ts), ry * np.sin(ts)
    pts = [
        (
            cx + a * math.cos(theta) - b * math.sin(theta),
            cy - (a * math.sin(theta) + b * math.cos(theta)),
        )
        for a, b in zip(ex, ey, strict=True)
    ]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return _rasterise(
        lambda d, to: d.polygon([to(x, y) for x, y in pts], fill=1),
        (min(xs), min(ys), max(xs), max(ys)),
    )


def build_shape(
    source: Mapping[str, Any], image: Image, box: Any, where: str, base_dir: str
) -> Shape:
    unknown = set(source) - SOURCE_KEYS
    if unknown:
        raise _usage(
            f"{where}: unknown source key(s) {sorted(unknown)}; accepted: {sorted(SOURCE_KEYS)}"
        )
    kinds = [
        k for k in ("polygon", "ellipse", "mask", "colour", "alpha") if k in source
    ]
    if len(kinds) != 1:
        raise _usage(
            f"{where}: exactly one of polygon / ellipse / mask / colour / alpha is required, got {kinds or 'none'}"
        )
    kind = kinds[0]
    if kind == "polygon":
        return _polygon_shape(source["polygon"], where)
    if kind == "ellipse":
        return _ellipse_shape(source["ellipse"], where)
    x0, y0, x1, y1 = (
        _box(box, f"{where}: box", image.size)
        if box is not None
        else (0, 0, *image.size)
    )
    crop = image.rgba[y0:y1, x0:x1]
    if kind == "mask":
        path = os.path.join(base_dir, str(source["mask"]))
        full = load_image(path)
        if full.size != image.size:
            raise _usage(
                f"{where}: mask {path} is {full.size[0]}x{full.size[1]} but the image is {image.size[0]}x{image.size[1]}",
                hint="the mask must have the image's size",
            )
        m = full.rgba[y0:y1, x0:x1, :3].max(axis=2) > 127
    elif kind == "alpha":
        m = crop[..., 3] > 127
    else:
        raw = source["colour"]
        colours = (
            raw
            if isinstance(raw, list) and raw and not isinstance(raw[0], int)
            else [raw]
        )
        tol = float(source.get("tol", 30))
        rgb = crop[..., :3].astype(np.float64)
        m = np.zeros(crop.shape[:2], dtype=bool)
        for c in colours:
            m |= (
                np.linalg.norm(
                    rgb - np.array(_hex_colour(c, where), dtype=np.float64), axis=2
                )
                <= tol
            )
        m &= crop[..., 3] > 127
    radius = int(source.get("close", 0))
    if radius < 0:
        raise _usage(
            f"{where}: close must be a non-negative number of pixels, got {source['close']!r}"
        )
    if radius:
        m = _close(m, radius)
    return Shape(_largest_component(m), 1, (float(x0), float(y0)))


# ---------------------------------------------------------------- measurement


def _boundary_corner_points(shape: Shape) -> NDArray[np.float64]:
    """Image-pixel coordinates of the corners of every boundary pixel of the filled shape (extents are exact, not pixel-centre)."""
    from scipy import ndimage as ndi

    filled = ndi.binary_fill_holes(shape.mask)
    edge = filled & ~ndi.binary_erosion(filled)
    ys, xs = np.nonzero(edge)
    pts = np.concatenate(
        [np.column_stack([xs + dx, ys + dy]) for dx in (0, 1) for dy in (0, 1)]
    ).astype(np.float64)
    pts = np.unique(pts, axis=0)
    return np.asarray(pts / shape.scale + np.array(shape.origin))


def _hull(points: NDArray[np.float64], where: str) -> NDArray[np.float64]:
    from scipy.spatial import ConvexHull, QhullError

    try:
        return np.asarray(points[ConvexHull(points).vertices])
    except QhullError as exc:
        raise _usage(
            f"{where}: the shape is degenerate (a line or a point), nothing to measure",
            hint="check the box / colour / mask that selects it",
        ) from exc


def _principal(
    shape: Shape,
) -> tuple[NDArray[np.float64], NDArray[np.float64], float, float]:
    """-> (pixel-centre points of the filled shape in image px, unit major axis, its angle in degrees
    (screen y up, in (-90, 90]), sqrt of the eigenvalue ratio)."""
    from scipy import ndimage as ndi

    ys, xs = np.nonzero(ndi.binary_fill_holes(shape.mask))
    pts = (
        np.column_stack([xs + 0.5, ys + 0.5]) / shape.scale + np.array(shape.origin)
    ).astype(np.float64)
    w, v = np.linalg.eigh(np.cov(pts.T))
    major = v[:, 1]
    angle = math.degrees(math.atan2(-major[1], major[0]))
    return pts, major, (angle + 90) % 180 - 90, math.sqrt(w[1] / max(w[0], 1e-12))


def _tip_centroids(
    pts: NDArray[np.float64], major: NDArray[np.float64]
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Centroids of the two end regions (outer TIP_SHARE of the extent along the major axis). Used as the corners:
    the farthest hull pair jitters by 3-7 degrees on pixel masks (a corner is 1-2 px wide), the centroid of the end region by < 1."""
    s = (pts - pts.mean(axis=0)) @ major
    lo, hi = float(s.min()), float(s.max())
    span = hi - lo
    return pts[s <= lo + TIP_SHARE * span].mean(axis=0), pts[
        s >= hi - TIP_SHARE * span
    ].mean(axis=0)


def _extents(shape: Shape, where: str) -> tuple[float, float, float, float]:
    hull = _hull(_boundary_corner_points(shape), where)
    return (
        float(hull[:, 0].min()),
        float(hull[:, 0].max()),
        float(hull[:, 1].min()),
        float(hull[:, 1].max()),
    )


def measure_shape(
    shape: Shape,
    inner_side: str | None,
    corners: Mapping[str, Sequence[float]] | None,
    where: str,
) -> tuple[dict[str, float], NDArray[np.float64], tuple[Point, Point]]:
    """-> (metrics in image pixels, hull points, (inner corner, outer corner)).

    Without explicit `corners` the corner line runs through the two tip-region centroids and corner_len is the hull's
    extent along it; with explicit corners corner_len is their distance (the chord a person picked)."""
    hull = _hull(_boundary_corner_points(shape), where)
    pts, major, angle, ratio = _principal(shape)
    if corners:
        inner = np.array(corners["inner"], dtype=np.float64)
        outer = np.array(corners["outer"], dtype=np.float64)
    else:
        a, b = _tip_centroids(pts, major)
        left, right = (a, b) if a[0] <= b[0] else (b, a)
        inner, outer = (left, right) if inner_side != "right" else (right, left)
    axis = outer - inner
    chord = float(np.linalg.norm(axis))
    if chord <= 0:
        raise _usage(f"{where}: both corners coincide, nothing to measure")
    u = axis / chord
    normal = np.array([-u[1], u[0]])
    along = hull @ u
    across = hull @ normal
    length = chord if corners else float(along.max() - along.min())
    thick = float(across.max() - across.min())
    metrics = {
        "corner_len": length,
        "thick": thick,
        "ar_axis": length / thick if thick > 0 else float("inf"),
        "W": float(hull[:, 0].max() - hull[:, 0].min()),
        "H": float(hull[:, 1].max() - hull[:, 1].min()),
        "area": float(len(pts) / shape.scale**2),
    }
    if inner_side is not None:
        metrics["tilt"] = math.degrees(math.atan2(-axis[1], abs(axis[0])))
        metrics["pca_tilt"] = angle if inner_side == "left" else -angle
    metrics["pca_ar"] = ratio
    return (
        metrics,
        hull,
        ((float(inner[0]), float(inner[1])), (float(outer[0]), float(outer[1]))),
    )


def scale_metrics(metrics: dict[str, float], factor: float) -> dict[str, float]:
    out = dict(metrics)
    for k, v in metrics.items():
        if k in LENGTH_KEYS or k.endswith(("_W", "_H")):
            out[k] = v * factor
        elif k == "area":
            out[k] = v * factor * factor
    return out


# ---------------------------------------------------------------- spec


@dataclass
class FeatureResult:
    name: str
    metrics: dict[str, dict[str, Any]]
    status: str
    failures: list[str]
    hulls: dict[str, NDArray[np.float64]]
    corners: dict[str, tuple[Point, Point]]
    boxes: dict[str, tuple[int, int, int, int]]


def parse_tolerance(raw: Any, where: str) -> dict[str, tuple[str, float | None]]:
    """{'tilt': 3, 'ar_axis': '15%', 'iris_W': 'off'} -> {'tilt': ('abs', 3.0), 'ar_axis': ('rel', 15.0), 'iris_W': ('off', None)}"""
    if not isinstance(raw, Mapping):
        raise _usage(
            f"{where}: tolerance must be an object {{metric: value}}, got {raw!r}"
        )
    out: dict[str, tuple[str, float | None]] = {}
    for key, value in raw.items():
        text = str(value).strip()
        try:
            if text == "off":
                out[str(key)] = ("off", None)
            elif text.endswith("%"):
                out[str(key)] = ("rel", float(text[:-1]))
            else:
                out[str(key)] = ("abs", float(text))
        except ValueError as exc:
            raise _usage(
                f"{where}: tolerance for {key} must be a number, 'N%' or 'off', got {value!r}"
            ) from exc
    return out


def load_spec(path: str) -> dict[str, Any]:
    if not os.path.isfile(path):
        raise InputNotFound(path, command=PROG)
    try:
        with open(path, encoding="utf-8") as fh:
            spec = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise _usage(f"{path}: not valid JSON ({exc})") from exc
    if (
        not isinstance(spec, dict)
        or not isinstance(spec.get("features"), dict)
        or not spec["features"]
    ):
        raise _usage(f'{path}: the spec needs a non-empty "features" object')
    extra = set(spec) - {"features", "tolerance"}
    if extra:
        raise _usage(
            f"{path}: unknown top-level key(s) {sorted(extra)}; accepted: features, tolerance"
        )
    spec["base_dir"] = os.path.dirname(os.path.abspath(path))
    return spec


def _compare(
    name: str, ref: float, cand: float, tol: tuple[str, float | None] | None
) -> dict[str, Any]:
    delta = cand - ref
    pct = 100.0 * delta / abs(ref) if abs(ref) > 1e-9 else None
    row: dict[str, Any] = {
        "reference": ref,
        "render": cand,
        "delta": delta,
        "delta_pct": pct,
        "tolerance": None,
        "status": "INFO",
    }
    if tol is None or tol[0] == "off":
        return row
    kind, limit = tol
    assert limit is not None
    row["tolerance"] = f"{limit:g}%" if kind == "rel" else f"{limit:g}"
    if kind == "rel":
        ok = pct is not None and abs(pct) <= limit + 1e-9
    else:
        ok = abs(delta) <= limit + 1e-9
    row["status"] = "PASS" if ok else "FAIL"
    return row


def measure_feature(
    name: str,
    feature: Mapping[str, Any],
    ref_img: Image,
    cand_img: Image,
    tolerance: Mapping[str, tuple[str, float | None]],
    cand_scale: float,
    base_dir: str,
) -> FeatureResult:
    where = f"feature {name!r}"
    unknown = set(feature) - FEATURE_KEYS
    if unknown:
        raise _usage(
            f"{where}: unknown key(s) {sorted(unknown)}; accepted: {sorted(FEATURE_KEYS)}"
        )
    inner_side = feature.get("inner_side")
    if inner_side not in (None, "left", "right"):
        raise _usage(
            f'{where}: inner_side must be "left" or "right", got {inner_side!r}'
        )
    for key in ("reference", "render"):
        if not isinstance(feature.get(key), Mapping):
            raise _usage(f'{where}: "{key}" source is required')
    boxes_raw = {
        "reference": feature.get("box"),
        "render": feature.get("render_box", feature.get("box")),
    }
    images = {"reference": ref_img, "render": cand_img}
    own_tol = (
        parse_tolerance(feature["tolerance"], where) if "tolerance" in feature else {}
    )
    tol = {**tolerance, **own_tol}
    corner_spec = feature.get("corners", {})
    parts_spec = feature.get("parts", {})
    metrics: dict[str, dict[str, float]] = {}
    hulls: dict[str, NDArray[np.float64]] = {}
    corners: dict[str, tuple[Point, Point]] = {}
    boxes: dict[str, tuple[int, int, int, int]] = {}
    part_shapes: dict[str, dict[str, Shape]] = {"reference": {}, "render": {}}
    absent: list[str] = []
    for side in ("reference", "render"):
        image = images[side]
        shape = build_shape(
            feature[side], image, boxes_raw[side], f"{where} {side}", base_dir
        )
        if shape.empty():
            if side == "reference":
                raise _usage(
                    f"{where}: the reference shape is empty (nothing selected by the {sorted(feature[side])} source in box {boxes_raw[side]})",
                    hint="check the box / colour / mask of the reference source",
                )
            absent.append(
                f"render shape is empty: the feature is missing from the render (box {boxes_raw[side]})"
            )
            continue
        m, hull, cn = measure_shape(
            shape, inner_side, corner_spec.get(side), f"{where} {side}"
        )
        for part, ps in parts_spec.items():
            if side not in ps:
                raise _usage(
                    f'{where}: part {part!r} needs both "reference" and "render" sources'
                )
            psh = build_shape(
                ps[side],
                image,
                boxes_raw[side],
                f"{where} part {part!r} {side}",
                base_dir,
            )
            if psh.empty():
                if side == "reference":
                    raise _usage(f"{where}: part {part!r} of the reference is empty")
                absent.append(f"part {part!r} is empty in the render")
                continue
            x0, x1, y0, y1 = _extents(psh, f"{where} part {part!r} {side}")
            m[f"{part}_W"], m[f"{part}_H"] = x1 - x0, y1 - y0
            part_shapes[side][part] = psh
            m[f"{part}__cy"] = (y0 + y1) / 2
        if (
            "iris" in parts_spec
            and "pupil" in parts_spec
            and "iris_W" in m
            and "pupil__cy" in m
        ):
            radius = m["iris_W"] / 2
            m["cover_top"] = 1 - (m["pupil__cy"] - float(hull[:, 1].min())) / radius
            m["cover_bot"] = 1 - (float(hull[:, 1].max()) - m["pupil__cy"]) / radius
        for k in [k for k in m if k.endswith("__cy")]:
            del m[k]
        if side == "render":
            m = scale_metrics(m, cand_scale)
        metrics[side] = m
        hulls[side] = hull
        corners[side] = cn
        boxes[side] = (
            _box(boxes_raw[side], f"{where}: box", image.size)
            if boxes_raw[side] is not None
            else (0, 0, *image.size)
        )
    failures = list(absent)
    rows: dict[str, dict[str, Any]] = {}
    if "render" in metrics:
        for key, ref_value in metrics["reference"].items():
            if key not in metrics["render"]:
                continue
            rows[key] = _compare(key, ref_value, metrics["render"][key], tol.get(key))
            if rows[key]["status"] == "FAIL":
                r = rows[key]
                failures.append(
                    f"{key}: render {r['render']:.2f} vs reference {r['reference']:.2f} (delta {r['delta']:+.2f}, tolerance {r['tolerance']})"
                )
    else:
        for key, ref_value in metrics["reference"].items():
            rows[key] = {
                "reference": ref_value,
                "render": None,
                "delta": None,
                "delta_pct": None,
                "tolerance": None,
                "status": "FAIL" if key in tol and tol[key][0] != "off" else "INFO",
            }
    return FeatureResult(
        name, rows, "FAIL" if failures else "PASS", failures, hulls, corners, boxes
    )


def run_measure(
    ref_path: str,
    render_path: str,
    spec: Mapping[str, Any],
    cli_tol: Mapping[str, tuple[str, float | None]],
    cand_scale: float,
) -> tuple[dict[str, Any], list[FeatureResult], tuple[Image, Image]]:
    ref_img, cand_img = load_image(ref_path), load_image(render_path)
    tolerance = parse_tolerance(DEFAULT_TOLERANCE, "defaults")
    tolerance.update(
        parse_tolerance(spec["tolerance"], "spec tolerance")
        if "tolerance" in spec
        else {}
    )
    tolerance.update(cli_tol)
    results = [
        measure_feature(
            n, f, ref_img, cand_img, tolerance, cand_scale, spec["base_dir"]
        )
        for n, f in spec["features"].items()
    ]
    failed = [r for r in results if r.status == "FAIL"]
    report = {
        "tool": "3d measure-ref",
        "status": "FAIL" if failed else "PASS",
        "reference": ref_path,
        "render": render_path,
        "cand_scale": cand_scale,
        "features": {
            r.name: {
                "status": r.status,
                "failures": r.failures,
                "metrics": r.metrics,
                "corners": {
                    s: {"inner": c[0], "outer": c[1]} for s, c in r.corners.items()
                },
            }
            for r in results
        },
    }
    return report, results, (ref_img, cand_img)


# ---------------------------------------------------------------- output


def format_table(report: Mapping[str, Any]) -> str:
    lines: list[str] = []
    for name, feat in report["features"].items():
        lines.append(f"{name}: {feat['status']}")
        lines.append(
            f"  {'metric':<12}{'reference':>10}{'render':>10}{'delta':>9}{'delta%':>8}  {'tol':>6}  status"
        )
        for key, r in feat["metrics"].items():
            render = "-" if r["render"] is None else f"{r['render']:.2f}"
            delta = "-" if r["delta"] is None else f"{r['delta']:+.2f}"
            pct = "-" if r["delta_pct"] is None else f"{r['delta_pct']:+.0f}%"
            lines.append(
                f"  {key:<12}{r['reference']:>10.2f}{render:>10}{delta:>9}{pct:>8}  {r['tolerance'] or '':>6}  {r['status']}"
            )
        lines.extend(f"  ! {f}" for f in feat["failures"])
    return "\n".join(lines)


def write_overlay(
    path: str,
    results: Sequence[FeatureResult],
    images: tuple[Image, Image],
    zoom: int = 8,
    margin: int = 4,
) -> None:
    """One row per feature: the reference crop and the render crop with the measured hull (red) and corners (yellow)."""
    from PIL import Image as PilImage
    from PIL import ImageDraw

    rows: list[Any] = []
    for res in results:
        panels = []
        for side, image in zip(("reference", "render"), images, strict=True):
            if side not in res.hulls:
                panels.append(PilImage.new("RGB", (zoom * 16, zoom * 16), (60, 0, 0)))
                continue
            hull = res.hulls[side]
            x0 = max(int(hull[:, 0].min()) - margin, 0)
            y0 = max(int(hull[:, 1].min()) - margin, 0)
            x1 = min(int(math.ceil(hull[:, 0].max())) + margin, image.size[0])
            y1 = min(int(math.ceil(hull[:, 1].max())) + margin, image.size[1])
            crop = (
                PilImage.fromarray(image.rgba[y0:y1, x0:x1])
                .convert("RGB")
                .resize(
                    ((x1 - x0) * zoom, (y1 - y0) * zoom), PilImage.Resampling.NEAREST
                )
            )
            draw = ImageDraw.Draw(crop)
            order = np.argsort(
                np.arctan2(
                    hull[:, 1] - hull[:, 1].mean(), hull[:, 0] - hull[:, 0].mean()
                )
            )
            draw.polygon(
                [((hull[i, 0] - x0) * zoom, (hull[i, 1] - y0) * zoom) for i in order],
                outline=(255, 0, 0),
            )
            for cx, cy in res.corners[side]:
                px, py = (cx - x0) * zoom, (cy - y0) * zoom
                draw.ellipse([px - 3, py - 3, px + 3, py + 3], outline=(255, 255, 0))
            panels.append(crop)
        h = max(p.size[1] for p in panels)
        row = PilImage.new(
            "RGB", (sum(p.size[0] for p in panels) + 10, h + 14), (30, 30, 30)
        )
        ImageDraw.Draw(row).text(
            (2, 1), f"{res.name} {res.status}: reference | render", fill=(255, 255, 255)
        )
        row.paste(panels[0], (0, 14))
        row.paste(panels[1], (panels[0].size[0] + 10, 14))
        rows.append(row)
    sheet = PilImage.new(
        "RGB",
        (max(r.size[0] for r in rows), sum(r.size[1] for r in rows)),
        (30, 30, 30),
    )
    y = 0
    for row in rows:
        sheet.paste(row, (0, y))
        y += row.size[1]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    sheet.save(path)


# ---------------------------------------------------------------- CLI


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"3d {PROG}", add_help=False)
    p.add_argument("reference")
    p.add_argument("render")
    p.add_argument("--spec", required=True)
    p.add_argument("--tol", action="append", default=[])
    p.add_argument("--cand-scale", type=float, default=1.0)
    p.add_argument("--overlay")
    p.add_argument("-o", "--out")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str]) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        raise _usage(
            "bad arguments; usage: 3d measure-ref <reference> <render> --spec FEATURES.json [--tol KEY=VAL] [--cand-scale S] [--overlay FILE.png] [-o FILE.json] [--json]"
        ) from exc
    if args.cand_scale <= 0:
        raise _usage(f"--cand-scale must be positive, got {args.cand_scale}")
    cli_tol: dict[str, str] = {}
    for item in args.tol:
        key, sep, value = item.partition("=")
        if not sep or not key or not value:
            raise _usage(
                f"--tol expects KEY=VALUE (KEY=3, KEY=15%, KEY=off), got {item!r}"
            )
        cli_tol[key] = value
    report, results, images = run_measure(
        args.reference,
        args.render,
        load_spec(args.spec),
        parse_tolerance(cli_tol, "--tol"),
        args.cand_scale,
    )
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
            fh.write("\n")
    if args.overlay:
        write_overlay(args.overlay, results, images)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_table(report))
    if report["status"] != "PASS":
        failing = [n for n, f in report["features"].items() if f["status"] == "FAIL"]
        raise GateFailure(
            f">>> MEASURE-REF: FAIL — {', '.join(failing)} outside the tolerance",
            command=PROG,
            silent=False,
        )
    if not args.json:
        print(">>> MEASURE-REF: PASS")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except ThreeDError as err:
        message = err.render()
        if message:
            print(message, file=sys.stderr)
        sys.exit(err.exit_code)
