"""Readable e2e workflows for `3d gcode-mix-gate` (issue #551)."""

from __future__ import annotations

import json
from pathlib import Path

from mix_gate_fixtures import stripe_gcode, write_config, write_parts

from .workflow_helper import run_cli, run_shell


def _project(tmp_path: Path, gcode: str) -> tuple[Path, Path, Path]:
    plate = tmp_path / "plate.gcode"
    plate.write_text(gcode, encoding="ascii")
    return plate, write_config(tmp_path / "gates.json"), write_parts(tmp_path)


def test_gate_a_sliced_two_colour_plate_before_it_goes_to_the_printer(
    tmp_path: Path,
) -> None:
    """A user sliced a plate whose right box alternates two filaments per layer. Before upload they gate
    the G-code against the part STLs and the config, see every volume pass, and keep the JSON report."""
    plate, cfg, parts = _project(tmp_path, stripe_gcode())

    done = run_cli(
        tmp_path,
        "gcode-mix-gate",
        str(plate),
        "--config",
        str(cfg),
        "--parts",
        str(parts),
    )
    assert done.returncode == 0, done.stderr
    assert "PASS  colour volume stripe: mix AB follows the pattern" in done.stdout
    assert "PASS  colour volume base: pure slot 3 (T2)" in done.stdout
    assert done.stdout.rstrip().splitlines()[-1] == "OVERALL PASS (not verified, no input: cavity)"

    report = run_cli(
        tmp_path,
        "gcode-mix-gate",
        str(plate),
        "--config",
        str(cfg),
        "--parts",
        str(parts),
        "--json",
    )
    payload = json.loads(report.stdout)
    assert payload["ok"] is True
    assert {g["name"] for g in payload["gates"] if g["status"] == "PASS"} >= {
        "colour volume base: pure slot 3 (T2)"
    }
    assert payload["stats"]["layers"] == 100


def test_a_shifted_pattern_exits_one_and_names_the_layers(tmp_path: Path) -> None:
    """The slicer indexed the pattern one layer late. The gate exits 1, and the user finds the first
    off-pattern layers with their Z, layer number and the tools that printed."""
    plate, cfg, parts = _project(tmp_path, stripe_gcode(shift=1))

    done = run_cli(
        tmp_path,
        "gcode-mix-gate",
        str(plate),
        "--config",
        str(cfg),
        "--parts",
        str(parts),
    )
    assert done.returncode == 1
    assert "FAIL  colour volume stripe: mix AB follows the pattern" in done.stdout
    assert "100 off the pattern at layer offset 0 [(0.1, 0, [1])" in done.stdout
    assert done.stdout.rstrip().splitlines()[-1].startswith("OVERALL FAIL")


def test_teeth_prove_the_gate_can_fail_on_the_real_plate(tmp_path: Path) -> None:
    """The user does not trust a gate that only ever says PASS: --mix-teeth re-runs it on a shifted pattern
    and on one wrong-tool layer and the controls must be caught; a pipe keeps just the verdicts."""
    plate, cfg, parts = _project(tmp_path, stripe_gcode())

    done = run_shell(
        f'"$THREED" gcode-mix-gate {plate} --config {cfg} --parts {parts} --mix-teeth > teeth.log 2> teeth.err; '
        "echo exit=$?; grep -c 'control CAUGHT' teeth.log; grep -E 'NOT CAUGHT|TEETH FAILED' teeth.log | wc -l",
        tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["exit=0", "4", "0"]
    log = (tmp_path / "teeth.log").read_text(encoding="utf-8")
    assert "[teeth stripe pattern shifted by one layer] expecting FAIL: FAIL" in log


def test_vacuous_teeth_exit_one(tmp_path: Path) -> None:
    """Every layer of the mix uses one tool, so a shifted pattern looks identical and the control cannot trip:
    the gate says so (TEETH FAILED) and exits 1 instead of passing quietly."""
    plate = tmp_path / "plate.gcode"
    plate.write_text(stripe_gcode(pattern="11"), encoding="ascii")
    cfg = write_config(
        tmp_path / "gates.json",
        parts=[
            {"name": "base", "assign": {"slot": 3}},
            {"name": "stripe", "assign": {"mix": "11", "mix_src": "AA"}},
        ],
    )
    done = run_cli(
        tmp_path,
        "gcode-mix-gate",
        str(plate),
        "--config",
        str(cfg),
        "--parts",
        str(write_parts(tmp_path)),
        "--mix-teeth",
    )
    assert done.returncode == 1
    assert "TEETH FAILED" in done.stdout and "NOT CAUGHT" in done.stdout


def test_bad_invocations_say_what_to_fix(tmp_path: Path) -> None:
    """A user forgets the config, then points at a missing G-code, then asks for help."""
    plate = tmp_path / "plate.gcode"
    plate.write_text(stripe_gcode(), encoding="ascii")

    no_cfg = run_cli(tmp_path, "gcode-mix-gate", str(plate))
    assert no_cfg.returncode == 2 and "--config GATES.json is required" in no_cfg.stderr

    missing = run_cli(
        tmp_path,
        "gcode-mix-gate",
        "nope.gcode",
        "--config",
        str(write_config(tmp_path / "g.json")),
    )
    assert missing.returncode == 2 and "file not found" in missing.stderr

    bad = run_cli(
        tmp_path,
        "gcode-mix-gate",
        str(plate),
        "--config",
        str(tmp_path / "g.json"),
        "--margin",
        "-1",
    )
    assert bad.returncode == 2 and "--margin" in bad.stderr

    helped = run_cli(tmp_path, "gcode-mix-gate", "--help")
    assert helped.returncode == 0
    assert "--mix-teeth" in helped.stdout
    assert "--cavity-teeth" in helped.stdout
