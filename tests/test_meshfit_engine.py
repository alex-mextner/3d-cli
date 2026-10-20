"""Engine contracts: keep-out is bit-identical, the gate has teeth, runs are reproducible, topology stays closed."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from errors import GateFailure, UsageError
from geometry.meshfit import engine, meshmath, retopo
from meshfit_fixtures import box, cube_sphere, spec, torus

EYE = {"sphere": {"center": [20.0, 0.0, 0.0], "radius": 6.0}, "falloff": 4.0}
BODY_OPS: list[dict[str, Any]] = [
    {"op": "smooth", "iterations": 4},
    {"op": "inflate", "amount": 0.4},
    {"op": "fur", "amplitude": 0.3, "pitch": 3.0, "length": 6.0, "overhang_safe": True},
    {"op": "crease", "region": "ridge", "depth": 0.3, "strength": 0.2},
    {
        "op": "scale_local",
        "region": "ridge",
        "factor": [1.0, 1.0, 1.1],
        "pivot": [0, 0, 0],
    },
    {
        "op": "grab",
        "handles": [
            {
                "region": {"sphere": {"center": [0, 0, 20], "radius": 3}},
                "translate": [0, 0, 2],
            }
        ],
        "anchors": [{"zrange": [-30, -12]}],
        "solver": "arap",
        "influence": 16,
    },
]
REGIONS: dict[str, Any] = {"eye": EYE, "ridge": {"zrange": [-5, 5], "falloff": 5}}


def _run(
    V: np.ndarray, F: np.ndarray, **kw: Any
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    return engine.run_spec(
        V,
        F,
        spec(regions=REGIONS, keepout=["eye"], ops=BODY_OPS, **kw),
        self_intersections=False,
    )


def test_keepout_vertices_are_bit_identical_while_the_neighbours_move() -> None:
    V, F = cube_sphere(32, 20.0)
    V2, F2, report = _run(V, F)
    d = np.linalg.norm(V2 - V, axis=1)
    centre = np.linalg.norm(V - [20.0, 0.0, 0.0], axis=1)
    locked = centre <= 6.0
    assert locked.sum() > 30
    assert np.array_equal(V2[locked], V[locked])  # 0.0 mm, not "small"
    fade = (centre > 6.0) & (centre < 10.0)
    assert d[fade].max() > 0.0  # the neighbours in the fade band do move...
    assert d[centre >= 10.0].max() > 0.5  # ...and the far body is clearly modified
    assert (
        report["keepout"]["max_displacement_mm"] == 0.0
        and report["keepout"]["gate"] == "PASS"
    )
    assert report["keepout"]["locked_vertices"] == int(locked.sum())
    assert report["status"] == "PASS"


def test_fade_band_displacement_grows_smoothly_away_from_the_keepout() -> None:
    V, F = cube_sphere(32, 20.0)
    V2, _, _ = engine.run_spec(
        V,
        F,
        spec(regions=REGIONS, keepout=["eye"], ops=[{"op": "inflate", "amount": 1.0}]),
        self_intersections=False,
    )
    centre = np.linalg.norm(V - [20.0, 0.0, 0.0], axis=1)
    d = np.linalg.norm(V2 - V, axis=1)
    band = (centre > 6.0) & (centre < 10.0)
    order = np.argsort(centre[band])
    assert np.corrcoef(centre[band][order], d[band][order])[0, 1] > 0.95
    assert d[centre >= 10.0].min() == pytest.approx(1.0, abs=1e-9)


def test_gate_fails_when_something_moves_a_keepout_vertex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teeth: a rogue op that ignores the keep-out multiplier is caught by the final check against the INPUT mesh."""
    V, F = cube_sphere(24, 20.0)

    def rogue(self: engine.Pipeline, name: str, op: Any) -> dict[str, Any]:
        self.V = self.V + np.array([0.0, 0.0, 0.05])
        return {}

    monkeypatch.setattr(engine.Pipeline, "_apply_vertex_op", rogue)
    _, _, report = engine.run_spec(
        V,
        F,
        spec(regions=REGIONS, keepout=["eye"], ops=[{"op": "smooth"}]),
        self_intersections=False,
    )
    assert report["status"] == "FAIL" and report["keepout"]["gate"] == "FAIL"
    assert report["keepout"]["max_displacement_mm"] > 0.0


def test_tolerance_turns_the_gate_into_a_surface_distance_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    V, F = cube_sphere(24, 20.0)

    def nudge(self: engine.Pipeline, name: str, op: Any) -> dict[str, Any]:
        self.V = self.V + np.array([0.0, 0.0, 0.05])
        return {}

    monkeypatch.setattr(engine.Pipeline, "_apply_vertex_op", nudge)
    sp = spec(
        regions=REGIONS,
        keepout=["eye"],
        ops=[{"op": "smooth"}],
        keepout_tolerance_mm=0.1,
    )
    _, _, report = engine.run_spec(V, F, sp, self_intersections=False)
    assert (
        report["keepout"]["gate"] == "PASS"
        and 0.0 < report["keepout"]["max_displacement_mm"] <= 0.1
    )


def test_two_runs_are_identical_and_the_seed_changes_the_result() -> None:
    V, F = cube_sphere(32, 20.0)
    a = _run(V, F, seed=11)
    b = _run(V, F, seed=11)
    c = _run(V, F, seed=12)
    assert a[2]["mesh_sha256"] == b[2]["mesh_sha256"] and np.array_equal(a[0], b[0])
    assert a[2]["mesh_sha256"] != c[2]["mesh_sha256"]


@pytest.mark.parametrize("shape", ["sphere", "box", "torus"])
def test_watertight_and_manifold_are_preserved_on_every_shape(shape: str) -> None:
    import trimesh

    V, F = {
        "sphere": lambda: cube_sphere(24, 20.0),
        "box": lambda: box((30, 20, 12), 16),
        "torus": lambda: torus(),
    }[shape]()
    ops = [
        {"op": "smooth", "iterations": 3},
        {"op": "inflate", "amount": 0.3},
        {"op": "fur", "amplitude": 0.25, "pitch": 3.0, "length": 5.0},
        {"op": "remesh", "target_edge": 1.5},
    ]
    V2, F2, report = engine.run_spec(V, F, spec(ops=ops), self_intersections=False)
    assert report["watertight_before"] and report["watertight_after"]
    tm = trimesh.Trimesh(V2, F2, process=False)
    assert tm.is_watertight and tm.is_winding_consistent and tm.volume > 0
    assert len(F2) > len(F)


def test_refine_keeps_every_original_vertex_and_the_exact_shape() -> None:
    V, F = cube_sphere(12, 20.0)
    V2, F2, rounds = meshmath.refine_long_edges(V, F, 1.0)
    assert rounds >= 1 and np.array_equal(V2[: len(V)], V)
    assert meshmath.is_watertight(F2)
    e = np.concatenate([F2[:, [0, 1]], F2[:, [1, 2]], F2[:, [2, 0]]])
    assert np.linalg.norm(V2[e[:, 0]] - V2[e[:, 1]], axis=1).max() <= 1.0 + 1e-9
    # midpoint splitting is exact: the piecewise-linear surface (hence the volume) is unchanged
    assert meshmath.signed_volume(V2, F2) == pytest.approx(
        meshmath.signed_volume(V, F), rel=1e-9
    )


def test_refine_remesh_under_a_keepout_zone_stays_bit_identical() -> None:
    V, F = cube_sphere(20, 20.0)
    sp = spec(
        regions=REGIONS,
        keepout=["eye"],
        ops=[{"op": "remesh", "target_edge": 1.0}, {"op": "inflate", "amount": 0.5}],
    )
    V2, F2, report = engine.run_spec(V, F, sp, self_intersections=False)
    assert len(F2) > len(F) and report["keepout"]["gate"] == "PASS"
    assert report["keepout"]["missing_vertices"] == 0


def test_vertex_moving_topology_op_is_refused_when_a_keepout_exists_and_no_tolerance_is_set() -> (
    None
):
    V, F = cube_sphere(40, 20.0)
    sp = spec(
        regions=REGIONS, keepout=["eye"], ops=[{"op": "decimate", "tolerance": 0.05}]
    )
    with pytest.raises(GateFailure, match="keep-out"):
        engine.run_spec(V, F, sp, self_intersections=False)


def test_decimate_without_keepout_reduces_faces_and_keeps_the_mesh_closed() -> None:
    V, F = cube_sphere(40, 20.0)
    V2, F2, report = engine.run_spec(
        V, F, spec(ops=[{"op": "decimate", "tolerance": 0.2}]), self_intersections=False
    )
    assert len(F2) < 0.8 * len(F) and report["watertight_after"]
    assert report["volume_after_mm3"] == pytest.approx(
        report["volume_before_mm3"], rel=0.02
    )


def test_decimate_with_tolerance_reports_the_surface_distance_of_the_keepout() -> None:
    V, F = cube_sphere(40, 20.0)
    sp = spec(
        regions=REGIONS,
        keepout=["eye"],
        ops=[{"op": "decimate", "tolerance": 0.2}],
        keepout_tolerance_mm=0.5,
    )
    _, _, report = engine.run_spec(V, F, sp, self_intersections=False)
    assert (
        report["keepout"]["gate"] == "PASS"
        and report["keepout"]["max_displacement_mm"] <= 0.5
    )


def test_repair_builtin_drops_duplicate_faces_and_keeps_the_solid() -> None:
    V, F = cube_sphere(10, 20.0)
    dup = np.concatenate([F, F[:7]])
    V2, F2, info, preserves = retopo.repair(V, dup, {"engine": "builtin"})
    assert len(F2) == len(F) and info["watertight_after"] and not preserves


def test_unknown_op_key_and_unknown_op_are_spec_errors() -> None:
    with pytest.raises(UsageError, match="unknown keys"):
        spec(ops=[{"op": "smooth", "iteratoins": 3}])
    with pytest.raises(UsageError, match="unknown op"):
        spec(ops=[{"op": "melt"}])
    from geometry.meshfit import spec as spec_mod

    with pytest.raises(UsageError, match="non-empty list"):
        spec_mod.normalise_spec({"ops": []}, ".")


def test_landmark_boxes_become_regions(tmp_path: Any) -> None:
    import json

    from geometry.meshfit import spec as spec_mod

    (tmp_path / "lm.json").write_text(
        json.dumps(
            {
                "landmarks": {
                    "head": {"box": [-1, 1, -1, 1, 0, 2]},
                    "tail": {"centre": [0, 0, 0]},
                }
            }
        )
    )
    s = spec_mod.normalise_spec(
        {
            "landmarks": {"file": "lm.json", "prefix": "lm_"},
            "keepout": ["lm_head"],
            "ops": [{"op": "smooth", "region": "lm_head"}],
        },
        str(tmp_path),
    )
    assert s["regions"] == {
        "lm_head": {"box": [-1, 1, -1, 1, 0, 2]}
    }  # entries without a box are not regions


def test_report_carries_per_op_counts_volume_and_timing() -> None:
    V, F = cube_sphere(24, 20.0)
    _, _, report = _run(V, F)
    assert [r["op"] for r in report["ops"]] == [o["op"] for o in BODY_OPS]
    smooth = report["ops"][0]
    assert (
        smooth["vertices_moved"] > 0
        and smooth["max_displacement_mm"] > 0
        and smooth["seconds"] >= 0
    )
    assert (
        report["volume_before_mm3"] > 0
        and report["volume_after_mm3"] != report["volume_before_mm3"]
    )
    assert report["timing_s"] > 0 and len(report["mesh_sha256"]) == 64
