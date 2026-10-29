"""Regression evidence for independently reviewed solid-conversion failure modes."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any
import pytest
from errors import ThreeDError


def checked_report(path: Path) -> dict[str, Any]:
    stats = {"valid": True, "solid_count": 1, "volume_mm3": 1.0, "extents_mm": [1.0, 1.0, 1.0], "bounds_mm": [0.0, 0.0, 0.0, 1.0, 1.0, 1.0]}
    return {"status": "CONVERTED", "representation": "faceted-brep", "output": str(path), "solid_count": 1, "volume_mm3": 1.0, "extents_mm": [1.0, 1.0, 1.0], "step_roundtrip": stats, "output_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def test_cleanup_failure_does_not_reverse_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from solid_job import run_conversion
    target = tmp_path / "final.step"
    class FailsToClean(tempfile.TemporaryDirectory):
        def cleanup(self) -> None:
            super().cleanup()
            raise PermissionError("simulated scanner sharing violation")
    def successful_worker(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        staged = Path(json.loads(argv[3])["output"])
        staged.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;")
        Path(argv[4]).write_text(json.dumps(checked_report(staged)), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    monkeypatch.setattr(tempfile, "TemporaryDirectory", FailsToClean)
    monkeypatch.setattr(subprocess, "run", successful_worker)
    code, report = run_conversion({"output": str(target)})
    assert code == 0
    assert target.is_file()
    assert any("cleanup" in warning.lower() for warning in report["warnings"])


@pytest.mark.parametrize("field,value", [("solid_count", 2), ("volume_mm3", "not a number"), ("volume_mm3", -1)])
def test_parent_rejects_malformed_success_metrics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: Any) -> None:
    from solid_job import run_conversion
    target = tmp_path / "final.step"
    def wrong_worker(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        staged = Path(json.loads(argv[3])["output"])
        staged.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;")
        report = checked_report(staged)
        report[field] = value
        Path(argv[4]).write_text(json.dumps(report), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    monkeypatch.setattr(subprocess, "run", wrong_worker)
    with pytest.raises(ThreeDError):
        run_conversion({"output": str(target)})
    assert not target.exists()


@pytest.fixture
def plate(tmp_path: Path) -> Path:
    pytest.importorskip("OCP")
    import trimesh
    source = tmp_path / "plate.stl"
    trimesh.creation.box(extents=[40, 30, 0.1]).export(source)
    return source


def test_missing_premerge_face_is_rejected(plate: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import solid_conversion
    original = solid_conversion.inspect_shape
    def lost_face(shape: Any) -> dict[str, Any]:
        report = original(shape)
        report["face_count"] -= 1
        return report
    monkeypatch.setattr(solid_conversion, "inspect_shape", lost_face)
    with pytest.raises(ThreeDError, match="face|triangle"):
        solid_conversion.convert_stl(plate, tmp_path / "lost-face.step", units="mm")
    assert not (tmp_path / "lost-face.step").exists()


def test_large_linear_tolerance_does_not_allow_volume_loss(plate: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import solid_conversion
    original = solid_conversion.read_step
    def changed_volume(path: Path) -> dict[str, Any]:
        report = original(path)
        report["volume_mm3"] *= 0.98
        return report
    monkeypatch.setattr(solid_conversion, "read_step", changed_volume)
    with pytest.raises(ThreeDError, match="volume|dimensions|agreement"):
        solid_conversion.convert_stl(plate, tmp_path / "bad-volume.step", units="mm", tolerance=0.004)
    assert not (tmp_path / "bad-volume.step").exists()


def test_features_below_resolution_are_rejected(tmp_path: Path) -> None:
    pytest.importorskip("OCP")
    import trimesh
    from solid_conversion import convert_stl
    source = tmp_path / "underresolved.stl"
    trimesh.creation.box(extents=[40, 20, 0.000005]).export(source)
    with pytest.raises(ThreeDError, match="edge|tolerance|resolution"):
        convert_stl(source, tmp_path / "underresolved.step", units="mm", tolerance=1e-6)


@pytest.mark.parametrize("argv", [["solid", "missing.stl", "--units", "mm", "-o", "output.txt", "--json"], ["solid", "--json"], ["qymcad", "launch", "--executable", "does-not-exist.exe", "--json"]])
def test_json_mode_reports_parent_errors(argv: list[str]) -> None:
    import sys
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / "bin/3d"), *argv], cwd=root, capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert json.loads(result.stdout)["status"] == "FAILED"


def test_qymcad_reports_immediate_startup_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import qymcad_tools
    exe = tmp_path / "qymcad.exe"
    exe.write_bytes(b"fake executable")
    exe.chmod(0o700)
    class FailedChild:
        pid = 12345
        def wait(self, timeout: float) -> int:
            return 23
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FailedChild())
    with pytest.raises(ThreeDError, match="startup|exited"):
        qymcad_tools.launch(str(exe))
