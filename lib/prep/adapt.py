"""adapt.py — change a model minimally so it prints upright with no supports.

WHAT: adaptation operators on a print-frame mesh, each returning an `AdaptResult` (a `StepResult`) with a
  watertight mesh:
  * `fill_cavity`: a hollow shell open at the bottom becomes a solid on a flat base; the fill never touches
    the outer skin, so visible relief (eyes, mouth, whiskers) stays as modelled.
  * `wedge_fill`: a top-down support wedge under every unsupported cell; only material UNDER existing
    material is added (thin features get a vertical column down to the bed), so top and side surfaces never
    change. `Zone`s let a height range use a relaxed angle and no columns (the cat's face relief).
  * `suggest` reads an `overhang.OverhangReport` and proposes the operators, in order, with the volume each
    would add; `apply_suggestion` runs one (its zones travel as plain JSON in `Suggestion.params`).
  * `auto` is `suggest` + apply on ONE voxelisation, one cavity search and one manifold of the input: the
    pipeline calls it instead of the pair.
WHY: the Chekhov cat took hours of hand adaptation (cavity fill, wedges, boolean repair loops). The
  voxel logic is cheap; the hours went into booleans producing non-watertight output.
INVARIANTS:
  * Faces outside the added region are the input's faces: the added material is voxel-derived and united with
    the exact original triangles by manifold3d. Coordinates survive to float32 precision (~1e-5 mm), the
    precision of manifold3d's vertex buffer. `fill_cavity` additionally shifts the model down by `cut_z`
    (the flat cut becomes the new Z = 0, so the part stays in the print frame).
  * Boolean output is simplified (`Manifold.simplify`) before it becomes a mesh: the zero-area triangles a
    union leaves behind are what made earlier outputs non-watertight.
  * Voids and slivers smaller than `VOID_MIN_MM3` are dropped; bigger genuine bodies are kept.
  * Speed (411k-face Chekhov, 0.25 mm: analyse 5 s, auto 19 s): per-layer work runs on a layer-major copy with
    `overhang.dilate_disc`, 3-D dilation is shifted ORs (`_dilate6`), and the cavity/outside tie-breaking visits only
    the thin band of undecided cells. All three are pinned to the plain scipy results in the tests.
  * The pitch is the layer height of the analysis (see `overhang`): wedge slope per layer is `tan(angle)` cells.
  * A model that is merely bad (not a valid manifold, inside-out, empty after the cut) comes back as a FAIL
    result with the input mesh and a reason; only unusable arguments raise `InvalidArgument`.
ASSUMPTIONS: a closed (repaired) mesh in the print frame; the voxel grid is the one `voxel.voxelize` builds.
Past bugs: the Chekhov version gaussian-smoothed the added wedge; smoothing recedes convex corners, which leaves
  one-cell unsupported ribs along the wedge corners (re-analysis flagged them) and triples the face count. The
  added material is the plain voxel iso-surface: it re-voxelises to exactly the cells that were planned.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import trimesh
from numpy.typing import NDArray
from scipy import ndimage

from errors import InvalidArgument
from prep.contracts import Verdict
from prep.overhang import (
    OverhangReport,
    Zone,
    check_angle,
    dilate_disc,
    mask_bbox,
    zone_arrays,
)
from prep.voxel import Grid, voxelize

#: shells (internal voids, seam slivers) smaller than this are dropped after a boolean
VOID_MIN_MM3 = 50.0
#: added cells in clusters smaller than this are voxel noise
_MIN_WEDGE_CELLS = 2
#: an open-bottom cavity smaller than this is not worth an operator
_MIN_CAVITY_MM3 = VOID_MIN_MM3

LogValue = float | int | bool | str | None | list[float]
Param = float | list[dict[str, float | bool]]


@dataclass
class AdaptResult:
    """Outcome of one operator; satisfies `contracts.StepResult`. On FAIL `mesh` is the unchanged input."""

    mesh: trimesh.Trimesh
    op: str
    verdict: Verdict
    added_volume_mm3: float
    removed_volume_mm3: float
    watertight: bool
    faces: int
    log: dict[str, LogValue] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "verdict": self.verdict,
            "added_volume_mm3": round(float(self.added_volume_mm3), 2),
            "removed_volume_mm3": round(float(self.removed_volume_mm3), 2),
            "watertight": bool(self.watertight),
            "faces": int(self.faces),
            "log": {k: _plain(v) for k, v in self.log.items()},
            "reasons": list(self.reasons),
        }

    def summary(self) -> str:
        if self.verdict == "FAIL":
            return f"{self.op}: FAIL, {'; '.join(self.reasons)}"
        if self.added_volume_mm3 == 0 and self.removed_volume_mm3 == 0:
            return f"{self.op}: nothing to change"
        return (
            f"{self.op}: +{self.added_volume_mm3:.0f} mm3 / -{self.removed_volume_mm3:.0f} mm3, "
            f"{self.faces} faces, {'watertight' if self.watertight else 'NOT watertight'}"
        )


def _plain(v: LogValue) -> Any:
    if isinstance(v, list):
        return [float(x) for x in v]
    if isinstance(v, (np.floating, np.integer, np.bool_)):
        return v.item()
    return v


@dataclass(frozen=True)
class Suggestion:
    """A proposed operator call: `apply_suggestion` runs it, `predicted_added_mm3` is what it would add."""

    op: str
    params: dict[str, Param]
    predicted_added_mm3: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "params": dict(self.params),
            "predicted_added_mm3": round(self.predicted_added_mm3, 2),
            "reason": self.reason,
        }


# --- manifold3d bridge and mesh clean-up ---------------------------------------------------------------------------


class _NotManifold(ValueError):
    """The input mesh cannot be a manifold3d solid (open, non-manifold edges, inside out)."""


def _to_manifold(mesh: trimesh.Trimesh) -> Any:
    import manifold3d as mf

    raw = mf.Mesh(
        vert_properties=np.asarray(mesh.vertices, np.float32),
        tri_verts=np.asarray(mesh.faces, np.uint32),
    )
    solid = mf.Manifold(raw)
    if solid.status() != mf.Error.NoError:
        raise _NotManifold(
            f"not a valid manifold solid ({solid.status().name}); repair the mesh first"
        )
    if solid.volume() <= 0:
        raise _NotManifold("mesh volume is not positive (inside-out or empty)")
    return solid


def _from_manifold(solid: Any) -> trimesh.Trimesh:
    # simplify drops the zero-area triangles booleans leave behind; geometry change <= 0.1 um
    out = solid.simplify(1e-4).to_mesh()
    return trimesh.Trimesh(
        np.asarray(out.vert_properties, float)[:, :3],
        np.asarray(out.tri_verts),
        process=False,
    )


def _drop_small_shells(mesh: trimesh.Trimesh) -> tuple[trimesh.Trimesh, int]:
    """Drop every shell but the largest whose |volume| < `VOID_MIN_MM3`: seam voids and slivers of a union.

    Faces are selected, never re-merged: welding vertices again (`process=True`) joins touching shells at
    near-coincident vertices and makes edges non-manifold."""
    shells = trimesh.graph.connected_components(
        mesh.face_adjacency, nodes=np.arange(len(mesh.faces))
    )
    if len(shells) == 1:
        return mesh, 0
    main = max(shells, key=len)
    drop = [
        s
        for s in shells
        if s is not main
        and abs(trimesh.Trimesh(mesh.vertices, mesh.faces[s], process=False).volume)
        < VOID_MIN_MM3
    ]
    if not drop:
        return mesh, 0
    keep = np.ones(len(mesh.faces), dtype=bool)
    keep[np.concatenate(drop)] = False
    out = trimesh.Trimesh(mesh.vertices, mesh.faces[keep], process=False)
    out.remove_unreferenced_vertices()
    return out, len(drop)


def _voxels_to_mesh(
    field_: NDArray[Any], grid: Grid, lo: tuple[int, int, int] = (0, 0, 0)
) -> trimesh.Trimesh:
    """Iso-surface (level 0.5) of a boolean cell field in world mm; `lo` is the grid index of `field_[0, 0, 0]`."""
    # imported by name: mypy (python_version 3.10) must not follow skimage into tifffile's 3.12-only syntax
    marching_cubes = importlib.import_module("skimage.measure").marching_cubes

    values = field_.astype(np.float32)
    # one cell of zero padding closes the surface at the field border
    verts, faces, _, _ = marching_cubes(
        np.pad(values, 1), 0.5, spacing=(grid.pitch,) * 3
    )
    verts = verts + grid.origin + (np.asarray(lo, float) - 0.5) * grid.pitch
    mesh = trimesh.Trimesh(verts, faces, process=False)
    if mesh.volume < 0:
        mesh.invert()
    return mesh


def _crop(
    mask: NDArray[np.bool_],
) -> tuple[NDArray[np.bool_], tuple[int, int, int]] | None:
    """`mask` cut to the box of its True cells, with the grid index of the box corner; None when empty."""
    box = mask_bbox(mask)
    if box is None:
        return None
    return mask[box], (box[0].start, box[1].start, box[2].start)


def _unchanged(op: str, mesh: trimesh.Trimesh, log: dict[str, LogValue]) -> AdaptResult:
    return AdaptResult(
        mesh, op, "PASS", 0.0, 0.0, bool(mesh.is_watertight), len(mesh.faces), log
    )


def _failed(op: str, mesh: trimesh.Trimesh, reason: str) -> AdaptResult:
    return AdaptResult(
        mesh,
        op,
        "FAIL",
        0.0,
        0.0,
        bool(mesh.is_watertight),
        len(mesh.faces),
        {},
        [reason],
    )


def _finish(
    op: str,
    union: Any,
    shift_z: float,
    base_volume: float,
    cut_removed: float,
    log: dict[str, LogValue],
) -> AdaptResult:
    """Union -> simplified mesh -> small shells dropped -> verdict. `base_volume` is what the union grew from;
    `cut_removed` is volume already cut away before it."""
    out, dropped = _drop_small_shells(_from_manifold(union))
    if shift_z:
        out.apply_translation([0.0, 0.0, -shift_z])
    watertight = bool(out.is_watertight)
    log.update(
        shells_dropped=dropped,
        watertight=watertight,
        volume_mm3=float(out.volume),
        faces=int(len(out.faces)),
    )
    reasons = [] if watertight else ["result is not watertight"]
    return AdaptResult(
        out,
        op,
        "PASS" if watertight else "FAIL",
        max(float(out.volume) - base_volume, 0.0),
        cut_removed + max(base_volume - float(out.volume), 0.0),
        watertight,
        int(len(out.faces)),
        log,
        reasons,
    )


# --- voxel kernels --------------------------------------------------------------------------------------------------


def _dilate6(mask: NDArray[np.bool_], iterations: int = 1) -> NDArray[np.bool_]:
    """6-connected 3-D dilation by shifted ORs: equal to `ndimage.binary_dilation` (border 0) and ~10x faster on bool grids."""
    cur = mask
    for _ in range(iterations):
        out = cur.copy()
        for axis in range(3):
            fwd = tuple(slice(1, None) if a == axis else slice(None) for a in range(3))
            back = tuple(
                slice(None, -1) if a == axis else slice(None) for a in range(3)
            )
            out[fwd] |= cur[back]
            out[back] |= cur[fwd]
        cur = out
    return cur


def _grow_labels(
    outside: NDArray[np.bool_],
    void: NDArray[np.bool_],
    unclaimed: NDArray[np.bool_],
    steps: int,
) -> tuple[NDArray[np.bool_], NDArray[np.bool_]]:
    """Hand each `unclaimed` cell to the side (outside / void) its 6-neighbours reach, `steps` rounds.

    Rounds are simultaneous and a cell touching both sides is never claimed (a tie stays unfilled), exactly like
    alternating dilation of the two masks, but only the thin band of unclaimed cells is visited, not the grid."""
    shape = outside.shape
    padded = tuple(n + 2 for n in shape)
    lab = np.zeros(padded, dtype=np.int8)
    inner = (slice(1, -1),) * 3
    lab[inner][outside] = 1
    lab[inner][void] = 2
    xs, ys, zs = np.nonzero(unclaimed)
    idx = np.ravel_multi_index((xs + 1, ys + 1, zs + 1), padded)
    flat = lab.reshape(-1)
    strides = (padded[1] * padded[2], padded[2], 1)
    offsets = [sign * st for st in strides for sign in (-1, 1)]
    for _ in range(steps):
        if len(idx) == 0:
            break
        to_out = np.zeros(len(idx), dtype=bool)
        to_void = np.zeros(len(idx), dtype=bool)
        for off in offsets:
            nb = flat[idx + off]
            to_out |= nb == 1
            to_void |= nb == 2
        claim_out = to_out & ~to_void
        claim_void = to_void & ~to_out
        claimed = claim_out | claim_void
        if not claimed.any():
            break
        flat[idx[claim_out]] = 1
        flat[idx[claim_void]] = 2
        idx = idx[~claimed]
    inside = lab[inner]
    return inside == 1, inside == 2


# --- cavity ---------------------------------------------------------------------------------------------------------


def _cavity_fill(
    occ: NDArray[np.bool_],
    zc: NDArray[np.float64],
    cut_z: float,
    seal: int,
    overlap: int,
    skin: int,
) -> tuple[NDArray[np.bool_], NDArray[np.bool_]]:
    """(fill, void) masks. `void` is the enclosed cavity with the bed closing the opening; `fill` adds a
    wall overlap (never reaching the skin) and is extended down through the opening.

    The shell is pierced by sub-millimetre slits (mouth, eye corners), so the cavity is found on a solid dilated by
    `seal` cells; air cells next to the wall are then handed to whichever side (cavity or outside) is nearer by
    alternating growth. Air on the outside is never filled, so thin-walled relief stays exactly as modelled."""
    closed = occ.copy()
    closed[:, :, zc < cut_z] = True  # the bed closes the opening
    air = ~closed
    sealed = _dilate6(closed, seal)
    labels, _ = ndimage.label(np.pad(~sealed, 1, constant_values=True))
    outside_label = labels[0, 0, 0]
    labels = labels[1:-1, 1:-1, 1:-1]
    outside = (labels == outside_label) & air
    void = (labels != outside_label) & ~sealed
    del labels
    # only air inside the sealed band is undecided; a cell in the corner between wall and bed is up to
    # 2 * seal steps from the nearest decided cell
    outside, void = _grow_labels(outside, void, air & sealed, 2 * seal + 2)
    del sealed, air
    keep_clear = _dilate6(outside, skin)
    fill = void | (_dilate6(void, overlap) & closed & ~keep_clear)
    k0 = int(np.searchsorted(zc, cut_z))
    if 0 < k0 < fill.shape[2]:
        fill[:, :, :k0] = fill[
            :, :, k0 : k0 + 1
        ]  # through the opening; cut off at `cut_z` afterwards
    return fill, void


def _check_pitch(pitch: float) -> None:
    if pitch <= 0:
        raise InvalidArgument(
            "pitch", str(pitch), ["a positive number of mm"], command="prep"
        )


def _check_cavity_args(seal: int, overlap: int, skin: int, cut_z: float) -> None:
    if min(seal, overlap, skin) < 0 or cut_z < 0:
        raise InvalidArgument(
            "seal / overlap / skin / cut_z",
            f"{seal} / {overlap} / {skin} / {cut_z}",
            ["non-negative values"],
            command="prep",
        )


def _apply_cavity(
    solid: Any,
    mesh: trimesh.Trimesh,
    grid: Grid,
    fill: NDArray[np.bool_],
    cut_z: float,
    log: dict[str, LogValue],
) -> tuple[Any, AdaptResult]:
    """Union the fill plug into `solid` (a manifold of `mesh`) and cut the flat base; returns (union, result).

    `union` is still in the grid frame (base at `cut_z`); the result mesh is shifted down so the base is Z = 0."""
    volume_in = float(solid.volume())
    cut = solid.trim_by_plane([0, 0, 1], cut_z)
    if cut.is_empty():
        return solid, _failed(
            "fill_cavity", mesh, f"nothing remains above cut_z={cut_z:g} mm"
        )
    cropped = _crop(fill)
    assert cropped is not None  # callers apply the cavity only when it is non-empty
    plug = _to_manifold(_voxels_to_mesh(cropped[0], grid, cropped[1]))
    union = (cut + plug).trim_by_plane([0, 0, 1], cut_z)
    removed = max(volume_in - float(cut.volume()), 0.0)
    return union, _finish(
        "fill_cavity", union, cut_z, float(cut.volume()), removed, log
    )


def fill_cavity(
    mesh: trimesh.Trimesh,
    *,
    pitch: float = 0.25,
    seal: int = 3,
    overlap: int = 2,
    skin: int = 2,
    cut_z: float = 0.3,
) -> AdaptResult:
    """Solid fill of every cavity, flat base cut at `cut_z` (which becomes the new Z = 0).

    `seal` (cells): dilation that closes leaks and the thin wall at the mouth; `overlap` (cells): how far the fill
    reaches into the wall so the union has no seam; `skin` (cells): clearance kept to the outer skin. `cut_z` also
    removes the un-flat first layer of the rim, so the base is one plane."""
    _check_pitch(pitch)
    _check_cavity_args(seal, overlap, skin, cut_z)
    try:
        solid = _to_manifold(mesh)
    except _NotManifold as exc:
        return _failed("fill_cavity", mesh, str(exc))
    grid = voxelize(mesh, pitch)
    fill, void = _cavity_fill(grid.occ, grid.centres(2), cut_z, seal, overlap, skin)
    void_mm3 = float(void.sum()) * pitch**3
    log: dict[str, LogValue] = {"void_volume_mm3": void_mm3, "cut_z_mm": cut_z}
    if void_mm3 < _MIN_CAVITY_MM3:
        return _unchanged("fill_cavity", mesh, log)
    return _apply_cavity(solid, mesh, grid, fill, cut_z, log)[1]


# --- wedges ---------------------------------------------------------------------------------------------------------


@dataclass
class _Wedge:
    """Planned wedge material: `field` is the added cells plus a seam overlap into the solid, cropped to a box
    whose corner has grid index `lo`; `added_cells` counts the added (not overlap) cells."""

    field: NDArray[np.bool_]
    lo: tuple[int, int, int]
    added_cells: int
    z_range_mm: tuple[float, float]


def _wedge_cells(
    occ: NDArray[np.bool_],
    zc: NDArray[np.float64],
    angle_deg: float,
    zones: Sequence[Zone],
    bed_z: float,
) -> _Wedge | None:
    """Plan the material to add so every layer is supported at `angle_deg`; cells below `bed_z` are the bed.

    Top-down: `F[k] = occ[k] | erode(F[k+1], disc)`; cells of `F[k+1]` the dilated `F[k]` still does not reach
    (thin features the erosion removed) get a vertical column. The first zone containing a layer's height
    overrides angle and columns for the wedge under that layer."""
    full = np.ascontiguousarray(
        occ.transpose(2, 0, 1)
    )  # layer-major: layers are contiguous slabs
    full[zc < bed_z] = True
    angle, columns = zone_arrays(zc, angle_deg, zones)
    filled = full.copy()
    for k in range(full.shape[0] - 2, -1, -1):
        above = filled[k + 1]
        if not above.any():
            continue
        reach = float(np.tan(np.radians(angle[k + 1])))
        here = filled[k] | ~dilate_disc(
            ~above, reach, border=True
        )  # erosion of `above`
        if columns[k + 1]:
            here |= above & ~dilate_disc(here, reach)
        filled[k] = here
    added = filled & ~full
    del filled
    box = mask_bbox(added)
    if box is None:
        return None
    # pad so the seam-overlap dilation below stays inside the crop (clamped at the grid border)
    pad = 3
    box = tuple(
        slice(max(s.start - pad, 0), min(s.stop + pad, n))
        for s, n in zip(box, added.shape, strict=True)
    )
    cells = np.ascontiguousarray(added[box].transpose(1, 2, 0))  # back to (x, y, z)
    labels, n = ndimage.label(cells, structure=np.ones((3, 3, 3)))
    sizes = np.bincount(labels.ravel(), minlength=n + 1)
    keep = sizes >= _MIN_WEDGE_CELLS
    keep[0] = False
    cells = keep[labels]
    count = int(cells.sum())
    if count == 0:
        return None
    lo = (int(box[1].start), int(box[2].start), int(box[0].start))
    occ_crop = occ[
        lo[0] : lo[0] + cells.shape[0],
        lo[1] : lo[1] + cells.shape[1],
        lo[2] : lo[2] + cells.shape[2],
    ]
    field_ = cells | (_dilate6(cells, 2) & occ_crop)  # overlap into the solid: no seam
    zs = np.flatnonzero(cells.any(axis=(0, 1))) + lo[2]
    field_[:, :, zc[lo[2] : lo[2] + cells.shape[2]] < bed_z] = False
    z0 = float(zc[zs.min()]) - 0.5 * (zc[1] - zc[0])
    z1 = float(zc[zs.max()]) + 0.5 * (zc[1] - zc[0])
    return _Wedge(field_, lo, count, (z0, z1))


def _apply_wedge(
    solid: Any,
    grid: Grid,
    wedge: _Wedge,
    bed_z: float,
    base_volume: float,
    log: dict[str, LogValue],
) -> tuple[Any, AdaptResult]:
    """Union the wedge into `solid` (grid frame) and cut at the bed; the result mesh is shifted down by `bed_z`."""
    add = _to_manifold(_voxels_to_mesh(wedge.field, grid, wedge.lo))
    union = (solid + add).trim_by_plane([0, 0, 1], bed_z)
    return union, _finish("wedge_fill", union, bed_z, base_volume, 0.0, log)


def _wedge_log(
    angle_deg: float, wedge: _Wedge | None, pitch: float
) -> dict[str, LogValue]:
    log: dict[str, LogValue] = {
        "angle_deg": angle_deg,
        "voxel_added_mm3": 0.0 if wedge is None else wedge.added_cells * pitch**3,
    }
    if wedge is not None:
        log["added_z_range_mm"] = [wedge.z_range_mm[0], wedge.z_range_mm[1]]
    return log


def wedge_fill(
    mesh: trimesh.Trimesh,
    *,
    angle_deg: float = 45.0,
    zones: Sequence[Zone] = (),
    pitch: float = 0.25,
) -> AdaptResult:
    """Support every overhang steeper than `angle_deg` with material added under it (see module doc)."""
    check_angle(angle_deg)
    _check_pitch(pitch)
    try:
        solid = _to_manifold(mesh)
    except _NotManifold as exc:
        return _failed("wedge_fill", mesh, str(exc))
    grid = voxelize(mesh, pitch)
    wedge = _wedge_cells(grid.occ, grid.centres(2), angle_deg, zones, 0.0)
    log = _wedge_log(angle_deg, wedge, pitch)
    if wedge is None:
        return _unchanged("wedge_fill", mesh, log)
    return _apply_wedge(solid, grid, wedge, 0.0, float(mesh.volume), log)[1]


# --- planning: suggest / auto ---------------------------------------------------------------------------------------

#: operator defaults shared by `fill_cavity`, `suggest` and `auto`
_CAVITY_DEFAULTS = {"seal": 3, "overlap": 2, "skin": 2}
_CUT_Z = 0.3


@dataclass
class _Plan:
    """What `auto` would do on one voxelisation: the cavity fill (if the model is open at the bottom) and the
    wedge that remains needed afterwards."""

    grid: Grid
    cavity_fill: NDArray[np.bool_] | None
    cavity_mm3: float
    wedge: _Wedge | None
    bed_z: float  # bed height in the grid frame once the cavity op (if any) has cut the base


def _plan(grid: Grid, angle_deg: float, zones: Sequence[Zone]) -> _Plan:
    zc = grid.centres(2)
    occ = grid.occ
    fill, void = _cavity_fill(occ, zc, _CUT_Z, **_CAVITY_DEFAULTS)
    k0 = int(np.searchsorted(zc, _CUT_Z))
    void_mm3 = float(void[:, :, k0:].sum()) * grid.pitch**3
    open_bottom = bool(void[:, :, k0 : k0 + 2].any())
    if void_mm3 >= _MIN_CAVITY_MM3 and open_bottom:
        wedge = _wedge_cells(occ | fill, zc, angle_deg, zones, _CUT_Z)
        return _Plan(grid, fill, void_mm3, wedge, _CUT_Z)
    return _Plan(grid, None, 0.0, _wedge_cells(occ, zc, angle_deg, zones, 0.0), 0.0)


def _zones_param(zones: Sequence[Zone]) -> list[dict[str, float | bool]]:
    return [z.to_dict() for z in zones]


def _resolve_zones(
    report: OverhangReport, zones: Sequence[Zone] | None
) -> Sequence[Zone]:
    """None means "the zones the report was judged with"; an explicit sequence (even empty) overrides them."""
    return report.zones if zones is None else zones


def _clean(report: OverhangReport) -> bool:
    return not report.regions and not report.islands


def suggest(
    mesh: trimesh.Trimesh,
    report: OverhangReport,
    *,
    zones: Sequence[Zone] | None = None,
) -> list[Suggestion]:
    """Operators, in application order, that clear `report` (made from `mesh`), with the volume each adds.

    An open-bottom cavity gets `fill_cavity`; whatever is still unsupported after that (in the voxel model with the
    cavity filled) gets `wedge_fill` at the report's angle and `zones` (default: the zones of `report`)."""
    if _clean(report):
        return []
    use = _resolve_zones(report, zones)
    plan = _plan(voxelize(mesh, report.pitch), report.angle_deg, use)
    out: list[Suggestion] = []
    if plan.cavity_fill is not None:
        out.append(
            Suggestion(
                "fill_cavity",
                {"pitch": report.pitch, "cut_z": _CUT_Z},
                plan.cavity_mm3,
                f"cavity of {plan.cavity_mm3:.0f} mm3 is open at the bottom",
            )
        )
    if plan.wedge is not None:
        out.append(
            Suggestion(
                "wedge_fill",
                {
                    "pitch": report.pitch,
                    "angle_deg": report.angle_deg,
                    "zones": _zones_param(use),
                },
                plan.wedge.added_cells * report.pitch**3,
                f"{len(report.regions)} overhang region(s), {len(report.islands)} island(s) at {report.angle_deg:g} deg",
            )
        )
    return out


def auto(
    mesh: trimesh.Trimesh,
    report: OverhangReport,
    *,
    zones: Sequence[Zone] | None = None,
    pitch: float | None = None,
) -> list[AdaptResult]:
    """Apply what `suggest` would propose, sharing one voxelisation, one cavity search and one manifold input.

    Returns one `AdaptResult` per operator that ran, in order; the last one's mesh is the adapted model (its
    `added_volume_mm3` is relative to the step before). A FAIL result ends the list. Empty list: nothing to do.
    `pitch` defaults to the report's, which is the layer height the overhangs were judged at."""
    if _clean(report):
        return []
    use = _resolve_zones(report, zones)
    pitch = report.pitch if pitch is None else pitch
    _check_pitch(pitch)
    try:
        solid = _to_manifold(mesh)
    except _NotManifold as exc:
        return [_failed("auto", mesh, str(exc))]
    grid = voxelize(mesh, pitch)
    plan = _plan(grid, report.angle_deg, use)
    results: list[AdaptResult] = []
    if plan.cavity_fill is not None:
        log: dict[str, LogValue] = {
            "void_volume_mm3": plan.cavity_mm3,
            "cut_z_mm": _CUT_Z,
        }
        solid, cavity = _apply_cavity(solid, mesh, grid, plan.cavity_fill, _CUT_Z, log)
        results.append(cavity)
        if cavity.verdict == "FAIL":
            return results
        base_volume = float(cavity.mesh.volume)
        before: trimesh.Trimesh = cavity.mesh
    else:
        base_volume = float(mesh.volume)
        before = mesh
    wedge_log = _wedge_log(report.angle_deg, plan.wedge, pitch)
    if plan.wedge is None:
        if not results:
            results.append(_unchanged("wedge_fill", before, wedge_log))
        return results
    results.append(
        _apply_wedge(solid, grid, plan.wedge, plan.bed_z, base_volume, wedge_log)[1]
    )
    return results


def _zone_list(raw: Param | None) -> list[Zone]:
    if raw is None or isinstance(raw, float):
        return []
    return [Zone.from_dict(d) for d in raw]


def apply_suggestion(mesh: trimesh.Trimesh, suggestion: Suggestion) -> AdaptResult:
    """Run the operator a `Suggestion` names with its parameters."""
    p = suggestion.params
    if suggestion.op == "fill_cavity":
        return fill_cavity(mesh, pitch=_num(p["pitch"]), cut_z=_num(p["cut_z"]))
    if suggestion.op == "wedge_fill":
        return wedge_fill(
            mesh,
            angle_deg=_num(p["angle_deg"]),
            pitch=_num(p["pitch"]),
            zones=_zone_list(p.get("zones")),
        )
    raise InvalidArgument(
        "suggestion op", suggestion.op, ["fill_cavity", "wedge_fill"], command="prep"
    )


def _num(v: Param) -> float:
    if isinstance(v, list):
        raise InvalidArgument(
            "suggestion parameter", "a list", ["a number"], command="prep"
        )
    return v
