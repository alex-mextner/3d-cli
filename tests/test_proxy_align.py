from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

np = pytest.importorskip("numpy")
trimesh = pytest.importorskip("trimesh")

from proxy_align import _json_float, _triangle_mask, main  # noqa: E402


def _write_box(path: Path, extents: tuple[float, float, float]) -> None:
    mesh = trimesh.creation.box(extents=extents)
    mesh.export(path)


def _l_fixture() -> Any:
    vertical = trimesh.creation.box(extents=(0.6, 2.0, 0.4))
    vertical.apply_translation((-0.7, 0.0, 0.0))
    horizontal = trimesh.creation.box(extents=(2.0, 0.6, 0.4))
    horizontal.apply_translation((0.0, -0.7, 0.0))
    return trimesh.util.concatenate([vertical, horizontal])


def test_triangle_mask_uses_triangle_local_bounds() -> None:
    triangle = np.asarray([[190.0, 190.0], [210.0, 190.0], [190.0, 210.0]], dtype=np.float64)
    mask = _triangle_mask(triangle, 256)

    assert mask.any()
    assert not mask[:100, :100].any()


def test_json_float_sanitizes_non_finite_values() -> None:
    assert _json_float(math.inf) is None
    assert _json_float(math.nan) is None
    assert _json_float(1.25) == 1.25


def test_proxy_align_recovers_transformed_box(tmp_path: Path) -> None:
    cad = tmp_path / "cad.stl"
    proxy = tmp_path / "proxy.stl"
    _write_box(cad, (1.0, 2.0, 0.6))
    mesh = trimesh.creation.box(extents=(1.0, 2.0, 0.6))
    mesh.apply_transform(trimesh.transformations.rotation_matrix(np.deg2rad(90.0), (0, 0, 1)))
    mesh.apply_scale(1.35)
    mesh.apply_translation((4.0, -2.0, 1.0))
    mesh.export(proxy)

    out = tmp_path / "align"
    code = main(
        [
            "--cad",
            str(cad),
            "--proxy",
            str(proxy),
            "--out",
            str(out),
            "--samples",
            "600",
            "--yaw-step",
            "90",
            "--icp-steps",
            "6",
            "--json",
        ]
    )

    assert code == 0
    data = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert data["schema"] == "3d-cli.proxy-align.v1"
    assert data["best"]["error"]["chamfer_mean"] < 0.04
    assert data["quality_gate"]["status"] in {"ok", "warning"}
    assert data["quality_gate"]["projection_edge_f1@3_min"] > 0.55
    assert Path(data["artifacts"]["proof"]).is_file()

    transform = data["best"]["transform_proxy_to_cad_original"]
    matrix = np.asarray(transform["matrix_3x3"], dtype=np.float64)
    translation = np.asarray(transform["translation"], dtype=np.float64)
    cad_mesh = trimesh.load(cad, force="mesh")
    proxy_mesh = trimesh.load(proxy, force="mesh")
    transformed_vertices = np.asarray(proxy_mesh.vertices, dtype=np.float64) @ matrix + translation
    cad_vertices = np.asarray(cad_mesh.vertices, dtype=np.float64)
    distances = np.linalg.norm(transformed_vertices[:, None, :] - cad_vertices[None, :, :], axis=2).min(axis=1)
    assert float(distances.max()) < 0.08

    normalized = data["best"]["transform_proxy_to_cad_normalized"]
    normalized_matrix = np.asarray(normalized["matrix_3x3"], dtype=np.float64)
    normalized_translation = np.asarray(normalized["translation"], dtype=np.float64)
    proxy_center = np.asarray(data["proxy"]["bbox_center"], dtype=np.float64)
    proxy_diag = float(data["proxy"]["bbox_diagonal"])
    cad_center = np.asarray(data["cad"]["bbox_center"], dtype=np.float64)
    cad_diag = float(data["cad"]["bbox_diagonal"])
    proxy_norm = (np.asarray(proxy_mesh.vertices, dtype=np.float64) - proxy_center) / proxy_diag
    cad_norm = (cad_vertices - cad_center) / cad_diag
    transformed_norm = proxy_norm @ normalized_matrix + normalized_translation
    norm_distances = np.linalg.norm(transformed_norm[:, None, :] - cad_norm[None, :, :], axis=2).min(axis=1)
    assert float(norm_distances.max()) < 0.08


def test_proxy_align_penalizes_component_mismatch(tmp_path: Path) -> None:
    cad = tmp_path / "cad.stl"
    proxy = tmp_path / "proxy.stl"
    _write_box(cad, (1.0, 1.0, 1.0))
    a = trimesh.creation.box(extents=(1.0, 1.0, 1.0))
    b = trimesh.creation.box(extents=(0.2, 0.2, 0.2))
    b.apply_translation((2.0, 0.0, 0.0))
    combined = trimesh.util.concatenate([a, b])
    combined.export(proxy)

    out = tmp_path / "align"
    assert main(["--cad", str(cad), "--proxy", str(proxy), "--out", str(out), "--samples", "400"]) == 1
    data = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert data["proxy"]["components"] == 2
    assert data["best"]["error"]["topology_penalty"] > 0
    assert data["quality_gate"]["status"] in {"warning", "reject"}


def test_proxy_align_rejects_bad_generated_proxy_shape(tmp_path: Path) -> None:
    cad = tmp_path / "cad.stl"
    proxy = tmp_path / "proxy.stl"
    _write_box(cad, (1.0, 3.0, 0.35))
    mesh = trimesh.creation.icosphere(subdivisions=2, radius=1.0)
    mesh.export(proxy)

    out = tmp_path / "align"
    assert main(["--cad", str(cad), "--proxy", str(proxy), "--out", str(out), "--samples", "700"]) == 1
    data = json.loads((out / "result.json").read_text(encoding="utf-8"))

    assert data["quality_gate"]["status"] == "reject"
    assert data["quality_gate"]["reasons"]
    assert data["quality_gate"]["projection_edge_f1@3_min"] < 0.55


def test_proxy_align_rejects_filled_proxy_with_same_outer_bbox(tmp_path: Path) -> None:
    cad = tmp_path / "cad.stl"
    proxy = tmp_path / "proxy.stl"
    _l_fixture().export(cad)
    trimesh.creation.box(extents=(2.0, 2.0, 0.4)).export(proxy)

    out = tmp_path / "align"
    assert main(["--cad", str(cad), "--proxy", str(proxy), "--out", str(out), "--samples", "900"]) == 1
    data = json.loads((out / "result.json").read_text(encoding="utf-8"))

    assert data["quality_gate"]["status"] == "reject"
    assert data["quality_gate"]["projection_edge_f1@3_min"] < 0.55
