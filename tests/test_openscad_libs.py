"""Unit tests for lib/registries/openscad_libs.py — where libraries go and what install/uninstall
may touch."""
from __future__ import annotations

import os
from pathlib import Path

from registries import openscad_libs as ol
import pytest
from errors import InvalidArgument, ThreeDError, UsageError


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("darwin", "home/Documents/OpenSCAD/libraries"),
        ("win32", "home/Documents/OpenSCAD/libraries"),
        ("linux", "data/OpenSCAD/libraries"),
    ],
)
def test_user_library_dir_matches_openscad_per_os(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str, expected: str
) -> None:
    monkeypatch.setattr(ol.sys, "platform", platform)
    monkeypatch.setattr(ol.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert ol.user_library_dir() == tmp_path / expected


def test_linux_user_library_dir_defaults_to_local_share(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ol.sys, "platform", "linux")
    monkeypatch.setattr(ol.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert ol.user_library_dir() == tmp_path / ".local/share/OpenSCAD/libraries"


def test_openscadpath_copy_shadows_the_user_library_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    user, extra = tmp_path / "user", tmp_path / "extra"
    (user / "WriteText").mkdir(parents=True)
    (extra / "WriteText").mkdir(parents=True)
    monkeypatch.setattr(ol, "user_library_dir", lambda: user)
    monkeypatch.setenv("OPENSCADPATH", os.pathsep.join([str(extra), str(user)]))

    assert ol.search_path() == [extra, user]
    assert ol.locate("WriteText") == extra / "WriteText"


def test_registry_lookup_is_case_insensitive_and_unknown_names_list_known_ones() -> None:
    assert ol.resolve_spec("writetext").name == "WriteText"
    with pytest.raises(InvalidArgument) as err:
        ol.resolve_spec("NoSuchLib")
    assert "WriteText" in err.value.accepted
    assert ol.resolve_spec("NoSuchLib", git="https://example.com/x.git").git == "https://example.com/x.git"


def test_install_refuses_a_folder_it_did_not_install_unless_forced(tmp_path: Path) -> None:
    mine = tmp_path / "WriteText"
    mine.mkdir()
    (mine / "hand-made.scad").write_text("// user's own copy\n", encoding="utf-8")
    spec = ol.resolve_spec("WriteText")

    with pytest.raises(UsageError):
        ol.install(spec, tmp_path)
    assert (mine / "hand-made.scad").exists()

    res = ol.install(spec, tmp_path, force=True)
    assert not (mine / "hand-made.scad").exists()
    assert (res.path / "WriteText.scad").is_file()


def test_uninstall_removes_only_recorded_files(tmp_path: Path) -> None:
    res = ol.install(ol.resolve_spec("WriteText"), tmp_path)
    (res.path / "my-notes.txt").write_text("keep me\n", encoding="utf-8")

    path, count, leftovers = ol.uninstall("WriteText", tmp_path)

    assert count >= 3
    assert leftovers == [str(path)]
    assert [p.name for p in path.iterdir()] == ["my-notes.txt"]


def test_uninstall_of_a_foreign_folder_is_refused(tmp_path: Path) -> None:
    (tmp_path / "BOSL2").mkdir()
    with pytest.raises(ThreeDError, match="not installed by 3d"):
        ol.uninstall("BOSL2", tmp_path)
    assert (tmp_path / "BOSL2").is_dir()


@pytest.mark.parametrize("name", ["../MyLib", "a/b", "/tmp/pwn", "..", ".hidden", "a\\b", ""])
def test_names_that_escape_the_library_folder_are_rejected(name: str, tmp_path: Path) -> None:
    with pytest.raises(InvalidArgument):
        ol.resolve_spec(name, git="https://example.com/x.git")
    with pytest.raises(InvalidArgument):
        ol.uninstall(name, tmp_path)


def test_uninstall_ignores_manifest_entries_outside_the_library(tmp_path: Path) -> None:
    outside = tmp_path / "precious.txt"
    outside.write_text("keep\n", encoding="utf-8")
    res = ol.install(ol.resolve_spec("WriteText"), tmp_path / "libs")
    manifest = res.path / ol.MANIFEST
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace('"LICENSE"', '"../../precious.txt"'),
        encoding="utf-8",
    )

    ol.uninstall("WriteText", tmp_path / "libs")

    assert outside.read_text(encoding="utf-8") == "keep\n"


def test_ref_is_refused_for_a_library_shipped_with_3d() -> None:
    with pytest.raises(UsageError, match="--ref only applies to git libraries"):
        ol.resolve_spec("WriteText", ref="v1")
