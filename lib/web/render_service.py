#!/usr/bin/env python3
"""render_service.py — async OpenSCAD render/export orchestration for the web dashboard.

Reuses the repo's existing render core (lib/render.py) and the `3d export` gate rather
than reimplementing OpenSCAD invocation. Exports a project model to STL (for the three.js
viewer) and renders named views to PNG, all via `asyncio.create_subprocess_exec` so the
event loop stays responsive and concurrent renders are bounded by a semaphore.

Outputs go to a per-project cache dir under the web state dir so we never write into the
user's project tree. Re-render is debounced by the caller (the param-change handler).
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


class RenderError(RuntimeError):
    pass


def _cache_dir(state_dir: pathlib.Path, scad: pathlib.Path) -> pathlib.Path:
    key = hashlib.sha1(str(scad).encode()).hexdigest()[:12]
    d = state_dir / "cache" / f"{scad.stem}-{key}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _defines_suffix(defines: dict[str, str] | None) -> str:
    """A short, stable suffix that distinguishes one define set from another, so cached
    outputs for different `-D` values never overwrite each other. Empty for no defines."""
    if not defines:
        return ""
    canon = ";".join(f"{k}={defines[k]}" for k in sorted(defines))
    return "-" + hashlib.sha1(canon.encode()).hexdigest()[:10]


def _openscad() -> str:
    import sys
    sys.path.insert(0, str(REPO_ROOT / "lib"))
    import render  # type: ignore  # lib/render.py
    return render.find_openscad()  # type: ignore[no-any-return]


async def _run(cmd: list[str], *, timeout: float = 180.0) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "REPO_ROOT": str(REPO_ROOT)},
    )
    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RenderError(f"render timed out after {timeout}s: {' '.join(cmd[:3])} …")
    return proc.returncode or 0, (out_b or b"").decode("utf-8", errors="replace")


async def export_stl(
    scad: str | pathlib.Path,
    state_dir: str | pathlib.Path,
    *,
    defines: dict[str, str] | None = None,
    timeout: float = 180.0,
) -> pathlib.Path:
    """Export a .scad to an ASCII STL (three.js's STLLoader reads ASCII & binary; we use
    binary via openscad directly for size). Returns the STL path. Raises RenderError."""
    scadp = pathlib.Path(scad).resolve()
    if not scadp.is_file():
        raise RenderError(f"no such scad: {scadp}")
    out = _cache_dir(pathlib.Path(state_dir), scadp) / f"{scadp.stem}{_defines_suffix(defines)}.stl"
    osc = _openscad()
    cmd = [osc, "--export-format", "binstl", "-o", str(out)]
    for k, v in (defines or {}).items():
        cmd += ["-D", f"{k}={v}"]
    cmd.append(str(scadp))
    rc, log = await _run(cmd, timeout=timeout)
    if rc != 0 or not out.is_file():
        raise RenderError(f"openscad export failed (rc={rc}):\n{log[-2000:]}")
    return out


async def render_view(
    scad: str | pathlib.Path,
    state_dir: str | pathlib.Path,
    *,
    view: str = "iso",
    size: str = "900x700",
    defines: dict[str, str] | None = None,
    timeout: float = 180.0,
) -> pathlib.Path:
    """Render a single named view to PNG using the repo's render core. Returns PNG path."""
    scadp = pathlib.Path(scad).resolve()
    out = _cache_dir(pathlib.Path(state_dir), scadp) / f"{scadp.stem}-{view}{_defines_suffix(defines)}.png"
    # Resolve render.py through the shared python runner (.venv -> uv -> system), the same
    # way every other `3d` python tool runs; the old bash `lib/pyrun` shim is gone.
    import sys
    sys.path.insert(0, str(REPO_ROOT / "lib"))
    from cli.pyrun import tool_argv  # type: ignore  # lib/cli/pyrun.py

    cmd = tool_argv(
        "trimesh", "render.py",
        ["single", str(scadp), "-o", str(out), "--view", view, "--size", size],
    )
    for k, v in (defines or {}).items():
        cmd += ["-D", f"{k}={v}"]
    rc, log = await _run(cmd, timeout=timeout)
    if rc != 0 or not out.is_file():
        raise RenderError(f"render --view {view} failed (rc={rc}):\n{log[-2000:]}")
    return out
