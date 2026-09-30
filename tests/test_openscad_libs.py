"""Unit tests for lib/registries/openscad_libs.py — where libraries go and what install/uninstall
may touch."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from cli import paths
from registries import openscad_libs as ol
import pytest
from errors import InvalidArgument, ThreeDError, UsageError

LIB_DIR = Path(__file__).resolve().parents[1] / "lib"


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Locks and git clones live under the 3d data dir; keep them out of the real one."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path_factory.mktemp("xdg-data")))


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


def test_failed_reinstall_leaves_the_install_and_its_manifest_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    res = ol.install(ol.resolve_spec("WriteText"), tmp_path / "libs")
    before = {p.name: p.read_bytes() for p in res.path.iterdir()}
    monkeypatch.setattr(ol, "repo_root", lambda: str(tmp_path / "no-checkout"))  # source gone

    with pytest.raises(ThreeDError, match="missing from this 3d checkout"):
        ol.update("WriteText", tmp_path / "libs")
    with pytest.raises(ThreeDError, match="missing from this 3d checkout"):
        ol.install(ol.resolve_spec("WriteText"), tmp_path / "libs", force=True)

    assert {p.name: p.read_bytes() for p in res.path.iterdir()} == before
    assert os.listdir(tmp_path / "libs") == ["WriteText"]  # no staging folder left behind


def _interrupt_landing(monkeypatch: pytest.MonkeyPatch, dest: Path, *, also_restore: bool = False) -> None:
    """Make os.rename raise KeyboardInterrupt when the staged new copy is renamed onto
    `dest` (the old copy is already aside), and with `also_restore` when the old copy is
    renamed back too (a second Ctrl-C)."""
    real_rename = os.rename

    def rename(src: str | Path, dst: str | Path) -> None:
        staged = Path(src).parent.name.startswith(f".{dest.name}.3d-staging-")
        if Path(dst) == dest and staged and (Path(src).name == dest.name or also_restore):
            raise KeyboardInterrupt
        real_rename(src, dst)

    monkeypatch.setattr(ol.os, "rename", rename)


def test_interrupted_swap_puts_the_old_install_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    res = ol.install(ol.resolve_spec("WriteText"), tmp_path)
    before = {p.name: p.read_bytes() for p in res.path.iterdir()}
    _interrupt_landing(monkeypatch, res.path)

    with pytest.raises(KeyboardInterrupt):
        ol.install(ol.resolve_spec("WriteText"), tmp_path, force=True)
    monkeypatch.undo()

    assert {p.name: p.read_bytes() for p in res.path.iterdir()} == before
    assert os.listdir(tmp_path) == ["WriteText"]


def test_swap_that_cannot_be_undone_says_where_the_old_install_is(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    res = ol.install(ol.resolve_spec("WriteText"), tmp_path)
    before = {p.name: p.read_bytes() for p in res.path.iterdir()}
    _interrupt_landing(monkeypatch, res.path, also_restore=True)

    with pytest.raises(ThreeDError, match="the previous copy is at ") as err:
        ol.install(ol.resolve_spec("WriteText"), tmp_path, force=True)
    monkeypatch.undo()

    kept = Path(str(err.value).rsplit("the previous copy is at ", 1)[1])
    assert {p.name: p.read_bytes() for p in kept.iterdir()} == before


def test_forced_reinstall_over_a_link_replaces_the_link_not_what_it_points_to(tmp_path: Path) -> None:
    real = ol.install(ol.resolve_spec("WriteText"), tmp_path / "elsewhere")
    (real.path / "notes.txt").write_text("keep me\n", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in real.path.iterdir()}
    (tmp_path / "libs").mkdir()
    (tmp_path / "libs" / "WriteText").symlink_to(real.path)

    again = ol.install(ol.resolve_spec("WriteText"), tmp_path / "libs", force=True)

    assert not again.path.is_symlink() and (again.path / "WriteText.scad").is_file()
    assert {p.name: p.read_bytes() for p in real.path.iterdir()} == before
    assert os.listdir(tmp_path / "libs") == ["WriteText"]


def test_reinstall_keeps_files_and_folders_you_added(tmp_path: Path) -> None:
    res = ol.install(ol.resolve_spec("WriteText"), tmp_path)
    (res.path / "notes.txt").write_text("keep me\n", encoding="utf-8")
    (res.path / "mine").mkdir()
    (res.path / "mine" / "part.scad").write_text("cube(1);\n", encoding="utf-8")
    (res.path / "scratch").mkdir()

    again = ol.install(ol.resolve_spec("WriteText"), tmp_path, force=True)

    assert (again.path / "notes.txt").read_text(encoding="utf-8") == "keep me\n"
    assert (again.path / "mine" / "part.scad").read_text(encoding="utf-8") == "cube(1);\n"
    assert (again.path / "scratch").is_dir()
    assert (again.path / "WriteText.scad").is_file()
    assert again.kept is None
    assert os.listdir(tmp_path) == ["WriteText"]


def test_reinstall_sets_aside_your_file_when_the_new_copy_ships_one_of_the_same_name(tmp_path: Path) -> None:
    res = ol.install(ol.resolve_spec("WriteText"), tmp_path)
    manifest_path = res.path / ol.MANIFEST
    # As if README.md were new upstream and the user had written their own before updating.
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].remove("README.md")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (res.path / "README.md").write_text("my readme\n", encoding="utf-8")

    again = ol.update("WriteText", tmp_path)

    assert (again.path / "README.md").read_text(encoding="utf-8") != "my readme\n"
    assert again.kept is not None
    assert (again.kept / "README.md").read_text(encoding="utf-8") == "my readme\n"
    assert "README.md" in json.loads((again.path / ol.MANIFEST).read_text(encoding="utf-8"))["files"]


def test_a_library_named_previous_can_be_reinstalled(tmp_path: Path) -> None:
    spec = ol.LibSpec(name="previous", summary="", homepage="", local="openscad-libs/WriteText")
    ol.install(spec, tmp_path)

    again = ol.install(spec, tmp_path, force=True)

    assert (again.path / "WriteText.scad").is_file()
    assert again.kept is None
    assert os.listdir(tmp_path) == ["previous"]


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


class _Run(threading.Thread):
    """Run `fn` in a thread, keeping its result or the exception it raised."""

    def __init__(self, fn: Callable[[], object]) -> None:
        super().__init__(daemon=True)
        self._fn = fn
        self.result: object = None
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.result = self._fn()
        except BaseException as exc:  # re-raised by finish()
            self.error = exc

    def finish(self) -> object:
        self.join(60)
        assert not self.is_alive(), "run did not finish"
        if self.error is not None:
            raise self.error
        return self.result


def _pause_after_staging(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
    """Make every build stop once the new copy is staged, until `release` is set; `staged`
    is set when the first one gets there."""
    staged, release = threading.Event(), threading.Event()
    real_build = ol._build

    def build(spec: ol.LibSpec, into: Path) -> dict[str, object]:
        manifest = real_build(spec, into)
        staged.set()
        assert release.wait(60)
        return manifest

    monkeypatch.setattr(ol, "_build", build)
    return staged, release


def test_uninstall_during_an_update_waits_and_the_library_stays_uninstalled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #49: update has staged its copy when uninstall arrives. Without a lock the
    uninstall ran at once and the update then renamed its staged copy into place, so the
    library was back after a successful uninstall."""
    ol.install(ol.resolve_spec("WriteText"), tmp_path)
    staged, release = _pause_after_staging(monkeypatch)
    update = _Run(lambda: ol.update("WriteText", tmp_path))
    update.start()
    assert staged.wait(60)

    uninstall = _Run(lambda: ol.uninstall("WriteText", tmp_path))
    uninstall.start()
    uninstall.join(0.5)
    try:
        assert uninstall.is_alive(), "uninstall ran while an update of the same library was in flight"
    finally:
        release.set()

    assert isinstance(update.finish(), ol.InstallResult)
    done = uninstall.finish()
    assert isinstance(done, tuple) and done[1] >= 3  # it removed what the update installed
    assert os.listdir(tmp_path) == []  # the order update, then uninstall: nothing is installed


def test_concurrent_updates_of_one_library_never_overlap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ol.install(ol.resolve_spec("WriteText"), tmp_path)
    guard = threading.Lock()
    active = peak = 0
    real_build = ol._build

    def build(spec: ol.LibSpec, into: Path) -> dict[str, object]:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.3)  # long enough for the others to arrive
            return real_build(spec, into)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(ol, "_build", build)
    runs = [_Run(lambda: ol.update("WriteText", tmp_path)) for _ in range(3)]
    for run in runs:
        run.start()
    for run in runs:
        run.finish()

    assert peak == 1
    assert os.listdir(tmp_path) == ["WriteText"]
    manifest = json.loads((tmp_path / "WriteText" / ol.MANIFEST).read_text(encoding="utf-8"))
    assert {p.name for p in (tmp_path / "WriteText").iterdir()} == {*manifest["files"], ol.MANIFEST}


_HOLD_LOCK = """
import sys, time
sys.path.insert(0, sys.argv[1])
from registries import openscad_libs as ol
with ol.library_lock(sys.argv[2]):
    print("locked", flush=True)
    time.sleep(600)
"""


def test_a_killed_run_leaves_no_lock_behind() -> None:
    holder = subprocess.Popen([sys.executable, "-c", _HOLD_LOCK, str(LIB_DIR), "WriteText"],
                              stdout=subprocess.PIPE, text=True,
                              env={**os.environ, "PYTHONWARNINGS": "error"})
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        with ol.library_lock("WriteText", wait=False) as free:
            assert not free  # held by the other process
        holder.kill()  # SIGKILL: no chance to clean up
        holder.wait(timeout=30)
        with ol.library_lock("WriteText", wait=False) as free:
            assert free
    finally:
        holder.kill()
        holder.wait(timeout=30)


def _half_built(stage: Path, name: str, *, copied: list[str], listed: list[str]) -> Path:
    """A staging folder as a killed install leaves it: the manifest is written first, listing
    every file the copy will hold (`listed`), and `copied` of them made it across."""
    copy = stage / name
    copy.mkdir(parents=True)
    (copy / ol.MANIFEST).write_text(json.dumps({"name": name, "files": listed}), encoding="utf-8")
    for rel in copied:
        (copy / rel).parent.mkdir(parents=True, exist_ok=True)
        (copy / rel).write_text("cube(1);\n", encoding="utf-8")
    return stage


def test_sweep_removes_half_built_folders_and_keeps_a_previous_copy(tmp_path: Path) -> None:
    """Issue #50: what a killed install leaves is removed, except a staging folder that
    holds a previous copy (your files); nothing that only looks similar is touched."""
    installed = ol.install(ol.resolve_spec("WriteText"), tmp_path)
    half = _half_built(tmp_path / ".WriteText.3d-staging-abcd1234", "WriteText", copied=["part.scad"],
                       listed=["part.scad", "not-copied-yet.scad"])  # killed in the middle of the copy
    empty = tmp_path / ".WriteText.3d-staging-klmn3456"
    empty.mkdir()
    swapped = tmp_path / ".WriteText.3d-staging-efgh5678"  # killed between the two renames
    (swapped / "WriteText.previous").mkdir(parents=True)
    (swapped / "WriteText.previous" / "notes.txt").write_text("mine\n", encoding="utf-8")
    (swapped / "WriteText").mkdir()
    unexpected = tmp_path / ".WriteText.3d-staging-pqrs7890"
    unexpected.mkdir()
    (unexpected / "other.txt").write_text("not ours\n", encoding="utf-8")
    lookalikes = [tmp_path / n for n in (".WriteText.3d-staging-x", ".WriteTextX.3d-staging-abcd1234",
                                        "WriteText.3d-staging-abcd1234", ".BOSL2.3d-staging-abcd1234")]
    for folder in lookalikes:
        folder.mkdir()
    clones = paths.data_dir() / "openscad-libs"
    (clones / "WriteText-zzzz9999" / "WriteText").mkdir(parents=True)
    (clones / "WriteText-zzzz9999" / "WriteText" / "big.bin").write_bytes(b"0" * 100)
    (clones / "BOSL2-zzzz9999").mkdir()
    (clones / "WriteText-zz").mkdir()

    stale = ol.sweep_stale("writetext", tmp_path)

    assert sorted(stale.removed) == sorted([half, empty, clones / "WriteText-zzzz9999"])
    assert sorted(stale.kept) == sorted([swapped, unexpected])
    assert (swapped / "WriteText.previous" / "notes.txt").read_text(encoding="utf-8") == "mine\n"
    assert (unexpected / "other.txt").is_file()
    assert all(folder.is_dir() for folder in lookalikes)
    assert (clones / "BOSL2-zzzz9999").is_dir() and (clones / "WriteText-zz").is_dir()
    assert (installed.path / "WriteText.scad").is_file()
    assert ol.sweep_stale("WriteText", tmp_path).removed == ()


def test_sweep_leaves_the_staging_folder_of_an_install_that_is_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staged, release = _pause_after_staging(monkeypatch)
    install = _Run(lambda: ol.install(ol.resolve_spec("WriteText"), tmp_path))
    install.start()
    assert staged.wait(60)
    (live,) = [p for p in tmp_path.iterdir() if ".3d-staging-" in p.name]

    sweep = _Run(lambda: ol.sweep_stale("WriteText", tmp_path))
    sweep.start()
    sweep.join(0.5)
    try:
        assert sweep.is_alive(), "the sweep did not wait for the running install"
        assert (live / "WriteText" / "WriteText.scad").is_file()
    finally:
        release.set()

    install.finish()
    assert sweep.finish() == ol.Stale()  # the install cleaned up after itself
    assert (tmp_path / "WriteText" / "WriteText.scad").is_file()


def test_leftovers_skip_a_library_that_is_being_worked_on_and_delete_nothing(tmp_path: Path) -> None:
    dead = tmp_path / ".WriteText.3d-staging-abcd1234"
    (dead / "WriteText").mkdir(parents=True)
    kept = tmp_path / ".BOSL2.3d-staging-efgh5678"
    (kept / "BOSL2.previous").mkdir(parents=True)
    live = tmp_path / ".NopSCADlib.3d-staging-ijkl9012"
    (live / "NopSCADlib").mkdir(parents=True)

    with ol.library_lock("NopSCADlib"):
        found = ol.find_leftovers(tmp_path)

    assert found == [ol.Leftover(kept, "BOSL2", half_built=False),
                     ol.Leftover(dead, "WriteText", half_built=True)]
    assert dead.is_dir() and kept.is_dir() and live.is_dir()


@pytest.mark.parametrize("how", ["update", "force"])
def test_replacing_a_library_keeps_the_mode_of_its_folder(how: str, tmp_path: Path) -> None:
    """Issue #50: the new folder used to get the default mode, dropping e.g. group write."""
    spec = ol.resolve_spec("WriteText")
    res = ol.install(spec, tmp_path)
    os.chmod(res.path, 0o770)
    old_umask = os.umask(0o022)  # a new folder would get 0o755
    try:
        if how == "update":
            ol.update("WriteText", tmp_path)
        else:
            ol.install(spec, tmp_path, force=True)
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(os.lstat(res.path).st_mode) == 0o770
    assert (res.path / "WriteText.scad").is_file()


def test_update_keeps_the_group_of_the_library_folder(tmp_path: Path) -> None:
    res = ol.install(ol.resolve_spec("WriteText"), tmp_path)
    others = [g for g in os.getgroups() if g not in {os.getegid(), os.lstat(tmp_path).st_gid}]
    if not others:
        pytest.skip("the current user has no second group to chgrp to")
    try:
        os.chown(res.path, -1, others[0])
    except PermissionError:
        pytest.skip("cannot chgrp to a secondary group here")

    ol.update("WriteText", tmp_path)

    assert os.lstat(res.path).st_gid == others[0]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS ACLs")
def test_update_keeps_the_acl_of_the_library_folder(tmp_path: Path) -> None:
    res = ol.install(ol.resolve_spec("WriteText"), tmp_path)
    subprocess.run(["/bin/chmod", "+a", "group:everyone allow list", str(res.path)], check=True)

    ol.update("WriteText", tmp_path)

    listing = subprocess.run(["/bin/ls", "-led", str(res.path)], capture_output=True, text=True,
                             check=True).stdout
    assert "group:everyone allow list" in listing


def test_the_lock_file_cannot_be_steered_outside_the_locks_folder() -> None:
    with pytest.raises(InvalidArgument), ol.library_lock("../../escape"):
        pass


def test_unusable_data_dir_is_a_structured_error_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("XDG_DATA_HOME", str(blocker))

    with pytest.raises(ThreeDError, match="cannot create the lock file"), ol.library_lock("WriteText"):
        pass


def test_sweep_waits_for_the_lock_even_when_it_sees_nothing_yet(tmp_path: Path) -> None:
    """A run holds the lock but has not created its staging folder when the sweep starts, and is
    killed right after. Looking before locking would return early and leave that folder behind."""
    with ol.library_lock("WriteText"):
        sweep = _Run(lambda: ol.sweep_stale("WriteText", tmp_path))
        sweep.start()
        sweep.join(0.5)
        assert sweep.is_alive(), "the sweep did not wait for the run holding the library"
        (tmp_path / ".WriteText.3d-staging-abcd1234" / "WriteText").mkdir(parents=True)

    stale = sweep.finish()
    assert isinstance(stale, ol.Stale)
    assert stale.removed == (tmp_path / ".WriteText.3d-staging-abcd1234",)
    assert os.listdir(tmp_path) == []


def test_sweep_reports_a_folder_it_cannot_remove_instead_of_calling_it_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".WriteText.3d-staging-abcd1234" / "WriteText").mkdir(parents=True)

    def refuse(path: str | Path, *args: object, **kwargs: object) -> None:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(ol.shutil, "rmtree", refuse)
    stale = ol.sweep_stale("WriteText", tmp_path)

    assert stale.removed == () and stale.kept == ()
    assert [p.name for p, _ in stale.stuck] == [".WriteText.3d-staging-abcd1234"]
    assert "Permission denied" in stale.stuck[0][1]


def test_a_stray_file_in_a_staging_folder_keeps_it(tmp_path: Path) -> None:
    """Finder drops a .DS_Store anywhere: a folder with anything but the half-built copy is not ours to delete."""
    stage = tmp_path / ".WriteText.3d-staging-abcd1234"
    (stage / "WriteText").mkdir(parents=True)
    (stage / ".DS_Store").write_bytes(b"\0")

    stale = ol.sweep_stale("WriteText", tmp_path)

    assert stale.kept == (stage,) and stale.removed == ()
    assert (stage / ".DS_Store").is_file()


def test_a_previous_copy_kept_by_a_successful_update_is_reported_and_never_swept(tmp_path: Path) -> None:
    res = ol.install(ol.resolve_spec("WriteText"), tmp_path)
    manifest = json.loads((res.path / ol.MANIFEST).read_text(encoding="utf-8"))
    manifest["files"].remove("README.md")  # as if README.md were new upstream ...
    (res.path / ol.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    (res.path / "README.md").write_text("my readme\n", encoding="utf-8")  # ... and this one is mine
    kept = ol.update("WriteText", tmp_path).kept
    assert kept is not None

    assert ol.find_leftovers(tmp_path) == [ol.Leftover(kept.parent, "WriteText", half_built=False)]
    stale = ol.sweep_stale("WriteText", tmp_path)

    assert stale.kept == (kept.parent,) and stale.removed == ()
    assert (kept / "README.md").read_text(encoding="utf-8") == "my readme\n"


def test_leftovers_with_a_malformed_name_or_an_unusable_lock_file_do_not_break_the_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".foo..bar.3d-staging-abcd1234").mkdir()
    (tmp_path / ".3d-staging-abcd1234").mkdir()
    ok = tmp_path / ".WriteText.3d-staging-efgh5678"
    ok.mkdir()
    assert ol.find_leftovers(tmp_path) == [ol.Leftover(ok, "WriteText", half_built=True)]

    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("XDG_DATA_HOME", str(blocker))  # no lock file can be made: report nothing
    assert ol.find_leftovers(tmp_path) == []


@pytest.mark.parametrize(("platform", "swept"), [("darwin", True), ("linux", False)])
def test_leftovers_of_a_differently_cased_name_are_swept_only_where_folders_are_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str, swept: bool
) -> None:
    monkeypatch.setattr(ol.sys, "platform", platform)
    stage = tmp_path / ".mylib.3d-staging-abcd1234"
    (stage / "mylib").mkdir(parents=True)
    clone = paths.data_dir() / "openscad-libs" / "mylib-abcd1234"
    clone.mkdir(parents=True)

    stale = ol.sweep_stale("MyLib", tmp_path)

    assert set(stale.removed) == ({stage, clone} if swept else set())
    assert stage.exists() is not swept and clone.exists() is not swept


def test_one_attribute_that_cannot_be_copied_does_not_cancel_the_others(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    res = ol.install(ol.resolve_spec("WriteText"), tmp_path)
    os.chmod(res.path, 0o770)

    def deny(*args: object, **kwargs: object) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(ol.os, "chown", deny)
    old_umask = os.umask(0o022)
    try:
        ol.update("WriteText", tmp_path)
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(os.lstat(res.path).st_mode) == 0o770


def test_a_file_you_left_inside_the_half_built_copy_keeps_the_folder(tmp_path: Path) -> None:
    """The staging folder is hidden but not off limits: a note you put inside the copy, or a copy
    with files but no manifest to vouch for them, is not 3d's to delete."""
    noted = _half_built(tmp_path / ".WriteText.3d-staging-aaaa1111", "WriteText",
                        copied=["WriteText.scad"], listed=["WriteText.scad"])
    (noted / "WriteText" / "notes.txt").write_text("mine\n", encoding="utf-8")
    nested = _half_built(tmp_path / ".WriteText.3d-staging-bbbb2222", "WriteText",
                         copied=["sub/a.scad"], listed=["sub/a.scad"])
    (nested / "WriteText" / "sub" / "mine.txt").write_text("mine\n", encoding="utf-8")
    unvouched = tmp_path / ".WriteText.3d-staging-cccc3333"
    (unvouched / "WriteText").mkdir(parents=True)
    (unvouched / "WriteText" / "x.scad").write_text("cube(2);\n", encoding="utf-8")

    stale = ol.sweep_stale("WriteText", tmp_path)

    assert sorted(stale.kept) == sorted([noted, nested, unvouched]) and stale.removed == ()
    assert (noted / "WriteText" / "notes.txt").read_text(encoding="utf-8") == "mine\n"
    assert (nested / "WriteText" / "sub" / "mine.txt").read_text(encoding="utf-8") == "mine\n"
    assert (unvouched / "WriteText" / "x.scad").is_file()


def test_a_killed_install_leaves_a_folder_the_sweep_recognises_as_its_own(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end on the library side: stop a real install after its third file was copied."""
    real_copy = ol.shutil.copy2
    copied: list[object] = []

    def copy_then_die(src: str | Path, dst: str | Path) -> object:
        if len(copied) == 2:
            raise KeyboardInterrupt
        copied.append(dst)
        return real_copy(src, dst)

    with monkeypatch.context() as killed:
        killed.setattr(ol.shutil, "copy2", copy_then_die)
        killed.setattr(ol.shutil, "rmtree", lambda *a, **k: None)  # as if SIGKILL: no cleanup ran
        with pytest.raises(KeyboardInterrupt):
            ol.install(ol.resolve_spec("WriteText"), tmp_path)
    (stage,) = [p for p in tmp_path.iterdir() if ".3d-staging-" in p.name]
    assert len(list((stage / "WriteText").iterdir())) == 3  # manifest + two of the files

    assert ol.sweep_stale("WriteText", tmp_path).removed == (stage,)


def test_case_variant_leftovers_are_each_reported_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ol.sys, "platform", "darwin")
    a = tmp_path / ".MyLib.3d-staging-aaaa1111"
    b = tmp_path / ".mylib.3d-staging-bbbb2222"
    a.mkdir()
    b.mkdir()

    assert sorted(item.path for item in ol.find_leftovers(tmp_path)) == sorted([a, b])
