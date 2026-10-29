"""Run geometry in a staging directory and publish only an acknowledged STEP."""

from __future__ import annotations

import hashlib
import json
import os
import math
from pathlib import Path
import subprocess
import sys
from typing import Any

from errors import ThreeDError, UsageError
from cli.venv_paths import executable
from solid_artifacts import workspace


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_report(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ThreeDError(
            "geometry worker did not produce a bounded report", command="solid"
        )
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        json.dumps(report, allow_nan=False)
    except (OSError, ValueError, TypeError) as exc:
        raise ThreeDError(
            "geometry worker returned an invalid report", command="solid"
        ) from exc
    if not isinstance(report, dict):
        raise ThreeDError("geometry worker report must be an object", command="solid")
    warnings = report.get("warnings", [])
    if not isinstance(warnings, list) or not all(
        isinstance(item, str) for item in warnings
    ):
        raise ThreeDError(
            "geometry worker report has invalid warnings", command="solid"
        )
    return report


def _positive_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        number = float(value)
    except OverflowError:
        return False
    return math.isfinite(number) and number > 0


def _valid_metrics(report: dict[str, Any]) -> bool:
    extents = report.get("extents_mm")
    return (
        type(report.get("solid_count")) is int
        and report["solid_count"] == 1
        and _positive_number(report.get("volume_mm3"))
        and isinstance(extents, list)
        and len(extents) == 3
        and all(_positive_number(value) for value in extents)
    )


def _validate_output(output: Path) -> None:
    if output.suffix.lower() not in (".step", ".stp"):
        raise UsageError("output must be STEP (.step or .stp)", command="solid")
    if output.exists() or output.is_symlink():
        raise UsageError(
            "output already exists; choose a new path to preserve existing work",
            command="solid",
        )
    if not output.parent.is_dir():
        raise UsageError("output directory does not exist", command="solid")


def _run_worker(
    request: dict[str, Any], report_path: Path
) -> tuple[int, dict[str, Any]]:
    """Run the geometry worker; return its exit code (normalised) and its bounded JSON report."""
    script = Path(__file__).with_name("solid_conversion.py")
    candidate = executable(script.parent.parent, "python")
    python = str(candidate) if candidate.is_file() else sys.executable
    try:
        process = subprocess.run(
            [python, str(script), "--worker", json.dumps(request), str(report_path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise ThreeDError(
            "geometry worker exceeded 120 seconds; no final STEP was published",
            command="solid",
        ) from exc
    except OSError as exc:
        raise ThreeDError(
            f"could not start the local geometry worker: {exc}", command="solid"
        ) from exc
    try:
        report = _read_report(report_path)
    except ThreeDError as exc:
        tail = (process.stderr or b"")[-2000:].decode("utf-8", errors="replace").strip()
        detail = f" (worker exit {process.returncode})" + (f": {tail}" if tail else "")
        raise ThreeDError(exc.message + detail, command="solid") from exc
    return (process.returncode if process.returncode in (0, 1, 2, 127) else 1), report


def _acknowledged(report: dict[str, Any]) -> bool:
    roundtrip = report.get("step_roundtrip")
    return bool(
        report.get("status") == "CONVERTED"
        and report.get("representation") == "faceted-brep"
        and _valid_metrics(report)
        and isinstance(roundtrip, dict)
        and roundtrip.get("valid") is True
        and _valid_metrics(roundtrip)
    )


def _publish(stage: Path, output: Path, report: dict[str, Any]) -> None:
    if not _acknowledged(report) or not stage.is_file() or stage.is_symlink():
        raise ThreeDError(
            "geometry worker did not acknowledge a checked STEP artifact with valid metrics",
            command="solid",
        )
    if report.get("output_sha256") != _file_hash(stage):
        raise ThreeDError(
            "staged STEP does not match the checked artifact hash", command="solid"
        )
    try:
        os.link(stage, output)
    except OSError as exc:
        raise ThreeDError(
            f"atomic no-clobber STEP publication failed: {exc}",
            command="solid",
            remediation=[
                "Choose a new destination on a filesystem that supports hard links, such as local NTFS or ext4."
            ],
        ) from exc


def run_conversion(request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Request a staged result; this is a commit protocol, not an OS sandbox."""
    output = Path(request["output"])
    _validate_output(output)
    cleanup_warnings: list[str] = []
    with workspace(output.parent, ".3d-solid-job-", cleanup_warnings) as temp:
        stage, report_path = temp / "converted.step", temp / "report.json"
        code, report = _run_worker(dict(request, output=str(stage)), report_path)
        if code != 0:
            report["status"] = "FAILED"
            report.setdefault("error", "geometry worker failed")
        else:
            _publish(stage, output, report)
        report["output"] = str(output)
    report.setdefault("warnings", []).extend(cleanup_warnings)
    return code, report
