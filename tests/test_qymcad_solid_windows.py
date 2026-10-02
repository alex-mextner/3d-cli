from __future__ import annotations
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
requires_solid = pytest.mark.skipif(importlib.util.find_spec("OCP") is None, reason="optional solid extra is not installed")

def cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(ROOT / "bin" / "3d"), *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=150)

@pytest.fixture
def cube(tmp_path: Path) -> Path:
    import trimesh
    path = tmp_path / "a cube.stl"
    trimesh.creation.box(extents=[40, 30, 6]).export(path)
    return path

@requires_solid
def test_cli_cube_to_step(cube: Path, tmp_path: Path) -> None:
    output = tmp_path / "result.step"
    result = cli("solid", str(cube), "--units", "mm", "-o", str(output), "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["representation"] == "faceted-brep"
    assert report["solid_count"] == 1
    assert report["volume_mm3"] == pytest.approx(7200, rel=1e-6)
    assert report["extents_mm"] == pytest.approx([40, 30, 6], abs=1e-5)
    assert report["step_roundtrip"]["valid"] is True
    assert output.read_text(errors="replace").startswith("ISO-10303-21;")

def test_native_worktree_doctor() -> None:
    if os.name != "nt":
        pytest.skip("native Windows regression")
    result = cli("worktree", "doctor", str(ROOT), "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["ok"] is True

def test_qymcad_doctor_is_honest_json() -> None:
    result = cli("qymcad", "doctor", "--json")
    assert result.returncode in (0, 127), result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["capabilities"]["headless_editing"] is False
    assert report["capabilities"]["cam"] is False
    assert report["cloud_required"] is False

def test_units_required(cube: Path, tmp_path: Path) -> None:
    result = cli("solid", str(cube), "-o", str(tmp_path / "no.step"), "--json")
    assert result.returncode == 2
    assert "units" in (result.stdout + result.stderr).lower()

@pytest.mark.parametrize("tol", ["nan", "inf", "-1", "0"])
def test_bad_tolerance_rejected(cube: Path, tmp_path: Path, tol: str) -> None:
    result = cli("solid", str(cube), "--units", "mm", "--tolerance", tol, "-o", str(tmp_path / "bad.step"), "--json")
    assert result.returncode == 2
    assert not (tmp_path / "bad.step").exists()

def test_never_overwrites_existing_output(cube: Path, tmp_path: Path) -> None:
    output = tmp_path / "existing.step"
    output.write_bytes(b"KEEP ORIGINAL")
    result = cli("solid", str(cube), "--units", "mm", "-o", str(output), "--json")
    assert result.returncode != 0
    assert output.read_bytes() == b"KEEP ORIGINAL"

@requires_solid
def test_open_mesh_rejected(tmp_path: Path) -> None:
    import trimesh
    mesh = trimesh.creation.box()
    mesh.update_faces(list(range(11)))
    source = tmp_path / "open.stl"
    mesh.export(source)
    result = cli("solid", str(source), "--units", "mm", "-o", str(tmp_path / "open.step"), "--json")
    assert result.returncode == 1, result.stdout + result.stderr
    assert not (tmp_path / "open.step").exists()

@requires_solid
def test_two_components_rejected(tmp_path: Path) -> None:
    import trimesh
    a = trimesh.creation.box()
    b = trimesh.creation.box()
    b.apply_translation([3, 0, 0])
    source = tmp_path / "two.stl"
    trimesh.util.concatenate([a, b]).export(source)
    result = cli("solid", str(source), "--units", "mm", "-o", str(tmp_path / "two.step"), "--json")
    assert result.returncode == 1, result.stdout + result.stderr
    assert not (tmp_path / "two.step").exists()

@requires_solid
def test_centimetres_are_scaled(cube: Path, tmp_path: Path) -> None:
    result = cli("solid", str(cube), "--units", "cm", "-o", str(tmp_path / "scaled.step"), "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["extents_mm"] == pytest.approx([400, 300, 60], abs=1e-4)
    assert report["volume_mm3"] == pytest.approx(7_200_000, rel=1e-6)


@requires_solid
def test_source_hash_describes_loaded_bytes(cube: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib
    import trimesh
    from solid_conversion import convert_stl
    original = cube.read_bytes()
    load_mesh = trimesh.load_mesh
    def mutate_after_load(*args, **kwargs):
        result = load_mesh(*args, **kwargs)
        cube.write_bytes(b"externally changed after loading")
        return result
    monkeypatch.setattr(trimesh, "load_mesh", mutate_after_load)
    report = convert_stl(cube, tmp_path / "snapshot.step", units="mm")
    assert report["source_sha256"] == hashlib.sha256(original).hexdigest()


@requires_solid
def test_through_hole_is_preserved(tmp_path: Path) -> None:
    import trimesh
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.STEPControl import STEPControl_Reader
    from OCP.TopAbs import TopAbs_OUT, TopAbs_IN
    from OCP.gp import gp_Pnt
    mesh = trimesh.creation.annulus(r_min=3, r_max=8, height=6, sections=24)
    source, output = tmp_path / "ring.stl", tmp_path / "ring.step"
    mesh.export(source)
    result = cli("solid", str(source), "--units", "mm", "-o", str(output), "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["volume_mm3"] == pytest.approx(mesh.volume, rel=1e-6)
    reader = STEPControl_Reader()
    reader.ReadFile(str(output))
    reader.TransferRoots()
    shape = reader.OneShape()
    assert BRepClass3d_SolidClassifier(shape, gp_Pnt(0, 0, 0), 1e-7).State() == TopAbs_OUT
    assert BRepClass3d_SolidClassifier(shape, gp_Pnt(5, 0, 0), 1e-7).State() == TopAbs_IN


@pytest.mark.parametrize("damage", ["nonfinite", "degenerate", "duplicate"])
@requires_solid
def test_damaged_triangles_are_not_repaired(cube: Path, tmp_path: Path, damage: str) -> None:
    import struct
    data = bytearray(cube.read_bytes())
    if damage == "nonfinite":
        struct.pack_into("<f", data, 96, float("nan"))
    elif damage == "degenerate":
        data[108:120] = data[96:108]
    else:
        data[134:184] = data[84:134]
    cube.write_bytes(data)
    output = tmp_path / "damaged.step"
    result = cli("solid", str(cube), "--units", "mm", "-o", str(output), "--json")
    assert result.returncode == 1, result.stdout + result.stderr
    assert not output.exists()


@requires_solid
def test_face_budget(cube: Path, tmp_path: Path) -> None:
    result = cli("solid", str(cube), "--units", "mm", "--max-faces", "5", "-o", str(tmp_path / "budget.step"), "--json")
    assert result.returncode == 1
    assert "budget" in result.stdout


def test_platform_venv_paths(tmp_path: Path) -> None:
    from cli.venv_paths import executable
    assert executable(tmp_path, "python", platform="win32") == tmp_path / ".venv" / "Scripts" / "python.exe"
    assert executable(tmp_path, "python", platform="linux") == tmp_path / ".venv" / "bin" / "python"
    assert executable(tmp_path, "ruff", platform="darwin") == tmp_path / ".venv" / "bin" / "ruff"


def test_directory_is_not_a_dev_executable(tmp_path: Path) -> None:
    from cli.venv_paths import executable
    from commands.worktree import _dev_tool_status
    executable(tmp_path, "ruff").mkdir(parents=True)
    assert _dev_tool_status(tmp_path)["ruff"] is False


def test_runtime_uses_native_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.pyrun import tool_argv
    monkeypatch.setenv("PY3D_NO_UV", "1")
    args = tool_argv("", "solid_conversion.py", ["--help"])
    assert Path(args[0]).resolve() == Path(sys.executable).resolve()


def test_qymcad_dry_run_does_not_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from qymcad_tools import launch
    exe = tmp_path / "path with spaces" / "qymcad.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"test executable")
    exe.chmod(0o700)
    def forbidden(*args, **kwargs):
        pytest.fail("dry-run launched a process")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    result = launch(str(exe), dry_run=True)
    assert result["status"] == "PLANNED"
    assert result["argv"] == [str(exe.resolve())]


def test_qymcad_explicit_invalid_path(tmp_path: Path) -> None:
    result = cli("qymcad", "launch", "--executable", str(tmp_path / "missing.exe"), "--json")
    assert result.returncode == 2


@pytest.mark.parametrize("command", ["solid", "qymcad"])
def test_local_cad_skips_openscad_downloads(command: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace
    from cli import dispatch
    from cli.registry import Registry, discover
    original = discover().resolve(command)
    assert original is not None
    registry = Registry()
    registry.add(replace(original, run=lambda args: 0))
    monkeypatch.setattr(dispatch, "discover", lambda: registry)
    def forbidden() -> None:
        pytest.fail("local CAD command attempted unrelated OpenSCAD library bootstrap")
    monkeypatch.setattr(dispatch, "maybe_bootstrap", forbidden)
    assert dispatch.main([command]) == 0


@requires_solid
def test_merge_coplanar_reduces_cube_to_six_faces(cube: Path, tmp_path: Path) -> None:
    result = cli("solid", str(cube), "--units", "mm", "--merge-coplanar", "-o", str(tmp_path / "six-faces.step"), "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["coplanar_merge"] == {"requested": True, "faces_before": 12, "faces_after": 6}
    assert report["step_roundtrip"]["face_count"] == 6
    assert report["volume_mm3"] == pytest.approx(7200, rel=1e-6)
    assert report["representation"] == "faceted-brep"


@requires_solid
def test_merge_keeps_polygonal_hole_walls(tmp_path: Path) -> None:
    import trimesh
    mesh = trimesh.creation.annulus(r_min=3, r_max=8, height=6, sections=24)
    source = tmp_path / "polygonal-ring.stl"
    mesh.export(source)
    result = cli("solid", str(source), "--units", "mm", "--merge-coplanar", "-o", str(tmp_path / "polygonal-ring.step"), "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["step_roundtrip"]["face_count"] == 50
    assert report["volume_mm3"] == pytest.approx(mesh.volume, rel=1e-6)
    assert "analytic surface recovery" in report["not_verified"]
