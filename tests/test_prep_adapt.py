"""Adaptation operators on synthetic solids: analytic volumes, untouched visible surfaces, honest failures."""

from __future__ import annotations

import json

import numpy as np
import pytest
import trimesh
from scipy import ndimage
from scipy.spatial import cKDTree

from errors import InvalidArgument
from prep.adapt import (
    Suggestion,
    _cavity_fill,
    _dilate6,
    _wedge_cells,
    apply_suggestion,
    auto,
    fill_cavity,
    suggest,
    wedge_fill,
)
from prep.contracts import StepResult
from prep.overhang import Zone, analyse, face_overhang
from prep.voxel import disc


def box(extents: list[float], lo: list[float]) -> trimesh.Trimesh:
    m = trimesh.creation.box(extents=extents)
    m.apply_translation(np.array(lo) + np.array(extents) / 2)
    return m


def union(*parts: trimesh.Trimesh) -> trimesh.Trimesh:
    return trimesh.boolean.union(list(parts), engine="manifold")


def t_shape() -> trimesh.Trimesh:
    return union(box([4, 4, 12], [-2, -2, 0]), box([20, 20, 2], [-10, -10, 12]))


def open_dome() -> trimesh.Trimesh:
    """Hollow hemisphere shell (R 10, wall 2) resting on the bed, open underneath."""
    outer = trimesh.creation.icosphere(subdivisions=4, radius=10.0)
    inner = trimesh.creation.icosphere(subdivisions=4, radius=8.0)
    upper = box([30, 30, 15], [-15, -15, 0])
    return trimesh.boolean.intersection(
        [trimesh.boolean.difference([outer, inner], engine="manifold"), upper],
        engine="manifold",
    )


# analytic: a 20x20 cap on a 4x4 stem, wedge from half-width 10 at z=12 down to the stem at z=4 (8 mm at 45 degrees)
T_WEDGE_MM3 = 4 * (1000 - 8) / 3 - 16 * 8


def test_wedge_fill_adds_the_analytic_wedge_and_leaves_the_top_exact() -> None:
    mesh = t_shape()
    res = wedge_fill(mesh)
    assert res.watertight and res.verdict == "PASS"
    assert res.added_volume_mm3 > 0
    assert res.added_volume_mm3 == pytest.approx(T_WEDGE_MM3, rel=0.10)
    assert res.mesh.volume == pytest.approx(
        mesh.volume + res.added_volume_mm3, rel=1e-6
    )
    # visible surfaces: every original vertex of the cap top and sides is still there
    cap = mesh.vertices[mesh.vertices[:, 2] > 12.5]
    dist, _ = cKDTree(res.mesh.vertices).query(cap)
    assert dist.max() < 1e-4
    # the re-analysis finds nothing left to support, where the input had a 20 mm bridge
    assert analyse(mesh).verdict == "FAIL"
    assert analyse(res.mesh).verdict == "PASS"
    assert (
        face_overhang(res.mesh, 46.0).total_area_mm2
        < face_overhang(mesh, 46.0).total_area_mm2 / 10
    )


def test_wedge_fill_of_a_45_degree_chamfer_adds_nothing() -> None:
    prism_pts = [
        (sx * h, sy, z)
        for h, z in ((2, 0), (10, 8))
        for sx in (-1, 1)
        for sy in (-10, 10)
    ]
    prism = trimesh.convex.convex_hull(np.array(prism_pts, dtype=float))
    mesh = union(prism, box([20, 20, 2], [-10, -10, 8]))
    res = wedge_fill(mesh)
    assert res.added_volume_mm3 < 0.002 * mesh.volume
    assert res.summary().endswith("nothing to change") or res.added_volume_mm3 < 5


def test_relaxed_zone_adds_less_and_no_columns_leaves_a_thin_rod_alone() -> None:
    plain = wedge_fill(t_shape())
    relaxed = wedge_fill(t_shape(), zones=[Zone(0.0, 20.0, 75.0, columns=True)])
    assert 0 < relaxed.added_volume_mm3 < 0.6 * plain.added_volume_mm3
    # a 0.6 mm rod sticking out of a stem has no wedge (erosion removes it): it gets a column, unless zoned off
    rod = union(box([4, 4, 12], [-2, -2, 0]), box([10, 0.6, 0.6], [2, -0.3, 6]))
    propped = wedge_fill(rod)
    free = wedge_fill(rod, zones=[Zone(0.0, 20.0, 45.0, columns=False)])
    assert propped.added_volume_mm3 > 15
    assert free.added_volume_mm3 < 1


def test_fill_cavity_makes_the_open_dome_a_solid_on_a_flat_base() -> None:
    dome = open_dome()
    assert face_overhang(dome, 45.0).total_area_mm2 > 100  # the inner ceiling
    res = fill_cavity(dome)
    assert res.watertight and res.verdict == "PASS"
    cavity = (
        2 / 3 * np.pi * 8**3 - np.pi * 8**2 * 0.3
    )  # inner hemisphere above the 0.3 mm cut
    assert res.added_volume_mm3 == pytest.approx(cavity, rel=0.10)
    assert res.removed_volume_mm3 == pytest.approx(
        dome.volume - res.mesh.volume + res.added_volume_mm3, rel=1e-6
    )
    assert res.mesh.bounds[0][2] == pytest.approx(0.0, abs=1e-4)
    base = res.mesh.face_normals[:, 2] < -0.999
    assert res.mesh.area_faces[base].sum() == pytest.approx(
        np.pi * 10**2, rel=0.02
    )  # one flat disc
    assert face_overhang(res.mesh, 45.0).total_area_mm2 < 1.0
    # exterior vertices are the input's (shifted down by the cut)
    outer = dome.vertices[
        (np.linalg.norm(dome.vertices, axis=1) > 9.9) & (dome.vertices[:, 2] > 1.0)
    ]
    dist, _ = cKDTree(res.mesh.vertices + [0, 0, 0.3]).query(outer)
    assert dist.max() < 1e-4


def test_fill_cavity_leaves_a_solid_alone() -> None:
    solid = box([10, 10, 10], [-5, -5, 0])
    res = fill_cavity(solid)
    assert res.added_volume_mm3 == 0 and res.removed_volume_mm3 == 0
    assert len(res.mesh.faces) == len(solid.faces)


def test_open_input_is_a_fail_result_not_an_exception() -> None:
    cube = box([10, 10, 10], [-5, -5, 0])
    open_cube = trimesh.Trimesh(cube.vertices, cube.faces[2:], process=False)
    for op in (fill_cavity, wedge_fill):
        res = op(open_cube)
        assert res.verdict == "FAIL" and res.reasons
        assert res.mesh is open_cube


def test_results_are_step_results_with_plain_json() -> None:
    res = wedge_fill(t_shape())
    assert isinstance(res, StepResult)
    data = json.loads(json.dumps(res.to_dict()))
    assert data["op"] == "wedge_fill" and data["watertight"] is True
    assert "wedge_fill" in res.summary()


def test_suggest_picks_wedge_for_the_cap_and_predicts_its_volume() -> None:
    mesh = t_shape()
    todo = suggest(mesh, analyse(mesh))
    assert [s.op for s in todo] == ["wedge_fill"]
    assert todo[0].predicted_added_mm3 == pytest.approx(
        wedge_fill(mesh).added_volume_mm3, rel=0.10
    )
    fixed = apply_suggestion(mesh, todo[0])
    assert analyse(fixed.mesh).verdict == "PASS"
    assert json.dumps(todo[0].to_dict())


def test_suggest_picks_the_cavity_fill_for_the_open_dome_and_nothing_for_a_cube() -> (
    None
):
    dome = open_dome()
    todo = suggest(dome, analyse(dome))
    assert [s.op for s in todo] == ["fill_cavity"]
    res = apply_suggestion(dome, todo[0])
    assert todo[0].predicted_added_mm3 == pytest.approx(res.added_volume_mm3, rel=0.10)
    assert analyse(res.mesh).verdict == "PASS"
    cube = box([10, 10, 10], [-5, -5, 0])
    assert suggest(cube, analyse(cube)) == []


def test_bad_arguments_are_rejected() -> None:
    mesh = t_shape()
    with pytest.raises(InvalidArgument):
        wedge_fill(mesh, angle_deg=90.0)
    with pytest.raises(InvalidArgument):
        Zone(5.0, 5.0, 45.0, columns=True)
    with pytest.raises(InvalidArgument):
        fill_cavity(mesh, pitch=0.0)
    with pytest.raises(InvalidArgument):
        apply_suggestion(mesh, Suggestion("polish", {}, 0.0, ""))


def test_dropping_a_sliver_shell_does_not_weld_the_main_shell_to_it() -> None:
    from prep.adapt import _drop_small_shells

    big = box([5, 5, 5], [0, 0, 0])
    sliver = box([1, 1, 1], [5, 5, 5])  # touches the big box in a single corner vertex
    both = trimesh.util.concatenate([big, sliver])
    out, dropped = _drop_small_shells(both)
    assert dropped == 1
    assert out.is_watertight and out.volume == pytest.approx(125.0)
    # a big second body is a real body and stays
    kept, none = _drop_small_shells(
        trimesh.util.concatenate([big, box([5, 5, 5], [10, 0, 0])])
    )
    assert none == 0 and kept.volume == pytest.approx(250.0)


# --- zones end to end ----------------------------------------------------------------------------------------------


def stem_with_spike() -> trimesh.Trimesh:
    """4x4 stem 12 high with a 0.6 mm square spike sticking 10 mm out sideways at z = 6 (a nose-like relief)."""
    return union(box([4, 4, 12], [-2, -2, 0]), box([10, 0.6, 0.6], [2, -0.3, 6]))


def test_spike_gets_a_column_below_the_zone_and_none_inside_it() -> None:
    spike = stem_with_spike()
    plain = wedge_fill(spike)
    zoned = wedge_fill(spike, zones=[Zone(5.0, 20.0, 45.0, columns=False)])
    outside_zone = wedge_fill(spike, zones=[Zone(8.0, 20.0, 45.0, columns=False)])
    assert plain.added_volume_mm3 > 15
    assert zoned.added_volume_mm3 < 1
    # the zone ends above the spike (z = 6..6.6): the column is back
    assert outside_zone.added_volume_mm3 == pytest.approx(
        plain.added_volume_mm3, rel=0.01
    )


def test_suggestion_carries_zones_as_json_and_apply_honours_them() -> None:
    spike = stem_with_spike()
    zone = Zone(5.0, 20.0, 45.0, columns=False)
    report = analyse(spike, zones=[zone])
    assert report.zones == (zone,)
    # the report was judged with the zone, so suggest defaults to it: the spike needs no column, nothing to do
    assert suggest(spike, report) == []
    bare = suggest(spike, analyse(spike), zones=())
    assert [s.op for s in bare] == ["wedge_fill"]
    assert bare[0].params["zones"] == []
    forced = suggest(spike, analyse(spike), zones=[zone])
    data = json.loads(json.dumps([s.to_dict() for s in forced]))
    assert (
        forced == []
    )  # an explicit zone list overrides the report's, and the zone removes the column
    assert data == []
    explicit = suggest(
        spike, analyse(spike), zones=[Zone(9.0, 20.0, 45.0, columns=False)]
    )
    assert json.loads(json.dumps(explicit[0].to_dict()))["params"]["zones"] == [
        {"z_min": 9.0, "z_max": 20.0, "angle_deg": 45.0, "columns": False}
    ]
    assert (
        apply_suggestion(spike, explicit[0]).added_volume_mm3 > 15
    )  # zone above the spike: column kept


# --- auto: one voxelisation for both operators ---------------------------------------------------------------------


def ledged_dome() -> trimesh.Trimesh:
    """Open dome shell with a 30 x 30 plate through it at z 6..7: a cavity AND a plate overhanging the outside."""
    return union(open_dome(), box([30, 30, 1], [-15, -15, 6]))


def test_auto_runs_the_cavity_fill_then_the_wedge_in_one_pass() -> None:
    mesh = ledged_dome()
    report = analyse(mesh)
    assert report.verdict == "FAIL"
    results = auto(mesh, report)
    assert [r.op for r in results] == ["fill_cavity", "wedge_fill"]
    assert all(r.verdict == "PASS" and r.watertight for r in results)
    assert results[0].added_volume_mm3 > 400  # the dome cavity
    assert results[1].added_volume_mm3 > 100  # the wedge under the plate overhang
    final = results[-1].mesh
    assert final.bounds[0][2] == pytest.approx(0.0, abs=1e-4)
    assert analyse(final).verdict == "PASS"
    # same answer as running the two operators one after the other, each on its own voxelisation
    first = fill_cavity(mesh)
    second = wedge_fill(first.mesh)
    assert results[0].added_volume_mm3 == pytest.approx(
        first.added_volume_mm3, rel=0.01
    )
    assert results[1].added_volume_mm3 == pytest.approx(
        second.added_volume_mm3, rel=0.05
    )
    assert final.volume == pytest.approx(second.mesh.volume, rel=0.01)
    # predicted by suggest to within the voxel noise
    todo = suggest(mesh, report)
    assert [s.op for s in todo] == ["fill_cavity", "wedge_fill"]
    assert todo[1].predicted_added_mm3 == pytest.approx(
        results[1].added_volume_mm3, rel=0.15
    )


def test_auto_on_a_clean_model_does_nothing_and_on_a_broken_one_fails_once() -> None:
    cube = box([10, 10, 10], [-5, -5, 0])
    assert auto(cube, analyse(cube)) == []
    mesh = t_shape()
    open_mesh = trimesh.Trimesh(mesh.vertices, mesh.faces[2:], process=False)
    results = auto(open_mesh, analyse(mesh))
    assert [r.verdict for r in results] == ["FAIL"]
    assert results[0].mesh is open_mesh
    only_wedge = auto(mesh, analyse(mesh))
    assert [r.op for r in only_wedge] == ["wedge_fill"]
    assert only_wedge[0].added_volume_mm3 == pytest.approx(
        wedge_fill(mesh).added_volume_mm3, rel=1e-6
    )


# --- the fast kernels equal the plain scipy ones --------------------------------------------------------------------


def test_dilate6_equals_scipy_binary_dilation() -> None:
    mask = np.random.default_rng(3).random((17, 19, 23)) < 0.03
    for n in (1, 2, 3):
        assert (_dilate6(mask, n) == ndimage.binary_dilation(mask, iterations=n)).all()


def _reference_cavity(
    occ: np.ndarray, zc: np.ndarray, cut_z: float, seal: int, overlap: int, skin: int
) -> tuple[np.ndarray, np.ndarray]:
    closed = occ.copy()
    closed[:, :, zc < cut_z] = True
    air = ~closed
    sealed = ndimage.binary_dilation(closed, iterations=seal)
    labels, _ = ndimage.label(np.pad(~sealed, 1, constant_values=True))
    out_label = labels[0, 0, 0]
    labels = labels[1:-1, 1:-1, 1:-1]
    outside = (labels == out_label) & air
    void = (labels != out_label) & ~sealed
    for _ in range(2 * seal + 2):
        to_o = ndimage.binary_dilation(outside) & air & ~outside & ~void
        to_v = ndimage.binary_dilation(void) & air & ~outside & ~void
        tie = to_o & to_v
        outside |= to_o & ~tie
        void |= to_v & ~tie
    clear = ndimage.binary_dilation(outside, iterations=skin)
    fill = void | (ndimage.binary_dilation(void, iterations=overlap) & closed & ~clear)
    k0 = int(np.searchsorted(zc, cut_z))
    fill[:, :, :k0] = fill[:, :, k0 : k0 + 1]
    return fill, void


def _reference_wedge(
    occ: np.ndarray, zc: np.ndarray, angle: float, bed_z: float
) -> np.ndarray:
    full = occ.copy()
    full[:, :, zc < bed_z] = True
    st = disc(float(np.tan(np.radians(angle))))
    filled = full.copy()
    for k in range(full.shape[2] - 2, -1, -1):
        above = filled[:, :, k + 1]
        if not above.any():
            continue
        here = filled[:, :, k] | ndimage.binary_erosion(above, structure=st)
        here |= above & ~ndimage.binary_dilation(here, structure=st)
        filled[:, :, k] = here
    return filled & ~full


@pytest.mark.parametrize("seed", range(3))
def test_cavity_planning_equals_the_plain_scipy_reference(seed: int) -> None:
    rng = np.random.default_rng(seed)
    blob = ndimage.gaussian_filter(rng.random((40, 36, 44)), 3) > 0.5
    shell = blob & ~ndimage.binary_erosion(blob, iterations=3)
    shell &= rng.random(shell.shape) > 0.02  # slits the seal has to close
    occ = np.zeros((40, 36, 48), dtype=bool)
    occ[:, :, 2:46] = shell
    zc = np.asarray(-0.5 + (np.arange(48) + 0.5) * 0.25, dtype=np.float64)
    for cut_z in (0.3, 0.0):
        fill, void = _cavity_fill(occ, zc, cut_z, 3, 2, 2)
        want_fill, want_void = _reference_cavity(occ, zc, cut_z, 3, 2, 2)
        assert (fill == want_fill).all() and (void == want_void).all()
        assert void.sum() > 500  # a real cavity was found


@pytest.mark.parametrize("seed", range(3))
def test_wedge_planning_equals_the_plain_scipy_reference(seed: int) -> None:
    rng = np.random.default_rng(seed)
    occ = ndimage.gaussian_filter(rng.random((34, 30, 40)), 2) > 0.52
    zc = np.asarray(-1.0 + (np.arange(40) + 0.5) * 0.25, dtype=np.float64)
    wedge = _wedge_cells(occ, zc, 45.0, (), 0.0)
    assert wedge is not None
    want = _reference_wedge(occ, zc, 45.0, 0.0)
    labels, n = ndimage.label(want, structure=np.ones((3, 3, 3)))
    sizes = np.bincount(labels.ravel(), minlength=n + 1)
    keep = sizes >= 2
    keep[0] = False
    assert wedge.added_cells == int(keep[labels].sum())
