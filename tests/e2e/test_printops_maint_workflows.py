"""E2E: `3d printops maint` against a real uvicorn 3d-api (#166): the user sees the U1's broken T2
seeded from the fleet file, swaps a nozzle (calibrations invalidated), logs a rule's maintenance,
reads what is due from a fake Moonraker history, and closes a fault."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from printops_fixtures import LiveApi

from printops.api import routes_maintenance
from printops.counters import FleetCounters

from .workflow_helper import run_cli

FLEET = """\
u1:
  model: Snapmaker U1
  owner: own
  access: offline
  heads:
    - {label: T1, index: 0, nozzle: {diameter_mm: 0.4, material: stainless, type: std}}
    - label: T2
      index: 1
      nozzle: {diameter_mm: 0.4, material: stainless, type: std}
      usable: false
      reason: "head T2 does not work"
    - {label: T3, index: 2, nozzle: {diameter_mm: 0.4, material: stainless, type: std}}
"""

RULES = """\
invalidates:
  - {component: ptfe, when: [replace], trigger: ptfe}
rules:
  - id: u1-linear-shafts
    title: clean and lubricate the linear shafts
    printers: [u1]
    component: kinematics
    task: lube
    every: {days: 30, filament_m: 1000}
    source: https://wiki.snapmaker.com/en/snapmaker_u1/troubleshooting/Core_Components_Maintenance_Guide
"""


def _history(url: str) -> Any:
    """Moonraker GET /server/history/*: 2240 m in total, 120 m in the last two weeks."""
    if url.endswith("/server/history/totals"):
        return {"result": {"job_totals": {"total_jobs": 88.0, "total_print_time": 1191293.4,
                                          "total_filament_used": 2240300.0}}}
    return {"result": {"count": 1, "jobs": [{"start_time": 1791900000.0, "print_duration": 7200.0,
                                             "filament_used": 120000.0}]}}


@pytest.fixture
def maint(tmp_path: Path, live_api: LiveApi, monkeypatch: pytest.MonkeyPatch) -> Any:
    fleet, rules = tmp_path / "printers.yaml", tmp_path / "maintenance-rules.yaml"
    fleet.write_text(FLEET, encoding="utf-8")
    rules.write_text(RULES, encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(fleet))
    monkeypatch.setenv("THREED_MAINTENANCE_RULES", str(rules))
    monkeypatch.setattr(routes_maintenance, "counter_provider",
                        lambda: FleetCounters(get_json=_history, resolve=lambda p: ("moonraker", "http://u1:7125")))
    # TZ pins the CLI's local-time rendering ("since 2026-10-14") on any host
    env = {"PRINTOPS_API": live_api.url, "PRINTOPS_TOKEN": live_api.tokens["rw"], "TZ": "Europe/Belgrade"}

    def cli(*argv: str) -> tuple[int, str, str]:
        r = run_cli(tmp_path, "printops", "maint", *argv, env_extra=env)
        return r.returncode, r.stdout, r.stderr

    return cli


def test_user_tracks_a_broken_head_a_nozzle_swap_and_due_maintenance(maint: Any) -> None:
    """The user sees the U1's T2 as unusable with its seeded fault, swaps T3's nozzle (the head's
    calibrations are invalidated), learns the linear shafts are overdue from the printer's job
    history, logs that maintenance, replaces a PTFE that fixes the T2 fault, and closes a new
    extruder fault as won't fix."""
    code, out, err = maint("components", "u1")
    assert code == 0, err
    assert "u1/T2  UNUSABLE (fleet: head T2 does not work; open fault #1)" in out
    assert "u1/T3  usable  nozzle #3 0.4 mm stainless std (since unknown date)" in out

    code, out, err = maint("fault", "ls")
    assert code == 0, err
    assert "#1  OPEN  u1/T2 [head]  head T2 does not work" in out

    code, out, err = maint("nozzle", "swap", "u1/T3", "--reason", "clogged", "--material", "hardened",
                           "--at", "2026-10-14T10:00+02:00")
    assert code == 0, err
    assert "removed nozzle #3 0.4 mm stainless std (since unknown date)" in out
    assert "installed nozzle #4 0.4 mm hardened std (since 2026-10-14) on u1/T3  (kept: diameter_mm, type)" in out
    assert "calibrations invalidated on u1/T3: nozzle_swap (every kind the catalog ties to it)" in out

    code, out, err = maint("due", "u1")
    assert code == 0, err
    assert "DUE        u1       u1-linear-shafts  clean and lubricate the linear shafts" in out
    assert "[days ?/30, filament_m 2240.3/1000; never logged]" in out
    assert "done? 3d printops maint log u1 --rule u1-linear-shafts" in out

    code, out, err = maint("log", "u1", "--rule", "u1-linear-shafts", "--at", "2026-10-10",
                           "--notes", "IPA + white lithium grease")
    assert code == 0, err
    assert "logged #2 kinematics lube on u1" in out and "(rule u1-linear-shafts)" in out

    code, out, err = maint("due", "u1", "--json")
    assert code == 0, err
    [item] = json.loads(out)
    assert item["status"] == "ok" and item["used"]["filament_m"] == 120.0
    assert item["baseline"]["maintenance_id"] == 2

    code, out, err = maint("log", "u1/T2", "--component", "ptfe", "--kind", "replace", "--label", "Capricorn",
                           "--fixes", "1")
    assert code == 0, err
    assert "calibrations invalidated on u1/T2: ptfe (every kind the catalog ties to it)" in out
    assert "fault #1 fixed: head T2 does not work" in out

    code, out, err = maint("history", "u1")
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0].startswith("#3  ") and "u1/T2  ptfe replace" in lines[0] and "invalidated: ptfe" in lines[0]
    assert "u1/T3  nozzle replace" in lines[1] and "invalidated: nozzle_swap" in lines[1]
    assert "kinematics lube" in lines[2] and "rule u1-linear-shafts" in lines[2]

    code, out, err = maint("fault", "open", "u1/T1", "--symptom", "grinding", "--component", "extruder")
    assert code == 0, err
    assert out.startswith("opened #2  OPEN  u1/T1 [extruder]  grinding")
    code, out, err = maint("fault", "close", "2", "--wontfix", "--root-cause", "noise only")
    assert code == 0, err
    assert "#2  WONTFIX  u1/T1 [extruder]  grinding" in out and "wontfix" in out and "noise only" in out


def test_user_checks_one_printer_and_sees_only_its_announcements(maint: Any, tmp_path: Path) -> None:
    """With a K1 SE in the fleet too, the user runs `due u1 --check`: only u1's due work is
    announced; the K1 SE's due rule waits for its own check (or the fleet-wide one)."""
    (tmp_path / "printers.yaml").write_text(
        FLEET + "k1se:\n  model: Creality K1 SE\n  owner: own\n  access: offline\n  heads:\n"
        "    - {label: T1, index: 0, nozzle: {diameter_mm: 0.4, material: hardened, type: std}}\n",
        encoding="utf-8")
    (tmp_path / "maintenance-rules.yaml").write_text(
        RULES + "  - id: k1se-y-rust\n    title: rust-prevention treatment of the Y-axis rods\n"
        "    printers: [k1se]\n    component: kinematics\n    task: inspect\n    every: {days: 180}\n"
        "    source: https://wiki.creality.com/en/k1-flagship-series/k1-se\n",
        encoding="utf-8")

    code, out, err = maint("due", "u1", "--check")
    assert code == 0, err
    assert "announced maintenance.due" in out and "u1-linear-shafts on u1" in out
    assert "k1se" not in out

    code, out, err = maint("due", "--check", "--json")
    assert code == 0, err
    assert [a["rule"] for a in json.loads(out)["announced"]] == ["k1se-y-rust"]  # scripts see what was announced

    code, out, err = maint("due", "--check")
    assert code == 0, err
    assert "announced maintenance.due" not in out  # everything due was announced once already


def test_user_mistakes_are_explained(maint: Any) -> None:
    """The user names no head for a swap, a head that does not exist, a nozzle replacement via
    `log`, a log without rule or component, and an unknown fault — each is explained."""
    code, _, err = maint("nozzle", "swap", "u1", "--reason", "x")
    assert code == 2 and "names no head" in err
    code, _, err = maint("nozzle", "swap", "u1/T9", "--reason", "x")
    assert code == 1 and "u1 has no head 'T9'" in err
    code, _, err = maint("log", "u1", "--component", "nozzle", "--kind", "replace")
    assert code == 1 and "nozzle swap" in err
    code, _, err = maint("log", "u1")
    assert code == 2 and "log needs --rule ID" in err
    code, _, err = maint("log", "u1/", "--component", "ptfe", "--kind", "clean")
    assert code == 2 and "is not PRINTER or PRINTER/HEAD" in err
    code, _, err = maint("fault", "close", "99")
    assert code == 1 and "no fault #99" in err
    code, out, _ = maint("--help")
    assert code == 0 and "3d printops maint due --check" in out
