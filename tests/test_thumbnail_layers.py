"""The thumbnail layers one by one (lib/slicing/thumbnails.py): each one alone gets a G-code from
"no thumbnails" / "some" to "all", and none of them touches a G-code that already has them."""

from __future__ import annotations

import json
import os
import stat
import sys
import zipfile
from pathlib import Path

import pytest
from thumbnail_fixtures import (
    ALL_SIZES,
    MACROS_ONLY,
    bare,
    block_sizes,
    complete,
    partial,
    plate_project,
)

import gcode_thumbnails as gt
from errors import GateFailure
from slicing import orca_cli, slice_warnings, thumbnails

FIXTURES = {"bare": bare, "partial": partial, "complete": complete}


# ------------------------------------------------------------------ (a) ask the slicer


def test_spec_for_adds_the_default_sizes_in_the_shape_the_preset_uses() -> None:
    assert thumbnails.spec_for({}) == "48x48/PNG, 96x96/PNG, 300x300/PNG"
    assert (
        thumbnails.spec_for({"thumbnails": "300x300/PNG"})
        == "300x300/PNG, 48x48/PNG, 96x96/PNG"
    )
    assert thumbnails.spec_for(
        {"thumbnails": ["230x110"], "thumbnails_format": "JPG"}
    ) == ["230x110/JPG", "48x48/PNG", "96x96/PNG", "300x300/PNG"]
    assert thumbnails.spec_for({"thumbnails": "48x48/PNG, 96x96/PNG, 300x300/PNG"}) == (
        "48x48/PNG, 96x96/PNG, 300x300/PNG"
    )
    assert (
        thumbnails.spec_for({"thumbnails": "nonsense"})
        == "48x48/PNG, 96x96/PNG, 300x300/PNG"
    )


def test_project_copy_changes_only_the_thumbnails_setting(tmp_path: Path) -> None:
    src = plate_project(tmp_path / "in.3mf")
    dst = tmp_path / "out.3mf"
    thumbnails.with_project_spec(src, dst)
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(dst) as b:
        assert a.namelist() == b.namelist()
        for name in a.namelist():
            if name != "Metadata/project_settings.config":
                assert a.read(name) == b.read(name)
        assert json.loads(b.read("Metadata/project_settings.config")) == {
            "layer_height": "0.2",
            "thumbnails": "48x48/PNG, 96x96/PNG, 300x300/PNG",
        }
    assert thumbnails.thumbnail_cfg(src) == {
        "thumbnails": "48x48/PNG, 96x96/PNG, 300x300/PNG"
    }


def test_project_without_settings_gets_no_override(tmp_path: Path) -> None:
    path = tmp_path / "bare.3mf"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", b"<model/>")
    assert thumbnails.thumbnail_cfg(path) == {}


def test_machine_preset_copy_asks_for_the_default_sizes(tmp_path: Path) -> None:
    src = tmp_path / "machine.json"
    src.write_text(json.dumps({"name": "M", "thumbnails": "48x48/PNG"}))
    dst = tmp_path / "machine.thumbnails.json"
    thumbnails.with_machine_spec(src, dst)
    assert json.loads(dst.read_text()) == {
        "name": "M",
        "thumbnails": "48x48/PNG, 96x96/PNG, 300x300/PNG",
    }
    assert (
        json.loads(src.read_text())["thumbnails"] == "48x48/PNG"
    )  # the user's preset stays as it is


def test_prusa_gets_the_sizes_as_cli_flags() -> None:
    assert thumbnails.PRUSA_ARGS == [
        "--thumbnails",
        "48x48,96x96,300x300",
        "--thumbnails-format",
        "PNG",
    ]


# ------------------------------------------------------------------ (b) draw what is missing


@pytest.mark.parametrize("kind", FIXTURES)
def test_finalize_gcode_gives_every_kind_of_input_all_the_sizes_once(
    tmp_path: Path, kind: str
) -> None:
    path = FIXTURES[kind](tmp_path / f"{kind}.gcode")
    before = path.read_bytes()
    pre_existing = block_sizes(path)

    result = thumbnails.finalize_gcode(path)

    assert result.ok and gt.check(str(path)).ok
    assert sorted(block_sizes(path)) == sorted(ALL_SIZES)  # all three, none twice
    after = path.read_bytes()
    assert after.endswith(
        before[before.index(b"G90") :]
    )  # the toolpath and config are untouched
    if kind == "complete":
        assert after == before and result.added == []
    if kind == "partial":
        assert pre_existing == [(48, 48)]
        first = before[
            before.index(b"; THUMBNAIL_BLOCK_START") : before.index(
                b"; THUMBNAIL_BLOCK_END"
            )
        ]
        assert first in after  # the 48x48 block that was there is byte-identical
        assert [(t.width, t.height) for t in result.added] == [(96, 96), (300, 300)]


def test_finalize_gcode_twice_changes_nothing_the_second_time(tmp_path: Path) -> None:
    path = bare(tmp_path / "a.gcode")
    thumbnails.finalize_gcode(path)
    once, mtime = path.read_bytes(), path.stat().st_mtime_ns
    thumbnails.finalize_gcode(path)
    assert path.read_bytes() == once and path.stat().st_mtime_ns == mtime


def test_finalize_gcode_without_a_spec_adds_the_default_sizes(tmp_path: Path) -> None:
    path = bare(tmp_path / "a.gcode", spec=None)
    thumbnails.finalize_gcode(path)
    assert sorted(block_sizes(path)) == sorted(ALL_SIZES)


def test_a_macro_only_gcode_still_gets_its_thumbnails(tmp_path: Path) -> None:
    """Nothing is extruded, so there is nothing to draw: a neutral tile stands in, the checks still pass."""
    path = bare(tmp_path / "shaper.gcode", body=MACROS_ONLY)
    thumbnails.finalize_gcode(path)
    assert gt.check(str(path)).ok and sorted(block_sizes(path)) == sorted(ALL_SIZES)


def test_finalize_gcode_failure_names_the_repair_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = bare(tmp_path / "a b.gcode")

    def boom(*_a: object, **_k: object) -> None:
        raise gt.ThumbnailError("disk full")

    monkeypatch.setattr(gt, "ensure", boom)
    with pytest.raises(GateFailure) as err:
        thumbnails.finalize_gcode(path)
    assert "disk full" in err.value.message
    assert f"3d gcode-thumbnails add '{path}'" in err.value.remediation[0]


def test_plate_images_are_added_to_a_project_without_them_once(tmp_path: Path) -> None:
    gcode = bare(tmp_path / "plate_1.gcode")
    project = plate_project(tmp_path / "sliced.3mf")
    stamp = (1_700_000_000, 1_700_000_000)
    os.utime(project, stamp)

    thumbnails.finalize_slice(gcode, project)

    with zipfile.ZipFile(project) as z:
        names = z.namelist()
        assert (
            names.count("Metadata/plate_1.png") == 1
            and names.count("Metadata/plate_1_small.png") == 1
        )
        assert z.read("Metadata/plate_1.png")[:8] == b"\x89PNG\r\n\x1a\n"
        assert b'Extension="png"' in z.read("[Content_Types].xml")
        assert b"/Metadata/plate_1.png" in z.read("_rels/.rels")
        assert z.read("3D/3dmodel.model") == b"<model/>"
    assert (
        project.stat().st_mtime == stamp[1]
    )  # the project must not look newer than the G-code sliced from it
    assert thumbnails.missing_plate_images(project) == []

    snapshot = project.read_bytes()
    thumbnails.finalize_slice(gcode, project)
    assert project.read_bytes() == snapshot  # idempotent


def test_a_project_that_already_has_its_plate_pictures_is_not_rewritten(
    tmp_path: Path,
) -> None:
    gcode = complete(tmp_path / "plate_2.gcode")
    png = b"\x89PNG\r\n\x1a\nORIGINAL"
    project = plate_project(
        tmp_path / "p.3mf",
        members={"Metadata/plate_2.png": png, "Metadata/plate_2_small.png": png},
    )
    before = project.read_bytes()
    thumbnails.finalize_slice(gcode, project)  # plate number comes from the file name
    assert project.read_bytes() == before


def test_a_corrupt_plate_picture_is_replaced_not_duplicated(tmp_path: Path) -> None:
    gcode = bare(tmp_path / "plate_1.gcode")
    project = plate_project(
        tmp_path / "p.3mf", members={"Metadata/plate_1.png": b"not a png"}
    )
    thumbnails.finalize_slice(gcode, project)
    with zipfile.ZipFile(project) as z:
        assert z.namelist().count("Metadata/plate_1.png") == 1
        assert z.read("Metadata/plate_1.png")[:4] == b"\x89PNG"


def test_embedded_plate_gcode_feeds_the_pictures_of_a_gcode_3mf(tmp_path: Path) -> None:
    gcode = bare(tmp_path / "g.gcode")
    project = plate_project(
        tmp_path / "x.gcode.3mf", members={"Metadata/plate_1.gcode": gcode.read_bytes()}
    )
    assert not thumbnails.check_plate_images(project).ok
    assert thumbnails.finalize_embedded(project).ok
    assert thumbnails.check_plate_images(project).ok


# ------------------------------------------------------------------ the slicer wrapper (orca_cli)

FAKE_ORCA = """#!__PYTHON__
import json, os, sys, zipfile
a = sys.argv[1:]
out = a[a.index("--outputdir") + 1]
with zipfile.ZipFile(a[-1]) as z:
    cfg = json.loads(z.read("Metadata/project_settings.config"))
open(os.path.join(out, "seen-thumbnails.json"), "w").write(json.dumps(cfg.get("thumbnails")))
open(os.path.join(out, "plate_1.gcode"), "w").write(__GCODE__)
with zipfile.ZipFile(os.path.join(out, "sliced.3mf"), "w") as z:
    z.writestr("Metadata/slice_info.config", "<config/>")
    z.writestr("[Content_Types].xml", "<Types></Types>")
    z.writestr("_rels/.rels", "<Relationships></Relationships>")
print("[warning] cli mode")
"""


@pytest.fixture
def fake_orca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> orca_cli.OrcaCli:
    monkeypatch.setenv(
        "THREED_HEAVY_HELD", "1"
    )  # as inside the machine lock: no flock prefix
    text = bare(tmp_path / "template.gcode").read_text(encoding="ascii")
    exe = tmp_path / "fake-orca"
    exe.write_text(FAKE_ORCA.replace("__PYTHON__", sys.executable).replace("__GCODE__", repr(text)))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return orca_cli.OrcaCli(exe, "test")


def test_slice_project_asks_for_the_sizes_and_returns_a_gcode_that_has_them(
    tmp_path: Path, fake_orca: orca_cli.OrcaCli
) -> None:
    project = plate_project(tmp_path / "p.3mf")
    out = orca_cli.slice_project(project, tmp_path / "work", orca=fake_orca)

    assert out.ok and out.gcode is not None
    assert (
        json.loads((tmp_path / "work" / "seen-thumbnails.json").read_text())
        == "48x48/PNG, 96x96/PNG, 300x300/PNG"
    )
    assert gt.check(str(out.gcode)).ok and sorted(block_sizes(out.gcode)) == sorted(
        ALL_SIZES
    )
    assert (
        out.sliced_3mf is not None
        and thumbnails.missing_plate_images(out.sliced_3mf) == []
    )


def test_slice_project_fails_loudly_when_the_thumbnails_cannot_be_added(
    tmp_path: Path, fake_orca: orca_cli.OrcaCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        gt,
        "ensure",
        lambda *_a, **_k: (_ for _ in ()).throw(gt.ThumbnailError("no space")),
    )
    out = orca_cli.slice_project(
        plate_project(tmp_path / "p.3mf"), tmp_path / "work", orca=fake_orca
    )
    assert not out.ok and out.failure is not None and out.failure.kind == "thumbnails"
    assert "3d gcode-thumbnails add" in out.failure.remediation[0]


# ------------------------------------------------------------------ (c) before a printer gets the file


def test_fix_in_place_adds_the_sizes_and_the_slice_warnings_record_follows(
    tmp_path: Path,
) -> None:
    path = bare(tmp_path / "part.gcode")
    slice_warnings.record(path, logs=[("slicer log", "[warning] thin wall")])
    old_sha = slice_warnings.load(slice_warnings.sidecar_path(path))["gcode_sha256"]

    fixed = thumbnails.fix_in_place(path)

    assert fixed is not None and len(fixed.added) == 3
    record = slice_warnings.load(slice_warnings.sidecar_path(path))
    assert record["gcode_sha256"] != old_sha
    steps = record["post_processed"]
    assert isinstance(steps, list) and steps[-1]["from_sha256"] == old_sha
    assert thumbnails.fix_in_place(path) is None  # nothing left to do


def test_fix_in_place_leaves_a_complete_gcode_alone(tmp_path: Path) -> None:
    path = complete(tmp_path / "part.gcode")
    before = path.read_bytes()
    assert thumbnails.fix_in_place(path) is None
    assert path.read_bytes() == before
