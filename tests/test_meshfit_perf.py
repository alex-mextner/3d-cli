"""Speed budgets. The 40k-face test always runs; the 400k-face one is heavy (run it on the remote box: MESHFIT_PERF=1).

Budgets are wall-clock seconds for the whole spec on a 16-core Linux box with the thread caps the repo sets;
they are deliberately ~3x the measured time so a loaded machine does not flake, while still failing on a
regression to per-vertex Python loops (which would be 100x slower).
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest
from geometry.meshfit import engine
from meshfit_fixtures import cube_sphere, spec

REGIONS: dict[str, Any] = {
    "cap": {"zrange": [10, 45], "falloff": 8},
    "eye": {"sphere": {"center": [40, 0, 0], "radius": 5}, "falloff": 4},
    "side": {"sphere": {"center": [0, -40, 0], "radius": 12}, "falloff": 6},
    "all": {"all": True},
    "wall": {"ramp": {"quantity": "nz", "from": -0.4, "to": -0.2}},
}


def _ops() -> list[dict[str, Any]]:
    return [
        {"op": "smooth", "region": "cap", "iterations": 5},
        {"op": "inflate", "region": "side", "amount": 0.8},
        {
            "op": "fur",
            "region": {"product": ["wall", "all"]},
            "amplitude": 0.35,
            "pitch": 1.9,
            "length": 11.0,
            "overhang_safe": True,
        },
        {
            "op": "grab",
            "handles": [
                {
                    "region": {"sphere": {"center": [0, 0, 40], "radius": 3}},
                    "translate": [0, 0, 3],
                }
            ],
            "anchors": [{"zrange": [-40, -20]}],
            "solver": "arap",
            "influence": 25,
            "iterations": 8,
        },
        {"op": "crease", "region": "side", "depth": 0.3, "strength": 0.3},
        {"op": "symmetrize", "axis": "x", "mode": "average"},
    ]


def _run(n: int) -> tuple[dict[str, Any], float, int]:
    V, F = cube_sphere(n, 40.0)
    started = time.perf_counter()
    _, _, report = engine.run_spec(
        V,
        F,
        spec(regions=REGIONS, keepout=["eye"], ops=_ops()),
        self_intersections=False,
    )
    return report, time.perf_counter() - started, len(F)


def test_forty_thousand_faces_run_in_a_few_seconds() -> None:
    report, seconds, faces = _run(58)  # 12 * 58^2 = 40,368 faces
    assert 35_000 < faces < 45_000
    assert (
        report["status"] == "PASS"
        and report["keepout"]["max_displacement_mm"] == 0.0
        and report["watertight_after"]
    )
    assert seconds < 15.0, f"{seconds:.1f}s for {faces} faces"


@pytest.mark.skipif(
    not os.environ.get("MESHFIT_PERF"),
    reason="heavy: set MESHFIT_PERF=1 (run on the remote box)",
)
def test_four_hundred_thousand_faces_run_under_the_stated_budget() -> None:
    report, seconds, faces = _run(183)  # 12 * 183^2 = 401,868 faces
    assert faces > 400_000
    assert (
        report["status"] == "PASS"
        and report["keepout"]["max_displacement_mm"] == 0.0
        and report["watertight_after"]
    )
    # measured on the 16-core box: ~4 s for the whole spec (ARAP on a 25 mm patch ~1.4 s, every brush < 0.4 s)
    assert seconds < 20.0, f"{seconds:.1f}s for {faces} faces; per op: " + ", ".join(
        f"{r['op']}={r['seconds']}" for r in report["ops"]
    )
    slow = {r["op"]: r["seconds"] for r in report["ops"] if r["seconds"] > 6.0}
    assert not slow, f"ops over 6 s: {slow}"
