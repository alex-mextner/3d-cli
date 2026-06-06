from __future__ import annotations

import builtins
import json
import math
from pathlib import Path
from typing import Any

import pytest

np = pytest.importorskip("numpy")
trimesh = pytest.importorskip("trimesh")

from proxy_align import (  # noqa: E402
    _json_float,
    _mask_rgb,
    _projection_masks,
    _quality_gate,
    _score_by_name,
    _svg_escape,
    _svg_mask_rects,
    _triangle_mask,
    evaluate_candidate,
    load_cloud,
    main,
    write_proof,
    write_svg_proof,
)


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


def test_mask_rgb_marks_overlap_and_one_sided_regions() -> None:
    cad_mask = np.asarray([[True, True], [False, False]])
    proxy_mask = np.asarray([[True, False], [True, False]])

    rgb = _mask_rgb(cad_mask, proxy_mask)

    assert tuple(rgb[0, 0]) == (245, 245, 245)
    assert tuple(rgb[0, 1]) == (220, 50, 50)
    assert tuple(rgb[1, 0]) == (20, 170, 230)
    assert tuple(rgb[1, 1]) == (18, 22, 28)


def test_svg_mask_rects_run_length_encodes_colored_rows() -> None:
    cad_mask = np.asarray([[True, True, False], [False, False, False]])
    proxy_mask = np.asarray([[True, False, False], [False, True, True]])

    rects = _svg_mask_rects(cad_mask, proxy_mask, offset_x=10, offset_y=20, scale=2)

    assert '<rect x="10" y="20" width="2" height="2" fill="rgb(245,245,245)" />' in rects
    assert '<rect x="12" y="20" width="2" height="2" fill="rgb(220,50,50)" />' in rects
    assert '<rect x="12" y="22" width="4" height="2" fill="rgb(20,170,230)" />' in rects


def test_svg_mask_rects_omits_all_background_rows() -> None:
    cad_mask = np.zeros((2, 2), dtype=bool)
    proxy_mask = np.zeros((2, 2), dtype=bool)

    assert _svg_mask_rects(cad_mask, proxy_mask, offset_x=0, offset_y=0, scale=1) == ""


def test_svg_escape_handles_xml_reserved_characters() -> None:
    assert _svg_escape('a&b<c>d"e') == "a&amp;b&lt;c&gt;d&quot;e"


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


def test_projection_masks_share_shape_and_overlap_for_aligned_boxes(tmp_path: Path) -> None:
    cad_path = tmp_path / "cad.stl"
    proxy_path = tmp_path / "proxy.stl"
    _write_box(cad_path, (1.0, 2.0, 0.6))
    _write_box(proxy_path, (1.0, 2.0, 0.6))
    cad = load_cloud(cad_path, samples=500, seed=11)
    proxy = load_cloud(proxy_path, samples=500, seed=29)
    candidate = evaluate_candidate(cad, proxy, yaw=0.0, pitch=0.0, roll=0.0, icp_steps=5)

    cad_mask, proxy_mask = _projection_masks(cad, proxy, candidate, (0, 1), size=64)

    assert cad_mask.shape == (64, 64)
    assert proxy_mask.shape == (64, 64)
    assert int(cad_mask.sum()) > 0
    assert int(proxy_mask.sum()) > 0
    assert float((cad_mask & proxy_mask).sum() / cad_mask.sum()) > 0.90


def test_svg_proof_fallback_uses_silhouette_masks_not_points(tmp_path: Path) -> None:
    cad_path = tmp_path / "cad.stl"
    proxy_path = tmp_path / "proxy.stl"
    _write_box(cad_path, (1.0, 2.0, 0.6))
    mesh = trimesh.creation.box(extents=(1.0, 2.0, 0.6))
    mesh.apply_translation((1.0, -0.5, 0.2))
    mesh.export(proxy_path)
    cad = load_cloud(cad_path, samples=500, seed=11)
    proxy = load_cloud(proxy_path, samples=500, seed=29)
    candidate = evaluate_candidate(cad, proxy, yaw=0.0, pitch=0.0, roll=0.0, icp_steps=5)
    gate = _quality_gate(cad, proxy, [candidate])

    proof = write_svg_proof(cad, proxy, candidate, gate, tmp_path)
    text = proof.read_text(encoding="utf-8")

    assert "white=overlap red=CAD only cyan=proxy only" in text
    assert "no convex hull or point cloud proof" in text
    assert "<circle" not in text
    assert "rgb(245,245,245)" in text


def test_write_proof_falls_back_to_svg_when_pillow_import_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cad_path = tmp_path / "cad.stl"
    proxy_path = tmp_path / "proxy.stl"
    _write_box(cad_path, (1.0, 2.0, 0.6))
    _write_box(proxy_path, (1.0, 2.0, 0.6))
    cad = load_cloud(cad_path, samples=500, seed=11)
    proxy = load_cloud(proxy_path, samples=500, seed=29)
    candidate = evaluate_candidate(cad, proxy, yaw=0.0, pitch=0.0, roll=0.0, icp_steps=5)
    gate = _quality_gate(cad, proxy, [candidate])
    original_import = builtins.__import__

    def blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("PIL"):
            raise ImportError("blocked PIL for fallback test")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)

    proof = write_proof(cad, proxy, candidate, gate, tmp_path)
    text = proof.read_text(encoding="utf-8")

    assert proof.name == "alignment_proof.svg"
    assert "white=overlap red=CAD only cyan=proxy only" in text
    assert "<circle" not in text


def test_png_proof_uses_silhouette_mask_colors_when_pillow_is_available(tmp_path: Path) -> None:
    image_module = pytest.importorskip("PIL.Image")
    cad_path = tmp_path / "cad.stl"
    proxy_path = tmp_path / "proxy.stl"
    _write_box(cad_path, (1.0, 2.0, 0.6))
    mesh = trimesh.creation.box(extents=(1.0, 2.0, 0.6))
    mesh.apply_translation((1.0, -0.5, 0.2))
    mesh.export(proxy_path)
    cad = load_cloud(cad_path, samples=500, seed=11)
    proxy = load_cloud(proxy_path, samples=500, seed=29)
    candidate = evaluate_candidate(cad, proxy, yaw=0.0, pitch=0.0, roll=0.0, icp_steps=5)
    gate = _quality_gate(cad, proxy, [candidate])

    proof = write_proof(cad, proxy, candidate, gate, tmp_path)
    with image_module.open(proof) as image:
        assert image.size == (1180, 760)
        colors = {color for _count, color in image.convert("RGB").getcolors(maxcolors=2_000_000)}

    assert (245, 245, 245) in colors
    assert (220, 50, 50) in colors
    assert (20, 170, 230) in colors
    assert (18, 22, 28) in colors


def test_score_by_name_raises_clear_error_for_missing_projection_score(tmp_path: Path) -> None:
    cad_path = tmp_path / "cad.stl"
    proxy_path = tmp_path / "proxy.stl"
    _write_box(cad_path, (1.0, 2.0, 0.6))
    _write_box(proxy_path, (1.0, 2.0, 0.6))
    cad = load_cloud(cad_path, samples=500, seed=11)
    proxy = load_cloud(proxy_path, samples=500, seed=29)
    candidate = evaluate_candidate(cad, proxy, yaw=0.0, pitch=0.0, roll=0.0, icp_steps=5)
    gate = _quality_gate(cad, proxy, [candidate])

    assert _score_by_name(gate, "XY").name == "XY"
    with pytest.raises(ValueError, match="missing FRONT"):
        _score_by_name(gate, "FRONT")


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
