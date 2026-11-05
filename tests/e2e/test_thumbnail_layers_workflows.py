"""Whatever way a G-code reaches the printer, it carries its thumbnails: slice, preflight, upload, start.

Real `bin/3d` runs against a fake slicer binary and a fake Moonraker server; no real printer or slicer."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import sys
import zipfile
from pathlib import Path

from printer_fakes import FakeMoonraker, write_devices
from thumbnail_fixtures import ALL_SIZES, bare, block_sizes, complete, plate_project

from .workflow_helper import run_cli, run_cli_with_hatch

FAKE_SLICER = """#!__PYTHON__
import sys
from pathlib import Path
args = sys.argv[1:]
Path(__file__ + ".argv").write_text(" ".join(args))
out = Path(args[args.index("--output") + 1]) if "--output" in args else Path(args[args.index("--outputdir") + 1]) / "plate_1.gcode"
out.write_text(__GCODE__)
print("estimated printing time: 1h 02m")
"""


def _fake_slicer(tmp_path: Path) -> Path:
    exe = tmp_path / "fake-slicer"
    exe.write_text(
        FAKE_SLICER.replace("__PYTHON__", sys.executable).replace(
            "__GCODE__",
            repr(bare(tmp_path / "template.gcode").read_text(encoding="ascii")),
        ),
        encoding="utf-8",
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


def test_slice_output_has_every_thumbnail_without_a_separate_step(
    tmp_path: Path,
) -> None:
    """A user slices with the slicer CLI that writes no preview and checks the file afterwards."""
    model = tmp_path / "part.stl"
    model.write_text("solid x\nendsolid x\n")
    out = tmp_path / "part.gcode"

    sliced = run_cli(
        tmp_path,
        "slice",
        str(model),
        "-o",
        str(out),
        env_extra={"SLICER": str(_fake_slicer(tmp_path))},
    )
    checked = run_cli(tmp_path, "gcode-thumbnails", "check", str(out))

    assert sliced.returncode == 0, sliced.stdout + sliced.stderr
    assert "thumbnails: present" in sliced.stdout
    assert (
        "--thumbnails 48x48,96x96,300x300"
        in (tmp_path / "fake-slicer.argv").read_text()
    )  # layer (a), Prusa form
    assert checked.returncode == 0, checked.stdout
    assert sorted(block_sizes(out)) == sorted(ALL_SIZES)


def test_slice_dry_run_keeps_nothing_and_renders_nothing(tmp_path: Path) -> None:
    """A user previews a slice with --dry-run: no G-code is written and no thumbnail is drawn or announced."""
    model = tmp_path / "part.stl"
    model.write_text("solid x\nendsolid x\n")
    dry = run_cli(
        tmp_path,
        "slice",
        str(model),
        "--dry-run",
        env_extra={"SLICER": str(_fake_slicer(tmp_path))},
    )
    assert dry.returncode == 0 and "thumbnails:" not in dry.stdout
    assert not (tmp_path / "part.gcode").exists()


def test_slice_of_a_3mf_project_asks_the_slicer_for_the_sizes_and_leaves_the_input_alone(
    tmp_path: Path,
) -> None:
    """A user slices a project 3MF: the slicer is asked for the thumbnail sizes through a temporary copy
    and the 3MF on disk stays byte for byte as it was."""
    project = plate_project(tmp_path / "p.3mf")
    before = project.read_bytes()
    slicer = tmp_path / "spy-slicer"
    slicer.write_text(
        f"#!{sys.executable}\nimport sys, zipfile, json\nfrom pathlib import Path\n"
        "a = sys.argv[1:]\nz = zipfile.ZipFile(a[-1])\n"
        "Path(a[a.index('--outputdir') + 1], 'seen.json').write_text(z.read('Metadata/project_settings.config').decode())\n"
        f"Path(a[a.index('--outputdir') + 1], 'plate_1.gcode').write_text({bare(tmp_path / 't.gcode').read_text()!r})\n"
        "if '--output' in a: raise SystemExit(2)\n",
        encoding="utf-8",
    )
    slicer.chmod(slicer.stat().st_mode | stat.S_IXUSR)
    out = tmp_path / "p.gcode"

    sliced = run_cli(
        tmp_path,
        "slice",
        str(project),
        "-o",
        str(out),
        env_extra={"SLICER": str(slicer)},
    )

    assert sliced.returncode == 0, sliced.stdout + sliced.stderr
    assert project.read_bytes() == before  # the user's project is never rewritten
    assert sorted(block_sizes(out)) == sorted(ALL_SIZES)


def test_preflight_names_the_fix_and_fix_applies_it(tmp_path: Path) -> None:
    """A user runs the preflight on a bare G-code, reads the command it names, then lets --fix do it."""
    gcode = bare(tmp_path / "plate one.gcode")
    gate = run_cli(tmp_path, "print-preflight", str(gcode), "--offline", "--json")
    lines = {c["name"]: c for c in json.loads(gate.stdout)["checks"]}
    assert lines["thumbnails"]["status"] == "FAIL"
    assert f"3d gcode-thumbnails add '{gcode}'" in lines["thumbnails"]["detail"]
    assert block_sizes(gcode) == []  # the preflight itself never writes

    fixed = run_cli(
        tmp_path, "print-preflight", str(gcode), "--offline", "--json", "--fix"
    )
    lines = {c["name"]: c for c in json.loads(fixed.stdout)["checks"]}
    assert lines["thumbnails"]["status"] == "PASS"
    assert "--fix: added" in fixed.stderr
    assert sorted(block_sizes(gcode)) == sorted(ALL_SIZES)


def test_upload_adds_the_missing_thumbnails_and_the_printer_receives_them(
    tmp_path: Path,
) -> None:
    """The G-code of a headless slice is uploaded: the bytes on the (fake) printer carry all three sizes."""
    gcode = bare(tmp_path / "part.gcode")
    moon = FakeMoonraker()
    with moon.serve():
        write_devices(tmp_path / "xdg-config", moon=moon)
        up = run_cli(tmp_path, "printer", "upload", "u1", "part.gcode")

    assert up.returncode == 0, up.stdout + up.stderr
    assert (
        "thumbnails:" in up.stdout
        and "added in place" in up.stdout
        and "sha256" in up.stdout
    )
    ((remote, body),) = moon.contents.items()
    assert re.fullmatch(r"part-[0-9a-f]{16}\.gcode", remote)
    assert (
        body == gcode.read_bytes()
    )  # what the printer got is the fixed file, byte for byte
    received = tmp_path / "received.gcode"
    received.write_bytes(body)
    assert sorted(block_sizes(received)) == sorted(ALL_SIZES)


def test_upload_of_a_complete_gcode_is_untouched_and_silent(tmp_path: Path) -> None:
    """A user uploads a G-code that already has its previews: the file is not touched, nothing is announced."""
    gcode = complete(tmp_path / "part.gcode")
    before = gcode.read_bytes()
    moon = FakeMoonraker()
    with moon.serve():
        write_devices(tmp_path / "xdg-config", moon=moon)
        up = run_cli(tmp_path, "printer", "upload", "u1", "part.gcode")
    assert up.returncode == 0 and "thumbnails:" not in up.stdout
    assert gcode.read_bytes() == before
    assert list(moon.contents.values()) == [before]


def test_start_adds_thumbnails_too_and_asks_for_the_upload_of_the_new_bytes(
    tmp_path: Path,
) -> None:
    """A file uploaded before this feature has none: `start` fixes FILE, so the stored copy is stale
    (content-addressed name) and the user is told to upload again. Then the whole chain passes."""
    gcode = bare(tmp_path / "part.gcode")
    old_remote = f"part-{hashlib.sha256(gcode.read_bytes()).hexdigest()[:16]}.gcode"
    moon = FakeMoonraker(
        files={old_remote: gcode.stat().st_size},
        contents={old_remote: gcode.read_bytes()},
    )
    with moon.serve():
        write_devices(tmp_path / "xdg-config", moon=moon)
        first = run_cli(tmp_path, "printer", "start", "u1", "part.gcode", "--yes")
        assert not [r for r in moon.writes() if r.path == "/printer/print/start"]
        up = run_cli(tmp_path, "printer", "upload", "u1", "part.gcode")
        started = run_cli_with_hatch(
            tmp_path, "printer", "start", "u1", "part.gcode", "--yes", "--force"
        )

    assert "added in place" in first.stdout and first.returncode != 0
    assert "Upload it first" in first.stderr
    assert (
        up.returncode == 0 and "thumbnails:" not in up.stdout
    )  # the file already carries them
    assert started.returncode == 0, started.stdout + started.stderr
    assert re.search(r"PASS\s+thumbnails", started.stdout)
    assert sorted(block_sizes(gcode)) == sorted(ALL_SIZES)


def test_gcode_thumbnails_add_updates_the_plate_pictures_of_a_project(
    tmp_path: Path,
) -> None:
    """`add G --3mf P` draws the G-code thumbnails and the project's plate pictures from one render;
    `check P` says which pictures a project lacks."""
    gcode = bare(tmp_path / "plate_1.gcode")
    project = plate_project(tmp_path / "sliced.3mf")

    before = run_cli(tmp_path, "gcode-thumbnails", "check", str(project))
    added = run_cli(
        tmp_path, "gcode-thumbnails", "add", str(gcode), "--3mf", str(project)
    )
    after = run_cli(tmp_path, "gcode-thumbnails", "check", str(gcode), str(project))

    assert before.returncode == 1 and "missing" in before.stdout
    assert added.returncode == 0, added.stdout + added.stderr
    assert "added Metadata/plate_1.png" in added.stderr
    assert after.returncode == 0 and after.stdout.count("PASS") == 2
    with zipfile.ZipFile(project) as z:
        assert {"Metadata/plate_1.png", "Metadata/plate_1_small.png"} <= set(
            z.namelist()
        )


def test_help_documents_the_layers(tmp_path: Path) -> None:
    """A user reads the three --help pages and finds where thumbnails are added and how --fix does it."""
    thumbs = run_cli(tmp_path, "gcode-thumbnails", "--help")
    flight = run_cli(tmp_path, "print-preflight", "--help")
    printer = run_cli(tmp_path, "printer", "--help")
    assert "--3mf" in thumbs.stdout and "96x96" in thumbs.stdout
    assert "--fix" in flight.stdout and "gcode-thumbnails add" in flight.stdout
    assert "thumbnails" in printer.stdout and "in place" in printer.stdout
