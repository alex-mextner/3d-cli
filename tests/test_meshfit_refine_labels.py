"""Region-limited refine, per-face parents / labels, and the fixed-direction displace op of `3d mesh deform`."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from errors import UsageError
from geometry.meshfit import engine, meshmath, retopo
from meshfit_fixtures import cube_sphere, spec

LAP = {
    "box": [-4.0, 4.0, -30.0, -10.0, -30.0, 30.0]
}  # a patch of the -Y side of the ball


def _areas(V: np.ndarray, F: np.ndarray) -> np.ndarray:
    t = V[F]
    return np.asarray(
        0.5 * np.linalg.norm(np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]), axis=1)
    )


def _longest(V: np.ndarray, F: np.ndarray) -> np.ndarray:
    t = V[F]
    return np.asarray(np.linalg.norm(t[:, [1, 2, 0]] - t, axis=2).max(axis=1))


def test_parents_say_which_input_face_every_child_was_cut_from() -> None:
    V, F = cube_sphere(10, 20.0)
    V2, F2, _, parent, zone = meshmath.refine_edges(V, F, 2.0)
    assert zone is None and len(parent) == len(F2) and len(F2) > len(F)
    # splitting is exact: the children of a face tile it, so their areas add up to the parent's
    assert np.allclose(
        np.bincount(parent, weights=_areas(V2, F2), minlength=len(F)), _areas(V, F)
    )
    # and the wrapper every existing caller uses still returns the same mesh
    V3, F3, _ = meshmath.refine_long_edges(V, F, 2.0)
    assert np.array_equal(V3, V2) and np.array_equal(F3, F2)


def test_zone_refine_cuts_only_near_the_zone_and_keeps_the_mesh_closed() -> None:
    V, F = cube_sphere(16, 20.0)
    zone = (np.abs(V[:, 0]) < 4.0) & (V[:, 1] < -10.0)
    assert 20 < zone.sum() < len(V) // 4
    V2, F2, _, parent, zone2 = meshmath.refine_edges(V, F, 1.2, 10, zone)
    assert zone2 is not None
    assert meshmath.is_watertight(
        F2
    )  # a neighbour outside the zone that shares a split edge was split too
    assert np.array_equal(V2[: len(V)], V)
    assert (
        _longest(V2, F2)[zone2[F2].any(axis=1)].max() <= 1.2 + 1e-9
    )  # the guarantee inside the zone
    far = np.abs(V[:, 1]) < 8.0
    far_faces = far[F].all(axis=1) & ~zone[F].any(axis=1)
    assert far_faces.sum() > 100
    assert (
        np.count_nonzero(np.isin(parent, np.nonzero(far_faces)[0])) == far_faces.sum()
    )  # untouched: one child each
    assert meshmath.signed_volume(V2, F2) == pytest.approx(
        meshmath.signed_volume(V, F), rel=1e-9
    )


def test_refine_without_a_zone_is_identical_to_a_zone_over_every_vertex() -> None:
    V, F = cube_sphere(8, 20.0)
    a = meshmath.refine_edges(V, F, 2.5)
    b = meshmath.refine_edges(V, F, 2.5, 10, np.ones(len(V), dtype=bool))
    assert (
        np.array_equal(a[0], b[0])
        and np.array_equal(a[1], b[1])
        and np.array_equal(a[3], b[3])
    )


def test_max_edge_is_a_hard_limit_target_edge_is_not() -> None:
    V, F = cube_sphere(10, 20.0)
    _, F2, info, _ = retopo.refine(V, F, {"max_edge": 1.5})
    assert info["longest_edge_in_zone_mm"] <= 1.5
    assert len(F2) > len(F)
    with pytest.raises(UsageError, match="above max_edge"):
        retopo.refine(V, F, {"max_edge": 0.2, "max_rounds": 1})
    with pytest.raises(UsageError, match="exactly one of"):
        retopo.refine(V, F, {"max_edge": 1.0, "target_edge": 1.0})
    with pytest.raises(UsageError, match="exactly one of"):
        retopo.refine(V, F, {})
    with pytest.raises(UsageError, match="positive"):
        retopo.refine(V, F, {"max_edge": -1.0})


def test_remesh_region_refines_only_there_and_reports_the_zone() -> None:
    V, F = cube_sphere(16, 20.0)
    ops: list[dict[str, Any]] = [{"op": "remesh", "region": "lap", "max_edge": 1.0}]
    V2, F2, report = engine.run_spec(
        V, F, spec(regions={"lap": LAP}, ops=ops), self_intersections=False
    )
    rec = report["ops"][0]
    assert rec["longest_edge_in_zone_mm"] <= 1.0 and rec["zone_vertices"] > 20
    assert rec["zone_faces_after"] > 0
    assert report["watertight_after"] and report["status"] == "PASS"
    # the far side is not touched: its faces keep their original (single) triangulation
    whole = engine.run_spec(
        V, F, spec(ops=[{"op": "remesh", "max_edge": 1.0}]), self_intersections=False
    )[1]
    assert len(F) < len(F2) < len(whole) // 2


def test_region_on_a_topology_op_that_cannot_honour_it_is_refused() -> None:
    V, F = cube_sphere(8, 20.0)
    for op in (
        {"op": "decimate", "region": "lap", "tolerance": 0.1},
        {"op": "repair", "region": "lap"},
    ):
        with pytest.raises(UsageError, match="only honoured by remesh engine refine"):
            engine.run_spec(
                V, F, spec(regions={"lap": LAP}, ops=[op]), self_intersections=False
            )
    with pytest.raises(UsageError, match="only honoured by engine refine"):
        retopo.remesh(
            V,
            F,
            {"engine": "pymeshlab", "target_edge": 2.0},
            np.ones(len(V), dtype=bool),
        )


def test_labels_follow_the_faces_through_refine_and_every_class_keeps_its_area() -> (
    None
):
    V, F = cube_sphere(12, 20.0)
    labels = (V[F].mean(axis=1)[:, 1] < 0).astype(np.int64) + 3 * (
        V[F].mean(axis=1)[:, 2] > 5
    )  # 4 classes
    ops: list[dict[str, Any]] = [{"op": "remesh", "region": "lap", "max_edge": 1.2}]
    V2, F2, _, parent = engine.run_spec_with_parents(
        V,
        F,
        spec(regions={"lap": LAP}, ops=ops),
        self_intersections=False,
        carry_faces=True,
    )
    assert parent is not None and len(parent) == len(F2) > len(F)
    carried = labels[parent]
    for cls in np.unique(labels):
        assert _areas(V2, F2)[carried == cls].sum() == pytest.approx(
            _areas(V, F)[labels == cls].sum(), rel=1e-9
        )


def test_vertex_ops_keep_the_face_order_so_labels_need_no_remap() -> None:
    V, F = cube_sphere(8, 20.0)
    _, F2, _, parent = engine.run_spec_with_parents(
        V,
        F,
        spec(ops=[{"op": "inflate", "amount": 0.5}]),
        self_intersections=False,
        carry_faces=True,
    )
    assert (
        parent is not None
        and np.array_equal(parent, np.arange(len(F)))
        and np.array_equal(F2, F)
    )


@pytest.mark.parametrize(
    "op",
    [
        {"op": "decimate", "tolerance": 0.05},
        {"op": "repair"},
        {"op": "remesh", "engine": "botsch", "target_edge": 3.0},
    ],
)
def test_carry_faces_refuses_ops_without_a_face_map_before_running_them(
    op: dict[str, Any],
) -> None:
    V, F = cube_sphere(8, 20.0)
    with pytest.raises(UsageError, match="cannot carry per-face labels"):
        engine.run_spec_with_parents(
            V, F, spec(ops=[op]), self_intersections=False, carry_faces=True
        )
    # without labels the same op is fine: run_spec only needs a parent map when asked for one
    _, _, report = engine.run_spec(
        V, F, spec(ops=[{"op": "repair"}]), self_intersections=False
    )
    assert report["status"] == "PASS"


def test_displace_moves_along_the_fixed_direction_scaled_by_the_region_weight() -> None:
    V, F = cube_sphere(16, 20.0)
    regions = {
        "cap": {"sphere": {"center": [0.0, -20.0, 0.0], "radius": 5.0}, "falloff": 5.0}
    }
    ops: list[dict[str, Any]] = [
        {"op": "displace", "region": "cap", "direction": [0, -2, 0], "amount": 1.5}
    ]
    V2, _, report = engine.run_spec(
        V, F, spec(regions=regions, ops=ops), self_intersections=False
    )
    delta = V2 - V
    inside = np.linalg.norm(V - [0, -20, 0], axis=1) <= 5.0
    assert inside.sum() > 5
    assert np.allclose(
        delta[inside], [0, -1.5, 0], atol=1e-5
    )  # full weight: exactly amount, along unit(direction)
    assert np.allclose(delta[:, [0, 2]], 0.0)  # only the direction axis moves
    ramp = (np.linalg.norm(V - [0, -20, 0], axis=1) > 5.0) & (
        np.linalg.norm(V - [0, -20, 0], axis=1) < 10.0
    )
    assert (
        ramp.sum() > 5
        and (np.abs(delta[ramp, 1]) < 1.5).all()
        and (np.abs(delta[ramp, 1]) > 0).any()
    )
    assert np.abs(delta[np.linalg.norm(V - [0, -20, 0], axis=1) >= 10.0]).max() == 0.0
    assert report["ops"][0]["max_displacement_mm"] == pytest.approx(1.5, abs=1e-5)


def test_displace_respects_the_keepout_zone() -> None:
    V, F = cube_sphere(16, 20.0)
    regions = {
        "cap": {"sphere": {"center": [0.0, -20.0, 0.0], "radius": 9.0}},
        "pin": {"sphere": {"center": [0.0, -20.0, 0.0], "radius": 4.0}},
    }
    ops: list[dict[str, Any]] = [
        {"op": "displace", "region": "cap", "direction": [0, -1, 0], "amount": 2.0}
    ]
    V2, _, report = engine.run_spec(
        V, F, spec(regions=regions, keepout=["pin"], ops=ops), self_intersections=False
    )
    pinned = np.linalg.norm(V - [0, -20, 0], axis=1) <= 4.0
    assert pinned.sum() > 3 and np.array_equal(V2[pinned], V[pinned])
    assert (
        np.abs(
            (V2 - V)[~pinned & (np.linalg.norm(V - [0, -20, 0], axis=1) < 9.0)]
        ).max()
        > 1.0
    )
    assert report["keepout"]["gate"] == "PASS"


@pytest.mark.parametrize("direction", [[0, 0, 0], [1, 2], "y", None])
def test_displace_needs_a_non_zero_three_vector_direction(direction: Any) -> None:
    raw = {
        "op": "displace",
        "amount": 1.0,
        **({} if direction is None else {"direction": direction}),
    }
    with pytest.raises(UsageError, match="displace needs direction"):
        spec(ops=[raw])
