"""examples/mesh-fit/cat-fur.yaml: the Chekhov fur script as a spec, run on a synthetic stand-in (never the purchased model)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from geometry.meshfit import engine, meshmath, spec as spec_mod
from meshfit_fixtures import cube_sphere

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "mesh-fit" / "cat-fur.yaml"


def _figure(n: int = 50) -> tuple[np.ndarray, np.ndarray]:
    """The 70 x 90 x 90 mm ellipsoid the demo landmarks.json was drawn for (centre (0, 0, 45))."""
    V, F = cube_sphere(n, 1.0)
    return (V * np.array([35.0, 45.0, 45.0]) + np.array([0.0, 0.0, 45.0])).astype(
        np.float32
    ).astype(np.float64), F


def _spec() -> dict[str, Any]:
    spec = spec_mod.load_spec(str(EXAMPLE))
    assert (
        spec["ops"][0]["op"] == "remesh" and spec["ops"][0]["target_edge"] == 0.32
    )  # the Chekhov 0.32 mm remesh
    spec["ops"] = spec["ops"][
        1:
    ]  # the stand-in is already ~1.6 mm; 0.32 mm would be 25x the faces
    return spec


@pytest.fixture(scope="module")
def result() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    V, F = _figure()
    V2, F2, report = engine.run_spec(V, F, _spec(), self_intersections=False)
    return V, F, V2, F2, report


def test_example_spec_validates_and_uses_the_landmark_boxes() -> None:
    spec = spec_mod.load_spec(str(EXAMPLE))
    assert {"lm_head", "lm_eye_L", "lm_cable_notch", "lm_tail"} <= set(spec["regions"])
    assert (
        spec["regions"]["face_details"]["union"][0]["box"]
        == spec["regions"]["lm_eye_L"]["box"]
    )  # `box: lm_eye_L` was resolved
    assert spec["keepout"] == ["cable_zone"] and spec["ops"][1]["op"] == "fur"


def test_example_reproduces_a_comparable_relief_on_the_synthetic_figure(
    result: Any,
) -> None:
    V, F, V2, F2, report = result
    d = np.linalg.norm(V2 - V, axis=1)
    fur = report["ops"][0]
    assert report["status"] == "PASS" and report["watertight_after"]
    assert (
        0.25 <= fur["max_relief_mm"] <= 0.45 + 1e-9
    )  # Chekhov: 0.25 head / 0.35 body / 0.45 tail amplitude
    textured = (d > 0.05).mean()
    assert (
        0.2 < textured < 0.8
    )  # a large share of the surface carries relief, not all of it (masks work)
    assert 0.03 < d[d > 0].mean() < 0.4


def test_example_keeps_face_details_smooth_and_the_cable_notch_untouched(
    result: Any,
) -> None:
    V, _, V2, _, report = result
    d = np.linalg.norm(V2 - V, axis=1)
    lm = spec_mod.load_spec(str(EXAMPLE))["regions"]

    def inside(box: list[float], margin: float) -> np.ndarray:
        lo, hi = np.array(box[0::2]) - margin, np.array(box[1::2]) + margin
        return np.all((lo <= V) & (hi >= V), axis=1)

    for name in ("lm_eye_L", "lm_eye_R", "lm_nose", "lm_mouth_chin"):
        zone = inside(lm[name]["box"], 0.5)
        assert zone.sum() > 0 and d[zone].max() == 0.0, (
            name
        )  # Chekhov: weight 0 within 0.6 mm of these boxes
    notch = inside(lm["lm_cable_notch"]["box"], 0.0)
    assert notch.sum() > 0 and d[notch].max() == 0.0
    assert (
        report["keepout"]["max_displacement_mm"] == 0.0
        and report["keepout"]["locked_vertices"] > 0
    )
    # the rest of the body is clearly textured
    assert (
        d[(V[:, 2] > 20) & (np.abs(V[:, 1]) < 15) & (np.abs(V[:, 0]) > 20)].mean()
        > 0.03
    )


def test_example_is_overhang_safe(result: Any) -> None:
    V, F, V2, F2, _ = result
    thr = -np.sin(np.radians(45.0))
    nz0 = meshmath.face_normals(V, F)[:, 2]
    nz1 = meshmath.face_normals(V2, F2)[:, 2]
    on_bed = V[F][:, :, 2].max(axis=1) < 0.3
    assert not ((nz1 < thr) & (nz1 < nz0 - 1e-9) & ~on_bed).any()


def test_example_is_reproducible() -> None:
    V, F = _figure(30)
    a = engine.run_spec(V, F, _spec(), self_intersections=False)[2]["mesh_sha256"]
    b = engine.run_spec(V, F, _spec(), self_intersections=False)[2]["mesh_sha256"]
    assert a == b
