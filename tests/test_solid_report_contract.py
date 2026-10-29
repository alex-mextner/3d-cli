"""Reviewed edge cases of the staged-conversion contract: report shape, staging errors, Windows venv."""

from __future__ import annotations
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any
import pytest
from errors import ThreeDError


def _checked_report(path: Path) -> dict[str, Any]:
    stats = {
        "valid": True,
        "solid_count": 1,
        "volume_mm3": 1.0,
        "extents_mm": [1.0, 1.0, 1.0],
    }
    return {
        "status": "CONVERTED",
        "representation": "faceted-brep",
        "output": str(path),
        "solid_count": 1,
        "volume_mm3": 1.0,
        "extents_mm": [1.0, 1.0, 1.0],
        "step_roundtrip": stats,
        "output_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


@pytest.mark.parametrize("warnings", [None, "warning", {}, [1]])
def test_malformed_warnings_are_rejected_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warnings: Any
) -> None:
    from solid_job import run_conversion

    target = tmp_path / "final.step"

    def worker(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        staged = Path(json.loads(argv[3])["output"])
        staged.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;")
        report = _checked_report(staged)
        report["warnings"] = warnings
        Path(argv[4]).write_text(json.dumps(report), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", worker)
    with pytest.raises(ThreeDError, match="invalid warnings"):
        run_conversion({"output": str(target)})
    assert not target.exists()


def test_staging_directory_failure_is_a_structured_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from solid_job import run_conversion

    def denied(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("simulated read-only destination")

    monkeypatch.setattr(tempfile, "TemporaryDirectory", denied)
    with pytest.raises(ThreeDError, match="staging directory"):
        run_conversion({"output": str(tmp_path / "final.step")})


def test_resolve_python_finds_windows_venv_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli import env

    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    interpreter = scripts / "python.exe"
    interpreter.write_bytes(b"")
    interpreter.chmod(0o755)
    monkeypatch.setattr(env, "repo_root", lambda: str(tmp_path))
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert env.resolve_python() == str(interpreter)


@pytest.mark.parametrize("field", ["volume_mm3", "extents_mm"])
def test_oversized_integer_metrics_are_rejected_not_crashed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    from solid_job import run_conversion
    target = tmp_path / "final.step"

    def worker(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        staged = Path(json.loads(argv[3])["output"])
        staged.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;")
        report = _checked_report(staged)
        report[field] = 10 ** 400 if field == "volume_mm3" else [1, 1, 10 ** 400]
        Path(argv[4]).write_text(json.dumps(report), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", worker)
    with pytest.raises(ThreeDError):
        run_conversion({"output": str(target)})
    assert not target.exists()
