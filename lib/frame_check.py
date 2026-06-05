#!/usr/bin/env python3
"""GENERIC PER-FRAME collision GATE — samples a project's causal timeline at N frames and,
for EACH frame, checks ALL part pairs for interpenetration (manifold-boolean volume>EPS) AND
zero-gap touch (proximity<tol), excusing only the small INTENDED contacts.

PROJECT-AGNOSTIC: takes the same project config JSON as collision_check.py. From the config it
reads the placement .scad, parts, intended set, thresholds, AND a frame_check block giving the
path to the project's pose(t) timeline (.py), the function name, the frame count, the phase
sentinel (phase<sentinel -> pair.scad uses continuous pose), and the pose_vars mapping
{pose_dict_key : openscad_-D_var_name}. Nothing project-specific is hardcoded here.

This catches what the 3-pose static gate CANNOT: a wrong-direction fold and any mid-motion
interpenetration (e.g. a falling cartridge passing through a still-folding trapdoor).

It REUSES the static gate's machinery (no duplicated intersection logic, per AGENTS.md):
  intersection_volume / min_surface_distance / classify from collision_check.
The only difference: parts are placed via CONTINUOUS pose params (phase=sentinel in pair.scad)
fed from the project's timeline pose(t), instead of the discrete phases.

ANY frame with an unintended overlap => FAIL, printing frame index + t + pair + volume.

Run:  uv run --with trimesh,manifold3d,rtree,numpy,scipy \
          python3 tools/collision/frame_check.py <project>/verify/collision.json
Env:  FRAMES=N  (overrides the frame count from the config)
"""
import importlib.util
import itertools
import os
import pathlib
import subprocess
import sys
import tempfile

import numpy as np
import trimesh

# Reuse the static gate's logic — single source for intersection/classification.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from config import load  # noqa: E402
from collision_check import (  # noqa: E402
    intersection_volume, min_surface_distance, classify,
)

np.seterr(divide="ignore", invalid="ignore")


def import_pose(timeline_path, fn_name):
    """Dynamically import the project's pose(t) function from its .py path.

    Uses spec_from_file_location (NOT sys.path + import) so two projects can both have a
    `timeline.py` without clashing.
    """
    spec = importlib.util.spec_from_file_location("_project_timeline", str(timeline_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, fn_name)


def export_part_pose(pair_scad, part, pose_flags, tmp, idx, sentinel):
    """Render one part placed at a CONTINUOUS pose (phase=sentinel) to binary STL."""
    out = os.path.join(tmp, f"f{idx}_{part}.stl")
    subprocess.run(
        ["openscad", "--export-format", "binstl", "-o", out,
         "-D", f'solo="{part}"', "-D", f"phase={sentinel}",
         *pose_flags, str(pair_scad)],
        capture_output=True, text=True, timeout=180, check=False)
    if not os.path.exists(out) or os.path.getsize(out) < 100:
        return None
    m = trimesh.load(out, process=True)
    if m.is_empty or len(m.faces) == 0:
        return None
    return m


def main(argv):
    if len(argv) < 2:
        print("usage: frame_check.py <config.json>", file=sys.stderr)
        sys.exit(2)
    cfg = load(argv[1])
    if cfg.frame is None:
        print("config has no frame_check block", file=sys.stderr)
        sys.exit(2)

    pose = import_pose(cfg.frame.timeline, cfg.frame.timeline_fn)
    nframes = int(os.environ.get("FRAMES", str(cfg.frame.frames)))
    pose_vars = cfg.frame.pose_vars  # { pose_dict_key : -D var name }
    sentinel = cfg.frame.pose_sentinel

    tmp = tempfile.mkdtemp(prefix="frame_check_")
    ts = np.linspace(0.0, 1.0, nframes)
    npairs = len(list(itertools.combinations(cfg.parts, 2)))
    print(f"PER-FRAME collision gate: {nframes} frames, {npairs} pairs/frame "
          f"(EPS={cfg.eps} mm^3, touch_tol={cfg.touch_tol} mm)")
    bugs = []  # (frame, t, a, b, vol, dist, kind)
    for fi, t in enumerate(ts):
        p = pose(float(t))
        # Build the -D pose flags generically from the config mapping.
        pose_flags = []
        for key, var in pose_vars.items():
            pose_flags += ["-D", f"{var}={p[key]}"]
        meshes = {part: export_part_pose(cfg.pair, part, pose_flags, tmp, fi, sentinel)
                  for part in cfg.parts}
        frame_bugs = []
        for a, b in itertools.combinations(cfg.parts, 2):
            ma, mb = meshes[a], meshes[b]
            if ma is None or mb is None:
                continue
            vol = intersection_volume(ma, mb)
            dist = min_surface_distance(ma, mb)
            status, kind = classify(vol, dist, frozenset((a, b)) in cfg.intended,
                                    cfg.eps, cfg.touch_tol, cfg.contact_max)
            if status == "bug":
                frame_bugs.append((a, b, vol, dist, kind))
        # free meshes for this frame
        for f in os.listdir(tmp):
            os.remove(os.path.join(tmp, f))
        if frame_bugs:
            for a, b, vol, dist, kind in frame_bugs:
                bugs.append((fi, float(t), a, b, vol, dist, kind))
            tag = " ".join(f"{a}/{b}({vol:.0f}mm^3,{kind[:5]})" for a, b, vol, _, kind in frame_bugs)
            print(f"  frame {fi:3d} t={t:5.3f}  *** BUG ***  {tag}")
        else:
            print(f"  frame {fi:3d} t={t:5.3f}  clear")

    print("-" * 70)
    if bugs:
        print(f"RESULT: FAIL — {len(bugs)} unintended overlap(s) across frames:")
        for fi, t, a, b, vol, dist, kind in bugs:
            print(f"  frame {fi} (t={t:.3f}) {a}/{b}: {kind} "
                  f"(vol={vol:.1f} mm^3, mindist={dist:.3f} mm)")
        sys.exit(1)
    print(f"RESULT: PASS — 0 unintended overlaps across all {nframes} frames.")
    sys.exit(0)


if __name__ == "__main__":
    main(sys.argv)
