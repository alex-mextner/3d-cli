"""E2E stories through `bin/3d printer` for the slot colour: `start --fix-colour` (rewrite, upload the
fixed file, start) and `slot set` / `slot show` (align the printer's head record with the file) on a
loopback fake U1; never a real printer."""

from __future__ import annotations

import json

from pathlib import Path

from colour_fix_fixtures import LOADED, SLICED, load_beige_petg, write_plate
from fleet_fixtures import u1_entry, write_fleet
from printer_fakes import FakeMoonraker, u1_status, write_devices

from .workflow_helper import hatch_requests, run_cli, run_cli_with_hatch


def slot_line(stdout: str) -> str:
    return next(line for line in stdout.splitlines() if "slot T3" in line)


def test_printer_start_fix_colour_needs_no_force_for_the_slot_colour(
    tmp_path: Path,
) -> None:
    """The slot colour no longer blocks: `start --fix-colour` rewrites, uploads the fixed file and starts it;
    only the unrelated thumbnail FAIL of this bare fixture needs the (approved) force."""
    moon = FakeMoonraker()
    load_beige_petg(moon)
    g = write_plate(tmp_path)
    with moon.serve():
        write_devices(tmp_path / "xdg-config", moon=moon)
        started = run_cli_with_hatch(
            tmp_path,
            "printer",
            "start",
            "u1",
            str(g),
            "--yes",
            "--fix-colour",
            "--force",
        )

    assert started.returncode == 0, started.stdout + started.stderr
    assert (
        f"{SLICED} -> {LOADED}" in started.stdout
        and "uploaded the colour-fixed file" in started.stdout
    )
    assert slot_line(started.stdout).split()[0] == "PASS"
    (request,) = hatch_requests(tmp_path)
    failing = json.dumps(request["context"])
    assert "slot T3" not in failing and "- thumbnails:" in failing
    assert [r.path for r in moon.writes()] == [
        "/server/files/upload",
        "/printer/print/start",
    ]
    assert moon.contents[moon.writes()[1].query["filename"][0]] == g.read_bytes()


def test_printer_slot_set_dry_run_then_write_aligns_the_printer_with_the_file(
    tmp_path: Path,
) -> None:
    """The other direction: the printer's record is set to the file's colour (U1 SET_PRINT_FILAMENT_CONFIG)."""
    moon = FakeMoonraker()
    with moon.serve():
        env = {
            "THREED_FLEET": str(write_fleet(tmp_path / "fleet.yaml", u1_entry(moon)))
        }
        dry = run_cli(
            tmp_path,
            "printer",
            "slot",
            "set",
            "u1",
            "T4",
            "--material",
            "PETG",
            "--color",
            SLICED,
            "--brand",
            "Elegoo",
            "--dry-run",
            env_extra=env,
        )
        assert moon.scripts == []
        wrote = run_cli(
            tmp_path,
            "printer",
            "slot",
            "set",
            "u1",
            "T4",
            "--material",
            "PETG",
            "--color",
            SLICED,
            "--brand",
            "Elegoo",
            env_extra=env,
        )
        shown = run_cli(tmp_path, "printer", "slot", "show", "u1", env_extra=env)
        moon.status = u1_status("printing", "big.gcode")
        busy = run_cli(
            tmp_path,
            "printer",
            "slot",
            "set",
            "u1",
            "T4",
            "--material",
            "PLA",
            "--color",
            "#FFFFFF",
            env_extra=env,
        )

    assert dry.returncode == 0, dry.stderr
    assert "DRY RUN" in dry.stdout and SLICED in dry.stdout
    assert wrote.returncode == 0, wrote.stderr
    assert f"u1/T4 now shows PETG {SLICED} Elegoo Basic (read back)" in wrote.stdout
    assert any(
        f"PETG {SLICED}" in line and line.lstrip().startswith("T4")
        for line in shown.stdout.splitlines()
    )
    assert busy.returncode == 1 and "never done during a job" in busy.stderr
    assert len(moon.scripts) == 1 and moon.scripts[0].startswith(
        "SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=3 "
    )


def test_slot_set_refuses_an_offline_printer_and_a_missing_head(tmp_path: Path) -> None:
    moon = FakeMoonraker()
    with moon.serve():
        env = {
            "THREED_FLEET": str(
                write_fleet(tmp_path / "fleet.yaml", u1_entry(moon, access="read-only"))
            )
        }
        readonly = run_cli(
            tmp_path,
            "printer",
            "slot",
            "set",
            "u1",
            "T4",
            "--material",
            "PETG",
            "--color",
            SLICED,
            env_extra=env,
        )
        no_head = run_cli(
            tmp_path,
            "printer",
            "slot",
            "set",
            "u1",
            "--material",
            "PETG",
            "--color",
            SLICED,
            env_extra=env,
        )

    assert readonly.returncode == 1 and "read-only" in readonly.stderr
    assert (
        no_head.returncode == 2 and "needs the printer and the head" in no_head.stderr
    )
    assert moon.scripts == []
