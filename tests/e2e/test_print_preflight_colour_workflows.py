"""E2E stories through `bin/3d print-preflight` for the 2026-11-04 SIMBOL print: beige PETG sliced as
#E8D8B0 on a U1 that shows it as #FEE5A5. The slot colour is judged by CIEDE2000 distance (no `--force`
for a same-family beige), and `--fix` rewrites the files to the printer's colour. Loopback fakes only."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from colour_fix_fixtures import (
    LOADED,
    SLICED,
    THUMBNAILS,
    gcode_text,
    load_beige_petg,
    write_plate,
)
from printer_fakes import FakeMoonraker, write_devices
from slicing import slice_warnings as sw

from .workflow_helper import run_cli


def slot_line(stdout: str) -> str:
    return next(line for line in stdout.splitlines() if "slot T3" in line)


def test_preflight_accepts_the_same_beige_and_prints_the_distance(
    tmp_path: Path,
) -> None:
    """The user preflights the SIMBOL plate: the slot line passes and says how far the colours are."""
    moon = FakeMoonraker()
    load_beige_petg(moon)
    g = write_plate(tmp_path)
    with moon.serve():
        write_devices(tmp_path / "xdg-config", moon=moon)
        default = run_cli(tmp_path, "print-preflight", str(g), "--printer", "u1")
        strict = run_cli(
            tmp_path,
            "print-preflight",
            str(g),
            "--printer",
            "u1",
            "--colour-delta-e",
            "5",
        )

    assert slot_line(default.stdout).split()[0] == "PASS" and "ΔE 6.4" in slot_line(
        default.stdout
    )
    assert slot_line(strict.stdout).split()[
        0
    ] == "FAIL" and "(ΔE 6.4 > 5)" in slot_line(strict.stdout)
    assert moon.writes() == []


def test_preflight_still_fails_a_wrong_material_and_a_different_colour(
    tmp_path: Path,
) -> None:
    moon = FakeMoonraker()
    load_beige_petg(moon)
    pla = tmp_path / "pla.gcode"
    pla.write_text(gcode_text(SLICED, "PLA"), encoding="utf-8")
    blue = tmp_path / "blue.gcode"
    blue.write_text(gcode_text("#0000FF"), encoding="utf-8")
    with moon.serve():
        write_devices(tmp_path / "xdg-config", moon=moon)
        wrong_material = run_cli(
            tmp_path, "print-preflight", str(pla), "--printer", "u1"
        )
        wrong_colour = run_cli(
            tmp_path, "print-preflight", str(blue), "--printer", "u1"
        )
        refused_fix = run_cli(
            tmp_path, "print-preflight", str(blue), "--printer", "u1", "--fix"
        )

    assert (
        slot_line(wrong_material.stdout).split()[0] == "FAIL"
        and "material: G-code PLA, loaded PETG" in wrong_material.stdout
    )
    assert slot_line(wrong_colour.stdout).split()[0] == "FAIL" and "ΔE" in slot_line(
        wrong_colour.stdout
    )
    assert refused_fix.returncode == 1 and "another colour" in refused_fix.stderr
    assert blue.read_text(encoding="utf-8") == gcode_text(
        "#0000FF"
    )  # nothing was rewritten


def test_preflight_fix_aligns_gcode_3mf_and_sidecars_and_prints_the_hashes(
    tmp_path: Path,
) -> None:
    """`--fix`: one command rewrites the colours at the source; old and new sha256 are on record."""
    moon = FakeMoonraker()
    load_beige_petg(moon)
    g = write_plate(tmp_path)
    old = sw.sha256_file(g)
    with moon.serve():
        write_devices(tmp_path / "xdg-config", moon=moon)
        fixed = run_cli(tmp_path, "print-preflight", str(g), "--printer", "u1", "--fix")

    assert f"colour fix: tool T3 (T4) {SLICED} -> {LOADED} (ΔE 6.4)" in fixed.stderr
    assert (
        old in fixed.stderr
        and sw.sha256_file(g) in fixed.stderr
        and "sha256 new" in fixed.stderr
    )
    assert (
        slot_line(fixed.stdout).split()[0] == "PASS" and "slicer warn" in fixed.stdout
    )
    assert f"; filament_colour = #000000;#808080;#1B5E20;{LOADED}" in g.read_text(
        encoding="utf-8"
    )
    with zipfile.ZipFile(g.with_suffix(".3mf")) as z:
        assert (
            json.loads(z.read("Metadata/project_settings.config"))["filament_colour"][3]
            == LOADED
        )
    assert (
        json.loads((tmp_path / "plate.manifest.json").read_text())["filaments"][3][
            "hex"
        ]
        == LOADED
    )
    assert sw.load(sw.sidecar_path(g))["gcode_sha256"] == sw.sha256_file(g)
    assert [line for line in fixed.stdout.splitlines() if "slicer warn" in line][
        0
    ].split()[0] == "PASS"
    assert moon.writes() == []


def test_one_fix_adds_the_missing_thumbnails_and_aligns_the_colours(
    tmp_path: Path,
) -> None:
    """A G-code without previews, sliced in another beige than the printer's: one `--fix` repairs both and the
    preflight passes; its thumbnails gate would have FAILed before."""
    moon = FakeMoonraker()
    load_beige_petg(moon)
    g = write_plate(tmp_path)
    g.write_text(gcode_text().replace(THUMBNAILS, ""), encoding="utf-8")
    sw.record(g, logs=[("slice.log", "")])
    with moon.serve():
        write_devices(tmp_path / "xdg-config", moon=moon)
        plain = run_cli(tmp_path, "print-preflight", str(g), "--printer", "u1")
        fixed = run_cli(tmp_path, "print-preflight", str(g), "--printer", "u1", "--fix")
        offline = run_cli(
            tmp_path, "print-preflight", str(g), "--axis-limits", "u1", "--fix"
        )

    assert plain.returncode == 1 and "FAIL  thumbnails" in plain.stdout
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr
    assert "--fix: added 48x48/PNG, 96x96/PNG, 300x300/PNG" in fixed.stderr
    assert f"colour fix: tool T3 (T4) {SLICED} -> {LOADED}" in fixed.stderr
    text = g.read_text(encoding="utf-8")
    assert "; thumbnail begin 300x300" in text and LOADED in text
    assert sw.load(sw.sidecar_path(g))["gcode_sha256"] == sw.sha256_file(g)
    # nothing to align to without a live printer: the colours stay, stderr says so, the thumbnails are done
    assert offline.returncode == 0 and "colours were not aligned" in offline.stderr
