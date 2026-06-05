"""3d doctor — read-only health/compat report: present/missing deps + install commands.

`3d setup` was removed (deps install via the first-run bootstrap + the per-item commands
printed here), so doctor never points at a `setup` command — it prints the exact install
line for each missing item instead.
"""
from __future__ import annotations

import os
import shutil
import sys

from cli.env import (
    PY_MESH_MODULES,
    detect_os,
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

  Checks: openscad, imagemagick, python3, uv/pip, the python mesh stack
  (trimesh, manifold3d, numpy, scipy, rtree, pillow, opencv), and a slicer
  (OrcaSlicer / Bambu Studio / PrusaSlicer)."""


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
        passl(".venv", f"{os.path.join(repo_root(), '.venv')} (pyrun prefers this)")
        has_venv = True
    else:
        warn(".venv", "absent — create it (python3 -m venv .venv) or rely on uv per-call")

    print()
    print(f"{b}Python mesh stack (mesh / check / printability / collision / preprocess){z}")
    if has_venv:
        print(f"  {dim}(.venv present — pyrun uses it, NOT uv; missing modules are real){z}")
    py = resolve_python()
    if py:
        for mod in PY_MESH_MODULES:
            if py_has_module(mod):
                passl(f"py:{mod}", f"importable by {py}")
            else:
                pkg = pypkg_for(mod)
                if not has_venv and has_uv:
                    warn(f"py:{mod}", f"not in {py} (ok: uv resolves '{pkg}' per-call)")
                else:
                    miss(f"py:{mod}", f"pip install {pkg}")
        if py_has_module("pyvista"):
            passl("py:pyvista", f"importable by {py} (collision --viz available)")
        elif not has_venv and has_uv:
            warn("py:pyvista", f"not in {py} (collision --viz: uv resolves per-call)")
        else:
            warn("py:pyvista", "absent — only 'collision --viz' needs it (pip install pyvista)")
    else:
        miss("python mesh stack", f"no python3 — {install_cmd('python3')}")

    print()
    print(f"{b}Web dashboard (3d web — OPTIONAL tier){z}")
    # import-name -> pip package. fastapi/uvicorn are required for the dashboard; markdown
    # (spec->HTML) and pyyaml (per-part colors) are nice-to-have and degrade gracefully.
    web_deps = [("fastapi", "fastapi"), ("uvicorn", "uvicorn"),
                ("markdown", "markdown"), ("yaml", "pyyaml")]
    if py:
        for mod, pkg in web_deps:
            if py_has_module(mod):
                passl(f"py:{mod}", f"importable by {py}")
            elif not has_venv and has_uv:
                warn(f"py:{mod}", f"absent (ok: uv resolves '{pkg}' per-call for 3d web)")
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
