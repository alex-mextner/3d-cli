"""End-to-end `3d openscad libs` workflows driven through the real `bin/3d`.

HOME / XDG dirs are isolated per test, so the "user library folder" is a temp folder and
nothing touches the real ~/Documents/OpenSCAD/libraries. Rendering assertions need a
working OpenSCAD and skip otherwise; git-source assertions need `git` and use a local
file:// repository (no network).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .workflow_helper import isolated_env, require_binary, require_working_openscad, run_cli, run_shell


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


def test_user_installs_updates_and_uninstalls_a_git_library(tmp_path: Path) -> None:
    """A library outside the registry installs from a git URL, `update` pulls new commits
    (and drops files deleted upstream), and `uninstall` keeps files the user added."""
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
    url = repo.as_uri()
    lib = _user_libs(tmp_path) / "MyLib"

    installed = run_cli(tmp_path, "openscad", "libs", "MyLib", "install", "--git", url)
    assert installed.returncode == 0, installed.stderr
    assert f"from {url} @ " in installed.stdout
    assert (lib / "mylib.scad").read_text(encoding="utf-8").startswith("module one()")
    assert not (lib / ".git").exists()

    (repo / "mylib.scad").write_text("module two() { cube(2); }\n", encoding="utf-8")
    (repo / "old.scad").unlink()
    sh("commit", "-q", "-am", "v2")
    updated = run_cli(tmp_path, "openscad", "libs", "MyLib", "update")
    assert updated.returncode == 0, updated.stderr
    assert "module two()" in (lib / "mylib.scad").read_text(encoding="utf-8")
    assert not (lib / "old.scad").exists()

    (lib / "notes.txt").write_text("mine\n", encoding="utf-8")
    removed = run_cli(tmp_path, "openscad", "libs", "MyLib", "uninstall")
    assert removed.returncode == 0, removed.stderr
    assert f"kept {lib}" in removed.stdout
    assert sorted(p.name for p in lib.iterdir()) == ["notes.txt"]


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
