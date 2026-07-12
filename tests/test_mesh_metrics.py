"""Known-answer unit tests for the geometry metric battery (lib/geometry/mesh_metrics.py).

Point-set functions (Chamfer / F-score / Hausdorff / normal-consistency) get EXACT
synthetic inputs: identical sets score perfect, a rigid translation shifts each metric
by a value we can compute by hand. Mesh-level tests (volumetric IoU + the file battery)
need trimesh and skip gracefully when it is absent, exactly like test_cli_smoke.
"""
from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from geometry import mesh_metrics as mm  # noqa: E402


_TETRA = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [0.0, 10.0, 0.0], [0.0, 0.0, 10.0]])


def test_chamfer_identical_is_zero() -> None:
    ch = mm.chamfer_distance(_TETRA, _TETRA)
    assert ch["l1"] == 0.0
    assert ch["l2"] == 0.0


def test_fscore_identical_is_one() -> None:
    assert mm.f_score(_TETRA, _TETRA, tau=1.0)["f_score"] == 1.0


def test_hausdorff_identical_is_zero() -> None:
    assert mm.hausdorff_distance(_TETRA, _TETRA)["symmetric"] == 0.0


def test_chamfer_translation_is_known() -> None:
    # Each point's nearest neighbour is its own copy shifted by 2 along X.
    shifted = _TETRA + np.array([2.0, 0.0, 0.0])
    ch = mm.chamfer_distance(_TETRA, shifted)
    assert ch["l1"] == pytest.approx(4.0)  # mean(2) a->b + mean(2) b->a
    assert ch["l2"] == pytest.approx(8.0)  # mean(4) + mean(4)


def test_hausdorff_translation_is_offset() -> None:
    shifted = _TETRA + np.array([2.0, 0.0, 0.0])
    assert mm.hausdorff_distance(_TETRA, shifted)["symmetric"] == pytest.approx(2.0)


def test_fscore_threshold_brackets_the_offset() -> None:
    shifted = _TETRA + np.array([2.0, 0.0, 0.0])
    assert mm.f_score(_TETRA, shifted, tau=3.0)["f_score"] == 1.0  # 2 < 3 -> all hit
    assert mm.f_score(_TETRA, shifted, tau=1.0)["f_score"] == 0.0  # 2 > 1 -> none hit


def test_normal_consistency_matching_and_perpendicular() -> None:
    n_same = np.tile([1.0, 0.0, 0.0], (4, 1))
    n_perp = np.tile([0.0, 1.0, 0.0], (4, 1))
    assert mm.normal_consistency(_TETRA, n_same, _TETRA, n_same) == pytest.approx(1.0)
    assert mm.normal_consistency(_TETRA, n_same, _TETRA, n_perp) == pytest.approx(0.0)


def test_normal_consistency_is_orientation_agnostic() -> None:
    n_pos = np.tile([1.0, 0.0, 0.0], (4, 1))
    n_neg = np.tile([-1.0, 0.0, 0.0], (4, 1))
    # abs() convention: flipped normals still count as consistent.
    assert mm.normal_consistency(_TETRA, n_pos, _TETRA, n_neg) == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Mesh-level (trimesh required).
# --------------------------------------------------------------------------- #
def _write_box(path: str, size: tuple[float, float, float], shift: tuple[float, float, float]) -> None:
    trimesh = pytest.importorskip("trimesh")
    box = trimesh.creation.box(size)
    box.apply_translation(shift)
    box.export(path)


def test_volumetric_iou_identical_box_is_one(tmp_path) -> None:  # type: ignore[no-untyped-def]
    trimesh = pytest.importorskip("trimesh")
    box = trimesh.creation.box((10.0, 10.0, 10.0))
    vol = mm.volumetric_iou(box, box, voxel_res=24)
    assert vol["value"] == pytest.approx(1.0)
    assert vol["sense"] == "higher_better"
    assert vol["watertight_a"] and vol["watertight_b"]


def test_volumetric_iou_half_overlap_is_one_third(tmp_path) -> None:  # type: ignore[no-untyped-def]
    trimesh = pytest.importorskip("trimesh")
    a = trimesh.creation.box((10.0, 10.0, 10.0))
    b = trimesh.creation.box((10.0, 10.0, 10.0))
    b.apply_translation((5.0, 0.0, 0.0))  # overlap 500, union 1500 -> 1/3
    assert mm.volumetric_iou(a, b, voxel_res=40)["value"] == pytest.approx(1.0 / 3.0, abs=0.03)


def test_geometry_battery_identical_file_is_perfect(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pytest.importorskip("trimesh")
    mesh = str(tmp_path / "a.stl")
    _write_box(mesh, (10.0, 10.0, 10.0), (0.0, 0.0, 0.0))
    report = mm.geometry_battery(mesh, mesh, n_samples=4000, voxel_res=24)
    assert report["f_score"]["f_score"] == 1.0
    assert report["f_score"]["primary"] is True
    assert report["chamfer"]["l1"] == 0.0
    assert report["normal_consistency"]["value"] == pytest.approx(1.0)
    assert report["volumetric_iou"]["value"] == pytest.approx(1.0)
    # Convention is recorded so the longitudinal store cannot silently drift.
    conv = report["convention"]
    assert conv["tau_frac"] == 0.01
    assert conv["n_samples"] == 4000
    assert conv["alignment"] == "none"
    assert conv["chamfer_reduction"] == "mean"


def test_geometry_battery_translated_file_degrades(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pytest.importorskip("trimesh")
    a = str(tmp_path / "a.stl")
    b = str(tmp_path / "b.stl")
    _write_box(a, (10.0, 10.0, 10.0), (0.0, 0.0, 0.0))
    _write_box(b, (10.0, 10.0, 10.0), (5.0, 0.0, 0.0))
    report = mm.geometry_battery(a, b, n_samples=4000, voxel_res=24)
    assert report["f_score"]["f_score"] < 0.5
    assert report["volumetric_iou"]["value"] == pytest.approx(1.0 / 3.0, abs=0.05)
    assert report["chamfer"]["l1"] > 0.0
