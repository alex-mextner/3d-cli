"""`3d 3mf` and the pipe through `3d slice -`, `3d print-preflight -` and `3d printer start NAME -`
through the real CLI. The slicer is a stand-in script (SLICER=…) that writes a prepared G-code;
printers are loopback fakes only."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from overhang_fixtures import bridge, plate
from printer_fakes import FakeMoonraker, write_devices
from threemf_fixtures import three_parts

from .test_printer_workflows import U1_GCODE
from .workflow_helper import REPO_ROOT, THREED, isolated_env, run_cli

RANGES = '<objects>\n <object id="1">\n  <range min_z="0" max_z="2"></range>\n </object>\n</objects>\n'
# the repo fleet: `--printer k1se` resolves to its model "Creality K1 SE" (conftest disables it)
FLEET = {"THREED_FLEET": str(REPO_ROOT / "fleet" / "printers.yaml")}


def _pipe(
    tmp_path: Path, script: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    """SCRIPT in bash with pipefail; `3d` runs the CLI under test."""
    full = isolated_env(tmp_path)
    full.update(env or {})
    prelude = f'set -o pipefail\n3d() {{ "{sys.executable}" "{THREED}" "$@"; }}\n'
    return subprocess.run(
        ["bash", "-c", prelude + script],
        cwd=tmp_path,
        env=full,
        capture_output=True,
        timeout=180,
    )


def _ls(tmp_path: Path, path: str) -> dict[str, dict[str, Any]]:
    out = run_cli(tmp_path, "3mf", "ls", path, "--json")
    assert out.returncode == 0, out.stderr
    return {o["name"]: o for o in json.loads(out.stdout)["objects"]}


def test_a_pipe_picks_two_parts_turns_one_and_packs_them_on_a_k1_bed(
    tmp_path: Path,
) -> None:
    """The user reprints CN and SW of a three-part project on a 220 mm bed, CN standing on its front."""
    three_parts(tmp_path / "proj.3mf")
    run = _pipe(
        tmp_path,
        "3d 3mf pick proj.3mf 'CN,SW*' | 3d 3mf orient --face-down front --only CN "
        "| 3d 3mf arrange --bed 220x220 --margin 10 | 3d 3mf check > out.3mf",
    )
    err = run.stderr.decode()

    assert run.returncode == 0, err
    assert "3mf pick: kept 2 of 3 object(s): CN — CN, SW — SW -> stdout" in err
    assert "CN — CN: now 20.0 x 3.0 x 10.0 mm" in err
    assert "2 object(s) on one 220 x 220 mm plate (--bed)" in err
    assert (
        "SKIP  printer    no --printer; the project is set up for Snapmaker U1" in err
    )
    assert "3mf check: PASS" in err
    objs = _ls(tmp_path, "out.3mf")
    assert sorted(objs) == ["CN — CN", "SW — SW"]
    assert (
        objs["CN — CN"]["size_mm"] == [20.0, 3.0, 10.0]
        and objs["SW — SW"]["extruder"] == "2"
    )
    for o in objs.values():
        x, y, z = o["min_mm"]
        assert x >= 10 and y >= 10 and z == 0


def test_ls_reads_stdin_and_pick_lists_the_names_when_a_selector_misses(
    tmp_path: Path,
) -> None:
    three_parts(tmp_path / "proj.3mf")
    listed = _pipe(tmp_path, "3d 3mf ls < proj.3mf")
    missed = run_cli(tmp_path, "3mf", "pick", "proj.3mf", "CN,DOOR", "-o", "x.3mf")
    bad_face = run_cli(
        tmp_path, "3mf", "orient", "proj.3mf", "--face-down", "up", "-o", "x.3mf"
    )

    assert listed.returncode == 0, listed.stderr
    assert "stdin: 3 object(s); printer preset: Snapmaker U1" in listed.stdout.decode()
    assert missed.returncode == 2 and "no object matches 'DOOR'" in missed.stderr
    assert "Objects: CN — CN; ROOF; SW — SW" in missed.stderr
    assert not (tmp_path / "x.3mf").exists()
    assert (
        bad_face.returncode == 2
        and "accepted: top, bottom, front, back, left, right, largest"
        in bad_face.stderr
    )


def test_check_passes_nothing_on_when_the_project_is_for_another_printer(
    tmp_path: Path,
) -> None:
    """A U1 project piped toward the K1 SE stops at check; the next step gets an empty stdin."""
    three_parts(tmp_path / "proj.3mf")
    run = _pipe(
        tmp_path,
        "3d 3mf pick proj.3mf CN | 3d 3mf check --printer k1se | 3d 3mf ls",
        FLEET,
    )
    err = run.stderr.decode()

    assert run.returncode != 0
    assert (
        "FAIL  printer    the project is set up for Snapmaker U1, the target is Creality K1 SE"
        in err
    )
    assert "3mf check: FAIL" in err
    assert (
        "no 3MF on stdin (the command before this one in the pipe passed nothing on)"
        in err
    )
    assert run.stdout == b""


def test_arrange_for_another_printer_and_orient_over_layer_ranges_are_refused(
    tmp_path: Path,
) -> None:
    three_parts(
        tmp_path / "proj.3mf", extra={"Metadata/layer_config_ranges.xml": RANGES}
    )
    wrong = run_cli(
        tmp_path,
        "3mf",
        "arrange",
        "proj.3mf",
        "--printer",
        "k1se",
        "-o",
        "x.3mf",
        env_extra=FLEET,
    )
    ranged = run_cli(
        tmp_path, "3mf", "orient", "proj.3mf", "--rotate", "90,0,0", "-o", "x.3mf"
    )
    other = run_cli(
        tmp_path,
        "3mf",
        "orient",
        "proj.3mf",
        "--rotate",
        "90,0,0",
        "--only",
        "SW",
        "-o",
        "x.3mf",
    )
    too_small = run_cli(
        tmp_path,
        "3mf",
        "arrange",
        "proj.3mf",
        "--bed",
        "45x45",
        "--margin",
        "2",
        "-o",
        "y.3mf",
    )

    assert (
        wrong.returncode == 1
        and "the project is set up for Snapmaker U1, --printer k1se is a Creality K1 SE"
        in wrong.stderr
    )
    assert (
        ranged.returncode == 2 and "CN — CN has height range modifiers" in ranged.stderr
    )
    assert other.returncode == 0, other.stderr
    assert too_small.returncode == 1
    assert "do not fit on one: CN — CN, SW — SW" in too_small.stderr
    assert not (tmp_path / "y.3mf").exists()


def _fake_slicer(tmp_path: Path, gcode: Path) -> Path:
    script = tmp_path / "fake-slicer"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, shutil, sys\n"
        "a = sys.argv[1:]\n"
        f"src = {str(gcode)!r}\n"
        "if '--output' in a:\n"
        "    shutil.copyfile(src, a[a.index('--output') + 1])\n"
        "else:\n"
        "    shutil.copyfile(src, os.path.join(a[a.index('--outputdir') + 1], 'plate_1.gcode'))\n"
        "print('[2026-10-17 10:00:00.0] [0x1] [info] sliced')\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_slice_from_a_pipe_keeps_warnings_and_project_for_print_preflight(
    tmp_path: Path,
) -> None:
    """`3d slice -` streams the G-code on; print-preflight on stdin still finds the slicer-warnings
    record and the source 3MF, and passes the G-code on unchanged."""
    three_parts(tmp_path / "proj.3mf")
    plate(
        tmp_path / "span.gcode", {"CN": bridge()}
    )  # the G-code's object is the picked CN
    thumbs = run_cli(
        tmp_path, "gcode-thumbnails", "add", "span.gcode", "--size", "48x48/PNG"
    )
    assert thumbs.returncode == 0, thumbs.stderr
    env = {"SLICER": str(_fake_slicer(tmp_path, tmp_path / "span.gcode"))}

    run = _pipe(
        tmp_path,
        "3d 3mf pick proj.3mf CN | 3d slice - | 3d print-preflight - > final.gcode",
        env,
    )
    err = run.stderr.decode()
    stash = sorted((tmp_path / "xdg-cache" / "3d-cli" / "pipe").glob("*/*"))

    assert run.returncode == 0, err
    assert (tmp_path / "final.gcode").read_bytes() == (
        tmp_path / "span.gcode"
    ).read_bytes()
    assert "slice: CN.3mf" in err and "STATUS: PASS - sliced OK -> stdout" in err
    assert "PASS  slicer warn the slicer reported no warnings" in err
    assert [p.name for p in stash] == ["CN.3mf", "CN.gcode", "CN.slice-warnings.json"]
    # the head-sequence gate read the picked project kept next to the stashed G-code
    assert (
        "SKIP  head seq    nothing to sequence" in err
        and "CN.3mf next to the G-code" in err
    )


def test_print_preflight_on_stdin_passes_nothing_on_when_it_fails(
    tmp_path: Path,
) -> None:
    (tmp_path / "part.gcode").write_text(U1_GCODE, encoding="utf-8")
    run = _pipe(tmp_path, "3d print-preflight - < part.gcode > out.gcode")
    as_json = _pipe(tmp_path, "3d print-preflight - --json < part.gcode")

    assert run.returncode == 1
    assert (tmp_path / "out.gcode").read_bytes() == b""
    assert (
        "STATUS: FAIL" in run.stderr.decode()
        and "the G-code is not passed on" in run.stderr.decode()
    )
    assert as_json.returncode == 1 and json.loads(as_json.stdout)["overall"] == "FAIL"


def test_printer_start_from_a_pipe_uploads_only_after_preflight(tmp_path: Path) -> None:
    """Started from a pipe, the G-code is checked first: a FAIL sends nothing to the printer; with
    --force it is uploaded, verified and started; without --yes nothing happens."""
    moon = FakeMoonraker()
    (tmp_path / "part.gcode").write_text(U1_GCODE, encoding="utf-8")
    with moon.serve():
        write_devices(tmp_path / "xdg-config", moon=moon)
        no_yes = _pipe(tmp_path, "3d printer start u1 - < part.gcode")
        refused = _pipe(tmp_path, "3d printer start u1 - --yes < part.gcode")
        writes_after_refusal = [r.path for r in moon.writes()]
        forced = _pipe(tmp_path, "cat part.gcode | 3d printer start u1 - --yes --force")
        empty = _pipe(tmp_path, "3d printer start u1 - --yes < /dev/null")

    out = forced.stdout.decode()
    assert no_yes.returncode == 2 and "needs --yes" in no_yes.stderr.decode()
    assert (
        refused.returncode == 1
        and "preflight FAIL; start refused" in refused.stderr.decode()
    )
    assert writes_after_refusal == []
    assert forced.returncode == 0, forced.stderr.decode()
    assert (
        "uploaded pipe-" in out
        and "read-back sha256 matches" in out
        and "start acknowledged by u1: pipe-" in out
    )
    assert out.index("PREFLIGHT") < out.index("uploaded pipe-")
    assert [r.path for r in moon.writes()] == [
        "/server/files/upload",
        "/printer/print/start",
    ]
    assert empty.returncode == 2 and "no G-code on stdin" in empty.stderr.decode()
