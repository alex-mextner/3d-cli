"""registries/openscad_libs.py — install OpenSCAD libraries where OpenSCAD always finds them.

WHAT: a small registry of known OpenSCAD libraries (WriteText shipped in this repo under
  openscad-libs/, plus git-hosted ones like BOSL2) and install / uninstall / update /
  locate operations on OpenSCAD's per-user library folder:
    macOS    ~/Documents/OpenSCAD/libraries
    Linux    $XDG_DATA_HOME/OpenSCAD/libraries  (default ~/.local/share/OpenSCAD/libraries)
    Windows  %USERPROFILE%\\Documents\\OpenSCAD\\libraries
  OpenSCAD searches that folder on every run, with no OPENSCADPATH needed, so
  `use <WriteText/WriteText.scad>` works from any cwd, in the GUI and on the CLI.

WHY not an existing manager: OpenSCAD has no official package manager
  (openscad/openscad#3479). scadm / scadman / scadder install into a per-project folder
  driven by a manifest, and olman is a Linux snap; none installs into the per-user
  folder or can ship a library that lives in this repo.

HOW: every install writes a manifest `.3d-openscad-lib.json` into the library folder
  listing the files it copied. Uninstall deletes exactly those files and nothing else, so
  a hand-installed library of the same name is never clobbered, and removal works even
  where directory listing is denied (macOS privacy protection on ~/Documents).
  Git libraries are shallow-cloned into a temporary folder under the 3d data dir. The new
  files (minus hidden folders such as .git/.github) and manifest are staged in a hidden
  folder next to the destination and only then renamed into place (the old folder renamed
  aside first, and back if that fails), so a failed clone or copy (offline, bad --ref)
  never touches a working install. Files the user added to a 3d install move into the
  new copy.

INVARIANTS: stdlib-only; command modules reach it lazily. Errors are lib/errors.py types.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cli import paths
from cli.env import repo_root
from errors import InvalidArgument, MissingDependency, ThreeDError, UsageError

MANIFEST = ".3d-openscad-lib.json"
ERR_CTX = "openscad libs"  # command label on structured errors


@dataclass(frozen=True)
class LibSpec:
    """A library 3d knows how to install. `name` is the folder name `use <name/...>`
    resolves, so it is also the install directory name."""

    name: str
    summary: str
    homepage: str
    git: str | None = None     # clone URL, or None for a library shipped in this repo
    local: str | None = None   # path relative to the repo root
    ref: str | None = None     # branch/tag to check out (git only; default branch if None)


KNOWN_LIBS: tuple[LibSpec, ...] = (
    LibSpec(
        name="WriteText",
        summary="text on cylinders/cones via native text() (Cyrillic OK); after Write.scad",
        homepage="https://www.thingiverse.com/thing:16193",
        local="openscad-libs/WriteText",
    ),
    LibSpec(
        name="BOSL2",
        summary="Belfry OpenSCAD Library v2: shapes, attachments, threading, rounding",
        homepage="https://github.com/BelfrySCAD/BOSL2",
        git="https://github.com/BelfrySCAD/BOSL2.git",
    ),
    LibSpec(
        name="NopSCADlib",
        summary="vitamins (screws, bearings, electronics) and printed parts",
        homepage="https://github.com/nophead/NopSCADlib",
        git="https://github.com/nophead/NopSCADlib.git",
    ),
    LibSpec(
        name="Round-Anything",
        summary="rounded polygons, fillets and minkowski-free rounding",
        homepage="https://github.com/Irev-Dev/Round-Anything",
        git="https://github.com/Irev-Dev/Round-Anything.git",
    ),
    LibSpec(
        name="threads-scad",
        summary="ISO metric threads, screws and nuts",
        homepage="https://github.com/rcolyer/threads-scad",
        git="https://github.com/rcolyer/threads-scad.git",
    ),
)


def known_lib(name: str) -> LibSpec | None:
    """Registry lookup, case-insensitive (`writetext` finds `WriteText`)."""
    for spec in KNOWN_LIBS:
        if spec.name.lower() == name.lower():
            return spec
    return None


_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")


def canonical_name(name: str) -> str:
    """The library folder name for `name`: the registry spelling for known libraries,
    else `name` itself. It must be one plain path component — never `..`, a separator or
    an absolute path — because it is joined onto the library folder we write and delete in."""
    if not _SAFE_NAME.fullmatch(name) or ".." in name:
        raise InvalidArgument(
            "library",
            name,
            ["a folder name: letters, digits, '.', '_', '+', '-' (no '/', no '..')"],
            command=ERR_CTX,
        )
    known = known_lib(name)
    return known.name if known else name


def resolve_spec(name: str, git: str | None = None, ref: str | None = None) -> LibSpec:
    """The spec to install for `name`: an explicit --git URL wins, else the registry."""
    folder = canonical_name(name)
    if git:
        known = known_lib(folder)
        return LibSpec(
            name=folder,
            summary=known.summary if known else "installed from a git URL",
            homepage=git,
            git=git,
            ref=ref,
        )
    spec = known_lib(name)
    if spec is None:
        raise InvalidArgument(
            "library",
            name,
            [s.name for s in KNOWN_LIBS],
            command=ERR_CTX,
            extra=f"For another library pass its git URL: 3d openscad libs {name} install --git URL",
        )
    if ref and not spec.git:
        raise UsageError(
            f"--ref only applies to git libraries; {spec.name} ships with 3d",
            command=ERR_CTX,
            remediation=[f"Drop --ref: 3d openscad libs {spec.name} install"],
        )
    if ref:
        return LibSpec(spec.name, spec.summary, spec.homepage, spec.git, spec.local, ref)
    return spec


def spec_source(spec: LibSpec) -> str:
    """Where `spec` installs from, in the same form `InstallResult.source` reports."""
    if spec.git:
        return f"{spec.git}@{spec.ref}" if spec.ref else spec.git
    return str(Path(repo_root()) / (spec.local or ""))


# ---------------------------------------------------------------------------
# Where OpenSCAD looks.
# ---------------------------------------------------------------------------
def user_library_dir() -> Path:
    """OpenSCAD's per-user library folder for this OS (searched on every run)."""
    if sys.platform == "darwin":
        return Path.home() / "Documents" / "OpenSCAD" / "libraries"
    if sys.platform.startswith("win"):
        return Path.home() / "Documents" / "OpenSCAD" / "libraries"
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data) / "OpenSCAD" / "libraries"


def search_path() -> list[Path]:
    """Folders a bare `openscad` resolves `use <X/...>` against, in its order: OPENSCADPATH
    entries first, then the user library folder. The repo libs/ that the 3d dispatcher
    prepends for its own renders is left out, and so is the built-in app library folder
    (last in order, MCAD only)."""
    private = (Path(repo_root()) / "libs").resolve()
    out: list[Path] = []
    for part in os.environ.get("OPENSCADPATH", "").split(os.pathsep):
        if part and Path(part).resolve() != private and Path(part) not in out:
            out.append(Path(part))
    user = user_library_dir()
    if user not in out:
        out.append(user)
    return out


def locate(name: str) -> Path | None:
    """The folder OpenSCAD would load `name` from, or None if it cannot find one."""
    for base in search_path():
        cand = base / name
        if cand.is_dir():
            return cand
    return None


def read_manifest(lib_dir: Path) -> dict[str, object] | None:
    try:
        data = json.loads((lib_dir / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Install / uninstall.
# ---------------------------------------------------------------------------
def _fs_error(action: str, exc: OSError) -> ThreeDError:
    return ThreeDError(
        f"cannot {action}: {exc}",
        command=ERR_CTX,
        remediation=[
            "macOS: allow your terminal to access the Documents folder (System Settings > "
            "Privacy & Security > Files and Folders, or Full Disk Access), then retry.",
            "Or install elsewhere with --dir DIR and add DIR to OPENSCADPATH.",
        ],
    )


_GIT_REPO_VARS = frozenset({"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                            "GIT_OBJECT_DIRECTORY", "GIT_NAMESPACE", "GIT_PREFIX"})


def _stage_git(spec: LibSpec, stage: Path) -> str:
    """Shallow-clone spec.git into `stage` (a fresh private temp dir); returns the commit."""
    assert spec.git is not None
    if shutil.which("git") is None:
        raise MissingDependency(
            "git",
            install="brew install git  (macOS) / sudo apt install git  (Debian/Ubuntu)",
            degrades=f"cannot install {spec.name} from {spec.git}",
            command=ERR_CTX,
        )
    cmd = ["git", "clone", "--quiet", "--depth", "1"]
    if spec.ref:
        cmd += ["--branch", spec.ref]
    # Run from a git hook (which exports GIT_DIR/GIT_INDEX_FILE/...), git would otherwise
    # act on the caller's repository instead of the fresh clone.
    env = {k: v for k, v in os.environ.items() if k not in _GIT_REPO_VARS}
    proc = subprocess.run([*cmd, spec.git, str(stage)], capture_output=True, text=True,
                          check=False, env=env)
    if proc.returncode != 0:
        raise ThreeDError(
            f"git clone of {spec.git} failed: {proc.stderr.strip() or proc.stdout.strip()}",
            command=ERR_CTX,
            remediation=["Check the URL and your network, then retry.",
                         "Pin a branch/tag that exists with --ref NAME."],
        )
    return subprocess.run(
        ["git", "-C", str(stage), "rev-parse", "HEAD"], capture_output=True, text=True,
        check=False, env=env,
    ).stdout.strip()


def _source_files(src: Path) -> list[Path]:
    """Regular files to copy, relative to src. Skips hidden folders (.git, .github CI
    config, ...), a stale manifest, and anything that is not a regular file."""
    files: list[Path] = []
    for root, dirs, names in os.walk(src):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for n in sorted(names):
            path = Path(root) / n
            if n != MANIFEST and path.is_file():
                files.append(path.relative_to(src))
    return files


def _remove_files(lib_dir: Path, files: list[str]) -> list[str]:
    """Delete the listed files (relative to lib_dir), then the directories that held them
    once empty, deepest first. Returns the folders kept because they still hold files
    3d did not install. Entries that resolve outside
    lib_dir (a tampered manifest) are never touched."""
    root = lib_dir.resolve()
    leftovers: list[str] = []
    inside = [rel for rel in files if (lib_dir / rel).resolve().is_relative_to(root)]
    for rel in inside:
        try:
            (lib_dir / rel).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise _fs_error(f"remove {lib_dir / rel}", exc) from exc
    dirs = {lib_dir / p for rel in inside for p in Path(rel).parents if str(p) != "."}
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True) + [lib_dir]:
        try:
            d.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            leftovers.append(str(d))
    return leftovers


@dataclass(frozen=True)
class InstallResult:
    path: Path
    files: int
    source: str
    commit: str | None
    already: bool = False
    kept: Path | None = None  # replaced copy left in place: it still holds files 3d did not install


def install(spec: LibSpec, target_dir: Path | None = None, force: bool = False) -> InstallResult:
    """Copy `spec` into `target_dir` (default: the user library folder) as `<name>/`.
    Idempotent: an existing 3d-managed install is left alone unless `force`.

    Transactional: the new copy and its manifest are built in a hidden staging folder inside
    `target_dir` (same filesystem), then renamed into place. A failed clone (offline, dead
    URL, bad --ref) or copy raises before the existing folder is touched."""
    base = target_dir or user_library_dir()
    dest = base / spec.name
    replace_foreign = False  # --force over a folder 3d did not install: it may be deleted
    if os.path.lexists(dest):
        manifest = read_manifest(dest)
        if manifest is None and not force:
            raise UsageError(
                f"{dest} already exists and was not installed by 3d",
                command=ERR_CTX,
                remediation=[f"Move it away, or replace it: 3d openscad libs {spec.name} install --force"],
            )
        if manifest is not None and not force:
            return InstallResult(dest, len(_manifest_files(manifest)), _source(manifest),
                                 _str(manifest, "commit"), already=True)
        replace_foreign = manifest is None

    try:
        base.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f".{spec.name}.3d-staging-", dir=base))
    except OSError as exc:
        raise _fs_error(f"create a staging folder in {base}", exc) from exc
    # The new copy and, once swapped out, the one it replaces (names differ by construction).
    new, previous = stage / spec.name, stage / f"{spec.name}.previous"
    try:
        manifest_data = _build(spec, new)
        try:
            if os.path.lexists(dest):
                os.rename(dest, previous)
            os.rename(new, dest)
        except OSError as exc:
            raise _fs_error(f"move the new {spec.name} into {dest}", exc) from exc
    except BaseException as exc:
        if os.path.lexists(previous):
            _put_back(previous, dest, exc)  # raises, keeping `stage`, unless it is back at dest
        shutil.rmtree(stage, ignore_errors=True)
        raise
    try:
        kept = _retire(previous, dest, replace_foreign) if os.path.lexists(previous) else None
    except BaseException as exc:  # e.g. Ctrl-C while moving your files over
        if not os.path.lexists(previous):
            raise
        raise ThreeDError(
            f"{spec.name} was replaced, but tidying up the previous copy stopped "
            f"({str(exc) or type(exc).__name__}); files you added may still be at {previous}",
            command=ERR_CTX,
            remediation=[f"Move what you need from {previous} into {dest}, then delete {stage}."],
        ) from exc
    if kept is None:
        shutil.rmtree(stage, ignore_errors=True)
    return InstallResult(dest, len(_manifest_files(manifest_data)), _source(manifest_data),
                         _str(manifest_data, "commit"), kept=kept)


def _build(spec: LibSpec, into: Path) -> dict[str, object]:
    """Fetch `spec` and put its files plus manifest into the fresh folder `into`; returns the
    manifest. Touches nothing else, so any failure here leaves an existing install as is."""
    if spec.git:
        try:
            clone_root = paths.data_dir() / "openscad-libs"
            clone_root.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(prefix=f"{spec.name}-", dir=clone_root))
        except OSError as exc:
            raise _fs_error(f"create a staging folder under {paths.data_dir()}", exc) from exc
        try:
            src = tmp / spec.name
            commit: str | None = _stage_git(spec, src)
            return _copy_in(spec, src, into, commit)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    assert spec.local is not None
    src = Path(repo_root()) / spec.local
    if not src.is_dir():
        raise ThreeDError(
            f"library source {src} is missing from this 3d checkout",
            command=ERR_CTX,
            remediation=["Update 3d-cli (git pull) or reinstall it."],
        )
    return _copy_in(spec, src, into, None)


def _copy_in(spec: LibSpec, src: Path, into: Path, commit: str | None) -> dict[str, object]:
    """Copy the library files from src into `into` and write their manifest there."""
    files = _source_files(src)
    manifest_data: dict[str, object] = {
        "name": spec.name,
        "git": spec.git,
        "ref": spec.ref,
        "local": None if spec.git else str(src),
        "homepage": spec.homepage,
        "commit": commit,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": [rel.as_posix() for rel in files],
    }
    try:
        into.mkdir(parents=True, exist_ok=True)
        for rel in files:
            (into / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, into / rel)
        (into / MANIFEST).write_text(json.dumps(manifest_data, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise _fs_error(f"stage {spec.name} in {into}", exc) from exc
    return manifest_data


def _put_back(previous: Path, dest: Path, exc: BaseException) -> None:
    """Undo a swap that failed or was interrupted (Ctrl-C) after the old library was renamed
    to `previous`: rename it back to `dest`. If that is impossible, raise an error saying
    where it is, so a replaced library is never left behind silently."""
    try:
        if not os.path.lexists(dest):
            os.rename(previous, dest)
            return
        problem = f"the new {dest.name} is already in place"
    except BaseException as restore_exc:  # a second Ctrl-C included
        problem = f"it could not be put back ({str(restore_exc) or type(restore_exc).__name__})"
    raise ThreeDError(
        f"replacing {dest} failed ({str(exc) or type(exc).__name__}) and {problem}; "
        f"the previous copy is at {previous}",
        command=ERR_CTX,
        remediation=[f"Move {previous} back to {dest} (or copy what you need from it), "
                     f"then delete {previous.parent}."],
    ) from exc


def _retire(previous: Path, dest: Path, replace_foreign: bool) -> Path | None:
    """Dispose of the copy `dest` replaced, now at `previous`. A link or file there is
    unlinked (never what a link points to). A 3d install, judged by the manifest inside it
    (so a concurrent update is retired by what it really installed), loses only its recorded
    files; everything you added moves into the new install unless the new library ships the
    same path. A folder 3d did not install is deleted only when --force asked to replace it.
    Returns `previous` if anything had to stay there, else None."""
    try:
        if previous.is_symlink() or not previous.is_dir():
            previous.unlink()
            return None
    except OSError:
        return previous
    manifest = read_manifest(previous)
    if manifest is None:
        if replace_foreign:
            shutil.rmtree(previous, ignore_errors=True)
        return previous if os.path.lexists(previous) else None
    try:
        if not _remove_files(previous, _manifest_files(manifest) + [MANIFEST]):
            return None  # it held only what 3d installed and is gone
    except ThreeDError:
        return previous
    _move_into(previous, dest)
    for root, dirs, _ in os.walk(previous, topdown=False):
        for d in dirs:
            try:
                (Path(root) / d).rmdir()
            except OSError:
                pass
    try:
        previous.rmdir()
    except OSError:
        return previous
    return None


def _move_into(src_root: Path, dest: Path) -> None:
    """Move everything under `src_root` to the same relative path under `dest`: a whole
    folder (empty ones too) where `dest` has none of that name, else descend into it. Paths
    `dest` already has stay where they are."""
    for root, dirs, names in os.walk(src_root):
        descend: list[str] = []
        for n in dirs + names:
            yours = Path(root) / n
            target = dest / yours.relative_to(src_root)
            if not os.path.lexists(target):
                try:
                    os.rename(yours, target)
                except OSError:
                    pass
            elif (n in dirs and target.is_dir() and not target.is_symlink()
                  and not yours.is_symlink()):
                descend.append(n)
        dirs[:] = descend


def _manifest_files(manifest: dict[str, object]) -> list[str]:
    files = manifest.get("files")
    return [f for f in files if isinstance(f, str)] if isinstance(files, list) else []


def _str(manifest: dict[str, object], key: str) -> str | None:
    v = manifest.get(key)
    return v if isinstance(v, str) else None


def _source(manifest: dict[str, object]) -> str:
    """Human-readable origin: `url[@ref]` for git, the copied-from path otherwise."""
    git = _str(manifest, "git")
    if git:
        ref = _str(manifest, "ref")
        return f"{git}@{ref}" if ref else git
    return _str(manifest, "local") or "?"


def installed_dir(name: str, target_dir: Path | None = None) -> Path:
    """The 3d-managed install of `name`; raises if there is none."""
    folder = canonical_name(name)
    dest = (target_dir or user_library_dir()) / folder
    if read_manifest(dest) is None:
        where = "exists but was not installed by 3d" if dest.exists() else "is not installed"
        raise ThreeDError(
            f"{folder} {where} ({dest})",
            command=ERR_CTX,
            remediation=[f"Install it: 3d openscad libs {folder} install"],
        )
    return dest


def uninstall(name: str, target_dir: Path | None = None) -> tuple[Path, int, list[str]]:
    """Remove a 3d-managed install. Returns (path, files removed, leftover dirs)."""
    dest = installed_dir(name, target_dir)
    manifest = read_manifest(dest) or {}
    files = _manifest_files(manifest)
    leftovers = _remove_files(dest, files + [MANIFEST])
    return dest, len(files), leftovers


def update(name: str, target_dir: Path | None = None) -> InstallResult:
    """Reinstall from the recorded source (git: a fresh clone of the same URL/ref; repo
    libraries: this checkout). Files dropped upstream are removed too; files you added are
    kept. If the source cannot be fetched the current install is left exactly as it was."""
    dest = installed_dir(name, target_dir)
    manifest = read_manifest(dest) or {}
    git = _str(manifest, "git")
    if git:
        spec = resolve_spec(dest.name, git=git, ref=_str(manifest, "ref"))
    else:
        spec = resolve_spec(dest.name)
    return install(spec, target_dir, force=True)


def unmanaged_dirs(base: Path) -> list[str] | None:
    """Library folders in `base` that 3d did not install; None if `base` can't be listed
    (missing, or listing denied by macOS privacy protection)."""
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return None
    return [e for e in entries if (base / e).is_dir() and not e.startswith(".")
            and read_manifest(base / e) is None]
