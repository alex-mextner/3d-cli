#!/usr/bin/env python3
"""render.py — typed + async OpenSCAD render core for the `3d` CLI.

Owns everything the `render` command surface needs:
  - the NAMED-VIEW -> camera-direction table (front/back/left/right/top/bottom/iso +
    diagonals 3-4/front-left/front-right/rear-left/rear-right);
  - a single CGAL render (--render) with a locked 6-param VECTOR camera;
  - --multi: render all standard angles concurrently (asyncio.gather, bounded by a
    semaphore ~ os.cpu_count());
  - --section: a TRUE cross-section that cuts ARBITRARY geometry (export STL once, then
    difference(import, halfspace) with color OUTSIDE the cut so the cut face takes the
    part colour), plus the richer per-part `-D cut=true` assembly colour mode.

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
  python3 render.py section model.scad -o sec.png --plane YZ [--color]
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

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
_VIEW_DIRS: dict[str, tuple[float, float, float]] = {
    "front":  (0.0, -1.0, 0.0),
    "back":   (0.0,  1.0, 0.0),
    "left":   (-1.0, 0.0, 0.0),
    "right":  (1.0,  0.0, 0.0),
    "top":    (0.0,  0.0, 1.0),
    "bottom": (0.0,  0.0, -1.0),
    "iso":    (1.0, -1.0, 1.0),
}


def _diag_dir(az_deg: float, el_deg: float) -> tuple[float, float, float]:
    """Eye direction from azimuth (deg, 0=+ -Y front, CCW toward +X) and elevation (deg)."""
    az = math.radians(az_deg)
    el = math.radians(el_deg)
    # front (-Y) at az=0; +az rotates toward +X (right). horizontal plane component:
    horiz = math.cos(el)
    x = horiz * math.sin(az)
    y = -horiz * math.cos(az)
    z = math.sin(el)
    return (x, y, z)


# three-quarter / corner views (azimuth, elevation)
_VIEW_DIRS["3-4"] = _diag_dir(45.0, 30.0)
_VIEW_DIRS["front-left"] = _diag_dir(-45.0, 25.0)
_VIEW_DIRS["front-right"] = _diag_dir(45.0, 25.0)
_VIEW_DIRS["rear-left"] = _diag_dir(-135.0, 25.0)
_VIEW_DIRS["rear-right"] = _diag_dir(135.0, 25.0)

VIEW_NAMES: list[str] = list(_VIEW_DIRS.keys())
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
        subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
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
            cam = bbox_cam              # exact bbox-fit camera
            use_viewall = False
        else:
            cam = _dir_orbit_cam(_VIEW_DIRS[view])  # orbit ray; --viewall fits the frame
            use_viewall = True

    cmd = _base_render_cmd(
        openscad, scad, out, cam=cam, size=size, ortho=args.ortho,
        scheme=args.colorscheme, defines=defines, render=True,
        use_viewall_fallback=False,
    )
    if use_viewall:
        idx = cmd.index("-o")
        cmd[idx:idx] = ["--autocenter", "--viewall"]
    print(f"render: {scad} -> {out}  view={args.view or ('cam' if args.cam else 'iso')}"
          f"  {'ortho' if args.ortho else 'persp'}  size={size}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
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
        return label, proc.returncode or 0, (err.decode(errors="replace") if err else "")


async def _render_multi_async(cmds: list[tuple[str, str, list[str]]]) -> list[tuple[str, int, str]]:
    """cmds = [(label, out, argv), ...]; run with a bounded semaphore."""
    limit = max(1, min(len(cmds), os.cpu_count() or 4))
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

    cmds: list[tuple[str, str, list[str]]] = []
    for view in MULTI_VIEWS:
        out = os.path.join(outdir, f"{base}_{view}.png")
        cam = view_camera(view, bbox)
        use_viewall = cam is None
        if use_viewall:
            cam = _dir_orbit_cam(_VIEW_DIRS[view])
        cmd = _base_render_cmd(
            openscad, scad, out, cam=cam, size=size, ortho=False,
            scheme="Tomorrow Night", defines=defines, render=render_flag,
            use_viewall_fallback=False,
        )
        if use_viewall:
            idx = cmd.index("-o")
            cmd[idx:idx] = ["--autocenter", "--viewall"]
        cmds.append((view, out, cmd))

    print(f"multi: {scad} -> {outdir}  ({'render' if render_flag else 'preview'}, "
          f"{'bbox cameras' if bbox else 'viewall fit'}, async)")
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
# section
# ===========================================================================
# camera direction per plane for the cut view (eye on the REMOVED side, angled for depth).
_SECTION_DIR: dict[str, tuple[float, float, float]] = {
    "YZ": (1.0, 0.35, 0.5),   # cut at X=cx, look from +X
    "XZ": (0.35, 1.0, 0.5),   # cut at Y=cy
    "XY": (0.5, 0.35, 1.0),   # cut at Z=cz
}


def _section_cam(
    plane: str, keep: str, bbox: tuple[tuple[float, float, float], float] | None
) -> str | None:
    if bbox is None:
        return None
    (cx, cy, cz), diag = bbox
    d = _SECTION_DIR[plane]
    sign = -1.0 if keep == "pos" else 1.0   # eye sits on the removed side
    dist = 2.4 * max(diag, 1.0)
    n = math.sqrt(d[0] ** 2 + d[1] ** 2 + d[2] ** 2)
    ex = cx + sign * d[0] / n * dist
    ey = cy + sign * d[1] / n * dist
    ez = cz + sign * d[2] / n * dist
    return f"{ex:.4f},{ey:.4f},{ez:.4f},{cx:.4f},{cy:.4f},{cz:.4f}"


def _halfspace_cube(
    plane: str, keep: str, centroid: tuple[float, float, float], diag: float
) -> str:
    """An OpenSCAD cube() expression that, subtracted, removes one half across `plane` at
    the centroid. Sized to the model diagonal so it always fully covers the part."""
    cx, cy, cz = centroid
    big = max(diag * 3.0, 50.0)
    # cube spans [coord, coord+big] on the cut axis when removing the +side (keep=neg),
    # or [coord-big, coord] when removing the -side (keep=pos).
    def span(c: float, cut_axis: bool) -> tuple[float, float]:
        if not cut_axis:
            return (c - big, big * 2.0)  # (translate, size) fully covering
        if keep == "pos":
            return (c - big, big)        # remove the negative half
        return (c, big)                  # remove the positive half (keep neg)
    if plane == "YZ":
        tx, sx = span(cx, True);  ty, sy = span(cy, False); tz, sz = span(cz, False)
    elif plane == "XZ":
        tx, sx = span(cx, False); ty, sy = span(cy, True);  tz, sz = span(cz, False)
    else:  # XY
        tx, sx = span(cx, False); ty, sy = span(cy, False); tz, sz = span(cz, True)
    return f"translate([{tx:.3f},{ty:.3f},{tz:.3f}]) cube([{sx:.3f},{sy:.3f},{sz:.3f}]);"


def render_section(args: argparse.Namespace) -> int:
    openscad = find_openscad()
    scad = args.file
    if not os.path.isfile(scad):
        print(f"section: file not found: {scad}", file=sys.stderr)
        return 2
    if not args.out:
        print("section: -o out.png is required", file=sys.stderr)
        return 2
    plane = args.plane
    keep = args.keep
    if plane not in ("YZ", "XZ", "XY"):
        print(f"section: unknown plane '{plane}' (use YZ|XZ|XY)", file=sys.stderr)
        return 2
    if keep not in ("neg", "pos"):
        print(f"section: unknown keep '{keep}' (use neg|pos)", file=sys.stderr)
        return 2
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    defines: list[str] = args.define or []
    size = args.size.replace("x", ",")

    # ---- COLOUR assembly mode: the assembly honours -D cut=true + colours each part
    # OUTSIDE its own cut, so the cut faces are per-part coloured. We just pass the flag.
    if args.color:
        bbox = model_bbox(scad, defines, openscad)
        cam = _section_cam(plane, keep, bbox)
        use_viewall = cam is None
        if use_viewall:
            # No bbox: orbit along the plane's view direction (on the removed side, so
            # plane+keep still pick the correct side/axis) and let --viewall fit.
            sd = _SECTION_DIR[plane]
            sgn = -1.0 if keep == "pos" else 1.0
            cam = f"{sgn*sd[0]:.4f},{sgn*sd[1]:.4f},{sgn*sd[2]:.4f},0,0,0"
        extra = list(defines) + [
            "cut=true",
            f'section_plane="{plane}"',
            f'section_keep="{keep}"',
        ]
        cmd = _base_render_cmd(
            openscad, scad, args.out, cam=cam, size=size, ortho=False,
            scheme="Tomorrow Night", defines=extra, render=True,
            use_viewall_fallback=False,
        )
        if use_viewall:
            idx = cmd.index("-o")
            cmd[idx:idx] = ["--autocenter", "--viewall"]
        print(f"section: COLOUR assembly  plane={plane} keep={keep}")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        if not os.path.isfile(args.out):
            print("section: no output produced", file=sys.stderr)
            return 1
        nbytes = os.path.getsize(args.out)
        print(f"section: wrote {args.out} ({nbytes} bytes)")
        if nbytes < 15000:
            print("  WARNING: tiny PNG — frame may be empty (check the assembly cut contract).")
        return 0

    # ---- GENERIC cut: works on ANY geometry (no cut-contract needed). Export the model
    # to STL once, then difference(import(stl), halfspace) with the colour OUTSIDE the
    # difference so the cut face takes the part colour. 6-param vector camera + --render.
    bbox = model_bbox(scad, defines, openscad)
    tmpdir = tempfile.mkdtemp(prefix="3d_section_")
    try:
        if bbox is not None:
            (cx, cy, cz), diag = bbox
            stl = os.path.join(tmpdir, "model.stl")
            ecmd = [openscad, "--export-format", "binstl"]
            for dv in defines:
                ecmd += ["-D", dv]
            ecmd += ["-o", stl, scad]
            subprocess.run(ecmd, capture_output=True, text=True, timeout=300, check=False)
            if not os.path.isfile(stl) or os.path.getsize(stl) < 100:
                print("section: STL export failed — cannot cut", file=sys.stderr)
                return 1
            halfspace = _halfspace_cube(plane, keep, (cx, cy, cz), diag)
            sec_scad = os.path.join(tmpdir, "section.scad")
            with open(sec_scad, "w") as fh:
                fh.write(
                    "// AUTO-GENERATED cross-section (3d render --section)\n"
                    f'color([0.82,0.66,0.46]) difference() {{\n'
                    f'  import("{stl}");\n'
                    f"  {halfspace}\n"
                    "}\n"
                )
            cam_sec: str | None = _section_cam(plane, keep, bbox)  # bbox not None here
            cmd = _base_render_cmd(
                openscad, sec_scad, args.out, cam=cam_sec, size=size, ortho=False,
                scheme="Tomorrow Night", defines=[], render=True,
                use_viewall_fallback=False,
            )
        else:
            # No mesh stack: fall back to a per-module cut in a temp file next to INPUT so
            # use<> resolves. Requires --module (cannot import an unknown top-level call).
            if not args.module:
                print(
                    "section: no python mesh stack -> need --module 'name();' to cut "
                    "(or install trimesh for the generic STL-import cut).",
                    file=sys.stderr,
                )
                return 2
            input_dir = os.path.dirname(os.path.abspath(scad))
            input_base = os.path.basename(scad)
            big = 600.0
            cut_map = {
                ("YZ", "neg"): ("translate([0,-300,-300]) cube([600,600,600]);", "240,70,120"),
                ("YZ", "pos"): ("translate([-600,-300,-300]) cube([600,600,600]);", "-240,70,120"),
                ("XZ", "neg"): ("translate([-300,0,-300]) cube([600,600,600]);", "70,240,120"),
                ("XZ", "pos"): ("translate([-300,-600,-300]) cube([600,600,600]);", "70,-240,120"),
                ("XY", "neg"): ("translate([-300,-300,0]) cube([600,600,600]);", "120,70,260"),
                ("XY", "pos"): ("translate([-300,-300,-600]) cube([600,600,600]);", "120,70,-260"),
            }
            cut_expr, eye = cut_map[(plane, keep)]
            sec_scad = os.path.join(input_dir, f".section_tmp_{os.getpid()}.scad")
            with open(sec_scad, "w") as fh:
                fh.write(
                    "// AUTO-GENERATED cross-section (3d render --section) — safe to delete\n"
                    f"use <{input_base}>\n"
                    f"color([0.82,0.66,0.46]) difference() {{\n  {args.module}\n  {cut_expr}\n}}\n"
                )
            cmd = _base_render_cmd(
                openscad, sec_scad, args.out, cam=f"{eye},0,0,0", size=size, ortho=False,
                scheme="Tomorrow Night", defines=defines, render=True,
                use_viewall_fallback=False,
            )
            idx = cmd.index("-o")
            cmd[idx:idx] = ["--autocenter", "--viewall"]

        print(f"section: {scad} plane={plane} keep={keep} -> {args.out}")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        if not os.path.isfile(args.out):
            print("section: no output produced", file=sys.stderr)
            return 1
        print(f"section: wrote {args.out} ({os.path.getsize(args.out)} bytes)")
        return 0
    finally:
        # clean any temp section file written next to INPUT
        leftover = os.path.join(
            os.path.dirname(os.path.abspath(scad)), f".section_tmp_{os.getpid()}.scad"
        )
        if os.path.isfile(leftover):
            os.remove(leftover)
        shutil.rmtree(tmpdir, ignore_errors=True)


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

    c = sub.add_parser("section", help="true cross-section (generic or coloured assembly)")
    c.add_argument("file")
    c.add_argument("-o", "--out", default=None)
    c.add_argument("--plane", default="YZ", choices=["YZ", "XZ", "XY"])
    c.add_argument("--keep", default="neg", choices=["neg", "pos"])
    c.add_argument("--color", action="store_true", help="coloured per-part assembly mode")
    c.add_argument("--module", default=None, help="module call to cut (no-mesh fallback)")
    c.add_argument("--size", default="1200x900")
    c.add_argument("-D", "--define", action="append", default=[])
    c.set_defaults(func=render_section)
    return p


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    func = args.func  # type: ignore[attr-defined]
    return int(func(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
