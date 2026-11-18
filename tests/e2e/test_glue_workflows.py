"""`3d glue` and the glue check of `3d printer upload|start` through bin/3d (issue #762), against the loopback
FakeMoonraker: no printer is ever contacted."""

from __future__ import annotations

import datetime
import json
import time
from pathlib import Path

from printer_fakes import FakeMoonraker, write_devices

from .workflow_helper import run_cli, run_cli_with_hatch

GCODE = """G28
T0
G1 X10 E1
; filament used [mm] = 12.0, 0.00, 0.00, 0.0
; filament_colour = #000000;#808080;#1B5E20;#FFFFFF
; filament_type = PLA;PLA;PLA;PLA
"""


def _iso(at: float) -> str:
    return datetime.datetime.fromtimestamp(at).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )  # local time, no offset


def _jobs(*starts: float, status: str = "completed") -> list[dict[str, object]]:
    return [
        {
            "job_id": f"{i:06X}",
            "filename": f"p{i}.gcode",
            "status": status,
            "start_time": s,
            "print_duration": 900.0,
            "filament_used": 1200.0,
        }
        for i, s in enumerate(starts)
    ]


def test_first_upload_and_start_on_a_printer_without_glue_on_record_warn(
    tmp_path: Path,
) -> None:
    """First use: nobody ever recorded glue on u1. Upload and start say so (fired "never") with the command,
    and still go on; the start lands in 3d's own start log."""
    moon = FakeMoonraker()
    (tmp_path / "part.gcode").write_text(GCODE, encoding="utf-8")
    with moon.serve():
        write_devices(tmp_path / "xdg-config", moon=moon)
        up = run_cli(tmp_path, "printer", "upload", "u1", "part.gcode")
        started = run_cli_with_hatch(
            tmp_path, "printer", "start", "u1", "part.gcode", "--yes", "--force"
        )
        status = run_cli(tmp_path, "glue", "status", "--json")
    for run in (up, started):
        assert "GLUE DUE on u1: no glue application on record" in run.stdout, (
            run.stdout + run.stderr
        )
        assert (
            "  Glue applied? Record it with: 3d glue applied --printer u1" in run.stdout
        )
    assert up.returncode == 0 and "NOT started" in up.stdout
    assert started.returncode == 0, started.stdout + started.stderr
    assert started.stdout.index("GLUE DUE") < started.stdout.index("PREFLIGHT")
    assert "start acknowledged by u1" in started.stdout
    log = (
        (tmp_path / "xdg-state" / "3d-cli" / "prints" / "u1.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert len(log) == 1 and json.loads(log[0])["remote"].startswith("part-")
    assert [(v["printer"], v["fired"]) for v in json.loads(status.stdout)] == [
        ("u1", ["never"])
    ]


def test_glue_status_counts_prints_from_the_printer_history_and_names_the_limit(
    tmp_path: Path,
) -> None:
    """The owner glued the bed two hours ago; three prints ran since (one from the touchscreen, not 3d)."""
    now = time.time()
    moon = FakeMoonraker()
    moon.history = _jobs(
        now - 600, now - 1800, now - 3600, now - 3 * 3600
    )  # newest first; the last is before
    with moon.serve():
        write_devices(tmp_path / "xdg-config", moon=moon)
        applied = run_cli(
            tmp_path, "glue", "applied", "--printer", "u1", "--at", _iso(now - 2 * 3600)
        )
        ok = run_cli(tmp_path, "glue", "status", "--printer", "u1", "--json")
        limits = run_cli(
            tmp_path,
            "glue",
            "limits",
            "--printer",
            "u1",
            "--prints",
            "3",
            "--days",
            "1.5",
        )
        due = run_cli(tmp_path, "glue", "status")
    assert applied.returncode == 0, applied.stderr
    assert "glue applied recorded: u1" in applied.stdout
    verdict = json.loads(ok.stdout)[0]
    assert (verdict["prints"], verdict["prints_exact"], verdict["due"]) == (
        3,
        True,
        False,
    )
    assert verdict["prints_source"].startswith("moonraker http://127.0.0.1:")
    assert (verdict["max_prints"], verdict["max_days"], verdict["limits_source"]) == (
        5,
        7.0,
        "default",
    )
    assert (
        limits.returncode == 0
        and "u1: glue due after 3 prints or 1.5 days" in limits.stdout
    )
    assert "prints since    3 of 3" in due.stdout
    assert (
        "status          DUE: 3 prints since the glue (limit 3). After gluing: 3d glue applied --printer u1"
        in due.stdout
    )
    assert moon.writes() == []  # GET only


def test_upload_warns_on_old_glue_and_glue_applied_clears_it(tmp_path: Path) -> None:
    moon = FakeMoonraker()
    (tmp_path / "part.gcode").write_text(GCODE, encoding="utf-8")
    with moon.serve():
        write_devices(tmp_path / "xdg-config", moon=moon)
        run_cli(
            tmp_path,
            "glue",
            "applied",
            "--printer",
            "u1",
            "--at",
            _iso(time.time() - 8 * 86400),
        )
        up = run_cli(tmp_path, "printer", "upload", "u1", "part.gcode")
        glued = run_cli(tmp_path, "glue", "applied", "--printer", "u1")
        again = run_cli(tmp_path, "printer", "upload", "u1", "part.gcode")
    assert "GLUE DUE on u1: 8d 00h since the glue (limit 7d)" in up.stdout, (
        up.stdout + up.stderr
    )
    assert up.returncode == 0 and glued.returncode == 0
    assert (
        "GLUE DUE" not in again.stdout and "glue u1: ok, 0 of 5 prints" in again.stdout
    )


def test_unreachable_printer_counts_from_3d_own_start_log(tmp_path: Path) -> None:
    """Moonraker does not answer: the count comes from 3d's start log and is marked as an estimate."""
    write_devices(
        tmp_path / "xdg-config", moon=FakeMoonraker(port=9)
    )  # nothing listens on the discard port
    log = tmp_path / "xdg-state" / "3d-cli" / "prints" / "u1.jsonl"
    log.parent.mkdir(parents=True)
    now = time.time()
    log.write_text(
        "".join(
            json.dumps({"at": now - 60 * i, "remote": f"p{i}.gcode"}) + "\n"
            for i in range(5)
        ),
        encoding="utf-8",
    )
    run_cli(tmp_path, "glue", "applied", "--printer", "u1", "--at", _iso(now - 3600))
    status = run_cli(tmp_path, "glue", "status", "--printer", "u1")
    assert status.returncode == 0, status.stderr
    assert "prints since    ~5 of 5 (3d-cli start log " in status.stdout
    assert (
        "unreachable" in status.stdout
        and "DUE: ~5 prints since the glue (limit 5)" in status.stdout
    )


def test_off_silences_a_printer_and_bad_input_is_refused(tmp_path: Path) -> None:
    moon = FakeMoonraker()
    (tmp_path / "part.gcode").write_text(GCODE, encoding="utf-8")
    with moon.serve():
        write_devices(tmp_path / "xdg-config", moon=moon)
        off = run_cli(tmp_path, "glue", "limits", "--printer", "u1", "--off")
        up = run_cli(tmp_path, "printer", "upload", "u1", "part.gcode")
        status = run_cli(tmp_path, "glue", "status")
        on = run_cli(tmp_path, "glue", "limits", "--printer", "u1", "--on", "--json")
    future = run_cli(
        tmp_path, "glue", "applied", "--printer", "u1", "--at", "2099-01-01"
    )
    unknown = run_cli(tmp_path, "glue", "applied", "--printer", "nope")
    plate = run_cli(
        tmp_path, "glue", "applied", "--printer", "u1", "--plate", "textured_pei"
    )
    both = run_cli(tmp_path, "glue", "limits", "--printer", "u1", "--off", "--on")
    assert off.returncode == 0 and "u1: glue reminder OFF" in off.stdout
    assert up.returncode == 0 and "glue" not in up.stdout.lower()
    assert "u1  glue reminder off" in status.stdout
    assert json.loads(on.stdout)["enabled"] is True
    assert future.returncode == 2 and "in the future" in future.stderr
    assert unknown.returncode == 2 and "--printer" in unknown.stderr
    assert plate.returncode == 2 and "unexpected argument '--plate'" in plate.stderr
    assert both.returncode == 2 and "contradict" in both.stderr


def test_glue_help_lists_every_flag_with_an_example(tmp_path: Path) -> None:
    out = run_cli(tmp_path, "glue", "--help")
    assert out.returncode == 0
    for flag in (
        "--printer",
        "--at",
        "--prints",
        "--days",
        "--reset",
        "--off",
        "--on",
        "--json",
    ):
        assert flag in out.stdout
    assert "3d glue applied --printer k1se --at 2026-11-18T00:35" in out.stdout


def test_oversized_day_limit_is_refused(tmp_path: Path) -> None:
    write_devices(tmp_path / "xdg-config", moon=FakeMoonraker(port=9))
    out = run_cli(tmp_path, "glue", "limits", "--printer", "u1", "--days", "1e308")
    assert out.returncode == 2 and "--days" in out.stderr


def test_a_malformed_glue_config_entry_is_a_clear_error(tmp_path: Path) -> None:
    write_devices(tmp_path / "xdg-config", moon=FakeMoonraker(port=9))
    config = tmp_path / "xdg-config" / "3d-cli" / "glue.json"
    for entry in ("7", '"weekly"', "[[1, 2]]"):
        config.write_text(f'{{"u1": {entry}}}', encoding="utf-8")
        for argv in (("--days", "4"), ("--json",)):
            out = run_cli(tmp_path, "glue", "limits", "--printer", "u1", *argv)
            assert out.returncode != 0, out.stdout
            assert (
                "u1 must be an object" in out.stderr and "Traceback" not in out.stderr
            )
