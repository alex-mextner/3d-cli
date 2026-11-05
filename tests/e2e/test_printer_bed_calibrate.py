"""`3d printer bed calibrate NAME` end to end through bin/3d against a loopback fake Moonraker (the printer is
idle and homed, its camera serves one fixed U1 frame, so every Z shows the plate in the same place: the plate
followed over the grid keeps its share and the point of the test is the CLI, the guarded Z-only moves and the file
it writes). The bed check's own geometry is tested in tests/test_bed_calibrate.py with a camera that moves."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from printer_fakes import FakeMoonraker

from .test_printer_bed_workflows import HAND_ROI, jpeg, u1_after_job
from .workflow_helper import run_cli

pytest.importorskip("numpy")
pytest.importorskip("cv2")

RESUME = """  resume:
    purge_macro: SM_PRINT_PREEXTRUDE_FILAMENT
    purge_mm: 30
    purge_retract_mm: 0.5
    no_tower_prime_mm: 12
    clearance_mm: 5
    tower_margin_mm: 3
    filament_diameter_mm: 1.75
    min_print_temp: 170
    default_accel: 10000
    z_max_mm: 275
    start_commands: []
"""


def homed_u1(state: str = "complete") -> FakeMoonraker:
    moon = u1_after_job(state)
    moon.status["toolhead"] = {"position": [5.0, 5.0, 200.0, 0.0], "homed_axes": "xyz"}
    moon.status["gcode_move"] = {"gcode_position": [5.0, 5.0, 200.0, 0.0]}
    return moon


def write_config(
    tmp_path: Path, moon: FakeMoonraker, bed: str, *, resume: bool = True
) -> None:
    d = tmp_path / "xdg-config" / "3d-cli"
    d.mkdir(parents=True, exist_ok=True)
    (d / "printer-devices.yaml").write_text(
        f"""u1:
  adapter: moonraker
  host: 127.0.0.1
  port: {moon.port}
  model: Snapmaker U1
  trusted_macros: [T0]
  watch:
    vision: {{keepalive: false}}
    bed: {bed}
{RESUME if resume else ""}""",
        encoding="utf-8",
    )


BED = f"{{enabled: true, plate_mask: {HAND_ROI}, light: {{enabled: false}}}}"


def calibration(tmp_path: Path) -> Path:
    return next(tmp_path.glob("xdg-data/**/bed/u1/calibration.json"))


def test_calibrate_moves_the_bed_in_z_only_writes_the_views_and_leaves_the_references_alone(
    tmp_path: Path,
) -> None:
    moon = homed_u1()
    with moon.serve():
        write_config(tmp_path, moon, BED)
        assert (
            run_cli(
                tmp_path, "printer", "bed", "reference", "u1", "--capture"
            ).returncode
            == 0
        )
        moon.camera = jpeg("u1_z200_clear_ref.jpg")
        moon.scripts.clear()
        out = run_cli(
            tmp_path, "printer", "bed", "calibrate", "u1", "--part-height", "20",
            "--z-from", "40", "--z-to", "100", "--z-step", "30", "--json",
        )  # fmt: skip
        scripts = [
            line
            for sc in moon.scripts
            for line in sc.splitlines()
            if line.startswith("G")
        ]
        listed = run_cli(tmp_path, "printer", "bed", "reference", "u1")

    assert out.returncode == 0, out.stdout + out.stderr
    res = json.loads(out.stdout)
    assert res["method"] == "plate_mask"
    assert res["grid"] == [40.0, 70.0, 100.0]
    assert res["view"]["restored"] is True
    # the grid, and the reference frame the plate was followed from (Z 200, where the printer rested)
    assert [z for z, _ in res["views"]] == [40.0, 70.0, 100.0, 200.0]
    assert min(share for _, share in res["views"]) > 0.9
    assert res["seed"]["z"] == 200.0
    stored = json.loads(calibration(tmp_path).read_text(encoding="utf-8"))
    assert stored["views"] == res["views"]
    assert len(stored["plate_mask"]) == 4
    assert [s for s in scripts if s.startswith("G1")] == [
        f"G1 Z{z:.3f} F600" for z in (40, 70, 100)
    ]
    assert "calibration.json" not in listed.stdout
    assert "plate" in listed.stdout


def test_calibrate_is_refused_while_a_job_runs_and_nothing_is_moved_or_stored(
    tmp_path: Path,
) -> None:
    moon = homed_u1("printing")
    with moon.serve():
        write_config(tmp_path, moon, BED)
        out = run_cli(
            tmp_path, "printer", "bed", "calibrate", "u1", "--part-height", "20"
        )
        writes = [r.path for r in moon.writes()]

    assert (
        out.returncode == 2
        and "printing" in out.stderr
        and "nothing was moved" in out.stderr
    )
    assert writes == [] and list(tmp_path.glob("xdg-data/**/calibration.json")) == []


def test_calibrate_without_the_part_height_or_with_a_grid_under_the_floor_moves_nothing(
    tmp_path: Path,
) -> None:
    moon = homed_u1()
    with moon.serve():
        write_config(tmp_path, moon, BED)
        no_height = run_cli(tmp_path, "printer", "bed", "calibrate", "u1")
        too_low = run_cli(
            tmp_path,
            "printer",
            "bed",
            "calibrate",
            "u1",
            "--part-height",
            "20",
            "--z-from",
            "20",
        )
        no_resume = None
        write_config(tmp_path, moon, BED, resume=False)
        no_resume = run_cli(
            tmp_path, "printer", "bed", "calibrate", "u1", "--part-height", "20"
        )
        writes = [r.path for r in moon.writes()]

    assert (
        no_height.returncode == 2
        and "height of what may stand on the bed is unknown" in no_height.stderr
    )
    assert too_low.returncode == 2 and "outside what may be moved to" in too_low.stderr
    assert no_resume.returncode == 2 and "no resume.z_max_mm" in no_resume.stderr
    assert writes == []


def test_calibrate_flags_belong_to_calibrate_and_are_validated(tmp_path: Path) -> None:
    moon = homed_u1()
    with moon.serve():
        write_config(tmp_path, moon, BED)
        stray = run_cli(tmp_path, "printer", "bed", "check", "u1", "--z-step", "5")
        bad_step = run_cli(
            tmp_path,
            "printer",
            "bed",
            "calibrate",
            "u1",
            "--part-height",
            "20",
            "--z-step",
            "0",
        )
        helped = run_cli(tmp_path, "printer", "bed", "--help")

    assert stray.returncode == 2 and "does not take --z-step" in stray.stderr
    assert bad_step.returncode == 2 and "positive --z-step" in bad_step.stderr
    assert "bed calibrate NAME [--z-from A --z-to B --z-step S]" in helped.stdout
    assert (
        "views: {70: 1.0, 200: 0.15}" in helped.stdout and "plate_mask" in helped.stdout
    )
