"""`3d printer vision doctor` end to end through bin/3d with a fake `systemctl` on PATH: exit 1 and
the fix commands when a vision service runs with --dry-run, exit 0 when it delivers."""
from __future__ import annotations

from pathlib import Path

from .workflow_helper import isolated_env, run_cli

SYSTEMCTL = """#!/bin/sh
unit="$3"
case "$unit" in
  3d-printer-vision@*) printf 'LoadState=loaded\\nActiveState=active\\nExecStart={ path=/x ; argv[]=/x printer vision e2eprn %s ; ignore_errors=no }\\n' "$DOCTOR_FLAGS" ;;
  *) printf 'LoadState=loaded\\nActiveState=active\\nExecStart={ path=/x ; argv[]=/x printer watch e2eprn ; ignore_errors=no }\\n' ;;
esac
"""


def _setup(tmp_path: Path) -> dict[str, str]:
    cfg = isolated_env(tmp_path)["XDG_CONFIG_HOME"]
    devices = Path(cfg) / "3d-cli" / "printer-devices.yaml"
    devices.write_text(
        "e2eprn:\n  adapter: moonraker\n  host: 127.0.0.1\n  port: 7125\n  model: Snapmaker U1\n",
        encoding="utf-8")
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "systemctl").write_text(SYSTEMCTL, encoding="utf-8")
    (fake / "systemctl").chmod(0o755)
    return {"PATH": f"{fake}:{isolated_env(tmp_path)['PATH']}"}


def test_doctor_fails_on_a_dry_run_service_and_prints_the_fix(tmp_path: Path) -> None:
    extra = {**_setup(tmp_path), "DOCTOR_FLAGS": "--dry-run --verbose"}
    r = run_cli(tmp_path, "printer", "vision", "doctor", env_extra=extra)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FAIL 3d-printer-vision@e2eprn.service" in r.stdout
    assert "systemctl --user restart 3d-printer-vision@e2eprn.service" in r.stdout


def test_doctor_passes_when_the_services_deliver(tmp_path: Path) -> None:
    extra = {**_setup(tmp_path), "DOCTOR_FLAGS": "--verbose"}
    r = run_cli(tmp_path, "printer", "vision", "doctor", env_extra=extra)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ok   3d-printer-vision@e2eprn.service" in r.stdout


def test_doctor_help_and_unknown_printer(tmp_path: Path) -> None:
    extra = _setup(tmp_path)
    assert "--dry-run" in run_cli(tmp_path, "printer", "vision", "doctor", "--help", env_extra=extra).stdout
    r = run_cli(tmp_path, "printer", "vision", "doctor", "nope", env_extra=extra)
    assert r.returncode == 2 and "nope" in r.stderr
