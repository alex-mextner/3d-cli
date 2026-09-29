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
  Git libraries are shallow-cloned into a temporary folder under the 3d data dir; their
  files (minus hidden folders such as .git/.github) are copied over and the clone removed.

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


def install(spec: LibSpec, target_dir: Path | None = None, force: bool = False) -> InstallResult:
    """Copy `spec` into `target_dir` (default: the user library folder) as `<name>/`.
    Idempotent: an existing 3d-managed install is left alone unless `force`."""
    base = target_dir or user_library_dir()
    dest = base / spec.name
    if dest.exists():
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
        if manifest is not None:
            _remove_files(dest, _manifest_files(manifest) + [MANIFEST])
        else:
            try:
                shutil.rmtree(dest)
            except OSError as exc:
                raise ThreeDError(
                    f"cannot remove the existing {dest}: {exc}",
                    command=ERR_CTX,
                    remediation=[f"Delete {dest} by hand (Finder / a terminal with disk access), then retry."],
                ) from exc

    if spec.git:
        try:
            stage_root = paths.data_dir() / "openscad-libs"
            stage_root.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(prefix=f"{spec.name}-", dir=stage_root))
        except OSError as exc:
            raise _fs_error(f"create a staging folder under {paths.data_dir()}", exc) from exc
        try:
            src = tmp / spec.name
            commit: str | None = _stage_git(spec, src)
            return _copy_in(spec, src, dest, commit)
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
    return _copy_in(spec, src, dest, None)


def _copy_in(spec: LibSpec, src: Path, dest: Path, commit: str | None) -> InstallResult:
    """Copy the library files from src to dest and record them in the manifest."""
    files = _source_files(src)
    try:
        for rel in files:
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, dest / rel)
    except OSError as exc:
        _remove_files(dest, [rel.as_posix() for rel in files])
        raise _fs_error(f"install {spec.name} into {dest}", exc) from exc
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
        (dest / MANIFEST).write_text(json.dumps(manifest_data, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        _remove_files(dest, [rel.as_posix() for rel in files])
        raise _fs_error(f"write {dest / MANIFEST}", exc) from exc
    return InstallResult(dest, len(files), _source(manifest_data), commit)


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
    libraries: this checkout). Files dropped upstream are removed too."""
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
