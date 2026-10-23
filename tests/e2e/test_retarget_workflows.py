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
    """A user asks `3d retarget --help` and sees every flag; a bare `3d retarget` prints usage and fails."""
    helped = run_cli(tmp_path, "retarget", "--help")
    assert helped.returncode == 0
    for flag in ("--to", "--keep", "--margin", "--process", "--filament", "--preset", "--set", "--preset-dir", "--gcode", "--dry-run"):
        assert flag in helped.stdout
    assert run_cli(tmp_path, "retarget").returncode == 1


def test_one_object_is_retargeted_validated_and_reported(tmp_path: Path) -> None:
    """A user runs `3d retarget` for this case: one object is retargeted validated and reported."""
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
    """A user runs `3d retarget` for this case: default output sits next to the input."""
    src, presets = _setup(tmp_path)
    res = run_cli(tmp_path, "retarget", str(src), "--to", "testk1", "--preset-dir", str(presets), "--keep", "2", env_extra=_env(tmp_path))
    assert res.returncode == 0, res.stderr
    assert (tmp_path / "spoon.testk1.3mf").is_file()


def test_part_that_cannot_fit_exits_1_with_the_numbers_and_writes_nothing(tmp_path: Path) -> None:
    """A user runs `3d retarget` for this case: part that cannot fit exits 1 with the numbers and writes nothing."""
    src, presets = _setup(tmp_path, [Box("slab", (260, 250, 10))])
    out = tmp_path / "slab.k1.3mf"
    res = run_cli(tmp_path, "retarget", str(src), "--to", "testk1", "--preset-dir", str(presets), "-o", str(out), env_extra=_env(tmp_path))
    assert res.returncode == 1
    assert "does not fit Test K1 0.4 nozzle" in res.stderr
    assert "usable 210 x 210" in res.stderr and "bed 220 x 220" in res.stderr
    assert not out.exists()
    # A smaller margin cannot rescue a 260 mm part on a 220 mm bed.
    again = run_cli(tmp_path, "retarget", str(src), "--to", "testk1", "--preset-dir", str(presets), "--margin", "0", "--dry-run", env_extra=_env(tmp_path))
    assert again.returncode == 1
    assert "even with no margin" in again.stderr


def test_ambiguous_printer_name_lists_the_others_it_could_have_meant(tmp_path: Path) -> None:
    """A user runs `3d retarget` for this case: ambiguous printer name lists the others it could have meant."""
    src, presets = _setup(tmp_path)
    res = run_cli(tmp_path, "retarget", str(src), "--to", "test", "--preset-dir", str(presets), "--keep", "1", "--dry-run", env_extra=_env(tmp_path))
    assert res.returncode == 0, res.stderr
    assert "machine  preset: Test K1 0.4 nozzle" in res.stdout
    assert "also matched: Test Dual 0.4 nozzle" in res.stdout


def test_a_failed_run_keeps_a_file_that_was_already_at_the_output_path(tmp_path: Path) -> None:
    """A user runs `3d retarget` for this case: a failed run keeps a file that was already at the output path."""
    src, presets = _setup(tmp_path, [Box("slab", (260, 250, 10))])
    out = tmp_path / "keep-me.3mf"
    out.write_bytes(b"an earlier good retarget")
    res = run_cli(tmp_path, "retarget", str(src), "--to", "testk1", "--preset-dir", str(presets), "-o", str(out), env_extra=_env(tmp_path))
    assert res.returncode == 1
    assert out.read_bytes() == b"an earlier good retarget"
    assert not list(tmp_path.glob(".*.staging"))


def test_output_may_not_overwrite_the_source_project(tmp_path: Path) -> None:
    """A user runs `3d retarget` for this case: output may not overwrite the source project."""
    src, presets = _setup(tmp_path)
    before = src.read_bytes()
    res = run_cli(tmp_path, "retarget", str(src), "--to", "testk1", "--preset-dir", str(presets), "-o", str(src), env_extra=_env(tmp_path))
    assert res.returncode == 2
    assert "never overwritten" in res.stderr
    assert src.read_bytes() == before


def test_keeping_objects_from_two_plates_exits_2_before_any_fit(tmp_path: Path) -> None:
    """A user runs `3d retarget` for this case: keeping objects from two plates exits 2 before any fit."""
    src = make_project(tmp_path / "plates.3mf", [Box("a", (40, 40, 10)), Box("b", (50, 30, 10))], plates=((1,), (2,)))
    presets = write_presets(tmp_path / "presets")
    res = run_cli(tmp_path, "retarget", str(src), "--to", "testk1", "--preset-dir", str(presets), env_extra=_env(tmp_path))
    assert res.returncode == 2
    assert "one plate" in res.stderr


def test_dry_run_reports_but_writes_nothing(tmp_path: Path) -> None:
    """A user runs `3d retarget` for this case: dry run reports but writes nothing."""
    src, presets = _setup(tmp_path)
    res = run_cli(tmp_path, "retarget", str(src), "--to", "testk1", "--preset-dir", str(presets), "--keep", "1", "--dry-run", env_extra=_env(tmp_path))
    assert res.returncode == 0
    assert "dry run, nothing written" in res.stdout
    assert not list(tmp_path.glob("*.testk1.3mf"))


def test_bad_inputs_exit_2_with_a_remedy(tmp_path: Path) -> None:
    """A user runs `3d retarget` for this case: bad inputs exit 2 with a remedy."""
    src, presets = _setup(tmp_path)
    base = ["retarget", str(src), "--preset-dir", str(presets)]
    env = _env(tmp_path)
    unknown_printer = run_cli(tmp_path, *base, "--to", "no-such-printer", env_extra=env)
    assert unknown_printer.returncode == 2
    assert "no-such-printer" in unknown_printer.stderr and "Test K1 0.4 nozzle" in unknown_printer.stderr
    bad_key = run_cli(tmp_path, *base, "--to", "testk1", "--set", "support_top_z_distanc=1", env_extra=env)
    assert bad_key.returncode == 2
    assert "support_top_z_distance" in bad_key.stderr
    bad_keep = run_cli(tmp_path, *base, "--to", "testk1", "--keep", "9", env_extra=env)
    assert bad_keep.returncode == 2
    assert "1..2" in bad_keep.stderr
    bad_preset = run_cli(tmp_path, *base, "--to", "testk1", "--preset", "nope", env_extra=env)
    assert bad_preset.returncode == 2
    assert "easy-release-supports" in bad_preset.stderr
    no_target = run_cli(tmp_path, "retarget", str(src), env_extra=env)
    assert no_target.returncode == 2
    assert "--to" in no_target.stderr
    missing = run_cli(tmp_path, "retarget", str(tmp_path / "gone.3mf"), "--to", "testk1", "--preset-dir", str(presets), env_extra=env)
    assert missing.returncode == 2
    assert "gone.3mf" in missing.stderr


def _fake_slicer(tmp_path: Path, gcode_x: int) -> Path:
    """An Orca-CLI stand-in. It insists on the recipe the real Snapmaker CLI needs: an `.stl`
    (never a project 3MF), `--load-settings machine;process` and `--load-filaments` files that
    are valid JSON naming the machine, and `--arrange 0 --orient 0`. It writes a one-move G-code
    at X=`gcode_x` into `--outputdir`."""
    script = tmp_path / "fake-slicer.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, struct, sys\n"
        "a = sys.argv[1:]\n"
        "get = lambda k: a[a.index(k) + 1]\n"
        "src = a[-1]\n"
        "assert src.endswith('.stl'), 'project 3mf given: ' + src\n"
        "data = open(src, 'rb').read()\n"
        "n = struct.unpack('<I', data[80:84])[0]\n"
        "assert n > 0 and len(data) == 84 + 50 * n, 'bad binary stl'\n"
        "machine, process = get('--load-settings').split(';')\n"
        "m, p, f = json.load(open(machine)), json.load(open(process)), json.load(open(get('--load-filaments')))\n"
        "assert p['compatible_printers'] == [m['name']] == f['compatible_printers']\n"
        "assert m['printable_area'] == ['0x0', '220x0', '220x220', '0x220'] and p['layer_height']\n"
        "assert get('--arrange') == '0' and get('--orient') == '0'\n"
        f"open(get('--outputdir') + '/plate_1.gcode', 'w').write('; printable_area = 0x0,220x0,220x220,0x220\\n;LAYER_CHANGE\\nG1 X{gcode_x} Y100 Z0.2 F3000\\nG1 X{gcode_x} Y120 E1\\n')\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_gcode_flag_slices_the_result_and_bounds_checks_it(tmp_path: Path) -> None:
    """A user runs `3d retarget` for this case: gcode flag slices the result and bounds checks it."""
    src, presets = _setup(tmp_path)
    slicer = _fake_slicer(tmp_path, gcode_x=100)
    res = run_cli(
        tmp_path, "retarget", str(src), "--to", "testk1", "--preset-dir", str(presets), "--keep", "1",
        "-o", str(tmp_path / "k1.3mf"), "--gcode", str(tmp_path / "k1.gcode"),
        env_extra={**_env(tmp_path), "SLICER": str(slicer)},
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert (tmp_path / "k1.gcode").is_file()
    assert "sliced OK" in res.stdout and "print area" in res.stdout


def test_gcode_flag_fails_when_the_sliced_gcode_leaves_the_bed(tmp_path: Path) -> None:
    """A user runs `3d retarget` for this case: gcode flag fails when the sliced gcode leaves the bed."""
    src, presets = _setup(tmp_path)
    slicer = _fake_slicer(tmp_path, gcode_x=400)  # 400 mm: far outside the 220 mm bed
    res = run_cli(
        tmp_path, "retarget", str(src), "--to", "testk1", "--preset-dir", str(presets), "--keep", "1",
        "-o", str(tmp_path / "k1.3mf"), "--gcode", str(tmp_path / "k1.gcode"),
        env_extra={**_env(tmp_path), "SLICER": str(slicer)},
    )
    assert res.returncode == 1, res.stdout + res.stderr
    assert "print area" in res.stdout


def test_gcode_flag_reports_a_slicer_that_refuses(tmp_path: Path) -> None:
    """A user runs `3d retarget` for this case: gcode flag reports a slicer that refuses."""
    src, presets = _setup(tmp_path)
    slicer = tmp_path / "refuse.sh"
    slicer.write_text("#!/bin/sh\necho 'run found error, return -17' >&2\nexit 239\n", encoding="utf-8")
    slicer.chmod(0o755)
    res = run_cli(
        tmp_path, "retarget", str(src), "--to", "testk1", "--preset-dir", str(presets), "--keep", "1",
        "--gcode", str(tmp_path / "k1.gcode"), env_extra={**_env(tmp_path), "SLICER": str(slicer)},
    )
    assert res.returncode == 1
    assert "slicing failed (exit 239" in res.stderr and "return -17" in res.stderr


def test_gcode_flag_records_the_slicer_warnings_sidecar(tmp_path: Path) -> None:
    """A user slices with --gcode and gets the slicer-warnings sidecar print-preflight needs."""
    src, presets = _setup(tmp_path)
    slicer = _fake_slicer(tmp_path, gcode_x=100)
    gcode = tmp_path / "k1.gcode"
    res = run_cli(
        tmp_path, "retarget", str(src), "--to", "testk1", "--preset-dir", str(presets), "--keep", "1",
        "-o", str(tmp_path / "k1.3mf"), "--gcode", str(gcode),
        env_extra={**_env(tmp_path), "SLICER": str(slicer)},
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert (tmp_path / "k1.slice-warnings.json").is_file() and "slicer warnings:" in res.stdout


def test_gcode_path_may_not_overwrite_either_project(tmp_path: Path) -> None:
    """A user mistypes --gcode as the source or the output project; both are refused and nothing is overwritten."""
    src, presets = _setup(tmp_path)
    before = src.read_bytes()
    out = tmp_path / "k1.3mf"
    base = ["retarget", str(src), "--to", "testk1", "--preset-dir", str(presets), "--keep", "1", "-o", str(out)]
    env = _env(tmp_path)
    onto_source = run_cli(tmp_path, *base, "--gcode", str(src), env_extra=env)
    onto_output = run_cli(tmp_path, *base, "--gcode", str(out), env_extra=env)
    assert onto_source.returncode == 2
    assert onto_output.returncode == 2
    assert src.read_bytes() == before and not out.exists()
