"""3d doctor — read-only health/compat report: present/missing deps + install commands.

WHAT: inspects the environment and reports which dependencies are present or missing for
  the full `3d` pipeline, with the exact install command for THIS OS.

WHY: before running renders, gates, or slicing, you need to know whether OpenSCAD,
  ImageMagick, the Python mesh stack, and a slicer are available. `doctor` gives a
  PASS/MISSING breakdown and the exact command to install each missing item — so you
  never guess. `3d setup` was removed; install is either automatic (first-run bootstrap)
  or driven by the per-item commands printed here.

Examples:
  3d doctor
  3d doctor | grep MISSING   # filter to only missing items
  3d doctor                  # run before CI to verify the environment

ROADMAP §2: "First-run auto-bootstrap (NO manual setup). On any 3d invocation, if not
  bootstrapped: auto-clone/configure OpenSCAD libraries (BOSL2, NopSCADlib) + set
  OPENSCADPATH, once, quietly, idempotent, non-fatal offline. 3d doctor stays (read-only
  health/compat report)."

`3d setup` was removed (deps install via the first-run bootstrap + the per-item commands
printed here), so doctor never points at a `setup` command — it prints the exact install
line for each missing item instead.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from cli.env import (
    PY_MESH_MODULES,
    detect_os,
    find_ffmpeg,
    find_magick,
    find_openscad,
    find_slicer,
    install_cmd,
    py_has_module,
    pypkg_for,
    repo_root,
    resolve_python,
)
from cli.registry import Command

USAGE = """3d doctor
  Report which dependencies are present/missing for the full `3d` pipeline,
  with the exact install command for THIS OS. Read-only — installs nothing.
  (OpenSCAD libraries auto-install on first run; python deps resolve via uv or a .venv.)

  Checks: openscad, imagemagick, ffmpeg, python3, uv/pip, the python mesh stack
  (trimesh, manifold3d, numpy, scipy, rtree, pillow, opencv), and a slicer
  (OrcaSlicer / Bambu Studio / PrusaSlicer).

Examples:
  3d doctor
  3d doctor | grep MISSING   # filter to only missing items
  3d doctor                  # run before CI to verify the environment"""


def run(argv: list[str]) -> int:  # noqa: C901
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    os_name = detect_os()
    tty = sys.stdout.isatty()
    grn = "\033[32m" if tty else ""
    red = "\033[31m" if tty else ""
    yel = "\033[33m" if tty else ""
    dim = "\033[2m" if tty else ""
    b = "\033[1m" if tty else ""
    z = "\033[0m" if tty else ""

    missing = 0

    def passl(item: str, detail: str = "") -> None:
        print(f"  {grn}PASS{z}    {item:<22} {detail}")

    def miss(item: str, install: str) -> None:
        nonlocal missing
        print(f"  {red}MISSING{z} {item:<22} {dim}install:{z} {install}")
        missing += 1

    def warn(item: str, detail: str = "") -> None:
        print(f"  {yel}WARN{z}    {item:<22} {detail}")

    def python_has_module(python: str, mod: str) -> bool:
        try:
            r = subprocess.run(
                [python, "-c", f"import importlib; importlib.import_module({mod!r})"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return r.returncode == 0

    print(f"{b}3d doctor{z}  —  OS={os_name}")
    print()

    print(f"{b}Core{z}")
    osc = find_openscad()
    if osc:
        passl("openscad", osc)
    else:
        miss("openscad", install_cmd("openscad"))
    mgk = find_magick()
    if mgk:
        passl("imagemagick (magick)", mgk if os.path.isabs(mgk) else (shutil.which(mgk) or mgk))
    else:
        miss("imagemagick (magick)", install_cmd("imagemagick"))
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        passl("ffmpeg", ffmpeg)
    else:
        miss("ffmpeg", install_cmd("ffmpeg"))
    py3 = shutil.which("python3")
    if py3:
        passl("python3", py3)
    else:
        miss("python3", install_cmd("python3"))

    print()
    print(f"{b}Python runtime (need uv OR a .venv OR importable system deps){z}")
    has_uv = False
    has_venv = False
    if os.environ.get("PY3D_NO_UV"):
        warn("uv", "disabled by PY3D_NO_UV=1 (pyrun will not use it)")
    elif shutil.which("uv"):
        passl("uv", f"{shutil.which('uv')} — resolves deps on the fly")
        has_uv = True
    else:
        warn("uv", f"not found — {install_cmd('uv')}")
    pip = shutil.which("pip3") or shutil.which("pip")
    if pip:
        passl("pip", pip)
    else:
        warn("pip", "not found (bundled with python3 -m venv)")
    venv_py = os.path.join(repo_root(), ".venv", "bin", "python")
    if os.access(venv_py, os.X_OK):
        passl(".venv", f"{os.path.join(repo_root(), '.venv')} (used when requested deps import)")
        has_venv = True
    else:
        warn(".venv", "absent — create it (python3 -m venv .venv) or rely on uv per-call")

    print()
    print(f"{b}Python mesh stack (mesh / check / printability / collision / preprocess){z}")
    if has_venv:
        print(f"  {dim}(.venv present — pyrun falls back to uv or importable system python when deps are missing){z}")
    py = resolve_python()
    system_py = shutil.which("python3")
    if py:
        for mod in PY_MESH_MODULES:
            if py_has_module(mod):
                passl(f"py:{mod}", f"importable by {py}")
            else:
                pkg = pypkg_for(mod)
                if has_uv:
                    warn(f"py:{mod}", f"not in {py} (ok: uv resolves '{pkg}' per-call)")
                elif system_py and system_py != py and python_has_module(system_py, mod):
                    passl(f"py:{mod}", f"importable by {system_py} (system fallback; not in {py})")
                else:
                    miss(f"py:{mod}", f"pip install {pkg}")
        if py_has_module("pyvista"):
            passl("py:pyvista", f"importable by {py} (collision --viz available)")
        elif has_uv:
            warn("py:pyvista", f"not in {py} (collision --viz: uv resolves per-call)")
        elif system_py and system_py != py and python_has_module(system_py, "pyvista"):
            passl("py:pyvista", f"importable by {system_py} (collision --viz system fallback)")
        else:
            warn("py:pyvista", "absent — only 'collision --viz' needs it (pip install pyvista)")
    else:
        miss("python mesh stack", f"no python3 — {install_cmd('python3')}")

    print()
    print(f"{b}Web dashboard (3d web — OPTIONAL tier){z}")
    print(f"  {dim}(3d web imports these in the dispatcher Python: {sys.executable}){z}")
    # import-name -> pip package. fastapi/uvicorn are required for the dashboard;
    # markdown (spec->HTML) and pyyaml (per-part colors) are nice-to-have.
    web_deps = [
        ("fastapi", "fastapi", True),
        ("uvicorn", "uvicorn", True),
        ("markdown", "markdown", False),
        ("yaml", "pyyaml", False),
    ]
    web_py = sys.executable
    if web_py:
        for mod, pkg, required in web_deps:
            if python_has_module(web_py, mod):
                passl(f"py:{mod}", f"importable by {web_py}")
            elif required:
                warn(f"py:{mod}", f"absent in dispatcher Python — 3d web needs: pip install {pkg}")
            else:
                warn(f"py:{mod}", f"absent — only 3d web needs it (pip install {pkg})")
    else:
        warn("web deps", f"no python3 — {install_cmd('python3')}")

    print()
    print(f"{b}Slicer (3d slice){z}")
    sl = find_slicer()
    if sl:
        passl(f"slicer ({sl[0]})", sl[1])
    else:
        miss("slicer", f"{install_cmd('slicer')}   [OrcaSlicer > Bambu Studio > PrusaSlicer]")

    print()
    print(f"{b}OpenSCAD libraries{z}")
    if os.path.isdir(os.path.join(repo_root(), "libs", "BOSL2")):
        passl("libs/BOSL2", os.path.join(repo_root(), "libs", "BOSL2"))
    else:
        warn("libs/BOSL2", "absent — auto-installs on next `3d` run (or: rm ~/.config/3d-cli/.bootstrapped)")

    print()
    if missing == 0:
        print(f"{grn}>>> DOCTOR: PASS{z} — all required dependencies present.")
        return 0
    print(
        f"{yel}>>> DOCTOR: {missing} MISSING{z} — install the missing items with the per-item commands above."
    )
    # informational: nonzero so scripts/CI can gate, but never crashes.
    return 1


COMMAND = Command(
    name="doctor",
    group="ENVIRONMENT",
    summary="report present/missing deps + the exact install command per OS (read-only)",
    usage=USAGE,
    run=run,
)
