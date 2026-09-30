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

  Every install, update and uninstall of a library holds an exclusive per-library lock
  (<3d data dir>/openscad-libs/locks/<name>.lock, flock), so overlapping runs wait for each
  other instead of interleaving; the OS drops the lock when the process ends, SIGKILL
  included, so a dead run never blocks the next. The same lock proves leftovers stale:
  a `.<name>.3d-staging-*` folder or a clone folder seen while holding it belongs to no
  live run. sweep_stale() deletes those holding only a half-built copy or a download and
  keeps (and reports) any that holds more than that, such as a previous copy of the library.
  A replaced library folder keeps the group and mode (and on macOS the ACL) of the one it
  replaces.

INVARIANTS: stdlib-only; command modules reach it lazily. Errors are lib/errors.py types.
"""
from __future__ import annotations

import errno
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
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


def _is_safe_name(name: str) -> bool:
    return bool(_SAFE_NAME.fullmatch(name)) and ".." not in name


def canonical_name(name: str) -> str:
    """The library folder name for `name`: the registry spelling for known libraries,
    else `name` itself. It must be one plain path component — never `..`, a separator or
    an absolute path — because it is joined onto the library folder we write and delete in."""
    if not _is_safe_name(name):
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


# ---------------------------------------------------------------------------
# One mutation of a library at a time.
# ---------------------------------------------------------------------------
def _lock_path(name: str) -> Path:
    """One lock file per library, whatever folder it is installed in; lower-cased because
    `WriteText` and `writetext` are the same folder on a case-insensitive filesystem. On
    Linux that also makes the two distinct libraries `Foo` and `foo` wait for each other:
    harmless, and the alternative (a per-platform key) is not worth a second code path."""
    return paths.data_dir() / "openscad-libs" / "locks" / f"{canonical_name(name).lower()}.lock"


def _lock_error(action: str, path: Path, exc: OSError) -> ThreeDError:
    return ThreeDError(
        f"cannot {action} {path}: {exc}",
        command=ERR_CTX,
        remediation=[f"Make {path.parent} writable, or point XDG_DATA_HOME at a folder that is."],
    )


def _lock(fd: int, wait: bool) -> bool:
    """Take the exclusive lock on the open file `fd`. With `wait` off, return False when
    another open of the file holds it; with `wait` on, block until it is free."""
    if sys.platform == "win32":  # no blocking byte-range lock that Ctrl-C can interrupt
        import msvcrt

        while True:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EDEADLOCK):
                    raise  # not contention: a structured error, not an endless retry
                if not wait:
                    return False
                time.sleep(0.2)
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
    except BlockingIOError:
        return False
    return True


def _open_locked(name: str, wait: bool) -> tuple[int, bool]:
    """Open the lock file of `name` and try to lock it; returns (fd, whether it is held)."""
    path = _lock_path(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)  # not inheritable: children never hold it
    except OSError as exc:
        raise _lock_error("create the lock file", path, exc) from exc
    try:
        held = _lock(fd, wait=False)
        if not held and wait:
            print(f"waiting for another `3d openscad libs` run on {name} to finish ...",
                  file=sys.stderr, flush=True)
            held = _lock(fd, wait=True)
    except OSError as exc:
        os.close(fd)
        raise _lock_error("lock", path, exc) from exc
    except BaseException:  # Ctrl-C while waiting
        os.close(fd)
        raise
    return fd, held


@contextmanager
def library_lock(name: str, *, wait: bool = True) -> Iterator[bool]:
    """Hold the exclusive lock of library `name` for the block. Every run by this user
    that changes `name` (in any --dir) queues here; the lock file is in the user's own
    data dir, so another user's runs on a shared --dir are not covered. With `wait` (the default) a busy lock is
    waited for and announced on stderr, and the block always runs holding it; with `wait`
    off a busy lock yields False and the block holds nothing. The lock is an flock on a
    file that stays in place: the OS releases it when the holder exits, however it dies, so
    a killed run never leaves a stale lock."""
    fd, held = _open_locked(name, wait)
    try:
        yield held
    finally:
        os.close(fd)  # releases the lock


# ---------------------------------------------------------------------------
# Leftovers of interrupted installs.
# ---------------------------------------------------------------------------
_MKDTEMP_SUFFIX = "[a-z0-9_]{8}"  # the part tempfile.mkdtemp appends to a prefix


@dataclass(frozen=True)
class Stale:
    """What sweep_stale found. `removed` held only a half-built copy or a download. `kept`
    hold more than that (a previous copy of the library, files you added, anything 3d did
    not create) and were not touched. `stuck` should have been removed but could not be."""

    removed: tuple[Path, ...] = ()
    kept: tuple[Path, ...] = ()
    stuck: tuple[tuple[Path, str], ...] = ()  # (path, why it could not be removed)


@dataclass(frozen=True)
class Leftover:
    """A staging folder in a library folder that no running 3d owns."""

    path: Path
    name: str
    half_built: bool  # holds only a half-built new copy; False: it holds more, never deleted by 3d


def _name_match(pattern: str) -> re.Pattern[str]:
    """Library folders are case-insensitive on macOS and Windows, so `mylib` and `MyLib`
    (custom --git names; registry names are canonical) are one library there, and their
    leftovers must match alike. On Linux they are two libraries and must not."""
    return re.compile(pattern, re.IGNORECASE if sys.platform in ("darwin", "win32") else 0)


def _staging_folders(base: Path, name: str | None = None) -> list[tuple[str, Path]]:
    """(library name, path) of the hidden `.<name>.3d-staging-*` folders in `base`: those of
    `name`, or of every library with a valid folder name when `name` is None."""
    pattern = _name_match(rf"\.({re.escape(name) if name else '.+'})\.3d-staging-{_MKDTEMP_SUFFIX}")
    try:
        entries = sorted(os.listdir(base))
    except OSError:  # missing, or listing denied by macOS privacy protection
        return []
    found: list[tuple[str, Path]] = []
    for entry in entries:
        match = pattern.fullmatch(entry)
        if (match and _is_safe_name(match[1])
                and (base / entry).is_dir() and not (base / entry).is_symlink()):
            found.append((match[1], base / entry))
    return found


def _clone_folders(name: str) -> list[Path]:
    """The `<name>-*` folders git libraries are cloned into under the 3d data dir."""
    root = paths.data_dir() / "openscad-libs"
    pattern = _name_match(rf"{re.escape(name)}-{_MKDTEMP_SUFFIX}")
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return []
    return [root / e for e in entries
            if pattern.fullmatch(e) and (root / e).is_dir() and not (root / e).is_symlink()]


def _holds_only_new_copy(stage: Path, name: str) -> bool:
    """True if the staging folder holds nothing but a half-built new `<name>`: no previous
    copy, and no file the new copy's own manifest does not list (a folder with no manifest
    qualifies only if it holds no file at all). Such a folder is 3d's own work in progress
    and safe to delete. Anything else, a stray .DS_Store or a note you left in the copy
    included, makes it False: that folder is kept, not deleted."""
    try:
        if set(os.listdir(stage)) - {name}:
            return False
        copy = stage / name
        if not os.path.lexists(copy):
            return True
        if copy.is_symlink() or not copy.is_dir():
            return False
        manifest = read_manifest(copy)
        known = {*_manifest_files(manifest), MANIFEST} if manifest else set()
        for root, dirs, files in os.walk(copy):
            if any((Path(root) / d).is_symlink() for d in dirs):
                return False
            if any((Path(root) / f).relative_to(copy).as_posix() not in known for f in files):
                return False
        return True
    except OSError:
        return False


def sweep_stale(name: str, target_dir: Path | None = None) -> Stale:
    """Delete what an interrupted install or update of `name` left behind: the staging
    folders in `target_dir` (default: the user library folder) that hold only a half-built
    copy, and the git clone folders. A staging folder that holds anything more is kept.
    Runs under the library lock (waiting for a live run to end first) and lists the folders
    only once it holds it, so a folder it touches cannot belong to a running install."""
    folder = canonical_name(name)
    base = target_dir or user_library_dir()
    removed: list[Path] = []
    kept: list[Path] = []
    stuck: list[tuple[Path, str]] = []

    def delete(path: Path) -> None:
        try:
            shutil.rmtree(path)
        except OSError as exc:
            stuck.append((path, str(exc)))
        else:
            removed.append(path)

    with library_lock(folder):
        for lib, stage in _staging_folders(base, folder):
            if _holds_only_new_copy(stage, lib):
                delete(stage)
            else:
                kept.append(stage)
        for clone in _clone_folders(folder):
            delete(clone)
    return Stale(tuple(removed), tuple(kept), tuple(stuck))


def find_leftovers(base: Path) -> list[Leftover]:
    """Staging folders in `base` that no running 3d owns, for `list` to report. Never
    waits and never deletes: a library whose lock is held is being worked on right now,
    so its staging folder is skipped, and so is one whose lock file cannot be used (nobody
    can tell whether it is stale)."""
    by_lock: dict[str, list[tuple[str, Path]]] = {}
    for lib, stage in _staging_folders(base):
        by_lock.setdefault(lib.lower(), []).append((lib, stage))  # one lock per library, as in _lock_path
    out: list[Leftover] = []
    for key in sorted(by_lock):
        try:
            with library_lock(by_lock[key][0][0], wait=False) as free:
                if free:
                    out += [Leftover(stage, lib, _holds_only_new_copy(stage, lib))
                            for lib, stage in by_lock[key]]
        except ThreeDError:
            continue
    return out


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
    URL, bad --ref) or copy raises before the existing folder is touched. Runs under the
    library lock: a concurrent install/update/uninstall of the same library waits."""
    with library_lock(spec.name):
        return _install_locked(spec, target_dir, force)


def _install_locked(spec: LibSpec, target_dir: Path | None, force: bool) -> InstallResult:
    """install() with the library lock already held (update() reads the manifest and
    installs under one hold, and flock does not nest)."""
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
        _carry_over_attrs(dest, new)
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
    """Copy the library files from src into `into` and write their manifest there. The
    manifest goes in first: a run killed mid-copy leaves a folder whose every file the
    manifest lists, which is how sweep_stale tells it from one that also holds yours."""
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
        (into / MANIFEST).write_text(json.dumps(manifest_data, indent=2) + "\n", encoding="utf-8")
        for rel in files:
            (into / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, into / rel)
    except OSError as exc:
        raise _fs_error(f"stage {spec.name} in {into}", exc) from exc
    return manifest_data


def _carry_over_attrs(old: Path, new: Path) -> None:
    """Give the fresh folder `new` the group and mode (and, on macOS, the ACL) of the library
    folder `old` it is about to replace, so a permission you set on the folder itself survives
    an update. Each attribute is best effort and independent: one that cannot be copied (a
    group you are not in, setgid for it, no ACL support) is skipped, never an error and never
    a reason to skip the others. Times, flags and extended attributes are not copied. A link
    or file at `old` has nothing worth carrying."""
    try:
        st = os.lstat(old)
    except OSError:
        return
    if not stat.S_ISDIR(st.st_mode):
        return
    try:
        if hasattr(os, "chown") and os.lstat(new).st_gid != st.st_gid:
            os.chown(new, -1, st.st_gid)
    except OSError:
        pass
    mode = stat.S_IMODE(st.st_mode)
    for wanted in (mode, mode & 0o777):  # chown may have cleared setgid; without it: plain bits
        try:
            os.chmod(new, wanted)
            break
        except OSError:
            continue
    if sys.platform == "darwin":
        _copy_macos_acl(old, new)


def _copy_macos_acl(old: Path, new: Path) -> None:
    """macOS ACL entries are not visible to the xattr API; move them with the system tools."""
    try:
        listing = subprocess.run(["/bin/ls", "-led", str(old.absolute())], capture_output=True,
                                 text=True, check=False, timeout=30).stdout
        entries = [m[1] for line in listing.splitlines()[1:]
                   if (m := re.fullmatch(r" *\d+: (.+)", line))]
        if entries:
            subprocess.run(["/bin/chmod", "-E", str(new.absolute())], input="\n".join(entries) + "\n",
                           text=True, capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


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
    """Remove a 3d-managed install. Returns (path, files removed, leftover dirs). Runs
    under the library lock."""
    with library_lock(canonical_name(name)):
        dest = installed_dir(name, target_dir)
        manifest = read_manifest(dest) or {}
        files = _manifest_files(manifest)
        leftovers = _remove_files(dest, files + [MANIFEST])
        return dest, len(files), leftovers


def update(name: str, target_dir: Path | None = None) -> InstallResult:
    """Reinstall from the recorded source (git: a fresh clone of the same URL/ref; repo
    libraries: this checkout). Files dropped upstream are removed too; files you added are
    kept. If the source cannot be fetched the current install is left exactly as it was.
    Runs under the library lock, from reading the recorded source to the swap."""
    folder = canonical_name(name)
    with library_lock(folder):
        dest = installed_dir(name, target_dir)
        manifest = read_manifest(dest) or {}
        git = _str(manifest, "git")
        if git:
            spec = resolve_spec(dest.name, git=git, ref=_str(manifest, "ref"))
        else:
            spec = resolve_spec(dest.name)
        return _install_locked(spec, target_dir, force=True)


def unmanaged_dirs(base: Path) -> list[str] | None:
    """Library folders in `base` that 3d did not install; None if `base` can't be listed
    (missing, or listing denied by macOS privacy protection)."""
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return None
    return [e for e in entries if (base / e).is_dir() and not e.startswith(".")
            and read_manifest(base / e) is None]
