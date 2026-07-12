"""Unit tests for commands.metrics — usage metrics + geometry/perceptual batteries."""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from typing import Any

import pytest
from commands.metrics import run
from errors import UsageError

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_THREED = os.path.join(_REPO, "bin", "3d")


def _installed(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _have_magick() -> bool:
    return bool(shutil.which("magick") or shutil.which("convert"))


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["REPO_ROOT"] = _REPO
    return subprocess.run(
        [sys.executable, _THREED, "metrics", *args],
        capture_output=True, text=True, timeout=300, env=env,
    )


def test_metrics_no_args() -> None:
    assert run([]) == 1


def test_metrics_help() -> None:
    assert run(["--help"]) == 0


def test_metrics_list(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("metrics.list_metric_files", lambda: [{"command": "render", "records": 5, "latest": "2026-01-01", "path": "/tmp/metrics.jsonl"}])
    assert run(["list"]) == 0


def test_metrics_show(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("metrics.read_records", lambda command=None, limit=None: [{"cmd": "render"}])
    assert run(["show"]) == 0


def test_metrics_show_limit() -> None:
    assert run(["show", "--limit", "5"]) == 0


def test_metrics_show_bad_limit() -> None:
    with pytest.raises(UsageError):
        run(["show", "--limit", "abc"])


def test_metrics_unknown_option() -> None:
    with pytest.raises(UsageError):
        run(["show", "--bogus"])


def test_metrics_unknown_subcommand_lists_new_battery_names() -> None:
    with pytest.raises(UsageError) as exc:
        run(["frobnicate"])
    remediation = " ".join(exc.value.remediation)
    assert "geometry" in remediation
    assert "perceptual" in remediation


def test_metrics_help_mentions_batteries() -> None:
    from commands.metrics import USAGE

    assert "geometry A B" in USAGE
    assert "perceptual A B" in USAGE
    assert "F-score" in USAGE


def test_geometry_routes_to_tool(monkeypatch: Any) -> None:
    seen: dict[str, Any] = {}

    def _fake(deps: str, script: str, args: list[str]) -> int:
        seen["deps"], seen["script"], seen["args"] = deps, script, args
        return 0

    monkeypatch.setattr("cli.pyrun.run_tool", _fake)
    assert run(["geometry", "a.stl", "b.stl", "--json"]) == 0
    assert seen["script"] == "geometry/mesh_metrics.py"
    assert seen["args"] == ["a.stl", "b.stl", "--json"]
    assert "trimesh" in seen["deps"]


def test_perceptual_routes_to_tool(monkeypatch: Any) -> None:
    seen: dict[str, Any] = {}

    def _fake(deps: str, script: str, args: list[str]) -> int:
        seen["script"], seen["args"] = script, args
        return 0

    monkeypatch.setattr("cli.pyrun.run_tool", _fake)
    assert run(["perceptual", "a.png", "b.png"]) == 0
    assert seen["script"] == "perceptual_metrics.py"
    assert seen["args"] == ["a.png", "b.png"]


# --------------------------------------------------------------------------- #
# e2e through bin/3d (skip when the heavy runtime / ImageMagick is unavailable).
# --------------------------------------------------------------------------- #
def test_e2e_geometry_battery_identical_mesh(tmp_path: Any) -> None:
    trimesh = pytest.importorskip("trimesh")
    if not (_installed("scipy") and _installed("numpy")):
        pytest.skip("scipy/numpy runtime unavailable")
    mesh = str(tmp_path / "box.stl")
    trimesh.creation.box((10.0, 10.0, 10.0)).export(mesh)
    proc = _run_cli(["geometry", mesh, mesh, "--samples", "3000", "--voxel-res", "20", "--no-store"])
    assert proc.returncode == 0, proc.stderr
    assert "F_SCORE=1.0000" in proc.stdout
    assert "VOLUMETRIC_IOU=1.0000" in proc.stdout
    assert "CHAMFER_L1=0.000000" in proc.stdout


def test_e2e_perceptual_battery_reports_psnr_and_degrades(tmp_path: Any) -> None:
    if not _have_magick():
        pytest.skip("ImageMagick unavailable")
    magick = shutil.which("magick") or shutil.which("convert")
    assert magick is not None
    img = str(tmp_path / "red.png")
    subprocess.run([magick, "-size", "16x16", "xc:red", img], check=True)
    proc = _run_cli(["perceptual", img, img, "--no-store"])
    assert proc.returncode == 0, proc.stderr
    assert "PSNR=100.0000" in proc.stdout  # identical image -> capped PSNR
    assert "PSNR_SENSE=higher_better" in proc.stdout
    if not _installed("lpips"):
        assert "LPIPS=unavailable" in proc.stdout
        assert "pip install lpips" in proc.stdout
