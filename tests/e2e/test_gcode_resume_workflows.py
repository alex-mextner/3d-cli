"""Readable e2e workflows for `3d gcode-resume` (issue #138)."""
from __future__ import annotations

from pathlib import Path

from gcode_resume_fixtures import U1_RESUME_YAML, check_resume, synthetic_gcode

from .workflow_helper import run_cli


def test_resume_a_stopped_two_head_plate_above_the_measured_top(tmp_path: Path) -> None:
    """A two-head print stopped at layer 6 of 8. The user finds the pause in the file, looks at the
    layer table, measures the part's top, and builds a resume G-code for the U1 whose purge macro
    and clearance come from printer-devices.yaml; the output lifts Z before any travel."""
    plate = tmp_path / "plate.gcode"
    plate.write_text(synthetic_gcode(), encoding="ascii")
    cfg = tmp_path / "xdg-config" / "3d-cli"
    cfg.mkdir(parents=True)
    (cfg / "printer-devices.yaml").write_text(U1_RESUME_YAML, encoding="utf-8")
    pause = plate.read_bytes().index(b";Z:1.2")

    where = run_cli(tmp_path, "gcode-resume", str(plate), "--where", str(pause))
    assert where.returncode == 0, where.stderr
    assert "layer 6 of 8" in where.stdout

    table = run_cli(tmp_path, "gcode-resume", str(plate), "--table", "3:5")
    assert table.returncode == 0, table.stderr
    assert len(table.stdout.strip().splitlines()) == 3

    no_printer = run_cli(tmp_path, "gcode-resume", str(plate), "--top-z", "0.79")
    assert no_printer.returncode == 2
    assert "--printer" in no_printer.stderr

    built = run_cli(tmp_path, "gcode-resume", str(plate), "--printer", "u1", "--top-z", "0.79",
                    "--paused-at", str(pause), "--no-tower")
    assert built.returncode == 0, built.stderr
    out = tmp_path / "plate_resume_L4.gcode"
    assert f"wrote {out}" in built.stdout
    text = out.read_text(encoding="ascii")
    assert "SM_PRINT_PREEXTRUDE_FILAMENT INDEX=0 FORCE=1" in text
    assert "DEFECT_DETECTION_START" in text
    check = check_resume(text, 0.8, no_tower=True)
    assert check.problems == []


def test_printer_without_a_resume_block_is_refused(tmp_path: Path) -> None:
    """A user points gcode-resume at a printer whose config has no `resume:` values and is told
    to add them instead of getting another printer's purge macro."""
    plate = tmp_path / "plate.gcode"
    plate.write_text(synthetic_gcode(), encoding="ascii")
    cfg = tmp_path / "xdg-config" / "3d-cli"
    cfg.mkdir(parents=True)
    (cfg / "printer-devices.yaml").write_text("k1se: {adapter: creality-stock, host: 127.0.0.1}\n",
                                              encoding="utf-8")
    result = run_cli(tmp_path, "gcode-resume", str(plate), "--printer", "k1se", "--layer", "4")
    assert result.returncode == 2
    assert "no `resume:` block" in result.stderr
    assert not list(tmp_path.glob("*_resume_*"))


def test_resume_never_overwrites_the_original(tmp_path: Path) -> None:
    """A user passes the input file as -o by mistake; the original plate must survive."""
    plate = tmp_path / "plate.gcode"
    plate.write_text(synthetic_gcode(), encoding="ascii")
    cfg = tmp_path / "xdg-config" / "3d-cli"
    cfg.mkdir(parents=True)
    (cfg / "printer-devices.yaml").write_text(U1_RESUME_YAML, encoding="utf-8")
    result = run_cli(tmp_path, "gcode-resume", str(plate), "--printer", "u1", "--layer", "4", "-o", str(plate))
    assert result.returncode == 2
    assert "the original must stay" in result.stderr
    assert plate.read_text(encoding="ascii") == synthetic_gcode()
