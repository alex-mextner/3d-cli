"""`3d lint --printops` fleet rules (printops.fleet_lint, issue #157): schema/model/device rules
and the read-only live topology check, against the in-process fakes — never a real printer."""

from __future__ import annotations

from pathlib import Path

import pytest
from fleet_fixtures import BAD_FLEET, REPO_FLEET, k1_entry, u1_entry, write_fleet
from printer_fakes import FakeCreality, FakeMoonraker

from printer_ops import devices
from printops import fleet as fleet_mod
from printops.fleet_lint import ERROR, WARN, LiveTopology, lint_fleet, probe_live


def test_repo_fleet_lints_clean() -> None:
    path, findings = lint_fleet(REPO_FLEET)
    assert path == REPO_FLEET
    assert findings == []


def test_lint_reports_schema_model_and_device_rules(tmp_path: Path) -> None:
    bad = tmp_path / "printers.yaml"
    bad.write_text(BAD_FLEET, encoding="utf-8")
    _, findings = lint_fleet(bad)
    rules = {(f.rule, f.subject) for f in findings}
    assert ("fleet/model-known", "unknown-model") in rules
    assert ("fleet/device", "bad-device") in rules
    assert ("fleet/schema", "typo") in rules
    assert all(f.level == ERROR for f in findings)
    device = next(f for f in findings if f.rule == "fleet/device")
    assert "octoprint" in device.message


def test_lint_missing_and_disabled_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, missing = lint_fleet(tmp_path / "nope.yaml")
    assert [f.message for f in missing] == ["fleet file not found"]
    monkeypatch.setenv(fleet_mod.ENV, "")
    path, disabled = lint_fleet()
    assert path is None and disabled[0].level == ERROR


def _live_fleet(tmp_path: Path) -> Path:
    return write_fleet(
        tmp_path / "fleet.yaml",
        u1_entry(FakeMoonraker(port=7001)),
        k1_entry(FakeCreality(http_port=1, ws_port=2)),
    )


def test_live_lint_flags_head_count_and_nozzle_mismatch(tmp_path: Path) -> None:
    def probe(device: devices.Device) -> LiveTopology:
        if device.name == "u1":  # only 3 extruders, T1 has a 0.6 nozzle
            return LiveTopology(
                ("extruder", "extruder1", "extruder2"),
                {"extruder": 0.6, "extruder1": 0.4},
            )
        return LiveTopology(("extruder",), {})

    _, findings = lint_fleet(_live_fleet(tmp_path), live=True, probe=probe)
    msgs = [f.message for f in findings if f.rule == "fleet/live-topology"]
    assert (
        "u1: fleet lists 4 head(s), the printer reports 3 extruder(s) (extruder, extruder1, extruder2)"
        in msgs
    )
    assert "u1.T4: extruder 'extruder3' is not on the printer" in msgs
    assert (
        "u1.T1: nozzle 0.4 mm in the fleet, 0.6 mm in the printer config (extruder)"
        in msgs
    )
    assert not any(f.subject == "k1se" for f in findings)


def test_live_lint_unreachable_printer_is_a_warning(tmp_path: Path) -> None:
    def probe(device: devices.Device) -> LiveTopology:
        raise OSError("connection refused")

    _, findings = lint_fleet(_live_fleet(tmp_path), live=True, probe=probe)
    assert {(f.level, f.subject) for f in findings} == {(WARN, "u1"), (WARN, "k1se")}
    assert (
        "live read failed, not checked: OSError: connection refused"
        in findings[0].message
    )


def test_probe_live_reads_moonraker_config_with_one_get(tmp_path: Path) -> None:
    moon = FakeMoonraker(
        settings={
            "extruder": {"nozzle_diameter": 0.4},
            "extruder1": {"nozzle_diameter": 0.4},
            "extruder3": {"nozzle_diameter": 0.6},
            "extruder2": {"nozzle_diameter": 0.4},
            "extruder_stepper x": {},
            "stepper_x": {"position_max": 270},
        }
    )
    with moon.serve():
        topo = probe_live(
            devices.Device("u1", "moonraker", "127.0.0.1", moon.port, None, tmp_path)
        )
    assert topo.extruders == ("extruder", "extruder1", "extruder2", "extruder3")
    assert topo.nozzle_mm["extruder3"] == 0.6
    assert [r.method for r in moon.requests] == ["GET"] and moon.writes() == []


def test_probe_live_counts_creality_nozzles_read_only(tmp_path: Path) -> None:
    k1 = FakeCreality()
    with k1.serve():
        dev = devices.Device(
            "k1se",
            "creality-stock",
            "127.0.0.1",
            k1.http_port,
            None,
            tmp_path,
            k1.ws_port,
        )
        topo = probe_live(dev)
    assert topo.extruders == ("extruder",)
    assert k1.writes() == []


def test_live_lint_checks_heads_without_extruder_by_tool_order(tmp_path: Path) -> None:
    """A head with no `extruder:` is still compared: Klipper names tool i `extruder`, `extruder1`, …"""
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text("u1:\n  model: Snapmaker U1\n  owner: own\n  access: full\n  adapter: moonraker\n"
                     "  host: 127.0.0.1\n  port: 7001\n"
                     "  heads: [{label: T1, index: 0, nozzle: {diameter_mm: 0.4}}, {label: T2, index: 1}]\n",
                     encoding="utf-8")

    def probe(device: devices.Device) -> LiveTopology:
        return LiveTopology(("extruder",), {"extruder": 0.6})

    _, findings = lint_fleet(fleet, live=True, probe=probe)
    msgs = [f.message for f in findings]
    assert "u1.T1: nozzle 0.4 mm in the fleet, 0.6 mm in the printer config (extruder)" in msgs
    assert "u1.T2: extruder 'extruder1' is not on the printer" in msgs
