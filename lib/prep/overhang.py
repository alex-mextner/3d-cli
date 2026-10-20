"""overhang.py — where a model needs support when printed upright, from faces and from layers.

WHAT: three views of the same question "what would a slicer have to support?"
  * `face_overhang(mesh)`: triangle based — downward facing triangles steeper than `angle_deg` from
    vertical that are not on the bed, grouped into regions by face adjacency (cheap, exact, but blind to
    "a ceiling directly above another part", which is not an overhang).
  * `layer_unsupported(grid)`: slicer like — per voxel layer a cell is unsupported when no cell of the
    layer below lies within `tan(angle)` cells horizontally; unsupported cells are grouped into 3-D regions
    (area / span / z-range / layers) and layer components with nothing below are counted as islands.
  * `analyse(mesh)`: voxelises the mesh and folds both into an `OverhangReport` (a `StepResult`) with a
    verdict, the worst regions, and a per-height histogram. `Zone`s give height ranges their own angle.
WHY: the upright Chekhov cat needed a 60 mm ceiling and ~100 thin features cleared by hand; the pipeline
  needs the same judgement in seconds and needs it as numbers (`adapt.suggest` consumes the report).
INVARIANTS:
  * Print frame in (Z up, bed at Z = 0, mm). Nothing is below Z = 0, and the bed supports the first layer.
  * Pitch semantic: the voxel pitch IS the layer height of the analysis. The overhang tolerance per layer is
    `tan(angle_deg) * pitch`, i.e. exactly `tan(angle_deg)` cells; so a pitch coarser than the real layer
    height under-reports steep surfaces and a finer pitch over-reports staircase noise. Default 0.25 mm.
  * Regions below `min_cells` cells are voxel noise and are not reported (islands obey the same floor).
  * The verdict is FAIL when an island exists or any region spans more than `bridge_max_mm`, WARN for
    smaller bridges only, PASS when nothing is unsupported.
ASSUMPTIONS: the mesh is closed (parity voxelisation); an open mesh still yields numbers but the report
  says so and cannot be better than WARN.
Past bugs: the Chekhov version reported region extents in grid units without the grid origin, so regions
  were offset by the 2 mm margin; extents here are world mm.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import numpy as np
import scipy.sparse as sp
import trimesh
from numpy.typing import NDArray
from scipy import ndimage
from scipy.sparse.csgraph import connected_components

from errors import InvalidArgument
from prep.contracts import Verdict
from prep.voxel import Grid, disc, voxelize

#: faces whose highest vertex is below this height rest on the bed and are not overhangs
BED_TOL_MM = 0.3
#: at most this many regions / islands go into `to_dict()` (the rest are only counted)
MAX_LISTED = 30
#: height of one histogram bin
HIST_BIN_MM = 2.0


def check_angle(angle_deg: float) -> None:
    """Overhang angles are measured from vertical; 0 would forbid everything, 90 allows everything."""
    if not 0.0 < angle_deg < 90.0:
        raise InvalidArgument(
            "angle_deg",
            str(angle_deg),
            ["a number above 0 and below 90"],
            command="prep",
        )


def _r(x: float, nd: int = 3) -> float:
    return round(float(x), nd)


@dataclass(frozen=True)
class Zone:
    """A height range `[z_min, z_max)` (mm) judged / filled with its own angle.

    `angle_deg` replaces the global angle there; `columns=False` tells `adapt.wedge_fill` not to prop thin
    features with a vertical column inside the zone (thin relief such as eyelids is left as modelled). Where
    zones overlap, the first one in the sequence wins."""

    z_min: float
    z_max: float
    angle_deg: float
    columns: bool

    def __post_init__(self) -> None:
        check_angle(self.angle_deg)
        if not self.z_min < self.z_max:
            raise InvalidArgument(
                "zone z range",
                f"{self.z_min}..{self.z_max}",
                ["z_min < z_max"],
                command="prep",
            )

    def to_dict(self) -> dict[str, float | bool]:
        return {
            "z_min": float(self.z_min),
            "z_max": float(self.z_max),
            "angle_deg": float(self.angle_deg),
            "columns": self.columns,
        }

    @staticmethod
    def from_dict(data: Mapping[str, float | bool]) -> Zone:
        return Zone(
            float(data["z_min"]),
            float(data["z_max"]),
            float(data["angle_deg"]),
            bool(data["columns"]),
        )


def zone_arrays(
    zc: NDArray[np.float64], angle_deg: float, zones: Sequence[Zone]
) -> tuple[NDArray[np.float64], NDArray[np.bool_]]:
    """Per-layer (angle_deg, columns) at cell-centre heights `zc`: first matching zone, else the global angle with columns."""
    angle = np.full(len(zc), angle_deg)
    columns = np.ones(len(zc), dtype=bool)
    claimed = np.zeros(len(zc), dtype=bool)
    for zone in zones:
        hit = (zc >= zone.z_min) & (zc < zone.z_max) & ~claimed
        angle[hit] = zone.angle_deg
        columns[hit] = zone.columns
        claimed |= hit
    return angle, columns


@lru_cache(maxsize=32)
def _disc_half_widths(radius: float) -> tuple[int, ...]:
    """Half-width (cells) of each row of `voxel.disc(radius)` (-1 for an empty row): the disc is the union of centred row segments."""
    rows = disc(radius)
    return tuple((int(row.sum()) - 1) // 2 for row in rows)


def dilate_disc(
    layer: NDArray[np.bool_], radius: float, *, border: bool = False
) -> NDArray[np.bool_]:
    """Dilation of a 2-D mask by `voxel.disc(radius)`; cells outside the array count as `border`.

    Equal to `ndimage.binary_dilation(layer, structure=disc(radius), border_value=border)` but built from one
    1-D maximum filter per distinct row width plus row shifts: ~10x faster on layer-sized masks, and the
    analysis runs it once per layer. Erosion is `~dilate_disc(~layer, r, border=True)`."""
    widths = _disc_half_widths(radius)
    k = len(widths) // 2
    n = layer.shape[0]
    as_u8 = layer.view(np.uint8)
    out = np.zeros_like(layer)
    row_dilated: dict[int, NDArray[np.bool_]] = {}
    for dy in range(-k, k + 1):
        w = widths[dy + k]
        if w < 0:
            continue
        if w not in row_dilated:
            row_dilated[w] = (
                ndimage.maximum_filter1d(
                    as_u8, size=2 * w + 1, axis=1, mode="constant", cval=int(border)
                ).view(np.bool_)
                if w
                else layer
            )
        src = row_dilated[w]
        shift = abs(dy)
        if shift >= n:  # the whole array moves out of range: only the border is left
            out |= border
        elif dy >= 0:
            out[dy:] |= src[: n - dy]
            out[:dy] |= border
        else:
            out[:dy] |= src[-dy:]
            out[dy:] |= border
    return out


@dataclass(frozen=True)
class FaceRegion:
    """One connected patch of overhanging triangles."""

    faces: int
    area_mm2: float
    centroid_mm: tuple[float, float, float]
    z_min_mm: float
    z_max_mm: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "faces": self.faces,
            "area_mm2": _r(self.area_mm2),
            "centroid_mm": [_r(c) for c in self.centroid_mm],
            "z_mm": [_r(self.z_min_mm), _r(self.z_max_mm)],
        }


@dataclass
class FaceOverhang:
    """`mask[i]` is True for an overhanging triangle; `regions` are sorted by area, largest first."""

    mask: NDArray[np.bool_]
    regions: list[FaceRegion] = field(default_factory=list)

    @property
    def total_area_mm2(self) -> float:
        return float(sum(r.area_mm2 for r in self.regions))


def face_overhang(
    mesh: trimesh.Trimesh, angle_deg: float = 45.0, bed_tol: float = BED_TOL_MM
) -> FaceOverhang:
    """Downward facing triangles steeper than `angle_deg` from vertical, off the bed, as connected regions."""
    check_angle(angle_deg)
    normals_z = np.asarray(mesh.face_normals)[:, 2]
    tri = np.asarray(mesh.triangles)
    on_bed = tri[:, :, 2].max(axis=1) < bed_tol
    mask: NDArray[np.bool_] = (normals_z < -np.sin(np.radians(angle_deg))) & ~on_bed
    idx = np.flatnonzero(mask)
    if len(idx) == 0:
        return FaceOverhang(mask)
    remap = np.full(len(mesh.faces), -1, dtype=np.int64)
    remap[idx] = np.arange(len(idx))
    adj = np.asarray(mesh.face_adjacency)
    both = mask[adj[:, 0]] & mask[adj[:, 1]]
    a, b = remap[adj[both, 0]], remap[adj[both, 1]]
    graph = sp.coo_matrix((np.ones(len(a)), (a, b)), shape=(len(idx), len(idx)))
    n, label = connected_components(graph, directed=False)
    area = np.asarray(mesh.area_faces)[idx]
    centre = np.asarray(mesh.triangles_center)[idx]
    region_area = np.bincount(label, weights=area, minlength=n)
    centroid = (
        np.stack(
            [
                np.bincount(label, weights=area * centre[:, d], minlength=n)
                for d in range(3)
            ],
            axis=1,
        )
        / np.maximum(region_area, 1e-12)[:, None]
    )
    order = np.argsort(label, kind="stable")
    starts = np.r_[0, np.cumsum(np.bincount(label, minlength=n))[:-1]]
    zlo = np.minimum.reduceat(tri[idx, :, 2].min(axis=1)[order], starts)
    zhi = np.maximum.reduceat(tri[idx, :, 2].max(axis=1)[order], starts)
    counts = np.bincount(label, minlength=n)
    regions = [
        FaceRegion(
            int(counts[i]),
            float(region_area[i]),
            (float(centroid[i, 0]), float(centroid[i, 1]), float(centroid[i, 2])),
            float(zlo[i]),
            float(zhi[i]),
        )
        for i in np.argsort(-region_area)
    ]
    return FaceOverhang(mask, regions)


@dataclass(frozen=True)
class LayerRegion:
    """A 3-D connected region of unsupported cells; extents are world mm."""

    cells: int
    area_mm2: float  # footprint seen from above (what a support would have to cover)
    volume_mm3: float
    x_mm: tuple[float, float]
    y_mm: tuple[float, float]
    z_mm: tuple[float, float]
    layers: int
    span_mm: (
        float  # larger of the X / Y extents: the length a bridge would have to cross
    )
    centroid_mm: tuple[float, float, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cells": self.cells,
            "area_mm2": _r(self.area_mm2),
            "volume_mm3": _r(self.volume_mm3),
            "x_mm": [_r(v) for v in self.x_mm],
            "y_mm": [_r(v) for v in self.y_mm],
            "z_mm": [_r(v) for v in self.z_mm],
            "layers": self.layers,
            "span_mm": _r(self.span_mm),
            "centroid_mm": [_r(c) for c in self.centroid_mm],
        }


@dataclass(frozen=True)
class Island:
    """A layer component with nothing under it: it would be printed in mid-air."""

    z_mm: float
    area_mm2: float
    centroid_xy_mm: tuple[float, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "z_mm": _r(self.z_mm),
            "area_mm2": _r(self.area_mm2),
            "centroid_xy_mm": [_r(c) for c in self.centroid_xy_mm],
        }


@dataclass
class LayerOverhang:
    """Per-layer analysis of a voxel grid: `mask` marks unsupported cells (all of them, regions are the kept clusters)."""

    mask: NDArray[np.bool_]
    regions: list[LayerRegion]
    islands: list[Island]
    layer_z_mm: NDArray[np.float64]  # cell-centre height of each layer
    layer_area_mm2: NDArray[np.float64]  # unsupported area per layer, kept regions only


def layer_unsupported(
    grid: Grid,
    angle_deg: float = 45.0,
    min_cells: int = 4,
    bed_cells: int = 0,
    zones: Sequence[Zone] = (),
) -> LayerOverhang:
    """Slicer-like unsupported cells of `grid`; `bed_cells` bottom layers are the build plate (always supported).

    A layer is judged with the angle of the first `Zone` containing its cell-centre height, else `angle_deg`."""
    check_angle(angle_deg)
    pitch = grid.pitch
    angles, _ = zone_arrays(grid.centres(2), angle_deg, zones)
    # layer-major copy: a layer is then one contiguous slab, not a stride-nz slice of the (x, y, z) grid
    occ = np.ascontiguousarray(grid.occ.transpose(2, 0, 1))
    occ[:bed_cells] = True
    nz = occ.shape[0]
    present: NDArray[np.bool_] = np.asarray(occ.any(axis=(1, 2)))
    unsup = np.zeros_like(occ)
    islands: list[Island] = []
    ox, oy, oz = (float(v) for v in grid.origin)
    for k in range(1, nz):
        if not present[k]:
            continue
        cur, below = occ[k], occ[k - 1]
        if present[k - 1]:
            # tan(angle) cells per layer, because layer height == pitch
            reach = dilate_disc(below, float(np.tan(np.radians(angles[k]))))
            unsup[k] = cur & ~reach
        else:
            unsup[k] = cur
        islands.extend(
            Island(
                oz + (k + 0.5) * pitch,
                cells * pitch * pitch,
                (ox + (cx + 0.5) * pitch, oy + (cy + 0.5) * pitch),
            )
            for cells, cx, cy in _islands(cur, below, min_cells)
        )
    layer_cells = np.zeros(nz)
    regions: list[LayerRegion] = []
    box = mask_bbox(unsup)
    if box is not None:
        lab3, n3 = ndimage.label(unsup[box], structure=np.ones((3, 3, 3)))
        off = np.array([s.start for s in box])
        for i, found in enumerate(ndimage.find_objects(lab3), start=1):
            if found is None:
                continue
            inside = lab3[found] == i
            cells = int(inside.sum())
            if cells < min_cells:
                continue
            # slices are in (z, x, y) order
            start = np.array([s.start for s in found]) + off
            stop = np.array([s.stop for s in found]) + off
            layer_cells[start[0] : stop[0]] += inside.sum(axis=(1, 2))
            lo = grid.origin + np.array([start[1], start[2], start[0]]) * pitch
            hi = grid.origin + np.array([stop[1], stop[2], stop[0]]) * pitch
            zxy = (np.argwhere(inside).mean(axis=0) + 0.5 + start) * pitch
            centroid = np.array([zxy[1], zxy[2], zxy[0]]) + grid.origin
            regions.append(
                LayerRegion(
                    cells=cells,
                    area_mm2=float(inside.any(axis=0).sum()) * pitch * pitch,
                    volume_mm3=cells * pitch**3,
                    x_mm=(float(lo[0]), float(hi[0])),
                    y_mm=(float(lo[1]), float(hi[1])),
                    z_mm=(float(lo[2]), float(hi[2])),
                    layers=int(stop[0] - start[0]),
                    span_mm=float(max(hi[0] - lo[0], hi[1] - lo[1])),
                    centroid_mm=(
                        float(centroid[0]),
                        float(centroid[1]),
                        float(centroid[2]),
                    ),
                )
            )
    # ties broken by position so the order does not depend on the array layout
    regions.sort(key=lambda r: (-r.area_mm2, r.z_mm[0], r.x_mm[0], r.y_mm[0]))
    return LayerOverhang(
        np.ascontiguousarray(unsup.transpose(1, 2, 0)),
        regions,
        islands,
        grid.centres(2),
        layer_cells * pitch * pitch,
    )


def mask_bbox(mask: NDArray[np.bool_]) -> tuple[slice, ...] | None:
    """Tight index box of the True cells of `mask`, or None when empty."""
    out: list[slice] = []
    for axis in range(mask.ndim):
        other = tuple(a for a in range(mask.ndim) if a != axis)
        hit = np.flatnonzero(mask.any(axis=other))
        if len(hit) == 0:
            return None
        out.append(slice(int(hit[0]), int(hit[-1]) + 1))
    return tuple(out)


def _islands(
    cur: NDArray[np.bool_], below: NDArray[np.bool_], min_cells: int
) -> list[tuple[float, float, float]]:
    """(cells, centre_x, centre_y) in cell units of every component of `cur` with no cell over `below`.

    Only components of the cells NOT over `below` can be islands, and such a component is one exactly when it
    is not 4-adjacent to a cell of `cur` that is over `below`; so labelling is done on that (small) set,
    cropped to its bounding box, instead of on the whole layer."""
    new = cur & ~below
    box = mask_bbox(new)
    if box is None:
        return []
    h, w = new.shape
    sl = (
        slice(max(box[0].start - 1, 0), min(box[0].stop + 1, h)),
        slice(max(box[1].start - 1, 0), min(box[1].stop + 1, w)),
    )
    new_c = new[sl]
    lab, n = ndimage.label(new_c)
    anchored = dilate_disc(cur[sl] & below[sl], 1.0) & new_c  # 4-neighbourhood
    touched = np.zeros(n + 1, dtype=bool)
    touched[lab[anchored]] = True
    sizes = np.bincount(lab.ravel(), minlength=n + 1)
    found: list[tuple[float, float, float]] = []
    for c in np.flatnonzero(~touched[1:] & (sizes[1:] >= min_cells)) + 1:
        xs, ys = np.nonzero(lab == c)
        found.append(
            (
                float(sizes[c]),
                float(xs.mean() + sl[0].start),
                float(ys.mean() + sl[1].start),
            )
        )
    return found


@dataclass
class OverhangReport:
    """Result of `analyse`; satisfies `contracts.StepResult`."""

    verdict: Verdict
    angle_deg: float
    pitch: float
    bridge_max_mm: float
    watertight: bool
    islands: list[Island]
    regions: list[LayerRegion]  # sorted by footprint area, largest first
    histogram: list[
        tuple[float, float, float]
    ]  # (z_from, z_to, unsupported footprint mm2) for non-empty bins
    face_area_mm2: (
        float  # triangle based overhang area, for cross-checking the voxel view
    )
    zones: tuple[Zone, ...]
    reasons: list[str]

    @property
    def total_area_mm2(self) -> float:
        return float(sum(r.area_mm2 for r in self.regions))

    @property
    def max_span_mm(self) -> float:
        return max((r.span_mm for r in self.regions), default=0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "angle_deg": _r(self.angle_deg),
            "pitch_mm": _r(self.pitch, 4),
            "bridge_max_mm": _r(self.bridge_max_mm),
            "watertight": bool(self.watertight),
            "island_count": len(self.islands),
            "islands": [i.to_dict() for i in self.islands[:MAX_LISTED]],
            "region_count": len(self.regions),
            "regions": [r.to_dict() for r in self.regions[:MAX_LISTED]],
            "total_area_mm2": _r(self.total_area_mm2),
            "max_span_mm": _r(self.max_span_mm),
            "face_overhang_area_mm2": _r(self.face_area_mm2),
            "histogram": [
                {"z_from_mm": _r(a), "z_to_mm": _r(b), "area_mm2": _r(v)}
                for a, b, v in self.histogram
            ],
            "zones": [z.to_dict() for z in self.zones],
            "reasons": list(self.reasons),
        }

    def summary(self) -> str:
        if self.verdict == "PASS":
            return f"PASS: nothing needs support at {self.angle_deg:g} deg"
        return (
            f"{self.verdict}: {len(self.islands)} islands, {len(self.regions)} overhang regions "
            f"({self.total_area_mm2:.0f} mm2, widest {self.max_span_mm:.1f} mm) at {self.angle_deg:g} deg"
        )


def _histogram(
    layer_z: NDArray[np.float64], area: NDArray[np.float64]
) -> list[tuple[float, float, float]]:
    bins = np.floor(layer_z / HIST_BIN_MM).astype(np.int64)
    lo = int(bins.min())
    sums = np.bincount(bins - lo, weights=area)
    return [
        ((lo + i) * HIST_BIN_MM, (lo + i + 1) * HIST_BIN_MM, float(v))
        for i, v in enumerate(sums)
        if v > 0
    ]


def analyse(
    mesh: trimesh.Trimesh,
    *,
    angle_deg: float = 45.0,
    pitch: float = 0.25,
    bridge_max_mm: float = 10.0,
    zones: Sequence[Zone] = (),
) -> OverhangReport:
    """Voxel layer analysis of a print-frame mesh plus the triangle view, folded into one verdict.

    `zones` give height ranges their own angle (e.g. 75 deg for thin face relief); `face_area_mm2` is always
    measured at `angle_deg`, it is only a cross-check."""
    check_angle(angle_deg)
    if pitch <= 0 or bridge_max_mm <= 0:
        raise InvalidArgument(
            "pitch / bridge_max_mm",
            f"{pitch} / {bridge_max_mm}",
            ["positive numbers"],
            command="prep",
        )
    grid = voxelize(mesh, pitch)
    bed_cells = int(np.count_nonzero(grid.centres(2) < 0))
    layers = layer_unsupported(grid, angle_deg, bed_cells=bed_cells, zones=zones)
    faces = face_overhang(mesh, angle_deg)
    watertight = bool(mesh.is_watertight)
    report = OverhangReport(
        verdict="PASS",
        angle_deg=angle_deg,
        pitch=pitch,
        bridge_max_mm=bridge_max_mm,
        watertight=watertight,
        islands=layers.islands,
        regions=layers.regions,
        histogram=_histogram(layers.layer_z_mm, layers.layer_area_mm2),
        face_area_mm2=faces.total_area_mm2,
        zones=tuple(zones),
        reasons=[],
    )
    reasons = report.reasons
    if report.islands:
        reasons.append(f"{len(report.islands)} island(s) printed in mid-air")
    wide = [r for r in report.regions if r.span_mm > bridge_max_mm]
    if wide:
        reasons.append(
            f"{len(wide)} overhang region(s) wider than the {bridge_max_mm:g} mm bridge limit"
        )
    if report.islands or wide:
        report.verdict = "FAIL"
    elif report.regions:
        report.verdict = "WARN"
        reasons.append(
            f"{len(report.regions)} small bridge(s), widest {report.max_span_mm:.1f} mm"
        )
    if not watertight:
        reasons.append("mesh is not watertight: layer analysis is unreliable")
        if report.verdict == "PASS":
            report.verdict = "WARN"
    return report
