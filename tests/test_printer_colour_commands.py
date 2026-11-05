"""`3d print-preflight --colour-delta-e/--fix`, `3d printer start --fix-colour` and `3d printer slot set`
against a loopback fake U1 (tests/printer_fakes.py); the printer is never real. Issue
https://github.com/alex-mextner/3d-cli/issues/728."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from colour_fix_fixtures import LOADED, SLICED, gcode_text, load_beige_petg, write_plate
from fleet_fixtures import u1_entry, write_fleet
from printer_fakes import FakeMoonraker, u1_status, write_devices

from errors import GateFailure, InvalidArgument, UsageError
from printer_ops import preflight
from printer_ops.base import PrinterRefused, safe_remote_name
from slicing import slice_warnings as sw


@pytest.fixture()
def moon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeMoonraker]:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    fake = FakeMoonraker()
    load_beige_petg(fake)
    with fake.serve():
        write_devices(tmp_path / "xdg", moon=fake)
        yield fake


@pytest.fixture()
def gates_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """The thumbnail / overhang / slicer-warning gates have their own tests: pin them to PASS."""
    monkeypatch.setattr(
        preflight,
        "check_thumbnails",
        lambda p, shown=None: preflight.Check("thumbnails", "PASS", "stub"),
    )
    monkeypatch.setattr(
        preflight,
        "check_overhangs",
        lambda p, *_: preflight.Check("overhangs", "PASS", "stub"),
    )
    monkeypatch.setattr(
        preflight,
        "check_slice_warnings",
        lambda o, g: preflight.Check("slicer warn", "PASS", "stub"),
    )


def preflight_cli(*argv: str) -> int:
    from commands.print_preflight import run

    return run(list(argv))


def printer(*argv: str) -> int:
    from commands.printer import run

    return run(list(argv))


# ---- print-preflight ---------------------------------------------------------------------


def test_preflight_passes_the_beige_pair_and_shows_the_distance(
    moon: FakeMoonraker,
    tmp_path: Path,
    gates_ok: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    g = write_plate(tmp_path)
    assert preflight_cli(str(g), "--printer", "u1") == 0
    out = capsys.readouterr().out
    assert "slot T3" in out and "ΔE 6.4" in out and f"G-code {SLICED}" in out
    assert moon.writes() == []


def test_preflight_threshold_option_and_validation(
    moon: FakeMoonraker,
    tmp_path: Path,
    gates_ok: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    g = write_plate(tmp_path)
    with pytest.raises(GateFailure):
        preflight_cli(str(g), "--printer", "u1", "--colour-delta-e", "5")
    assert (
        f"colour: G-code {SLICED}, loaded {LOADED} (ΔE 6.4 > 5)"
        in capsys.readouterr().out
    )
    with pytest.raises(InvalidArgument):
        preflight_cli(str(g), "--printer", "u1", "--colour-delta-e", "0")


def test_preflight_fails_a_different_colour_and_a_wrong_material(
    moon: FakeMoonraker,
    tmp_path: Path,
    gates_ok: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    other = tmp_path / "other.gcode"
    other.write_text(gcode_text("#0000FF"), encoding="utf-8")
    with pytest.raises(GateFailure):
        preflight_cli(str(other), "--printer", "u1")
    assert "G-code #0000FF, loaded #FEE5A5 (ΔE " in capsys.readouterr().out
    petg_as_pla = tmp_path / "pla.gcode"
    petg_as_pla.write_text(gcode_text(SLICED, "PLA"), encoding="utf-8")
    with pytest.raises(GateFailure):
        preflight_cli(str(petg_as_pla), "--printer", "u1")
    assert "material: G-code PLA, loaded PETG" in capsys.readouterr().out


def test_preflight_fix_rewrites_then_passes_and_prints_old_and_new_sha(
    moon: FakeMoonraker,
    tmp_path: Path,
    gates_ok: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    g = write_plate(tmp_path)
    old = sw.sha256_file(g)
    assert preflight_cli(str(g), "--printer", "u1", "--fix") == 0
    cap = capsys.readouterr()
    assert (
        f"{SLICED} -> {LOADED}" in cap.err
        and old in cap.err
        and sw.sha256_file(g) in cap.err
    )
    assert f"G-code {LOADED}" not in cap.out  # exact match now: no note
    assert "slot T3" in cap.out and "PASS" in cap.out
    assert f"; filament_colour = #000000;#808080;#1B5E20;{LOADED}" in g.read_text(
        encoding="utf-8"
    )
    assert moon.writes() == []  # --fix writes files, never the printer

    assert preflight_cli(str(g), "--printer", "u1", "--fix") == 0
    assert "already equal" in capsys.readouterr().err


def test_preflight_fix_json_stays_machine_readable(
    moon: FakeMoonraker,
    tmp_path: Path,
    gates_ok: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    g = write_plate(tmp_path)
    assert preflight_cli(str(g), "--printer", "u1", "--fix", "--json") == 0
    cap = capsys.readouterr()
    assert any(c["name"] == "slot T3" for c in json.loads(cap.out)["checks"])
    assert "sha256 old" in cap.err


def test_preflight_fix_refuses_another_colour_and_aligns_colours_only_with_a_live_printer(
    moon: FakeMoonraker,
    tmp_path: Path,
    gates_ok: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    g = tmp_path / "blue.gcode"
    g.write_text(gcode_text("#0000FF"), encoding="utf-8")
    before = g.read_bytes()
    with pytest.raises(PrinterRefused, match="another colour"):
        preflight_cli(str(g), "--printer", "u1", "--fix")
    assert g.read_bytes() == before
    # widening the limit is an explicit, per-run decision
    assert (
        preflight_cli(str(g), "--printer", "u1", "--fix", "--fix-max-delta-e", "100")
        == 0
    )
    assert LOADED in g.read_text(encoding="utf-8")
    g.write_bytes(before)
    capsys.readouterr()
    # no live printer (none, or --offline): the colours have nothing to align to, the rest of --fix still runs
    for argv in (("--axis-limits", "u1"), ("--printer", "u1", "--offline")):
        assert preflight_cli(str(g), *argv, "--fix") == 0
        assert "colours were not aligned" in capsys.readouterr().err
        assert g.read_bytes() == before


# ---- printer start --fix-colour ----------------------------------------------------------


def test_start_fix_colour_rewrites_uploads_the_fixed_file_and_starts(
    moon: FakeMoonraker,
    tmp_path: Path,
    gates_ok: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    g = write_plate(tmp_path)
    old_name = safe_remote_name(g)
    assert printer("start", "u1", str(g), "--yes", "--fix-colour") == 0
    out = capsys.readouterr().out
    assert f"{SLICED} -> {LOADED}" in out and "uploaded the colour-fixed file" in out
    new_name = safe_remote_name(g)
    assert (
        new_name != old_name and new_name in moon.files and old_name not in moon.files
    )
    assert (
        moon.contents[new_name] == g.read_bytes()
        and LOADED.encode() in moon.contents[new_name]
    )
    assert [(r.path, r.query.get("filename")) for r in moon.writes()] == [
        ("/server/files/upload", None),
        ("/printer/print/start", [new_name]),
    ]
    assert "slot T3" in out and "start acknowledged by u1" in out


def test_start_fix_colour_refuses_another_colour_before_writing_anything(
    moon: FakeMoonraker, tmp_path: Path, gates_ok: None
) -> None:
    g = tmp_path / "blue.gcode"
    g.write_text(gcode_text("#0000FF"), encoding="utf-8")
    before = g.read_bytes()
    with pytest.raises(PrinterRefused, match="another colour"):
        printer("start", "u1", str(g), "--yes", "--fix-colour")
    assert g.read_bytes() == before and moon.writes() == []


def test_start_without_fix_colour_passes_the_beige_without_rewriting(
    moon: FakeMoonraker,
    tmp_path: Path,
    gates_ok: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    g = write_plate(tmp_path)
    moon.put(safe_remote_name(g), g.read_bytes())
    before = g.read_bytes()
    assert printer("start", "u1", str(g), "--yes") == 0
    assert "ΔE 6.4" in capsys.readouterr().out and g.read_bytes() == before


def test_start_fix_colour_is_refused_while_printing(
    moon: FakeMoonraker, tmp_path: Path, gates_ok: None
) -> None:
    g = write_plate(tmp_path)
    before = g.read_bytes()
    moon.status = u1_status("printing", "x.gcode")
    load_beige_petg(moon)
    with pytest.raises(PrinterRefused, match="printing"):
        printer("start", "u1", str(g), "--yes", "--fix-colour")
    assert g.read_bytes() == before and moon.writes() == []


# ---- printer slot set --------------------------------------------------------------------


@pytest.fixture()
def fleet_moon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[FakeMoonraker]:
    fake = FakeMoonraker()
    with fake.serve():
        monkeypatch.setenv(
            "THREED_FLEET", str(write_fleet(tmp_path / "fleet.yaml", u1_entry(fake)))
        )
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        monkeypatch.chdir(tmp_path)
        yield fake


def test_slot_set_writes_the_head_and_reads_it_back(
    fleet_moon: FakeMoonraker, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        printer(
            "slot",
            "set",
            "u1",
            "t4",
            "--material",
            "PETG",
            "--color",
            "fee5a5",
            "--brand",
            "Elegoo",
        )
        == 0
    )
    assert (
        "u1/T4 now shows PETG #FEE5A5 Elegoo Basic (read back)"
        in capsys.readouterr().out
    )
    (script,) = fleet_moon.scripts
    assert (
        script.startswith("SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=3 ")
        and 'VENDOR="Elegoo"' in script
    )
    assert "FILAMENT_COLOR_RGBA=FEE5A5FF" in script and 'FILAMENT_TYPE="PETG"' in script


def test_slot_set_dry_run_sends_nothing_and_predicts_refusals(
    fleet_moon: FakeMoonraker, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        printer(
            "slot",
            "set",
            "u1",
            "T4",
            "--material",
            "PETG",
            "--color",
            "#FEE5A5",
            "--dry-run",
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "FEE5A5" in out
    assert fleet_moon.writes() == []

    fleet_moon.status = u1_status("printing", "x.gcode")
    with pytest.raises(GateFailure):
        printer(
            "slot",
            "set",
            "u1",
            "T4",
            "--material",
            "PETG",
            "--color",
            "#FEE5A5",
            "--dry-run",
        )
    assert "WOULD BE REFUSED" in capsys.readouterr().out and fleet_moon.writes() == []


def test_slot_set_refuses_a_running_job_a_read_only_printer_and_bad_arguments(
    tmp_path: Path, fleet_moon: FakeMoonraker, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet_moon.status = u1_status("printing", "x.gcode")
    with pytest.raises(PrinterRefused, match="never done during a job"):
        printer("slot", "set", "u1", "T4", "--material", "PETG", "--color", "#FEE5A5")
    assert fleet_moon.scripts == []

    monkeypatch.setenv(
        "THREED_FLEET",
        str(
            write_fleet(tmp_path / "ro.yaml", u1_entry(fleet_moon, access="read-only"))
        ),
    )
    with pytest.raises(PrinterRefused, match="read-only"):
        printer(
            "slot",
            "set",
            "u1",
            "T4",
            "--material",
            "PETG",
            "--color",
            "#FEE5A5",
            "--dry-run",
        )
    assert fleet_moon.scripts == []

    with pytest.raises(UsageError, match="not a head label"):
        printer("slot", "set", "u1", "4", "--material", "PETG", "--color", "#FEE5A5")
    with pytest.raises(UsageError, match="needs the printer and the head"):
        printer("slot", "set", "u1")
    with pytest.raises(UsageError, match="unknown slot action"):
        printer("slot", "wipe", "u1")


def test_slot_show_lists_the_heads(
    fleet_moon: FakeMoonraker, capsys: pytest.CaptureFixture[str]
) -> None:
    assert printer("slot", "show", "u1") == 0
    out = capsys.readouterr().out
    assert "T4" in out and "PLA" in out and fleet_moon.writes() == []
