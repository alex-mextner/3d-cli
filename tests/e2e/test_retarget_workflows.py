"""Readable e2e workflows for `3d retarget`: a U1 project 3MF moved to a K1-like printer through
the real `bin/3d`, with synthetic projects/presets and a fake slicer (no real printer or slicer)."""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

from retarget_fixtures import TWO, Box, make_project, write_presets

from .workflow_helper import run_cli

def _env(tmp_path: Path) -> dict[str, str]:
    return {"THREED_PROFILES_DIR": str(tmp_path / "no-profiles")}


def _setup(tmp_path: Path, boxes: list[Box] = TWO) -> tuple[Path, Path]:
    return make_project(tmp_path / "spoon.3mf", boxes, filaments=2), write_presets(tmp_path / "presets")


def _settings(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as zf:
        data: dict[str, Any] = json.loads(zf.read("Metadata/project_settings.config"))
    return data


def test_help_and_no_args_print_usage(tmp_path: Path) -> None:
    helped = run_cli(tmp_path, "retarget", "--help")
    assert helped.returncode == 0
    for flag in ("--to", "--keep", "--margin", "--process", "--filament", "--preset", "--set", "--preset-dir", "--gcode", "--dry-run"):
        assert flag in helped.stdout
    assert run_cli(tmp_path, "retarget").returncode == 1


def test_one_object_is_retargeted_validated_and_reported(tmp_path: Path) -> None:
    src, presets = _setup(tmp_path)
    before = src.read_bytes()
    out = tmp_path / "out" / "spoon.k1.3mf"
    res = run_cli(
        tmp_path, "retarget", str(src), "--to", "testk1", "--preset-dir", str(presets), "--keep", "1",
        "--preset", "easy-release-supports", "--set", "support_interface_spacing=0.4", "-o", str(out),
        env_extra=_env(tmp_path),
    )
    assert res.returncode == 0, res.stderr
    assert "VALIDATION: PASS" in res.stdout and "RESULT: PASS" in res.stdout
    assert "machine  preset: Test K1 0.4 nozzle  [extra]" in res.stdout
    assert "object   1: left (object 2)  -> KEPT" in res.stdout and "object   2: right (object 4)  -> dropped" in res.stdout
    assert "printable_height: 270.05 -> 250" in res.stdout
    assert "support_top_z_distance: 0.12 -> 0.2" in res.stdout  # from the preset
    assert "support_interface_spacing: 0.5 -> 0.4" in res.stdout  # from --set, applied last
    assert re.search(r"in \d+\.\d s", res.stdout)
    s = _settings(out)
    assert s["printable_area"] == ["0x0", "220x0", "220x220", "0x220"] and s["nozzle_diameter"] == ["0.4"]
    assert len(s["filament_settings_id"]) == 1
    assert s["support_interface_spacing"] == "0.4" and s["time_lapse_gcode"] == ""
    assert src.read_bytes() == before, "the source project must never be modified"


def test_default_output_sits_next_to_the_input(tmp_path: Path) -> None:
    src, presets = _setup(tmp_path)
    res = run_cli(tmp_path, "retarget", str(src), "--to", "testk1", "--preset-dir", str(presets), "--keep", "2", env_extra=_env(tmp_path))
    assert res.returncode == 0, res.stderr
    assert (tmp_path / "spoon.testk1.3mf").is_file()


def test_part_that_cannot_fit_exits_1_with_the_numbers_and_writes_nothing(tmp_path: Path) -> None:
    src, presets = _setup(tmp_path, [Box("slab", (260, 250, 10))])
    out = tmp_path / "slab.k1.3mf"
    res = run_cli(tmp_path, "retarget", str(src), "--to", "testk1", "--preset-dir", str(presets), "-o", str(out), env_extra=_env(tmp_path))
    assert res.returncode == 1
    assert "does not fit Test K1 0.4 nozzle" in res.stderr
    assert "usable 210 x 210" in res.stderr and "bed 220 x 220" in res.stderr
    assert not out.exists()
    # A smaller margin cannot rescue a 260 mm part on a 220 mm bed.
    again = run_cli(tmp_path, "retarget", str(src), "--to", "testk1", "--preset-dir", str(presets), "--margin", "0", "--dry-run", env_extra=_env(tmp_path))
    assert again.returncode == 1 and "even with no margin" in again.stderr


def test_ambiguous_printer_name_lists_the_others_it_could_have_meant(tmp_path: Path) -> None:
    src, presets = _setup(tmp_path)
    res = run_cli(tmp_path, "retarget", str(src), "--to", "test", "--preset-dir", str(presets), "--keep", "1", "--dry-run", env_extra=_env(tmp_path))
    assert res.returncode == 0, res.stderr
    assert "machine  preset: Test K1 0.4 nozzle" in res.stdout
    assert "also matched: Test Dual 0.4 nozzle" in res.stdout


def test_a_failed_run_keeps_a_file_that_was_already_at_the_output_path(tmp_path: Path) -> None:
    src, presets = _setup(tmp_path, [Box("slab", (260, 250, 10))])
    out = tmp_path / "keep-me.3mf"
    out.write_bytes(b"an earlier good retarget")
    res = run_cli(tmp_path, "retarget", str(src), "--to", "testk1", "--preset-dir", str(presets), "-o", str(out), env_extra=_env(tmp_path))
    assert res.returncode == 1
    assert out.read_bytes() == b"an earlier good retarget"
    assert not list(tmp_path.glob(".*.staging"))


def test_output_may_not_overwrite_the_source_project(tmp_path: Path) -> None:
    src, presets = _setup(tmp_path)
    before = src.read_bytes()
    res = run_cli(tmp_path, "retarget", str(src), "--to", "testk1", "--preset-dir", str(presets), "-o", str(src), env_extra=_env(tmp_path))
    assert res.returncode == 2 and "never overwritten" in res.stderr
    assert src.read_bytes() == before


def test_keeping_objects_from_two_plates_exits_2_before_any_fit(tmp_path: Path) -> None:
    src = make_project(tmp_path / "plates.3mf", [Box("a", (40, 40, 10)), Box("b", (50, 30, 10))], plates=((1,), (2,)))
    presets = write_presets(tmp_path / "presets")
    res = run_cli(tmp_path, "retarget", str(src), "--to", "testk1", "--preset-dir", str(presets), env_extra=_env(tmp_path))
    assert res.returncode == 2 and "one plate" in res.stderr


def test_dry_run_reports_but_writes_nothing(tmp_path: Path) -> None:
    src, presets = _setup(tmp_path)
    res = run_cli(tmp_path, "retarget", str(src), "--to", "testk1", "--preset-dir", str(presets), "--keep", "1", "--dry-run", env_extra=_env(tmp_path))
    assert res.returncode == 0 and "dry run, nothing written" in res.stdout
    assert not list(tmp_path.glob("*.testk1.3mf"))


def test_bad_inputs_exit_2_with_a_remedy(tmp_path: Path) -> None:
    src, presets = _setup(tmp_path)
    base = ["retarget", str(src), "--preset-dir", str(presets)]
    env = _env(tmp_path)
    unknown_printer = run_cli(tmp_path, *base, "--to", "no-such-printer", env_extra=env)
    assert unknown_printer.returncode == 2 and "no-such-printer" in unknown_printer.stderr and "Test K1 0.4 nozzle" in unknown_printer.stderr
    bad_key = run_cli(tmp_path, *base, "--to", "testk1", "--set", "support_top_z_distanc=1", env_extra=env)
    assert bad_key.returncode == 2 and "support_top_z_distance" in bad_key.stderr
    bad_keep = run_cli(tmp_path, *base, "--to", "testk1", "--keep", "9", env_extra=env)
    assert bad_keep.returncode == 2 and "1..2" in bad_keep.stderr
    bad_preset = run_cli(tmp_path, *base, "--to", "testk1", "--preset", "nope", env_extra=env)
    assert bad_preset.returncode == 2 and "easy-release-supports" in bad_preset.stderr
    no_target = run_cli(tmp_path, "retarget", str(src), env_extra=env)
    assert no_target.returncode == 2 and "--to" in no_target.stderr
    missing = run_cli(tmp_path, "retarget", str(tmp_path / "gone.3mf"), "--to", "testk1", "--preset-dir", str(presets), env_extra=env)
    assert missing.returncode == 2 and "gone.3mf" in missing.stderr


def _fake_slicer(tmp_path: Path, gcode_x: int) -> Path:
    """An Orca-CLI stand-in: `-g` (the PrusaSlicer probe `3d slice` tries first for an unknown
    slicer) fails; `--slice 0 --outputdir DIR in.3mf` writes a one-move G-code at X=`gcode_x`."""
    script = tmp_path / "fake-slicer.sh"
    script.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do [ "$a" = "-g" ] && exit 1; done\n'
        'while [ $# -gt 0 ]; do [ "$1" = "--outputdir" ] && out="$2"; shift; done\n'
        f"printf '; printable_area = 0x0,220x0,220x220,0x220\\n;LAYER_CHANGE\\nG1 X{gcode_x} Y100 Z0.2 F3000\\nG1 X{gcode_x} Y120 E1\\n' > \"$out/plate_1.gcode\"\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_gcode_flag_slices_the_result_and_bounds_checks_it(tmp_path: Path) -> None:
    src, presets = _setup(tmp_path)
    slicer = _fake_slicer(tmp_path, gcode_x=100)
    res = run_cli(
        tmp_path, "retarget", str(src), "--to", "testk1", "--preset-dir", str(presets), "--keep", "1",
        "-o", str(tmp_path / "k1.3mf"), "--gcode", str(tmp_path / "k1.gcode"),
        env_extra={**_env(tmp_path), "SLICER": str(slicer)},
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert (tmp_path / "k1.gcode").is_file()
    assert "sliced OK" in res.stdout


def test_gcode_flag_fails_when_the_sliced_gcode_leaves_the_bed(tmp_path: Path) -> None:
    src, presets = _setup(tmp_path)
    slicer = _fake_slicer(tmp_path, gcode_x=400)  # 400 mm: far outside the 220 mm bed
    res = run_cli(
        tmp_path, "retarget", str(src), "--to", "testk1", "--preset-dir", str(presets), "--keep", "1",
        "-o", str(tmp_path / "k1.3mf"), "--gcode", str(tmp_path / "k1.gcode"),
        env_extra={**_env(tmp_path), "SLICER": str(slicer)},
    )
    assert res.returncode == 1, res.stdout + res.stderr
