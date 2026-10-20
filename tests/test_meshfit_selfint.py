"""Self-intersection gate (built-in KD-tree edge/triangle test) and the optional remesh / decimate engines."""

from __future__ import annotations

import numpy as np
import pytest
from errors import GateFailure
from geometry.meshfit import engine, meshmath, selfint
from meshfit_fixtures import cube_sphere, spec, torus

EYE = {"sphere": {"center": [20.0, 0.0, 0.0], "radius": 6.0}, "falloff": 4.0}


def _two_spheres(shift: float) -> tuple[np.ndarray, np.ndarray]:
    V, F = cube_sphere(20, 10.0)
    return np.concatenate([V, V + [shift, 0.0, 0.0]]), np.concatenate([F, F + len(V)])


def test_clean_meshes_have_no_piercing_pairs() -> None:
    for V, F in (cube_sphere(24, 20.0), torus(), _two_spheres(30.0)):
        hits, candidates = selfint.piercing_pairs(V, F)
        assert hits == 0 and candidates >= 0


def test_overlapping_spheres_are_detected_and_apart_spheres_are_not() -> None:
    overlapping = selfint.piercing_pairs(*_two_spheres(12.0))[0]
    assert (
        overlapping > 50
    )  # two radius-10 spheres 12 mm apart interpenetrate along a circle
    assert selfint.piercing_pairs(*_two_spheres(25.0))[0] == 0


def test_count_reports_engine_and_unit() -> None:
    info = selfint.count_self_intersections(*_two_spheres(12.0))
    assert info["count"] > 0 and info["engine"] and info["unit"]


def test_a_fur_that_pushes_a_surface_through_another_is_reported_in_the_report() -> (
    None
):
    """Two spheres 0.4 mm apart touch-free; inflating by 1 mm pushes them through each other: after > before."""
    V, F = _two_spheres(20.4)
    sp = spec(ops=[{"op": "inflate", "amount": 1.0}])
    _, _, report = engine.run_spec(V, F, sp)
    si = report["self_intersections"]
    assert si["before"]["count"] == 0 and si["after"]["count"] > 0


def test_botsch_remesh_changes_topology_keeps_it_closed_and_is_refused_next_to_a_keepout() -> (
    None
):
    pytest.importorskip("gpytoolbox")
    V, F = cube_sphere(24, 20.0)
    sp = spec(
        ops=[{"op": "remesh", "engine": "botsch", "target_edge": 2.0, "iterations": 5}]
    )
    V2, F2, report = engine.run_spec(V, F, sp, self_intersections=False)
    assert report["watertight_after"] and report["ops"][0]["engine"] == "botsch"
    assert report["volume_after_mm3"] == pytest.approx(
        report["volume_before_mm3"], rel=0.02
    )
    edge = meshmath.mean_edge_length(V2, F2)
    assert 1.4 < edge < 2.6
    with pytest.raises(GateFailure, match="keep-out"):
        engine.run_spec(
            V,
            F,
            spec(regions={"eye": EYE}, keepout=["eye"], ops=sp["ops"]),
            self_intersections=False,
        )


def test_quadric_decimate_hits_the_requested_face_count_and_stays_closed() -> None:
    pytest.importorskip("fast_simplification")
    V, F = cube_sphere(40, 20.0)
    sp = spec(ops=[{"op": "decimate", "target_faces": 4000}])
    _, F2, report = engine.run_spec(V, F, sp, self_intersections=False)
    assert (
        report["ops"][0]["engine"] == "quadric"
    )  # target_faces selects the quadric engine
    assert 3500 < len(F2) < 4600 and report["watertight_after"]
