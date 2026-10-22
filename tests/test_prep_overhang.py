"""Overhang analysis: face view and layer view on synthetic solids with analytic answers."""

from __future__ import annotations

import json

import numpy as np
import pytest
import trimesh
from scipy import ndimage

from errors import InvalidArgument
from prep.contracts import StepResult
from prep.overhang import (
    Zone,
    analyse,
    dilate_disc,
    face_overhang,
    layer_unsupported,
    zone_arrays,
)
from prep.voxel import Grid, disc, voxelize


def box(extents: list[float], lo: list[float]) -> trimesh.Trimesh:
    m = trimesh.creation.box(extents=extents)
    m.apply_translation(np.array(lo) + np.array(extents) / 2)
    return m


def union(*parts: trimesh.Trimesh) -> trimesh.Trimesh:
    return trimesh.boolean.union(list(parts), engine="manifold")


def t_shape() -> trimesh.Trimesh:
    """4x4 stem 12 high, 20x20x2 cap on top: the cap underside (20x20 minus the stem) hangs in the air."""
    return union(box([4, 4, 12], [-2, -2, 0]), box([20, 20, 2], [-10, -10, 12]))


def chamfered_t() -> trimesh.Trimesh:
    """Trapezoid prism (4 mm wide at the bed, 20 mm at z=8: exactly 45 degrees, extruded 20 mm along Y) under a cap.

    A prism, not a square frustum: the corner ridges of a frustum climb at sqrt(2) cells per layer and are
    genuinely steeper than 45 degrees."""
    pts = [
        (sx * h, sy, z)
        for h, z in ((2, 0), (10, 8))
        for sx in (-1, 1)
        for sy in (-10, 10)
    ]
    prism = trimesh.convex.convex_hull(np.array(pts, dtype=float))
    return union(prism, box([20, 20, 2], [-10, -10, 8]))


def tilted_plate(from_horizontal_deg: float, z_lift: float = 5.0) -> trimesh.Trimesh:
    plate = box([20, 20, 1], [-10, -10, -0.5])
    rot = trimesh.transformations.rotation_matrix(
        np.radians(from_horizontal_deg), [1, 0, 0]
    )
    plate.apply_transform(rot)
    plate.apply_translation([0, 0, z_lift - plate.bounds[0][2]])
    return plate


def test_cube_on_bed_has_no_overhang_and_no_island() -> None:
    rep = analyse(box([10, 10, 10], [-5, -5, 0]))
    assert rep.verdict == "PASS"
    assert rep.regions == [] and rep.islands == []
    assert rep.face_area_mm2 == 0.0


def test_floating_bar_is_one_island_and_fails() -> None:
    rep = analyse(box([10, 2, 2], [-5, -1, 5]))
    assert len(rep.islands) == 1
    assert rep.islands[0].z_mm == pytest.approx(5.125)
    assert rep.islands[0].area_mm2 == pytest.approx(20.0)
    assert rep.verdict == "FAIL"


def test_t_cap_underside_is_one_region_with_analytic_numbers() -> None:
    rep = analyse(t_shape())
    assert len(rep.regions) == 1
    region = rep.regions[0]
    assert region.area_mm2 == pytest.approx(
        400 - 20
    )  # cap footprint minus the stem and the one-cell tolerance ring around it
    assert region.span_mm == pytest.approx(20.0)
    assert region.z_mm[0] == pytest.approx(12.0)
    assert region.layers == 1
    assert rep.verdict == "FAIL"  # 20 mm > 10 mm bridge limit
    assert rep.histogram == [(12.0, 14.0, pytest.approx(380.0))]


def test_small_bridge_is_warn_and_larger_limit_changes_the_verdict() -> None:
    assert analyse(t_shape(), bridge_max_mm=25.0).verdict == "WARN"


def test_45_degree_chamfer_needs_nothing() -> None:
    solid = chamfered_t()
    rep = analyse(solid)
    assert rep.verdict == "PASS"
    # and a 40 degree-from-vertical cone angle tolerance is violated by the same solid (the check can fail)
    assert analyse(solid, angle_deg=30.0).regions


def test_layer_unsupported_marks_the_cap_layer_only() -> None:
    grid = voxelize(t_shape(), 0.25)
    bed = int(np.count_nonzero(grid.centres(2) < 0))
    res = layer_unsupported(grid, 45.0, bed_cells=bed)
    zs = np.flatnonzero(res.mask.any(axis=(0, 1)))
    assert len(zs) == 1
    assert grid.centres(2)[zs[0]] == pytest.approx(12.125)
    # without a bed every cell of the first layer is a floating island
    assert layer_unsupported(grid, 45.0, bed_cells=0).islands


def test_face_overhang_area_of_tilted_plate_matches_analytic() -> None:
    steep = face_overhang(
        tilted_plate(30.0), 45.0
    )  # 60 degrees from vertical: overhang
    assert steep.total_area_mm2 == pytest.approx(400.0, rel=1e-6)
    assert len(steep.regions) == 1
    assert steep.regions[0].z_min_mm == pytest.approx(5.0)
    assert steep.regions[0].z_max_mm == pytest.approx(
        5.0 + 20 * np.sin(np.radians(30)), abs=1e-6
    )
    # 30 degrees from vertical: the big faces are printable; only the 1 x 20 mm end face looks down steeply
    shallow = face_overhang(tilted_plate(60.0), 45.0)
    assert shallow.total_area_mm2 == pytest.approx(20.0, rel=1e-6)


def test_face_overhang_ignores_the_bed_and_sorts_regions_by_area() -> None:
    assert face_overhang(box([20, 20, 1], [-10, -10, 0])).total_area_mm2 == 0.0
    small, big = box([5, 5, 1], [0, 0, 3]), box([10, 10, 1], [20, 0, 3])
    scene = trimesh.util.concatenate([small, big])
    regions = face_overhang(scene).regions
    assert [r.area_mm2 for r in regions] == pytest.approx([100.0, 25.0])
    assert regions[0].centroid_mm[0] == pytest.approx(25.0)


def test_report_satisfies_step_result_and_serialises_to_json() -> None:
    rep = analyse(t_shape())
    assert isinstance(rep, StepResult)
    data = json.loads(json.dumps(rep.to_dict()))
    assert data["verdict"] == "FAIL" and data["region_count"] == 1
    assert "FAIL" in rep.summary()


def test_dict_lists_at_most_30_but_counts_all() -> None:
    cubes = [box([2, 2, 2], [4.0 * i, 4.0 * j, 5]) for i in range(7) for j in range(6)]
    rep = analyse(trimesh.util.concatenate(cubes))
    data = rep.to_dict()
    assert data["island_count"] == 42
    assert len(data["islands"]) == 30


def test_open_mesh_is_reported_not_raised() -> None:
    cube = box([10, 10, 10], [-5, -5, 0])
    open_cube = trimesh.Trimesh(cube.vertices, cube.faces[2:], process=False)
    rep = analyse(open_cube)
    assert not rep.watertight
    assert rep.verdict in ("WARN", "FAIL")
    assert any("watertight" in r for r in rep.reasons)


def test_bad_angle_is_rejected() -> None:
    cube = box([10, 10, 10], [-5, -5, 0])
    with pytest.raises(InvalidArgument):
        analyse(cube, angle_deg=95.0)
    with pytest.raises(InvalidArgument):
        face_overhang(cube, angle_deg=0.0)


# --- zones, the fast kernels and the pinned reference behaviour ----------------------------------------------------


def test_zone_over_the_cap_relaxes_the_judgement_and_a_zone_elsewhere_does_not() -> (
    None
):
    shape = t_shape()
    assert analyse(shape).verdict == "FAIL"
    relaxed = analyse(shape, zones=[Zone(11.0, 15.0, 89.9, columns=True)])
    assert relaxed.verdict == "PASS" and relaxed.regions == []
    elsewhere = analyse(shape, zones=[Zone(0.0, 5.0, 89.9, columns=True)])
    assert elsewhere.verdict == "FAIL" and len(elsewhere.regions) == 1
    assert relaxed.to_dict()["zones"] == [
        {"z_min": 11.0, "z_max": 15.0, "angle_deg": 89.9, "columns": True}
    ]


def test_first_matching_zone_wins_and_zones_round_trip() -> None:
    zc = np.array([1.0, 6.0, 9.0])
    angle, columns = zone_arrays(
        zc, 45.0, [Zone(5.0, 10.0, 70.0, False), Zone(0.0, 20.0, 60.0, True)]
    )
    assert angle.tolist() == [60.0, 70.0, 70.0]
    assert columns.tolist() == [True, False, False]
    zone = Zone(1.5, 9.0, 75.0, False)
    assert Zone.from_dict(zone.to_dict()) == zone


@pytest.mark.parametrize("radius", [1.0, 0.9999999, 1.5, 2.0, 3.73, 60.0])
@pytest.mark.parametrize("border", [False, True])
def test_dilate_disc_equals_scipy_binary_dilation(radius: float, border: bool) -> None:
    rng = np.random.default_rng(7)
    mask = rng.random((37, 51)) < 0.08
    for layer in (mask, mask[3:30, 5:44]):  # the second is a non-contiguous view
        want = ndimage.binary_dilation(
            layer, structure=disc(radius), border_value=int(border)
        )
        assert (dilate_disc(layer, radius, border=border) == want).all()


def _reference_layers(
    occ: np.ndarray, angle: float, bed: int, min_cells: int
) -> tuple[np.ndarray, int, int]:
    """The straightforward per-layer scipy algorithm: (unsupported mask, island count, 3-D region count)."""
    occ = occ.copy()
    occ[:, :, :bed] = True
    st = disc(float(np.tan(np.radians(angle))))
    unsup = np.zeros_like(occ)
    islands = 0
    for k in range(1, occ.shape[2]):
        cur, below = occ[:, :, k], occ[:, :, k - 1]
        if not cur.any():
            continue
        reach = ndimage.binary_dilation(below, structure=st) if below.any() else below
        unsup[:, :, k] = cur & ~reach
        lab, n = ndimage.label(cur)
        ids = np.arange(1, n + 1)
        touches = np.asarray(ndimage.maximum(below.astype(np.uint8), lab, index=ids))
        sizes = np.asarray(ndimage.sum(cur, lab, index=ids))
        islands += int(((touches == 0) & (sizes >= min_cells)).sum())
    lab3, n3 = ndimage.label(unsup, structure=np.ones((3, 3, 3)))
    sizes3 = np.bincount(lab3.ravel(), minlength=n3 + 1)[1:]
    return unsup, islands, int((sizes3 >= min_cells).sum())


@pytest.mark.parametrize("seed", range(4))
@pytest.mark.parametrize("angle,bed", [(45.0, 0), (60.0, 2), (30.0, 0)])
def test_layer_unsupported_matches_the_plain_scipy_reference(
    seed: int, angle: float, bed: int
) -> None:
    rng = np.random.default_rng(seed)
    occ = ndimage.gaussian_filter(rng.random((44, 38, 36)), 2) > 0.505
    grid = Grid(occ, np.array([-5.0, -6.0, -1.0]), 0.25)
    got = layer_unsupported(grid, angle, 4, bed)
    mask, islands, region_count = _reference_layers(occ, angle, bed, 4)
    assert (got.mask == mask).all()
    assert len(got.islands) == islands
    assert len(got.regions) == region_count
    assert islands > 0 and region_count > 10  # the fixture is not trivially empty
