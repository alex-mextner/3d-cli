"""A crashed or untrustworthy geometry worker must never publish a final STEP."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any
import pytest
from commands import solid
from errors import ThreeDError

@pytest.mark.parametrize("failure", ["timeout", "missing-report", "nonzero", "wrong-hash", "missing-step"])
def test_worker_failure_leaves_no_final_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, capsys: pytest.CaptureFixture[str]) -> None:
    source, output = tmp_path / "input.stl", tmp_path / "final.step"
    source.write_bytes(b"fixture handled by the fake worker")
    def fake_worker(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        request = json.loads(argv[3])
        generated = Path(request["output"])
        if failure != "missing-step":
            generated.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 120)
        report = {"status": "FAILED" if failure == "nonzero" else "CONVERTED", "error": "worker failed", "representation": "faceted-brep", "output": str(generated), "volume_mm3": 1.0, "solid_count": 1, "extents_mm": [1.0, 1.0, 1.0], "step_roundtrip": {"valid": True, "solid_count": 1, "volume_mm3": 1.0, "extents_mm": [1.0, 1.0, 1.0]}, "output_sha256": "0" * 64 if failure == "wrong-hash" else hashlib.sha256(b"ISO-10303-21;\nEND-ISO-10303-21;").hexdigest()}
        if failure != "missing-report":
            Path(argv[4]).write_text(json.dumps(report), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 1 if failure == "nonzero" else 0, b"", b"")
    monkeypatch.setattr(subprocess, "run", fake_worker)
    try:
        code = solid.run([str(source), "--units", "mm", "-o", str(output), "--json"])
    except ThreeDError:
        code = 1
    assert code != 0, "worker failure was reported as conversion success"
    assert not output.exists(), "an unacknowledged worker output escaped into the final path"
    error = json.loads(capsys.readouterr().out)["error"]
    if failure == "wrong-hash":
        assert "hash" in error
    elif failure == "missing-step":
        assert "artifact" in error


def test_geometry_nan_cannot_be_published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("OCP")
    import trimesh
    import solid_conversion
    source, output = tmp_path / "cube.stl", tmp_path / "invalid.step"
    trimesh.creation.box(extents=[2, 2, 2]).export(source)
    original = solid_conversion.read_step
    def invalid_metrics(path: Path) -> dict[str, Any]:
        report = original(path)
        report["volume_mm3"] = float("nan")
        return report
    monkeypatch.setattr(solid_conversion, "read_step", invalid_metrics)
    with pytest.raises(ThreeDError):
        solid_conversion.convert_stl(source, output, units="mm")
    assert not output.exists()


def test_destination_appearing_during_conversion_is_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, output = tmp_path / "input.stl", tmp_path / "final.step"
    source.write_bytes(b"source fixture")
    def racing_worker(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        staged = Path(json.loads(argv[3])["output"])
        staged.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;")
        metrics = {"valid": True, "solid_count": 1, "volume_mm3": 1.0, "extents_mm": [1.0, 1.0, 1.0]}
        report = {"status": "CONVERTED", "representation": "faceted-brep", "solid_count": 1, "volume_mm3": 1.0, "extents_mm": [1.0, 1.0, 1.0], "step_roundtrip": metrics, "output_sha256": hashlib.sha256(staged.read_bytes()).hexdigest()}
        Path(argv[4]).write_text(json.dumps(report), encoding="utf-8")
        output.write_bytes(b"another writer's work")
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    monkeypatch.setattr(subprocess, "run", racing_worker)
    assert solid.run([str(source), "--units", "mm", "-o", str(output), "--json"]) != 0
    assert output.read_bytes() == b"another writer's work"
