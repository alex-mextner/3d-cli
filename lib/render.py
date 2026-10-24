#!/usr/bin/env python3
"""render.py — typed + async OpenSCAD render core for the `3d` CLI.

Owns everything the `render` command surface needs:
  - the NAMED-VIEW -> camera-direction table (front/back/left/right/top/bottom/iso +
    diagonals 3-4/front-left/front-right/rear-left/rear-right);
  - a single CGAL render (--render) with a locked 6-param VECTOR camera;
  - --multi: render all standard angles concurrently (asyncio.gather, bounded by a
    semaphore of cli.resources.max_workers(): <= 6, THREED_MAX_WORKERS overrides);
  - --section is NOT here: see section_render.py (mesh-level cut, violet hatched cut faces).

Camera model: OpenSCAD has no bbox CLI flag, so a named view is realised as a viewing
DIRECTION (unit-ish vector) + `--autocenter --viewall`, which makes OpenSCAD compute the
look-at centroid and a fit distance for us. The eye is placed along that direction at a
distance scaled from the model diagonal (read from a temp STL when the mesh stack is
available; otherwise a fixed large multiple that --viewall then refits). This keeps render
working with NO python mesh deps (the whole repo's degrade-gracefully ethos) while honouring
"compute from the bounding box" exactly when deps exist.

Run via the CLI (resolves deps + openscad): `3d render ... `. Direct:
  python3 render.py single  model.scad -o out.png --view left
  python3 render.py multi   model.scad outdir/ [--render]
  (cross-sections live in section_render.py: `3d section` / `3d render --section`)
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from axis import VIEW_DIRECTIONS, VIEW_NAMES
from cli.resources import pool_size
from cli.env import openscad_display_prefix

# ---------------------------------------------------------------------------
# named-view direction table.  Each value is the eye DIRECTION from the look-at
# centroid (OpenSCAD right-handed: +X right, +Y away/into screen, +Z up). With
# --viewall the magnitude only sets the orbit ray; --viewall fits the frame.
#   front  : camera on -Y looking toward +Y   (you see the +... face nearest -Y)
#   back   : camera on +Y
#   left   : camera on -X ; right: +X ; top: +Z ; bottom: -Z
#   iso / 3-4 / corners : the standard 3/4 diagonals.
# 3-4 is the canonical "three-quarter" hero angle (azimuth 45 deg, elevation ~30 deg).
# ---------------------------------------------------------------------------
_VIEW_DIRS = VIEW_DIRECTIONS
# the standard angle set rendered by --multi
MULTI_VIEWS: list[str] = ["front", "back", "left", "right", "top", "iso"]


def find_openscad() -> str:
    """Locate the openscad binary (env OPENSCAD, PATH, common macOS/Homebrew paths)."""
    env = os.environ.get("OPENSCAD")
    if env and (shutil.which(env) or os.path.isfile(env)):
        return env
    found = shutil.which("openscad")
    if found:
        return found
    for p in (
        "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD",
        "/opt/homebrew/bin/openscad",
        "/usr/local/bin/openscad",
    ):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    raise FileNotFoundError(
        "OpenSCAD not found on PATH or common locations. "
        "Install: brew install --cask openscad"
    )


def _run_openscad(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run openscad (possibly wrapped in xvfb-run) and return the result."""
    full = [*openscad_display_prefix(), *cmd]
    return subprocess.run(full, **kwargs)  # type: ignore[arg-type]


def model_bbox(
    scad_path: str, defines: list[str], openscad: str
) -> tuple[tuple[float, float, float], float] | None:
    """Return (centroid_xyz, diagonal_mm) by exporting a temp STL and reading it with
    trimesh. None if the mesh stack is unavailable or the export produced nothing — the
    caller then falls back to --autocenter --viewall with a fixed orbit radius."""
    try:
        import trimesh  # type: ignore
    except Exception:
        return None
    tmpdir = tempfile.mkdtemp(prefix="3d_bbox_")
    try:
        stl = os.path.join(tmpdir, "m.stl")
        cmd = [openscad, "--export-format", "binstl"]
        for d in defines:
            cmd += ["-D", d]
        cmd += ["-o", stl, scad_path]
        _run_openscad(cmd, capture_output=True, text=True, timeout=300, check=False)
        if not os.path.isfile(stl) or os.path.getsize(stl) < 100:
            return None
        mesh = trimesh.load(stl, force="mesh")
        if mesh is None or mesh.is_empty:
            return None
        lo, hi = mesh.bounds
        centroid = tuple(float((lo[i] + hi[i]) / 2.0) for i in range(3))
        diag = float(math.dist(lo, hi))
        return centroid, diag  # type: ignore[return-value]
    except Exception:
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def view_camera(
    view: str,
    bbox: tuple[tuple[float, float, float], float] | None,
) -> str | None:
    """6-param vector camera string 'ex,ey,ez,cx,cy,cz' for a named view, or None to let
    the caller use --autocenter --viewall (when bbox is unknown). With a bbox, place the
    eye along the view direction at ~2.5x the diagonal from the centroid."""
    d = _VIEW_DIRS.get(view)
    if d is None:
        raise ValueError(f"unknown view '{view}' (known: {', '.join(VIEW_NAMES)})")
    if bbox is None:
        return None
    (cx, cy, cz), diag = bbox
    norm = math.sqrt(d[0] ** 2 + d[1] ** 2 + d[2] ** 2) or 1.0
    dist = 2.5 * max(diag, 1.0)
    ex = cx + d[0] / norm * dist
    ey = cy + d[1] / norm * dist
    ez = cz + d[2] / norm * dist
    return f"{ex:.4f},{ey:.4f},{ez:.4f},{cx:.4f},{cy:.4f},{cz:.4f}"


def _base_render_cmd(
    openscad: str,
    scad_path: str,
    out: str,
    *,
    cam: str | None,
    size: str,
    ortho: bool,
    scheme: str,
    defines: list[str],
    render: bool = True,
    use_viewall_fallback: bool = True,
) -> list[str]:
    """Assemble an openscad argv. If cam is None we fall back to a fixed-direction orbit
    eye + --autocenter --viewall so OpenSCAD computes centroid + fit distance."""
    cmd = [openscad]
    if render:
        cmd.append("--render")
    if ortho:
        cmd.append("--projection=ortho")
    if cam is not None:
        cmd.append(f"--camera={cam}")
    elif use_viewall_fallback:
        cmd += ["--camera=1,-1,1,0,0,0", "--autocenter", "--viewall"]
    cmd += [f"--imgsize={size}", f"--colorscheme={scheme}"]
    for d in defines:
        cmd += ["-D", d]
    cmd += ["-o", out, scad_path]
    return cmd


def view_camera_or_fallback(
    view: str,
    bbox: tuple[tuple[float, float, float], float] | None,
) -> tuple[str | None, tuple[float, float, float]]:
    """Return (cam_string_or_None, direction) for a view. When bbox is None the cam is
    None and the caller orbits along `direction` with --viewall."""
    direction = _VIEW_DIRS[view]
    return view_camera(view, bbox), direction


def _dir_orbit_cam(direction: tuple[float, float, float]) -> str:
    """A unit-ish eye vector along `direction` looking at origin — used with --viewall when
    no bbox is known (--viewall refits, the magnitude is irrelevant, only the ray matters)."""
    return f"{direction[0]:.4f},{direction[1]:.4f},{direction[2]:.4f},0,0,0"


# ===========================================================================
# single render
# ===========================================================================
def render_single(args: argparse.Namespace) -> int:
    openscad = find_openscad()
    scad = args.file
    if not os.path.isfile(scad):
        print(f"render: file not found: {scad}", file=sys.stderr)
        return 2
    out = args.out or (os.path.splitext(scad)[0] + ".png")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    defines: list[str] = args.define or []
    size = args.size.replace("x", ",")

    # Resolve (cam, use_viewall). Precedence: --cam > --view > default iso.
    cam: str
    use_viewall: bool
    if args.cam:  # manual override wins over --view
        n = len(args.cam.split(","))
        if n != 6:
            print(
                f"render: --cam needs 6 values ex,ey,ez,cx,cy,cz (got {n}). "
                "7 = gimbal => empty frame.",
                file=sys.stderr,
            )
            return 2
        cam = args.cam
        use_viewall = False
    else:
        view = args.view or "iso"
        bbox = model_bbox(scad, defines, openscad)
        bbox_cam = view_camera(view, bbox)
        if bbox_cam is not None:
            cam = bbox_cam  # exact bbox-fit camera
            use_viewall = False
        else:
            cam = _dir_orbit_cam(
                _VIEW_DIRS[view]
            )  # orbit ray; --viewall fits the frame
            use_viewall = True

    cmd = _base_render_cmd(
        openscad,
        scad,
        out,
        cam=cam,
        size=size,
        ortho=args.ortho,
        scheme=args.colorscheme,
        defines=defines,
        render=True,
        use_viewall_fallback=False,
    )
    if use_viewall:
        idx = cmd.index("-o")
        cmd[idx:idx] = ["--autocenter", "--viewall"]
    print(
        f"render: {scad} -> {out}  view={args.view or ('cam' if args.cam else 'iso')}"
        f"  {'ortho' if args.ortho else 'persp'}  size={size}"
    )
    proc = _run_openscad(cmd, capture_output=True, text=True)
    if proc.stdout:
        sys.stderr.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if not os.path.isfile(out):
        print("render: no output produced", file=sys.stderr)
        return 1
    print(f"render: wrote {out}")
    return 0


# ===========================================================================
# multi render (async, bounded)
# ===========================================================================
async def _render_one_async(
    cmd: list[str], sem: asyncio.Semaphore, label: str
) -> tuple[str, int, str]:
    async with sem:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate()
        return (
            label,
            proc.returncode or 0,
            (err.decode(errors="replace") if err else ""),
        )


async def _render_multi_async(
    cmds: list[tuple[str, str, list[str]]],
) -> list[tuple[str, int, str]]:
    """cmds = [(label, out, argv), ...]; run with a bounded semaphore."""
    limit = pool_size(len(cmds))
    sem = asyncio.Semaphore(limit)
    tasks = [_render_one_async(argv, sem, label) for (label, _out, argv) in cmds]
    return await asyncio.gather(*tasks)


def render_multi(args: argparse.Namespace) -> int:
    openscad = find_openscad()
    scad = args.file
    if not os.path.isfile(scad):
        print(f"multi: file not found: {scad}", file=sys.stderr)
        return 2
    outdir = args.outdir or "previews"
    os.makedirs(outdir, exist_ok=True)
    defines: list[str] = args.define or []
    size = args.size.replace("x", ",")
    base = os.path.splitext(os.path.basename(scad))[0]
    render_flag = bool(args.render)

    bbox = model_bbox(scad, defines, openscad) if render_flag else None

    prefix = openscad_display_prefix()
    cmds: list[tuple[str, str, list[str]]] = []
    for view in MULTI_VIEWS:
        out = os.path.join(outdir, f"{base}_{view}.png")
        cam = view_camera(view, bbox)
        use_viewall = cam is None
        if use_viewall:
            cam = _dir_orbit_cam(_VIEW_DIRS[view])
        cmd = _base_render_cmd(
            openscad,
            scad,
            out,
            cam=cam,
            size=size,
            ortho=False,
            scheme="Tomorrow Night",
            defines=defines,
            render=render_flag,
            use_viewall_fallback=False,
        )
        if use_viewall:
            idx = cmd.index("-o")
            cmd[idx:idx] = ["--autocenter", "--viewall"]
        cmds.append((view, out, [*prefix, *cmd]))

    print(
        f"multi: {scad} -> {outdir}  ({'render' if render_flag else 'preview'}, "
        f"{'bbox cameras' if bbox else 'viewall fit'}, async)"
    )
    results = asyncio.run(_render_multi_async(cmds))
    rc = 0
    by_label = {lbl: (code, err) for (lbl, code, err) in results}
    for view, out, _cmd in cmds:
        code, err = by_label[view]
        ok = os.path.isfile(out) and code == 0
        print(f"  [{view:6s}] {'OK' if ok else 'FAIL'}  {out}")
        if not ok:
            rc = 1
            if err.strip():
                sys.stderr.write("    " + err.strip().splitlines()[-1] + "\n")
    if rc == 0:
        print(f"multi: wrote {len(cmds)} views to {outdir}")
    return rc


# ===========================================================================
# argparse
# ===========================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="render.py", description="OpenSCAD render core")
    sub = p.add_subparsers(dest="mode", required=True)

    s = sub.add_parser("single", help="one render (named view / cam / iso default)")
    s.add_argument("file")
    s.add_argument("-o", "--out", default=None)
    s.add_argument("--view", default=None, choices=VIEW_NAMES)
    s.add_argument("--cam", default=None, help="6-param vector ex,ey,ez,cx,cy,cz")
    s.add_argument("--size", default="1200x900")
    s.add_argument("--ortho", action="store_true")
    s.add_argument("--colorscheme", default="Tomorrow Night")
    s.add_argument("-D", "--define", action="append", default=[])
    s.set_defaults(func=render_single)

    m = sub.add_parser("multi", help="render all standard angles concurrently")
    m.add_argument("file")
    m.add_argument("outdir", nargs="?", default="previews")
    m.add_argument("--render", action="store_true")
    m.add_argument("--size", default="800x600")
    m.add_argument("-D", "--define", action="append", default=[])
    m.set_defaults(func=render_multi)
    return p


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    func = args.func  # type: ignore[attr-defined]
    return int(func(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
