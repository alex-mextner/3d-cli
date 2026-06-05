"""env.py — environment + dependency helpers for the `3d` CLI (port of common.sh).

Pure-stdlib so it is safe to import from the dispatcher and any command module:
  - repo_root()                : the 3d-cli repo root (resolved through the bin/3d symlink).
  - export_openscadpath()      : prepend repo libs/ to OPENSCADPATH (subprocesses inherit it).
  - find_openscad/magick/slicer: locate external tools (PATH + macOS app bundles).
  - require_openscad/magick    : same, but raise MissingDependency with the OS install line.
  - detect_os / install_cmd    : OS + per-OS install command (shared by doctor).
  - resolve_python / py_has_module : which python pyrun would use + import probe.
  - maybe_bootstrap()          : first-run OpenSCAD-library clone (idempotent, offline-safe).

External-tool invocation stays subprocess-based (we never import trimesh etc. here);
heavy python work runs through `cli.pyrun`.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

from errors import MissingDependency

# ---------------------------------------------------------------------------
# Repo root — resolve through the bin/3d symlink. This file lives at
# <repo>/lib/cli/env.py, so the repo root is two parents up from lib/.
# ---------------------------------------------------------------------------
_REPO_ROOT: str | None = None


def repo_root() -> str:
    global _REPO_ROOT
    if _REPO_ROOT is not None:
        return _REPO_ROOT
    env = os.environ.get("REPO_ROOT")
    if env:
        _REPO_ROOT = env
    else:
        here = pathlib.Path(__file__).resolve()
        _REPO_ROOT = str(here.parents[2])  # lib/cli/env.py -> repo/
    return _REPO_ROOT


def export_openscadpath() -> None:
    """Prepend the repo libs/ dir to OPENSCADPATH so `include <BOSL2/std.scad>` resolves
    in every OpenSCAD subprocess. Idempotent (no double-prepend)."""
    libs = os.path.join(repo_root(), "libs")
    if not os.path.isdir(libs):
        return
    cur = os.environ.get("OPENSCADPATH", "")
    parts = cur.split(os.pathsep) if cur else []
    if libs in parts:
        return
    os.environ["OPENSCADPATH"] = os.pathsep.join([libs, *parts]) if parts else libs


# ---------------------------------------------------------------------------
# External-tool discovery (PATH + common macOS app bundles).
# ---------------------------------------------------------------------------
def find_openscad() -> str | None:
    env = os.environ.get("OPENSCAD")
    if env and shutil.which(env):
        return env
    w = shutil.which("openscad")
    if w:
        return w
    for p in (
        "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD",
        "/opt/homebrew/bin/openscad",
        "/usr/local/bin/openscad",
    ):
        if os.access(p, os.X_OK):
            return p
    return None


def require_openscad(command: str | None = None) -> str:
    osc = find_openscad()
    if osc is None:
        raise MissingDependency(
            "OpenSCAD",
            install=install_cmd("openscad"),
            degrades="render / export / check / silhouette / slice cannot run",
            command=command,
        )
    os.environ["OPENSCAD"] = osc
    return osc


def find_magick() -> str | None:
    for c in ("magick",):
        w = shutil.which(c)
        if w:
            return c
    for p in ("/opt/homebrew/bin/magick", "/usr/local/bin/magick"):
        if os.access(p, os.X_OK):
            return p
    # IM6 legacy: `convert` exists but no `magick`.
    if shutil.which("convert"):
        return "convert"
    return None


def magick_compare(magick: str) -> list[str]:
    """The `compare` invocation for this ImageMagick (IM7 ships `magick compare`,
    IM6 a separate `compare` binary)."""
    return ["magick", "compare"] if magick == "magick" else ["compare"]


def require_magick(command: str | None = None) -> str:
    mgk = find_magick()
    if mgk is None:
        raise MissingDependency(
            "ImageMagick",
            install=install_cmd("imagemagick"),
            degrades="silhouette / score / overlay (image diffing) cannot run",
            command=command,
        )
    return mgk


# Slicer preference: OrcaSlicer > Bambu Studio > PrusaSlicer. Each is checked across
# BOTH PATH and macOS bundles before falling to the next, so preference holds regardless
# of install location. Returns (kind, path) or None.
_SLICER_TABLE: list[tuple[str, list[str], list[str]]] = [
    (
        "orca",
        ["orca-slicer", "OrcaSlicer"],
        ["/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"],
    ),
    (
        "bambu",
        ["bambu-studio", "BambuStudio", "bambu-studio-cli"],
        [
            "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio",
            "/Applications/Bambu Studio.app/Contents/MacOS/BambuStudio",
        ],
    ),
    (
        "prusa",
        ["prusa-slicer", "PrusaSlicer", "prusa-slicer-console"],
        [
            "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer",
            "/Applications/Original Prusa Drivers/PrusaSlicer.app/Contents/MacOS/PrusaSlicer",
        ],
    ),
]


def find_slicer() -> tuple[str, str] | None:
    forced = os.environ.get("SLICER")
    if forced and os.access(forced, os.X_OK):
        return ("custom", forced)
    for kind, cmds, bundles in _SLICER_TABLE:
        for c in cmds:
            w = shutil.which(c)
            if w:
                return (kind, w)
        for p in bundles:
            if os.access(p, os.X_OK):
                return (kind, p)
    return None


# ---------------------------------------------------------------------------
# OS detection + install-command table (shared with `3d doctor`).
# ---------------------------------------------------------------------------
def detect_os() -> str:
    """One of: macos linux-apt linux-dnf linux-pacman linux-unknown other."""
    plat = sys.platform
    if plat == "darwin":
        return "macos"
    if plat.startswith("linux"):
        if shutil.which("apt-get"):
            return "linux-apt"
        if shutil.which("dnf"):
            return "linux-dnf"
        if shutil.which("pacman"):
            return "linux-pacman"
        return "linux-unknown"
    return "other"


def sudo_prefix() -> str:
    if os.geteuid() == 0 if hasattr(os, "geteuid") else False:
        return ""
    return "sudo " if shutil.which("sudo") else ""


# tool name -> per-OS install command. Mirrors common.sh/cmd_doctor's table.
_INSTALL: dict[str, dict[str, str]] = {
    "openscad": {
        "macos": "brew install --cask openscad",
        "linux-apt": "{sudo}apt-get install -y openscad",
        "linux-dnf": "{sudo}dnf install -y openscad",
        "linux-pacman": "{sudo}pacman -S --noconfirm openscad",
    },
    "imagemagick": {
        "macos": "brew install imagemagick",
        "linux-apt": "{sudo}apt-get install -y imagemagick",
        "linux-dnf": "{sudo}dnf install -y ImageMagick",
        "linux-pacman": "{sudo}pacman -S --noconfirm imagemagick",
    },
    "python3": {
        "macos": "brew install python",
        "linux-apt": "{sudo}apt-get install -y python3 python3-venv python3-pip",
        "linux-dnf": "{sudo}dnf install -y python3 python3-pip",
        "linux-pacman": "{sudo}pacman -S --noconfirm python python-pip",
    },
    "uv": {
        "macos": "brew install uv  # or: curl -LsSf https://astral.sh/uv/install.sh | sh",
        "linux-apt": "curl -LsSf https://astral.sh/uv/install.sh | sh",
        "linux-dnf": "curl -LsSf https://astral.sh/uv/install.sh | sh",
        "linux-pacman": "{sudo}pacman -S --noconfirm uv  # or astral install script",
    },
    "slicer": {
        "macos": "brew install --cask orcaslicer  # or bambu-studio / prusaslicer",
        "linux-apt": "download OrcaSlicer AppImage from github.com/SoftFever/OrcaSlicer/releases",
        "linux-dnf": "download OrcaSlicer AppImage from github.com/SoftFever/OrcaSlicer/releases",
        "linux-pacman": "{sudo}pacman -S --noconfirm orca-slicer  # (AUR) or AppImage",
    },
}


def install_cmd(tool: str) -> str:
    os_name = detect_os()
    table = _INSTALL.get(tool, {})
    cmd = table.get(os_name)
    if cmd is None:
        return f"(no package map for OS={os_name} — install '{tool}' manually)"
    return cmd.format(sudo=sudo_prefix())


# ---------------------------------------------------------------------------
# Python runtime resolution (mirrors lib/pyrun's venv > system probe).
# ---------------------------------------------------------------------------
# Import-name -> pip package where they differ (used by doctor).
PY_MESH_MODULES = ["trimesh", "manifold3d", "numpy", "scipy", "rtree", "PIL", "cv2"]


def pypkg_for(mod: str) -> str:
    return {"PIL": "pillow", "cv2": "opencv-python-headless"}.get(mod, mod)


def resolve_python() -> str | None:
    venv = os.path.join(repo_root(), ".venv", "bin", "python")
    if os.access(venv, os.X_OK):
        return venv
    w = shutil.which("python3")
    return w if w else None


def py_has_module(mod: str) -> bool:
    py = resolve_python()
    if py is None:
        return False
    try:
        r = subprocess.run(
            [py, "-c", f"import importlib; importlib.import_module('{mod}')"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return r.returncode == 0
    except OSError:
        return False


# ---------------------------------------------------------------------------
# First-run bootstrap of the OpenSCAD libraries (BOSL2, NopSCADlib) into libs/.
# Gated by ~/.config/3d/.bootstrapped. Quiet, idempotent, NON-FATAL if offline —
# it must never block `3d help`/`render`. (Keep the /3d/ marker path: switching it
# would silently re-trigger bootstrap against a live marker.)
# ---------------------------------------------------------------------------
_BOOTSTRAP_LIBS = [
    ("https://github.com/BelfrySCAD/BOSL2.git", "BOSL2"),
    ("https://github.com/nophead/NopSCADlib.git", "NopSCADlib"),
]


def _state_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "3d")


def bootstrap_marker() -> str:
    return os.path.join(_state_dir(), ".bootstrapped")


def maybe_bootstrap() -> None:
    """First-run hook: clone the OpenSCAD libs once. Always returns (non-fatal)."""
    marker = bootstrap_marker()
    if os.path.isfile(marker):  # fast path: single stat
        return
    if shutil.which("git") is None:
        return  # no git -> can't bootstrap, skip quietly
    libs_dir = os.path.join(repo_root(), "libs")
    try:
        os.makedirs(libs_dir, exist_ok=True)
    except OSError:
        return

    # bounded clone so an offline/slow network can NEVER stall `3d help`/`render`.
    timeout_cmd: list[str] = []
    if shutil.which("timeout"):
        timeout_cmd = ["timeout", "60"]
    elif shutil.which("gtimeout"):
        timeout_cmd = ["gtimeout", "60"]

    announced = False
    for url, dirname in _BOOTSTRAP_LIBS:
        dst = os.path.join(libs_dir, dirname)
        if os.path.isdir(os.path.join(dst, ".git")):
            continue  # already present
        if not announced:
            sys.stderr.write(
                "3d: first run — installing OpenSCAD libraries into libs/ (once)...\n"
            )
            announced = True
        cmd = [
            *timeout_cmd,
            "git",
            "-c", "http.connectTimeout=10",
            "-c", "http.lowSpeedLimit=1000",
            "-c", "http.lowSpeedTime=20",
            "clone", "--depth", "1", url, dst,
        ]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass  # non-fatal

    try:
        os.makedirs(_state_dir(), exist_ok=True)
        with open(marker, "w"):
            pass
    except OSError:
        pass
