"""End-to-end `3d openscad libs` workflows driven through the real `bin/3d`.

HOME / XDG dirs are isolated per test, so the "user library folder" is a temp folder and
nothing touches the real ~/Documents/OpenSCAD/libraries. Rendering assertions need a
working OpenSCAD and skip otherwise; git-source assertions need `git` and use a local
file:// repository (no network).
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from .workflow_helper import (
    REPO_ROOT,
    THREED,
    isolated_env,
    require_binary,
    require_working_openscad,
    run_cli,
    run_shell,
)


def _user_libs(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    if sys.platform == "darwin":
        return home / "Documents" / "OpenSCAD" / "libraries"
    return tmp_path / "xdg-data" / "OpenSCAD" / "libraries"


def test_user_installs_lists_locates_and_uninstalls_writetext(tmp_path: Path) -> None:
    """A user installs the bundled WriteText library into OpenSCAD's user library folder,
    sees it in `list`, locates it with `where`, reinstalls harmlessly, then removes it."""
    lib = _user_libs(tmp_path) / "WriteText"

    first = run_cli(tmp_path, "openscad", "libs", "writetext", "install")
    assert first.returncode == 0, first.stderr
    assert f"installed WriteText -> {lib}" in first.stdout
    assert "use <WriteText/...>" in first.stdout
    assert (lib / "WriteText.scad").read_text(encoding="utf-8").count("module cone_text(") == 1
    assert "thing:16193" in (lib / "LICENSE").read_text(encoding="utf-8")
    manifest = json.loads((lib / ".3d-openscad-lib.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "WriteText"
    assert "WriteText.scad" in manifest["files"]

    again = run_cli(tmp_path, "openscad", "libs", "WriteText", "install")
    assert again.returncode == 0, again.stderr
    assert "already installed" in again.stdout

    listed = run_shell('"$PYTHON" "$THREED" openscad libs list | grep WriteText > list.txt', tmp_path)
    assert listed.returncode == 0, listed.stderr
    assert f"installed    {lib}" in (tmp_path / "list.txt").read_text(encoding="utf-8")

    where = run_cli(tmp_path, "openscad", "libs", "WriteText", "where")
    assert where.stdout.strip() == str(lib)

    gone = run_cli(tmp_path, "openscad", "libs", "WriteText", "uninstall")
    assert gone.returncode == 0, gone.stderr
    assert "removed" in gone.stdout
    assert not lib.exists()

    missing = run_cli(tmp_path, "openscad", "libs", "WriteText", "where")
    assert missing.returncode == 1
    assert "3d openscad libs WriteText install" in missing.stderr
    after = run_cli(tmp_path, "openscad", "libs", "list")
    assert any(line.split()[:3] == ["WriteText", "not", "installed"] for line in after.stdout.splitlines())


def test_installed_writetext_renders_cyrillic_with_plain_openscad_and_no_warnings(tmp_path: Path) -> None:
    """After installing WriteText into a library folder, a bare `openscad` run from an
    unrelated directory resolves `use <WriteText/...>` and renders raised Cyrillic and
    engraved Latin lettering without a single warning."""
    openscad = require_working_openscad()
    libs = tmp_path / "scad-libs"
    installed = run_cli(tmp_path, "openscad", "libs", "WriteText", "install", "--dir", str(libs))
    assert installed.returncode == 0, installed.stderr

    model = tmp_path / "work" / "cup.scad"
    model.parent.mkdir()
    model.write_text(
        "use <WriteText/WriteText.scad>\n"
        "$fn = 48;\n"
        "cylinder(r1 = 40, r2 = 32, h = 30);\n"
        'cone_text("Привет", r1 = 40, r2 = 32, h = 30, size = 10, relief = 1);\n'
        "translate([100, 0, 0]) difference() {\n"
        "  cylinder(r = 30, h = 30);\n"
        '  cylinder_text_cut("Hello", r = 30, h = 30, size = 10, depth = 1);\n'
        "}\n",
        encoding="utf-8",
    )
    env = isolated_env(tmp_path)
    env["OPENSCADPATH"] = str(libs)
    out = tmp_path / "cup.stl"
    proc = subprocess.run(
        [openscad, "-o", str(out), str(model)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300,
    )
    log = proc.stdout + proc.stderr
    assert proc.returncode == 0, log
    assert [ln for ln in log.splitlines() if ln.startswith(("WARNING", "ERROR", "DEPRECATED"))] == []
    assert out.stat().st_size > 10_000


def _git_lib_repo(tmp_path: Path) -> tuple[Path, Callable[..., None]]:
    """A local git repository `MyLib-src` with one commit (mylib.scad, old.scad) tagged v1,
    and a runner for further git commands in it."""
    git = require_binary("git")
    repo = tmp_path / "MyLib-src"
    repo.mkdir()

    def sh(*args: str) -> None:
        subprocess.run([git, *args], cwd=repo, env=isolated_env(tmp_path), check=True,
                       capture_output=True, text=True)

    sh("init", "-q", "-b", "main")
    sh("config", "user.email", "t@example.com")
    sh("config", "user.name", "t")
    (repo / "mylib.scad").write_text("module one() { cube(1); }\n", encoding="utf-8")
    (repo / "old.scad").write_text("// dropped later\n", encoding="utf-8")
    sh("add", ".")
    sh("commit", "-q", "-m", "v1")
    sh("tag", "v1")
    return repo, sh


def _snapshot(folder: Path) -> dict[str, bytes]:
    """Every file under `folder` (manifest included) with its bytes."""
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def test_user_installs_updates_and_uninstalls_a_git_library(tmp_path: Path) -> None:
    """A library outside the registry installs from a git URL, `update` pulls new commits
    (dropping files deleted upstream, keeping files the user added), and `uninstall` keeps
    files the user added."""
    repo, sh = _git_lib_repo(tmp_path)
    url = repo.as_uri()
    lib = _user_libs(tmp_path) / "MyLib"

    installed = run_cli(tmp_path, "openscad", "libs", "MyLib", "install", "--git", url)
    assert installed.returncode == 0, installed.stderr
    assert f"from {url} @ " in installed.stdout
    assert (lib / "mylib.scad").read_text(encoding="utf-8").startswith("module one()")
    assert not (lib / ".git").exists()

    (lib / "notes.txt").write_text("mine\n", encoding="utf-8")
    (repo / "mylib.scad").write_text("module two() { cube(2); }\n", encoding="utf-8")
    (repo / "old.scad").unlink()
    sh("commit", "-q", "-am", "v2")
    updated = run_cli(tmp_path, "openscad", "libs", "MyLib", "update")
    assert updated.returncode == 0, updated.stderr
    assert "module two()" in (lib / "mylib.scad").read_text(encoding="utf-8")
    assert not (lib / "old.scad").exists()
    assert (lib / "notes.txt").read_text(encoding="utf-8") == "mine\n"
    manifest = json.loads((lib / ".3d-openscad-lib.json").read_text(encoding="utf-8"))
    assert manifest["files"] == ["mylib.scad"]

    removed = run_cli(tmp_path, "openscad", "libs", "MyLib", "uninstall")
    assert removed.returncode == 0, removed.stderr
    assert f"kept {lib}" in removed.stdout
    assert sorted(p.name for p in lib.iterdir()) == ["notes.txt"]


def test_failed_update_or_forced_reinstall_leaves_the_working_git_library_untouched(tmp_path: Path) -> None:
    """Issue #48: `update` and `install --force` fetch the replacement before touching the
    install. A --ref that does not exist, or a source that is gone (offline, dead URL),
    exits non-zero and leaves every installed file and the manifest byte-for-byte as it was."""
    repo, sh = _git_lib_repo(tmp_path)
    url = repo.as_uri()
    libs = _user_libs(tmp_path)
    lib = libs / "MyLib"
    installed = run_cli(tmp_path, "openscad", "libs", "MyLib", "install", "--git", url, "--ref", "v1")
    assert installed.returncode == 0, installed.stderr
    (lib / "notes.txt").write_text("mine\n", encoding="utf-8")
    before = _snapshot(lib)
    assert {"mylib.scad", "old.scad", "notes.txt", ".3d-openscad-lib.json"} <= before.keys()

    def fails(*argv: str) -> None:
        res = run_cli(tmp_path, "openscad", "libs", "MyLib", *argv)
        assert res.returncode != 0, (argv, res.stdout)
        assert "git clone" in res.stderr, (argv, res.stderr)
        assert _snapshot(lib) == before, argv
        assert sorted(p.name for p in libs.iterdir()) == ["MyLib"], argv  # no staging debris

    fails("install", "--git", url, "--ref", "no-such-tag", "--force")
    sh("tag", "-d", "v1")
    fails("update")  # the recorded --ref no longer exists upstream
    repo.rename(tmp_path / "moved-away")
    fails("update")  # the recorded URL no longer answers
    fails("install", "--git", url, "--force")


def test_update_that_ships_a_file_you_wrote_keeps_yours_and_says_where(tmp_path: Path) -> None:
    """A file the user added to the install is also added upstream later: `update` installs
    the upstream one and keeps the user's in the previous copy, printing where it is."""
    repo, sh = _git_lib_repo(tmp_path)
    lib = _user_libs(tmp_path) / "MyLib"
    assert run_cli(tmp_path, "openscad", "libs", "MyLib", "install", "--git", repo.as_uri()).returncode == 0
    (lib / "extra.scad").write_text("// mine\n", encoding="utf-8")
    (repo / "extra.scad").write_text("// upstream\n", encoding="utf-8")
    sh("add", "extra.scad")
    sh("commit", "-q", "-m", "v2")

    updated = run_cli(tmp_path, "openscad", "libs", "MyLib", "update")

    assert updated.returncode == 0, updated.stderr
    assert (lib / "extra.scad").read_text(encoding="utf-8") == "// upstream\n"
    kept = re.search(r"kept the previous copy at (.+?): it still holds", updated.stdout)
    assert kept is not None, updated.stdout
    assert (Path(kept[1]) / "extra.scad").read_text(encoding="utf-8") == "// mine\n"


def test_user_sees_search_order_and_is_warned_when_openscadpath_shadows_an_install(tmp_path: Path) -> None:
    """With an older WriteText on OPENSCADPATH, `path` shows that folder first and
    `install` warns that OpenSCAD will keep loading the shadowing copy."""
    shadow = tmp_path / "old-libs"
    (shadow / "WriteText").mkdir(parents=True)
    env = {"OPENSCADPATH": str(shadow)}

    order = run_cli(tmp_path, "openscad", "libs", "path", env_extra=env)
    assert order.returncode == 0, order.stderr
    lines = [ln.strip() for ln in order.stdout.splitlines()]
    assert lines.index(str(shadow)) < lines.index(str(_user_libs(tmp_path)))

    installed = run_cli(tmp_path, "openscad", "libs", "WriteText", "install", env_extra=env)
    assert installed.returncode == 0, installed.stderr
    assert f"OpenSCAD loads WriteText from {shadow / 'WriteText'} first" in installed.stdout
    where = run_cli(tmp_path, "openscad", "libs", "WriteText", "where", env_extra=env)
    assert where.stdout.strip() == str(shadow / "WriteText")


def test_user_manages_a_library_in_a_custom_folder_with_dir(tmp_path: Path) -> None:
    """`--dir` installs, locates and removes a library in a folder of the user's choice,
    and a path-like library name is refused before anything is written."""
    libs = tmp_path / "scad-libs"
    installed = run_cli(tmp_path, "openscad", "libs", "WriteText", "install", "--dir", str(libs))
    assert installed.returncode == 0, installed.stderr
    where = run_cli(tmp_path, "openscad", "libs", "WriteText", "where", "--dir", str(libs))
    assert where.stdout.strip() == str(libs / "WriteText")
    gone = run_cli(tmp_path, "openscad", "libs", "WriteText", "uninstall", "--dir", str(libs))
    assert gone.returncode == 0, gone.stderr
    assert not (libs / "WriteText").exists()

    escape = run_cli(tmp_path, "openscad", "libs", "../evil", "install", "--git", "https://example.com/x.git")
    assert escape.returncode == 2
    assert "no '/', no '..'" in escape.stderr
    assert not (tmp_path / "home" / "Documents" / "OpenSCAD" / "evil").exists()


_HOLD_LOCK = """
import sys, time
sys.path.insert(0, sys.argv[1])
from registries import openscad_libs as ol
with ol.library_lock("WriteText"):
    print("locked", flush=True)
    time.sleep(600)
"""

# A `3d openscad libs WriteText install --force` that stops dead (to be SIGKILLed) at a chosen
# point: "staging" once the new copy is built, "swap" between renaming the old library aside
# and moving the new one in.
_STALLED_INSTALL = """
import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from registries import openscad_libs as ol
point, base = sys.argv[2], Path(sys.argv[3])
if point == "staging":
    real_build = ol._build
    def build(spec, into):
        manifest = real_build(spec, into)
        print("stalled", flush=True)
        time.sleep(600)
        return manifest
    ol._build = build
else:
    real_rename = os.rename
    def rename(src, dst):
        if Path(dst) == base / "WriteText" and Path(src).name == "WriteText":
            print("stalled", flush=True)
            time.sleep(600)
        real_rename(src, dst)
    os.rename = rename
ol.install(ol.resolve_spec("WriteText"), base, force=True)
"""


def _stall(tmp_path: Path, script: str, *args: str) -> subprocess.Popen[str]:
    """Start `script` (python -c) with the 3d lib dir first, and return once it printed a line."""
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(REPO_ROOT / "lib"), *args],
        cwd=tmp_path, env=isolated_env(tmp_path), stdout=subprocess.PIPE, text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() in ("locked", "stalled")
    return proc


def _stop(proc: subprocess.Popen[str]) -> None:
    """SIGKILL: the process gets no chance to clean up or release anything."""
    proc.kill()
    proc.wait(timeout=30)


def test_second_run_waits_for_the_one_in_flight_and_a_killed_run_blocks_nobody(tmp_path: Path) -> None:
    """Issue #49: `uninstall` started while another run holds the library waits (and says
    so) instead of interleaving. SIGKILLing the holder frees it at once: no stale lock."""
    lib = _user_libs(tmp_path) / "WriteText"
    assert run_cli(tmp_path, "openscad", "libs", "WriteText", "install").returncode == 0
    holder = _stall(tmp_path, _HOLD_LOCK)
    try:
        waiting = subprocess.Popen(
            [sys.executable, str(THREED), "openscad", "libs", "WriteText", "uninstall"],
            cwd=tmp_path, env=isolated_env(tmp_path),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            assert waiting.stderr is not None
            notice = waiting.stderr.readline()  # blocks until it is queued behind the holder
            assert "waiting for another `3d openscad libs` run on WriteText" in notice, notice
            assert waiting.poll() is None, "uninstall ran while another run held the library"
            assert lib.is_dir()
            _stop(holder)
            out, rest = waiting.communicate(timeout=60)
            err = notice + rest
        finally:
            if waiting.poll() is None:
                waiting.kill()
                waiting.communicate()
    finally:
        _stop(holder)

    assert waiting.returncode == 0, err
    assert "waiting for another `3d openscad libs` run on WriteText" in err
    assert "uninstalled WriteText: removed" in out
    assert not lib.exists()
    again = run_cli(tmp_path, "openscad", "libs", "WriteText", "install", timeout=60)
    assert again.returncode == 0, again.stderr
    assert "waiting" not in again.stderr


def test_install_killed_while_staging_is_reported_by_list_and_swept_by_the_next_update(tmp_path: Path) -> None:
    """Issue #50: a SIGKILLed update leaves its half-built copy in a hidden folder. `list`
    reports it (deleting nothing); the next `update` removes it; the installed library and
    its files are never touched."""
    libs = _user_libs(tmp_path)
    lib = libs / "WriteText"
    assert run_cli(tmp_path, "openscad", "libs", "WriteText", "install").returncode == 0
    before = _snapshot(lib)
    stalled = _stall(tmp_path, _STALLED_INSTALL, "staging", str(libs))
    _stop(stalled)
    (leftover,) = [p for p in libs.iterdir() if ".3d-staging-" in p.name]
    assert (leftover / "WriteText" / "WriteText.scad").is_file()

    listed = run_cli(tmp_path, "openscad", "libs", "list")
    assert listed.returncode == 0, listed.stderr
    assert f"{leftover}  half-built copy of WriteText" in listed.stdout
    assert leftover.is_dir()

    updated = run_cli(tmp_path, "openscad", "libs", "WriteText", "update")
    assert updated.returncode == 0, updated.stderr
    assert f"removed {leftover}: left by an interrupted install of WriteText" in updated.stdout
    assert [p.name for p in libs.iterdir()] == ["WriteText"]
    assert _snapshot(lib).keys() == before.keys()
    assert "half-built" not in run_cli(tmp_path, "openscad", "libs", "list").stdout


def test_install_killed_between_the_renames_keeps_the_previous_library_and_says_so(tmp_path: Path) -> None:
    """Issue #50: SIGKILL after the old library was set aside but before the new one moved
    in leaves no library, and the old one (with the user's files) in the staging folder. The
    next install must not delete it: it reports the folder, then installs afresh."""
    libs = _user_libs(tmp_path)
    lib = libs / "WriteText"
    assert run_cli(tmp_path, "openscad", "libs", "WriteText", "install").returncode == 0
    (lib / "notes.txt").write_text("mine\n", encoding="utf-8")
    stalled = _stall(tmp_path, _STALLED_INSTALL, "swap", str(libs))
    _stop(stalled)
    assert not lib.exists()
    (leftover,) = [p for p in libs.iterdir() if ".3d-staging-" in p.name]

    listed = run_cli(tmp_path, "openscad", "libs", "list")
    assert f"{leftover}  holds more than a half-built copy of WriteText" in listed.stdout

    installed = run_cli(tmp_path, "openscad", "libs", "WriteText", "install")

    assert installed.returncode == 0, installed.stderr
    assert f"kept {leftover}: it holds more than a half-built copy of WriteText" in installed.stdout
    assert (lib / "WriteText.scad").is_file()
    assert (leftover / "WriteText.previous" / "notes.txt").read_text(encoding="utf-8") == "mine\n"
    assert (leftover / "WriteText.previous" / "WriteText.scad").is_file()


def test_update_keeps_the_permissions_of_the_library_folder(tmp_path: Path) -> None:
    """Issue #50: `update` replaces the library folder; a group-write bit set on it must survive."""
    lib = _user_libs(tmp_path) / "WriteText"
    assert run_cli(tmp_path, "openscad", "libs", "WriteText", "install").returncode == 0
    os.chmod(lib, 0o770)

    updated = run_cli(tmp_path, "openscad", "libs", "WriteText", "update")

    assert updated.returncode == 0, updated.stderr
    assert stat.S_IMODE(os.lstat(lib).st_mode) == 0o770


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs a folder that even its owner cannot empty")
def test_leftover_that_cannot_be_removed_is_reported_and_the_update_still_succeeds(tmp_path: Path) -> None:
    """Issue #50: a leftover the sweep is not allowed to delete is named, not silently skipped or
    mislabelled as your previous copy; the command goes on with its own work."""
    libs = _user_libs(tmp_path)
    lib = libs / "WriteText"
    assert run_cli(tmp_path, "openscad", "libs", "WriteText", "install").returncode == 0
    stalled = _stall(tmp_path, _STALLED_INSTALL, "staging", str(libs))
    _stop(stalled)
    (leftover,) = [p for p in libs.iterdir() if ".3d-staging-" in p.name]
    locked = leftover / "WriteText"
    os.chmod(locked, 0o500)  # its files cannot be unlinked
    try:
        updated = run_cli(tmp_path, "openscad", "libs", "WriteText", "update")
    finally:
        os.chmod(locked, 0o700)

    assert updated.returncode == 0, updated.stderr
    assert f"could not remove {leftover} (" in updated.stdout
    assert "delete it yourself" in updated.stdout
    assert "kept " not in updated.stdout  # not mislabelled as a folder that holds your files
    assert leftover.is_dir()
    assert (lib / "WriteText.scad").is_file()
