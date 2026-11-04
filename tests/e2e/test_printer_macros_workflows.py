"""Real `bin/3d printer -n NAME macro|aliases|ALIAS` runs against the loopback fake Moonraker.

The fleet is a copy of the repo's fleet/printers.yaml (`u1`, `k1se`) with the address swapped for the
fake's (tests/printer_fakes.py `write_fleet`); no real printer is reachable. Issue #718.
"""

from __future__ import annotations

import json
from pathlib import Path

from printer_fakes import FakeMoonraker, u1_status
from printer_macro_fakes import FakeMoonrakerMacros, write_fleet

from .workflow_helper import run_cli

U1_MACROS = {
    n: n
    for n in (
        "PAUSE",
        "RESUME",
        "M600",
        "AUTO_FEEDING",
        "SM_PRINT_AUTO_FEED",
        "ROUGHLY_CLEAN_NOZZLE",
        "BED_MESH_CALIBRATE",
    )
}
U1_COMMANDS = {
    n: n for n in ("PARK_EXTRUDER", "SET_HEATER_TEMPERATURE", "TURN_OFF_HEATERS")
}


def test_a_user_lists_macros_then_runs_one_and_a_dry_run_sends_nothing(
    tmp_path: Path,
) -> None:
    u1 = FakeMoonrakerMacros(macros=dict(U1_MACROS), commands=dict(U1_COMMANDS))
    with u1.serve():
        env = {"THREED_FLEET": str(write_fleet(tmp_path, u1=u1))}
        listed = run_cli(
            tmp_path, "printer", "-n", "u1", "macro", "list", env_extra=env
        )
        dry = run_cli(
            tmp_path,
            "printer",
            "-n",
            "u1",
            "macro",
            "run",
            "AUTO_FEEDING",
            "EXTRUDER=1",
            "--yes",
            "--dry-run",
            env_extra=env,
        )
        writes_after_dry = u1.writes()
        ran = run_cli(
            tmp_path,
            "printer",
            "-n",
            "u1",
            "macro",
            "run",
            "SM_PRINT_AUTO_FEED",
            "EXTRUDER=1",
            env_extra=env,
        )

    assert listed.returncode == 0, listed.stderr
    assert (
        "u1: 7 macros defined by the printer" in listed.stdout
        and "SM_PRINT_AUTO_FEED" in listed.stdout
    )
    assert dry.returncode == 0, dry.stderr
    assert (
        "AUTO_FEEDING EXTRUDER=1" in dry.stdout
        and "script=AUTO_FEEDING%20EXTRUDER%3D1" in dry.stdout
    )
    assert writes_after_dry == []
    assert ran.returncode == 0, ran.stderr
    assert u1.scripts == ["SM_PRINT_AUTO_FEED EXTRUDER=1"]


def test_a_user_runs_aliases_and_gets_a_clear_refusal_for_a_busy_printer_and_for_injection(
    tmp_path: Path,
) -> None:
    u1 = FakeMoonrakerMacros(macros=dict(U1_MACROS), commands=dict(U1_COMMANDS))
    with u1.serve():
        env = {"THREED_FLEET": str(write_fleet(tmp_path, u1=u1))}
        load = run_cli(tmp_path, "printer", "-n", "u1", "load", "T2", env_extra=env)
        injected = run_cli(
            tmp_path,
            "printer",
            "-n",
            "u1",
            "macro",
            "run",
            "AUTO_FEEDING",
            "EXTRUDER=1;M112",
            "--yes",
            env_extra=env,
        )
        emergency = run_cli(
            tmp_path, "printer", "-n", "u1", "macro", "run", "M112", env_extra=env
        )
        scripts_before = list(u1.scripts)
        u1.status = u1_status("printing", "part.gcode", 0.3)
        busy = run_cli(tmp_path, "printer", "-n", "u1", "park", env_extra=env)
        pause = run_cli(tmp_path, "printer", "-n", "u1", "pause", env_extra=env)

    assert load.returncode == 0, load.stderr
    assert scripts_before == ["AUTO_FEEDING EXTRUDER=1 LOAD=1"]
    assert injected.returncode == 2 and "EXTRUDER" in injected.stderr
    assert (
        emergency.returncode == 1
        and "3d never sends the emergency stop" in emergency.stderr
    )
    assert (
        busy.returncode == 1
        and "is printing" in busy.stderr
        and "needs an idle printer" in busy.stderr
    )
    assert pause.returncode == 0, pause.stderr
    assert u1.scripts == [*scripts_before, "PAUSE"]


def test_read_only_and_offline_printers_are_refused_and_never_contacted(
    tmp_path: Path,
) -> None:
    k1 = FakeMoonrakerMacros(macros={"PAUSE": "p"})
    with k1.serve():
        env = {
            "THREED_FLEET": str(
                write_fleet(tmp_path, access={"k1se": "read-only"}, k1se=k1)
            )
        }
        read_only = run_cli(tmp_path, "printer", "-n", "k1se", "home", env_extra=env)
        offline = run_cli(
            tmp_path, "printer", "-n", "p1s", "macro", "run", "M620", env_extra=env
        )
        requests = list(k1.requests)

    assert read_only.returncode == 1 and "is read-only" in read_only.stderr
    assert offline.returncode == 1 and "is offline" in offline.stderr
    assert requests == []


def test_aliases_listing_and_help_per_model(tmp_path: Path) -> None:
    k1 = FakeMoonraker()
    with k1.serve():
        env = {"THREED_FLEET": str(write_fleet(tmp_path, k1se=k1))}
        unknown = run_cli(
            tmp_path, "printer", "-n", "k1se", "purifier", "hot", env_extra=env
        )
        table = run_cli(
            tmp_path, "printer", "-n", "k1se", "aliases", "--json", env_extra=env
        )
        helped = run_cli(tmp_path, "printer", "preheat", "--help", env_extra=env)
        top = run_cli(tmp_path, "printer", "--help", env_extra=env)

    assert (
        unknown.returncode == 2
        and "alias `purifier` is not defined for k1se" in unknown.stderr
    )
    assert json.loads(table.stdout)["load"]["gcode"] == ["LOAD_MATERIAL"]
    assert helped.returncode == 0 and "SET_HEATER_TEMPERATURE" in helped.stdout
    assert (
        top.returncode == 0
        and "macro list|run" in top.stdout
        and "level-heated" in top.stdout
    )
    assert k1.requests == []
