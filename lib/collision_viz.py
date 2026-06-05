#!/usr/bin/env python3
"""GENERIC collision VISUALIZER — render each phase with overlaps highlighted RED.

PROJECT-AGNOSTIC: takes the same project config JSON as collision_check.py. For every
configured phase it:
  1. Exports every part placed in its phase position (pair.scad `solo` mode) -> trimesh.
  2. Recomputes the collision table (same EPS/TOUCH_TOL/intended set as the gate): boolean
     intersection volume (manifold engine) + min surface distance (proximity).
  3. Renders the whole assembly in MUTED grey (the parts), then overlays in BRIGHT RED the
     exact boolean-intersection mesh of every BUG pair (interpenetration) and a red marker at
     the closest-point of every BUG gap-0 touch; intended contacts shown dim green.
  4. Burns an on-image legend panel listing each flagged pair, phase, type, and number.

Output: <viz.outdir>/<viz.name_prefix>_<phase>.png  (one per phase)

Run:  uv run --with trimesh,manifold3d,rtree,numpy,scipy,pyvista \
          python3 tools/collision/collision_viz.py <project>/verify/collision.json
"""
from __future__ import annotations

import itertools
import os
import pathlib
import subprocess
import sys
import tempfile
from typing import Any, cast

import numpy as np
import pyvista as pv
import trimesh

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from config import load  # noqa: E402

np.seterr(divide="ignore", invalid="ignore")
pv.OFF_SCREEN = True


def export_part(
    pair_scad: pathlib.Path, part: str, phase: int, tmp: str
) -> trimesh.Trimesh | None:
    out = os.path.join(tmp, f"solo_{part}_{phase}.stl")
    subprocess.run(
        ["openscad", "--export-format", "binstl", "-o", out,
         "-D", f'solo="{part}"', "-D", f"phase={phase}", str(pair_scad)],
        capture_output=True, text=True, timeout=180, check=False)
    if not os.path.exists(out) or os.path.getsize(out) < 100:
        return None
    m = cast(trimesh.Trimesh, trimesh.load(out, force="mesh", process=True))
    return None if (m.is_empty or len(m.faces) == 0) else m


def intersection_mesh(a: trimesh.Trimesh, b: trimesh.Trimesh) -> trimesh.Trimesh | None:
    try:
        inter = trimesh.boolean.intersection([a, b], engine="manifold")
    except Exception:
        return None
    return None if (inter is None or inter.is_empty or len(inter.faces) == 0) else inter


def sample(m: trimesh.Trimesh) -> Any:
    return np.vstack([m.vertices, m.triangles.mean(axis=1)])


def min_dist_and_point(a: trimesh.Trimesh, b: trimesh.Trimesh) -> tuple[float, Any]:
    pa = sample(a)
    _, da, _ = trimesh.proximity.closest_point(b, pa)
    pb = sample(b)
    _, db, _ = trimesh.proximity.closest_point(a, pb)
    if da.min() <= db.min():
        return float(da.min()), pa[int(np.argmin(da))]
    return float(db.min()), pb[int(np.argmin(db))]


def to_pv(m: trimesh.Trimesh) -> Any:
    faces = np.hstack([np.full((len(m.faces), 1), 3), m.faces]).astype(np.int64).ravel()
    return pv.PolyData(m.vertices, faces)


def section_clip(pdata: Any, y: float = 0.0) -> Any:
    """Clip away +Y half so the bore interior (and any intruding part) is visible."""
    try:
        return pdata.clip(normal="y", origin=(0, y, 0), invert=True)
    except Exception:
        return pdata


def render_phase(
    cfg: Any, ph: int, ph_name: str, meshes: dict[tuple[str, int], trimesh.Trimesh | None],
    flagged: list[Any], intended_rows: list[Any], out_png: pathlib.Path,
) -> None:
    pl = pv.Plotter(off_screen=True, window_size=[1400, 1100])
    pl.set_background("white")
    for p in cfg.parts:
        m = meshes.get((p, ph))
        if m is None:
            continue
        pl.add_mesh(section_clip(to_pv(m)), color=(0.78, 0.80, 0.83),
                    opacity=0.30, show_edges=False, name=f"part_{p}")
    for (a, b, kind, vol, dist, pt, imesh) in intended_rows:
        if imesh is not None:
            pl.add_mesh(to_pv(imesh), color=(0.20, 0.65, 0.30), opacity=0.55)
        elif pt is not None:
            pl.add_mesh(pv.Sphere(radius=1.2, center=pt), color=(0.20, 0.65, 0.30), opacity=0.6)
    for (a, b, kind, vol, dist, pt, imesh) in flagged:
        if imesh is not None:
            pl.add_mesh(to_pv(imesh), color=(0.95, 0.05, 0.05), opacity=1.0,
                        show_edges=True, edge_color=(0.4, 0, 0))
        elif pt is not None:
            pl.add_mesh(pv.Sphere(radius=1.8, center=pt), color=(0.95, 0.05, 0.05))

    lines = [f"PHASE {ph}: {ph_name.upper()}",
             f"(EPS={cfg.eps} mm^3   touch_tol={cfg.touch_tol} mm)", ""]
    if flagged:
        lines.append("RED = BUG (unintended):")
        for (a, b, kind, vol, dist, pt, imesh) in flagged:
            if kind == "interpenetration":
                lines.append(f"  {a}/{b}: interpenetration  {vol:.0f} mm^3")
            else:
                lines.append(f"  {a}/{b}: gap-0 touch  d={dist:.3f} mm")
    else:
        lines.append("RED = BUG: none in this phase")
    lines.append("")
    lines.append("GREEN = intended contact:")
    for (a, b, kind, vol, dist, pt, imesh) in intended_rows:
        note = (f"{vol:.0f} mm^3" if kind == "interpenetration" else f"d={dist:.3f} mm")
        lines.append(f"  {a}/{b}  ({note})")
    pl.add_text("\n".join(lines), position="upper_left", font_size=11,
                color="black", font="courier")
    title_color = "red" if flagged else "darkgreen"
    pl.add_text(("DIRTY — " + str(len(flagged)) + " BUG(S)") if flagged else "CLEAN",
                position="upper_right", font_size=16, color=title_color, font="arial")

    pl.camera_position = "yz"
    pl.camera.azimuth = 35
    pl.camera.elevation = 18
    pl.reset_camera()
    pl.camera.zoom(1.25)
    pl.screenshot(str(out_png))
    pl.close()


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        print("usage: collision_viz.py <config.json>", file=sys.stderr)
        sys.exit(2)
    cfg = load(argv[1])
    if cfg.viz is None:
        print("config has no viz block", file=sys.stderr)
        sys.exit(2)
    outdir = cfg.viz.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="collision_viz_")
    meshes = {(p, ph): export_part(cfg.pair, p, ph, tmp)
              for ph in cfg.phases for p in cfg.parts}

    produced: list[tuple[str, pathlib.Path, int]] = []
    for ph, ph_name in cfg.phases.items():
        flagged: list[Any] = []
        intended_rows: list[Any] = []
        for a, b in itertools.combinations(cfg.parts, 2):
            ma, mb = meshes[(a, ph)], meshes[(b, ph)]
            if ma is None or mb is None:
                continue
            imesh = intersection_mesh(ma, mb)
            vol = abs(float(imesh.volume)) if imesh is not None else 0.0
            dist, pt = min_dist_and_point(ma, mb)
            interpen = vol > cfg.eps
            touch = dist < cfg.touch_tol
            if not interpen and not touch:
                continue
            kind = "interpenetration" if interpen else "gap-0 touch"
            row = (a, b, kind, vol, dist, pt, imesh if interpen else None)
            if frozenset((a, b)) in cfg.intended:
                intended_rows.append(row)
            else:
                flagged.append(row)
        out_png = outdir / f"{cfg.viz.name_prefix}_{ph_name}.png"
        render_phase(cfg, ph, ph_name, meshes, flagged, intended_rows, out_png)
        produced.append((ph_name, out_png, len(flagged)))
        print(f"[{ph_name:>6s}] {len(flagged)} bug(s) -> {out_png}")

    print("\nrendered:")
    for name, p, n in produced:
        print(f"  {name}: {p}  ({n} red bug region(s))")


if __name__ == "__main__":
    main(sys.argv)
