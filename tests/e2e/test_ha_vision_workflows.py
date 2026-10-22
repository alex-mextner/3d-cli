"""`3d ha vision` and the camera-less printer, end to end through bin/3d.

The command reads fleet/printers.yaml, prints what every fleet printer gets (and why the K1 SE has no
ghost), checks the committed Home Assistant files against it and rewrites them with --write. A printer
without a camera is told so by `printer vision` / `printer bed`, with no U1 default and no traceback.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from .workflow_helper import REPO_ROOT, run_cli

FLEET = str(REPO_ROOT / "fleet" / "printers.yaml")
HA = REPO_ROOT / "integrations" / "homeassistant"


def scratch_checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    shutil.copytree(
        HA,
        root / "integrations" / "homeassistant",
        ignore=shutil.ignore_patterns(
            "__pycache__", "tests", "www", "docs", "scripts", "requirements"
        ),
    )
    return root


def test_the_committed_files_are_current_and_the_plan_names_every_printer() -> None:
    """The owner runs `3d ha vision` on the committed repo: every fleet printer is planned (camera printers get their files, offline ones are named as having none) and the generated files are current."""
    import subprocess
    import sys

    done = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "bin" / "3d"),
            "ha",
            "vision",
            "--fleet",
            FLEET,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert (
        "u1: camera moonraker-monitor, proxy :8090, vision sensors, ghost"
        in done.stdout
    )
    assert (
        "k1se: camera mjpg-streamer, direct stream, vision sensors, NO ghost: no bundled camera calibration for Creality K1 SE"
        in done.stdout
    )
    assert (
        "p1s: no camera (offline: never contacted) -> nothing generated" in done.stdout
    )
    assert "4 generated file(s) current" in done.stdout


def test_a_hand_edit_is_reported_and_write_restores_it(tmp_path: Path) -> None:
    """Someone edits a generated Home Assistant package by hand: `3d ha vision` reports it out of date (exit 1) and `--write` restores the generated text."""
    root = scratch_checkout(tmp_path)
    package = (
        root / "integrations" / "homeassistant" / "packages" / "printer_vision.yaml"
    )
    package.write_text(
        package.read_text(encoding="utf-8").replace(
            "scan_interval: 30", "scan_interval: 99"
        ),
        encoding="utf-8",
    )

    stale = run_cli(tmp_path, "ha", "vision", "--fleet", FLEET, "--root", str(root))
    fixed = run_cli(
        tmp_path, "ha", "vision", "--fleet", FLEET, "--root", str(root), "--write"
    )
    again = run_cli(tmp_path, "ha", "vision", "--fleet", FLEET, "--root", str(root))

    assert stale.returncode == 1
    assert "out of date: integrations/homeassistant/packages/printer_vision.yaml" in stale.stdout
    assert "run `3d ha vision --write`" in stale.stdout
    assert (
        fixed.returncode == 0
        and "wrote integrations/homeassistant/packages/printer_vision.yaml"
        in fixed.stdout
    )
    assert "scan_interval: 30" in package.read_text(encoding="utf-8")
    assert again.returncode == 0 and "4 generated file(s) current" in again.stdout


def test_a_missing_entity_makes_the_command_fail_naming_it(tmp_path: Path) -> None:
    """A dashboard entity or a manifest entry is lost: the check names exactly what is missing and exits 1; `--write` regenerates a card."""
    root = scratch_checkout(tmp_path)
    dash = (
        root / "integrations" / "homeassistant" / "dashboards" / "ui-3d-printers.yaml"
    )
    dash.write_text(
        dash.read_text(encoding="utf-8").replace(
            "binary_sensor.printer_k1_se_vision_alive", "binary_sensor.gone"
        ),
        encoding="utf-8",
    )

    done = run_cli(
        tmp_path, "ha", "vision", "--fleet", FLEET, "--root", str(root), "--write"
    )

    assert (
        done.returncode == 0
    )  # --write regenerates the card: nothing is missing afterwards
    manifest = root / "integrations" / "homeassistant" / "manifest.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "src: packages/printer_vision.yaml", "src: packages/x.yaml"
        ),
        encoding="utf-8",
    )
    gap = run_cli(tmp_path, "ha", "vision", "--fleet", FLEET, "--root", str(root))
    assert gap.returncode == 1
    assert "missing: packages/printer_vision.yaml is not a `files` entry" in gap.stdout


def test_usage_errors_are_structured(tmp_path: Path) -> None:
    """Wrong `3d ha vision` usage (unknown flag, no fleet, flag without a value) gives a structured usage error, never a traceback; `--help` documents the surface."""
    unknown = run_cli(tmp_path, "ha", "vision", "--bogus")
    no_fleet = run_cli(tmp_path, "ha", "vision")
    no_value = run_cli(tmp_path, "ha", "vision", "--root")
    help_ = run_cli(tmp_path, "ha", "vision", "--help")

    assert unknown.returncode == 2
    assert "unknown option '--bogus'" in unknown.stderr
    assert "no fleet registry to generate from" in no_fleet.stderr
    assert "--root needs a value" in no_value.stderr
    assert "vision [--write] [--root DIR] [--fleet FILE]" in help_.stdout
    assert no_fleet.returncode == no_value.returncode == 2
    assert help_.returncode == 0


@pytest.mark.parametrize(
    "subcommand",
    [("printer", "vision", "bare", "--once"), ("printer", "bed", "status", "bare")],
)
def test_a_printer_without_a_camera_is_told_so_by_vision_and_bed(
    tmp_path: Path, subcommand: tuple[str, ...]
) -> None:
    """A printer with no camera configured is told so by `printer vision` and `printer bed` instead of guessing a U1 path."""
    pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    config = tmp_path / "xdg-config" / "3d-cli"
    config.mkdir(parents=True, exist_ok=True)
    (config / "printer-devices.yaml").write_text(
        "bare:\n  adapter: moonraker\n  host: 127.0.0.1\n  port: 9\n  model: Voron 2.4\n"
        "  watch:\n    tg_fallback: true\n",
        encoding="utf-8",
    )

    done = run_cli(tmp_path, *subcommand)

    assert done.returncode == 2, done.stdout + done.stderr
    assert "bare: no camera" in done.stderr and "watch.vision.camera" in done.stderr
    assert "monitor.jpg" not in done.stderr and "Traceback" not in done.stderr
