"""`3d printer vision doctor` (lib/printer_ops/deploy_check.py): the deployment trap of
2026-10-22 — a vision/watch service running with --dry-run prints every event and delivers none —
must fail loudly for every printer that has a camera. systemd is faked with `systemctl show` text."""

from __future__ import annotations

from pathlib import Path

import pytest

from errors import GateFailure
from printer_ops import deploy_check as dc


def _show(exec_args: str, state: str = "active", env_files: str = "") -> str:
    return (
        f"LoadState=loaded\nActiveState={state}\n"
        f"ExecStart={{ path=/usr/bin/3d ; argv[]=/usr/bin/3d {exec_args} ; ignore_errors=no ; pid=0 }}\n"
        f"EnvironmentFiles={env_files}\n"
    )


def _systemd(units: dict[str, str]) -> dc.Show:
    return lambda unit: units.get(unit, "LoadState=not-found\nActiveState=inactive\n")


def test_a_running_vision_service_with_dry_run_fails_with_the_fix() -> None:
    show = _systemd({
        "3d-printer-vision@u1.service": _show("printer vision u1 --dry-run --verbose"),
        "3d-printer-watch@u1.service": _show("printer watch u1"),
    })
    v = dc.check_printer("u1", True, show)
    assert [u.unit for u in v.failures] == ["3d-printer-vision@u1.service"]
    text = dc.render([v])
    assert "FAIL 3d-printer-vision@u1.service" in text
    assert "systemctl --user edit --full 3d-printer-vision@u1.service" in text
    assert "3d printer subscriptions test u1 --kind vision_part_detached --send" in text  # the delivery proof


def test_dry_run_in_the_environment_file_of_the_installed_unit_is_found(tmp_path: Path) -> None:
    env = tmp_path / "u1.env"
    env.write_text("WATCH_ARGS=--no-stdout --dry-run\n", encoding="utf-8")
    show = _systemd({"3d-printer-watch@u1.service": _show(
        "printer watch u1 $WATCH_ARGS", env_files=f"{env} (ignore_errors=yes)")})
    unit = dc.check_unit("3d-printer-watch@u1.service", show)
    assert unit.dry_run and unit.source == "EnvironmentFile WATCH_ARGS" and not unit.ok


def test_a_stopped_dry_run_unit_is_not_a_failure() -> None:
    show = _systemd({"3d-printer-watch@k1se.service": _show("printer watch k1se --dry-run", state="inactive")})
    assert dc.check_unit("3d-printer-watch@k1se.service", show).ok


def test_healthy_units_pass_and_a_missing_vision_service_only_warns() -> None:
    ok = dc.check_printer("u1", True, _systemd({
        "3d-printer-vision@u1.service": _show("printer vision u1"),
        "3d-printer-watch@u1.service": _show("printer watch u1"),
    }))
    assert not ok.failures and not ok.warnings
    missing = dc.check_printer("k1se", True, _systemd({}))
    assert not missing.failures
    assert "no running vision service" in missing.warnings[0]


def test_a_printer_without_a_camera_is_not_checked() -> None:
    v = dc.check_printer("a1mini", False, _systemd({"3d-printer-vision@a1mini.service": _show("--dry-run")}))
    assert v.units == [] and not v.failures


def test_without_systemctl_the_answer_is_unknown_not_ok() -> None:
    unit = dc.check_unit("3d-printer-vision@u1.service", lambda _u: "")
    assert unit.state == "unknown" and not unit.running


@pytest.fixture()
def devices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "xdg" / "3d-cli"
    cfg.mkdir(parents=True)
    (cfg / "printer-devices.yaml").write_text(
        "u1:\n  adapter: moonraker\n  host: 127.0.0.1\n  port: 7125\n  model: Snapmaker U1\n"
        "bare:\n  adapter: moonraker\n  host: 127.0.0.2\n  port: 7125\n",
        encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)


def test_run_raises_a_silent_gate_failure_and_names_the_unit(
        devices: None, capsys: pytest.CaptureFixture[str]) -> None:
    show = _systemd({"3d-printer-vision@u1.service": _show("printer vision u1 --dry-run")})
    with pytest.raises(GateFailure) as info:
        dc.run([], show)
    assert "3d-printer-vision@u1.service" in info.value.message
    out = capsys.readouterr().out
    assert "FAIL 3d-printer-vision@u1.service" in out
    assert "bare: no camera" in out  # the printer without a camera is skipped, not failed


def test_run_passes_for_a_delivering_deployment_and_json_is_machine_readable(
        devices: None, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    show = _systemd({
        "3d-printer-vision@u1.service": _show("printer vision u1"),
        "3d-printer-watch@u1.service": _show("printer watch u1"),
    })
    assert dc.run(["u1", "--json"], show) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["printer"] == "u1" and all(u["ok"] for u in data[0]["units"])
