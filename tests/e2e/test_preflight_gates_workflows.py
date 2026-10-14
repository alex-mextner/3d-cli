"""The mandatory G-code gates through the real CLI: `3d gcode-overhangs`, `3d slice-warnings`,
the overhang, slicer-warning, head-sequence and visible-faces steps of `3d print-preflight`,
and `3d printer start` refusing a plate that would print in the air. Printers are loopback
fakes only."""

from __future__ import annotations

import json
from pathlib import Path

from fit_fixtures import box, write_insert, write_manifold
from head_sequence_fixtures import gcode as head_gcode
from head_sequence_fixtures import project_3mf
from overhang_fixtures import bridge, floating, plate
from printer_fakes import FakeMoonraker, write_devices

from .workflow_helper import run_cli

LOG = (
    "[2026-10-14 13:30:55.575588] [0x1] [warning] cli mode, Current Snapmaker_Orca Version 01.10.01.50\n"
    "[2026-10-14 13:30:55.643576] [0x1] [warning] no filament colors found in projects\n"
    "[2026-10-14 13:30:57.667134] [0x1] [error]   init opengl failed! skip thumbnail generating\n"
)


def _checks(stdout: str) -> dict[str, dict[str, object]]:
    return {c["name"]: c for c in json.loads(stdout)["checks"]}


def test_gcode_overhangs_fails_a_slab_in_the_air_and_passes_a_bridge(tmp_path: Path) -> None:
    """A user slices an overlay whose bottom is not flat; the check names the object, the
    island heights and writes a map."""
    plate(tmp_path / "ovl.gcode", {"OVL1": floating()})
    plate(tmp_path / "span.gcode", {"SPAN": bridge()})

    bad = run_cli(tmp_path, "gcode-overhangs", "ovl.gcode", "--png", "ovl.png")
    good = run_cli(tmp_path, "gcode-overhangs", "span.gcode", "--json", "--no-png")
    wrong = run_cli(tmp_path, "gcode-overhangs", "ovl.gcode", "--bridge-max", "0")

    assert bad.returncode == 1, bad.stderr
    assert "FAIL OVL1:" in bad.stdout and "printed on nothing" in bad.stdout and "island z0.4" in bad.stdout
    assert (tmp_path / "ovl.png").read_bytes()[:4] == b"\x89PNG"
    assert good.returncode == 0, good.stderr
    assert json.loads(good.stdout)["status"] == "PASS"
    assert wrong.returncode == 2 and "--bridge-max" in wrong.stderr


def test_print_preflight_runs_every_gcode_gate_and_says_what_is_skipped(tmp_path: Path) -> None:
    """A user preflights a freshly sliced overlay: the overhang gate names the floating object,
    the slicer-warnings gate says no record exists, and the gates without input say what to pass."""
    plate(tmp_path / "ovl.gcode", {"OVL1": floating()})

    result = run_cli(tmp_path, "print-preflight", "ovl.gcode")
    as_json = run_cli(tmp_path, "print-preflight", "ovl.gcode", "--json")

    assert result.returncode == 1
    assert "FAIL  overhangs" in result.stdout and "FAIL OVL1:" in result.stdout
    assert "FAIL  slicer warn no ovl.slice-warnings.json" in result.stdout
    assert "SKIP  head seq    no project 3MF" in result.stdout
    assert "SKIP  visible     no --visible spec" in result.stdout
    checks = _checks(as_json.stdout)
    assert checks["overhangs"]["status"] == "FAIL" and checks["overhangs"]["lines"]


def test_slicer_warnings_block_preflight_until_each_is_accepted(tmp_path: Path) -> None:
    """The slicer's log is recorded for the G-code; preflight FAILs on the open warning,
    passes once it is accepted with a reason, and FAILs again when the G-code is re-sliced."""
    plate(tmp_path / "span.gcode", {"SPAN": bridge()})
    (tmp_path / "slice.log").write_text(LOG, encoding="utf-8")

    rec = run_cli(tmp_path, "slice-warnings", "record", "span.gcode", "--log", "slice.log")
    blocked = run_cli(tmp_path, "print-preflight", "span.gcode", "--json")
    wid = rec.stdout.split("\n")[1].split()[0]
    acc = run_cli(tmp_path, "slice-warnings", "accept", "span.gcode", wid, "colours are set per head on the printer")
    passed = run_cli(tmp_path, "print-preflight", "span.gcode", "--json")
    (tmp_path / "span.gcode").write_text((tmp_path / "span.gcode").read_text() + "G1 X1 Y1\n", encoding="utf-8")
    stale = run_cli(tmp_path, "slice-warnings", "check", "span.gcode")
    no_source = run_cli(tmp_path, "slice-warnings", "record", "span.gcode")

    assert rec.returncode == 0, rec.stderr
    assert "recorded 1 slicer warning(s)" in rec.stdout and "2 host-noise line(s) ignored" in rec.stdout
    assert "no filament colors found in projects  [OPEN]" in rec.stdout
    gate = _checks(blocked.stdout)["slicer warn"]
    assert blocked.returncode == 1 and gate["status"] == "FAIL" and "1 of 1" in str(gate["detail"])
    assert acc.returncode == 0, acc.stderr
    gate = _checks(passed.stdout)["slicer warn"]  # the plate has no thumbnails: overall still FAIL
    assert gate["status"] == "PASS" and "accepted: colours are set per head" in str(gate["lines"])
    assert stale.returncode == 1 and "stale" in stale.stdout
    assert no_source.returncode == 2 and "no slicer output" in no_source.stderr


def test_print_preflight_runs_head_sequence_on_the_sibling_project(
    tmp_path: Path,
) -> None:
    """Issue #103: plate.3mf sits next to plate.gcode; object B printed off its mix pattern fails."""
    project_3mf(tmp_path / "plate.3mf", {"A": 5, "B": 5})
    head_gcode(
        tmp_path / "plate.gcode",
        [{"A": 4 if i % 2 == 0 else 1, "B": 1} for i in range(6)],
    )
    good_dir = tmp_path / "good"
    good_dir.mkdir()
    project_3mf(good_dir / "plate.3mf", {"A": 5})
    head_gcode(
        good_dir / "plate.gcode", [{"A": 4 if i % 2 == 0 else 1} for i in range(6)]
    )

    bad = run_cli(tmp_path, "print-preflight", "plate.gcode")
    good = run_cli(tmp_path, "print-preflight", "good/plate.gcode", "--json")

    assert bad.returncode == 1
    assert (
        "FAIL  head seq    1 object(s) off their recipes (plate.3mf next to the G-code)"
        in bad.stdout
    )
    assert "FAIL B:" in bad.stdout
    head = _checks(good.stdout)["head seq"]
    assert head["status"] == "PASS" and "1 object(s) on their recipes" in str(
        head["detail"]
    )


def test_printer_start_refuses_a_plate_that_would_print_in_the_air(
    tmp_path: Path,
) -> None:
    """`printer start` checks a snapshot copy; the slicer-warnings sidecar is still found next
    to the original G-code."""
    moon = FakeMoonraker()
    plate(tmp_path / "ovl.gcode", {"OVL1": floating()})
    (tmp_path / "slice.log").write_text("[2026-10-14 13:30:55.0] [0x1] [info] sliced\n", encoding="utf-8")
    rec = run_cli(tmp_path, "slice-warnings", "record", "ovl.gcode", "--log", "slice.log")
    with moon.serve():
        write_devices(tmp_path / "xdg-config", moon=moon)
        up = run_cli(tmp_path, "printer", "upload", "u1", "ovl.gcode")
        start = run_cli(tmp_path, "printer", "start", "u1", "ovl.gcode", "--yes")

    assert rec.returncode == 0 and up.returncode == 0, rec.stderr + up.stderr
    assert start.returncode == 1
    assert "FAIL  overhangs" in start.stdout and "preflight FAIL; start refused" in start.stderr
    assert "PASS  slicer warn the slicer reported no warnings" in start.stdout
    assert [r.path for r in moon.writes()] == ["/server/files/upload"]


def test_fit_check_refuses_a_socket_capped_over_the_insert(tmp_path: Path) -> None:
    """A rebuild roofs over the pocket: the printed insert could never be dropped in."""
    write_insert(tmp_path / "INSERT.stl")
    write_manifold(
        tmp_path / "CAPPED.stl", box(40.0, 30.0, 8.0) - box(20.4, 10.4, 5.2, z0=2.0)
    )

    capped = run_cli(
        tmp_path,
        "fit-check",
        "--insert",
        "INSERT.stl",
        "--socket",
        "CAPPED.stl",
        "--gap",
        "0.2",
    )
    captured = run_cli(
        tmp_path,
        "fit-check",
        "--insert",
        "INSERT.stl",
        "--socket",
        "CAPPED.stl",
        "--gap",
        "0.2",
        "--open",
        "none",
    )

    assert capped.returncode == 1
    assert "NOT open above" in capped.stdout and "FAIL not open above:" in capped.stdout
    assert captured.returncode == 0, captured.stdout + captured.stderr
