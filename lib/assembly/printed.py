"""printed.py — the as-printed side of the fit check: G-code wall paths against the model, in the assembly frame.

WHAT: for one part with a sliced G-code,
  1. places the model on the bed (`PrintPlacement`): its print orientation (rotate / scale, like a pose), dropped onto
     z = 0, and the XY position either given or REGISTERED - a translation-only least-squares fit of the model's
     layer sections to the object's printed outer walls (the slicer's own placement, usually the project's object
     position), so a G-code that was not made from this exact model-to-bed transform still lines up;
  2. measures the per-layer deviation of the printed wall from the model section: the centre line of an outer wall lies
     half a line width inside the surface, so `|signed distance to the section - width/2|` is how far the printed
     surface is from the model (elephant foot, hole / thread compensation, a wrong model all show here);
  3. returns the printed wall points mapped into the ASSEMBLY frame (bed -> model -> pose), each with the offset to the
     printed SURFACE (+-width/2 along the radius), ready for `helical` profiles.
WHY: the slicer moves walls (xy_hole_compensation, elephant-foot compensation, line width): a thread clearance of 0.6 mm
  on the model is another number on the plastic; the cactus rim/plug screwing was verified this way only by an ad-hoc script.
INVARIANTS: the deviation uses the layer's mid height (`z - h/2`); layers below `skip_below_mm` are reported separately
  (the first layer is flared on purpose); the registration is reported with its residual - a model that is mirrored or
  turned against the G-code does not register and the deviation says so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt

from assembly import helical
from gcode_walls import WallLayer, Walls, densify_segments

Array = npt.NDArray[np.float64]
Matrix = npt.NDArray[np.float64]

REGISTER_LAYERS = 12
REGISTER_ITERATIONS = 12
SPACING_MM = 0.1
DEVIATION_SPACING_MM = 0.4


@dataclass
class Section:
    """The cross-section of the model at one height: boundary points, the (holed) polygons and the plane frame."""

    z: float
    polygons: list[Any]
    boundary: Array  # (N, 2) points along the boundary, ~0.05 mm apart
    to_3d: Matrix
    tree: Any = field(repr=False, default=None)  # cKDTree of `boundary`


@dataclass
class PrintPlacement:
    matrix: Matrix  # model (already scaled) -> bed
    registered: bool
    residual_mm: float
    shift_mm: tuple[float, float]


def sections(mesh: Any, heights: Array) -> list[Section | None]:
    """Sections of a bed-frame mesh at the given z values (None where the plane misses it)."""
    import trimesh
    from scipy.spatial import cKDTree
    from trimesh import intersections

    lines, to_3d, _ = intersections.mesh_multiplane(
        mesh, np.zeros(3), np.array([0.0, 0.0, 1.0]), heights
    )
    out: list[Section | None] = []
    for h, seg, mat in zip(heights, lines, to_3d, strict=True):
        if len(seg) == 0:
            out.append(None)
            continue
        seg = np.asarray(seg, dtype=float)
        path = trimesh.load_path(seg)
        polys = list(path.polygons_full)
        if not polys:
            out.append(None)
            continue
        pts, _ = densify_segments(seg[:, 0, :], seg[:, 1, :], 0.05)
        out.append(
            Section(float(h), polys, pts, np.asarray(mat, dtype=float), cKDTree(pts))
        )
    return out


def signed_distance(sec: Section, plane_xy: Array) -> tuple[Array, Array, Array]:
    """(distance to the boundary, inside flag, nearest boundary point) of plane points."""
    import shapely

    dist, idx = sec.tree.query(plane_xy)
    inside = np.zeros(len(plane_xy), dtype=bool)
    for poly in sec.polygons:
        inside |= shapely.contains_xy(poly, plane_xy[:, 0], plane_xy[:, 1])
    return np.asarray(dist), inside, sec.boundary[idx]


def _layer_pts(layer: WallLayer, spacing: float) -> tuple[Array, Array]:
    pts, width = layer.points(spacing)
    return pts, width


def _gauss_newton_rows(
    data: list[tuple[Array, Array, Section]], shift: Array
) -> tuple[Array, Array]:
    """Jacobian rows (the inward boundary normals) and residuals `signed distance - width/2` of the wall points at `shift`."""
    rows_a: list[Array] = []
    rows_b: list[Array] = []
    for pts, width, sec in data:
        p = pts + shift
        dist, inside, q = signed_distance(sec, p)
        sign = np.where(inside, 1.0, -1.0)
        n_in = (p - q) * sign[:, None]
        n_in = n_in / np.maximum(np.linalg.norm(n_in, axis=1), 1e-9)[:, None]
        rows_a.append(n_in)
        rows_b.append(sign * dist - width / 2.0)
    return np.vstack(rows_a), np.concatenate(rows_b)


def register_xy(
    mesh_bed: Any, walls: Walls, skip_below_mm: float
) -> tuple[tuple[float, float], float]:
    """Translation (dx, dy) that, ADDED TO THE WALL POINTS, lays them best on the model, and the rms residual of
    `signed distance - width/2` after it. Gauss-Newton on the signed distance, started at the centroid difference."""
    layers = [
        lyr
        for lyr in walls.layers
        if lyr.mid_z is not None and lyr.mid_z > skip_below_mm
    ]
    pick = layers[:: max(1, len(layers) // REGISTER_LAYERS)]
    secs = sections(mesh_bed, np.array([lyr.mid_z for lyr in pick], dtype=float))
    data = [
        (*_layer_pts(lyr, 0.3), s) for lyr, s in zip(pick, secs, strict=True) if s is not None
    ]
    if not data:
        return (0.0, 0.0), math.nan
    wall_c = np.mean(np.vstack([d[0] for d in data]), axis=0)
    bnd_c = np.mean(np.vstack([d[2].boundary for d in data]), axis=0)
    shift = np.asarray(bnd_c - wall_c, dtype=float)  # added to the wall points it brings the centroids together
    for _ in range(REGISTER_ITERATIONS):
        a, b = _gauss_newton_rows(data, shift)
        step, *_ = np.linalg.lstsq(a, -b, rcond=None)
        shift = shift + step
        if float(np.linalg.norm(step)) < 1e-4:
            break
    _, resid = _gauss_newton_rows(data, shift)
    return (float(shift[0]), float(shift[1])), float(np.sqrt(np.mean(resid**2)))


def layers_in_window(
    walls: Walls, bed_to_axis: Matrix, z_range: tuple[float, float] | None
) -> list[WallLayer]:
    """The layers whose wall centroid lies inside `z_range` along the axis (all layers for None)."""
    layers = [lyr for lyr in walls.layers if lyr.mid_z is not None]
    if z_range is None:
        return layers
    keep: list[WallLayer] = []
    for lyr in layers:
        c = lyr.a.mean(axis=0)
        z = helical.apply(bed_to_axis, np.array([[c[0], c[1], lyr.mid_z]]))[0, 2]
        if z_range[0] <= z <= z_range[1]:
            keep.append(lyr)
    return keep


def deviation(
    mesh_bed: Any,
    layers: list[WallLayer],
    skip_below_mm: float,
    shift: tuple[float, float] = (0.0, 0.0),
) -> dict[str, Any]:
    """Per-layer deviation of the printed wall from the model section (`shift` is added to the wall points)."""
    secs = sections(mesh_bed, np.array([lyr.mid_z for lyr in layers], dtype=float))
    rows: list[tuple[float, float]] = []
    first: float | None = None
    for lyr, sec in zip(layers, secs, strict=True):
        if sec is None or lyr.mid_z is None:
            continue
        pts, width = lyr.points(DEVIATION_SPACING_MM)
        p = pts + np.asarray(shift)
        dist, inside, _ = signed_distance(sec, p)
        signed = np.where(inside, dist, -dist)
        dev = np.abs(signed - width / 2.0)
        if lyr.mid_z <= skip_below_mm:
            first = float(dev.max()) if first is None else max(first, float(dev.max()))
            continue
        rows.append((lyr.mid_z, float(dev.max())))
    if not rows:
        return {"layers": 0}
    arr = np.array(rows)
    worst = int(np.argmax(arr[:, 1]))
    return {
        "layers": len(rows),
        "max_mm": round(float(arr[worst, 1]), 4),
        "max_at_z_mm": round(float(arr[worst, 0]), 3),
        "mean_of_layer_max_mm": round(float(arr[:, 1].mean()), 4),
        "p95_of_layer_max_mm": round(float(np.percentile(arr[:, 1], 95)), 4),
        "first_layer_max_mm": None if first is None else round(first, 4),
        "skipped_below_mm": skip_below_mm,
    }


def surface_offsets(width: Array, side: str) -> Array:
    """Radial offset from a wall's centre line to its surface: outward surface (boss) = +w/2, inward (bore) = -w/2."""
    return width * 0.5 * (1.0 if side == "outer" else -1.0)


@dataclass
class PrintedWalls:
    """Printed wall points in the assembly frame plus how they were placed."""

    points: Array  # (N, 3) assembly frame
    width: Array
    placement: PrintPlacement
    deviation: dict[str, Any] = field(default_factory=dict)


def to_assembly(
    walls: Walls, bed_to_assembly: Matrix, spacing: float = SPACING_MM
) -> tuple[Array, Array]:
    """Printed wall points (bed xy at the layer mid height) -> assembly frame, with their line widths."""
    pts_all: list[Array] = []
    w_all: list[Array] = []
    for lyr in walls.layers:
        if lyr.mid_z is None:
            continue
        xy, w = lyr.points(spacing)
        p = np.column_stack([xy, np.full(len(xy), lyr.mid_z)])
        pts_all.append(helical.apply(bed_to_assembly, p))
        w_all.append(w)
    if not pts_all:
        return np.zeros((0, 3)), np.zeros(0)
    return np.vstack(pts_all), np.concatenate(w_all)
