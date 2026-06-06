from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from .workflow_helper import require_python_module, run_cli, run_shell

trimesh: Any | None = None


def _trimesh() -> Any:
    global trimesh
    if trimesh is None:
        trimesh = pytest.importorskip("trimesh")
    return trimesh


def _write_proxy_pair(tmp_path: Path) -> tuple[Path, Path]:
    tm = _trimesh()
    cad = tmp_path / "cad.stl"
    proxy = tmp_path / "proxy.stl"
    cad_mesh = tm.creation.box(extents=(1.0, 3.0, 0.35))
    proxy_mesh = tm.creation.box(extents=(1.0, 3.0, 0.35))
    proxy_mesh.apply_scale(1.2)
    proxy_mesh.apply_translation((2.0, -1.0, 0.5))
    cad_mesh.export(cad)
    proxy_mesh.export(proxy)
    return cad, proxy


def _write_bad_proxy_pair(tmp_path: Path) -> tuple[Path, Path]:
    tm = _trimesh()
    cad = tmp_path / "bad-cad.stl"
    proxy = tmp_path / "bad-proxy.stl"
    tm.creation.box(extents=(1.0, 3.0, 0.35)).export(cad)
    tm.creation.icosphere(subdivisions=2, radius=1.0).export(proxy)
    return cad, proxy


def test_auth_status_json_is_scriptable(tmp_path: Path) -> None:
    """A script checks whether Hugging Face auth is configured before using ZeroGPU."""
    result = run_cli(tmp_path, "auth", "hf", "status", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "missing"
    assert "Hugging Face token" in payload["detail"]


def test_proxy_align_cli_writes_quality_gate_for_shell_chain(tmp_path: Path) -> None:
    """A user aligns a generated proxy mesh, then inspects the quality gate JSON."""
    require_python_module("numpy")
    require_python_module("scipy")
    require_python_module("trimesh")
    cad, proxy = _write_proxy_pair(tmp_path)
    out = tmp_path / "match" / "proxy"

    result = run_cli(
        tmp_path,
        "proxy-align",
        str(cad),
        str(proxy),
        "--out",
        str(out),
        "--samples",
        "700",
        "--yaw-step",
        "90",
        "--json",
        timeout=180,
    )

    assert result.returncode == 0, result.stderr
    result_path = Path(result.stdout.strip())
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["quality_gate"]["status"] in {"ok", "warning"}
    assert payload["quality_gate"]["projection_edge_f1@3_min"] > 0.55
    assert Path(payload["artifacts"]["proof"]).is_file()


def test_proxy_align_quality_gate_is_readable_from_pipe(tmp_path: Path) -> None:
    """A shell pipeline reads the result path from --json and extracts proxy status."""
    require_python_module("numpy")
    require_python_module("scipy")
    require_python_module("trimesh")
    cad, proxy = _write_proxy_pair(tmp_path)
    out = tmp_path / "pipe-proxy"
    script = (
        f'"$PYTHON" "$THREED" proxy-align "{cad}" "{proxy}" --out "{out}" '
        "--samples 700 --yaw-step 90 --json | xargs -I{} cat {} | "
        "\"$PYTHON\" -c 'import json,sys; print(json.load(sys.stdin)[\"quality_gate\"][\"status\"])'"
    )

    result = run_shell(script, tmp_path, timeout=180)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() in {"ok", "warning"}


def test_proxy_align_rejects_bad_generated_mesh_through_cli(tmp_path: Path) -> None:
    """A bad image-to-3D proxy is rejected before it can seed fit-camera."""
    require_python_module("numpy")
    require_python_module("scipy")
    require_python_module("trimesh")
    cad, proxy = _write_bad_proxy_pair(tmp_path)
    out = tmp_path / "bad-proxy-out"

    result = run_cli(
        tmp_path,
        "proxy-align",
        str(cad),
        str(proxy),
        "--out",
        str(out),
        "--samples",
        "700",
        "--json",
        timeout=180,
    )

    assert result.returncode == 1, result.stderr
    payload = json.loads(Path(result.stdout.strip()).read_text(encoding="utf-8"))
    assert payload["quality_gate"]["status"] == "reject"
