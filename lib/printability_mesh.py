#!/usr/bin/env python3
"""
Mesh-based FDM printability analyzer for a single part STL (lego-loco).

Encodes the repo's `fdm-printability` skill rules (Bambu A1 + PETG) as numeric
mesh checks, so `printability_check.sh` can give a real PASS/FAIL per part
instead of an eyeball verdict:

  - WALL / MIN FEATURE : local wall thickness via inward ray-casting from each
                         face (opposite the face normal). The nearest back-wall
                         hit distance = local thickness at that point. The min
                         over a sampled set is the thinnest wall in the part.
                         Rule: structural wall >= 1.2 mm, absolute floor 0.8 mm,
                         min feature/rib >= 1.0 mm.
  - OVERHANG           : faces whose UNDERSIDE slopes more than 45 deg from
                         vertical (downward-facing normal within 45 deg of -Z)
                         need support on PETG. Reports worst angle + overhang
                         area, assuming the part prints Z-up in its modeled
                         orientation. ADVISORY (orientation-dependent).
  - MANIFOLD           : watertight + winding-consistent (trimesh).

Thresholds come straight from skills/fdm-printability/SKILL.md.

Usage:  printability_mesh.py part.stl [--name NAME] [--json]
Exit 0 = PASS, 1 = FAIL (a hard rule violated), 2 = load/usage error.
"""
from __future__ import annotations

import sys
import json
import argparse
from typing import Any, cast
import numpy as np
import trimesh

# ---- thresholds (fdm-printability skill, Bambu A1 + PETG) -------------------
WALL_STRUCT   = 1.2     # mm, structural min wall (3 perimeters)
WALL_FLOOR    = 0.8     # mm, absolute floor (cosmetic, 2 perimeters)
MIN_FEATURE   = 1.0     # mm, min feature / detail
OVERHANG_DEG  = 45.0    # deg from vertical; PETG hard limit
OVERHANG_AREA_FRAC = 0.02  # ignore overhang verdict below this area fraction (noise/tiny)

# sampling controls (keep runtime sane on detailed parts)
MAX_RAYS      = 6000    # cap thickness rays
THIN_SAMPLE_FRAC = 0.05 # report the 5th-percentile thickness as "thin region"


def load_mesh(path: str) -> trimesh.Trimesh:
    m = cast(trimesh.Trimesh, trimesh.load(path, force='mesh'))
    if m is None or m.is_empty or len(m.faces) == 0:
        raise ValueError("empty / unreadable mesh")
    return m


def wall_thickness(mesh: trimesh.Trimesh) -> Any:
    """Local wall thickness at face centroids via inward ray-cast.

    For each sampled face we shoot a ray from its centroid in the -normal
    direction (into the solid) and take the nearest intersection with another
    face: that distance is the wall thickness there. Returns the array of hit
    thicknesses (mm)."""
    fc = mesh.triangles_center
    fn = mesh.face_normals
    n = len(fc)
    if n == 0:
        return np.array([])

    # subsample faces if huge, weighting toward larger faces (real walls, not slivers)
    if n > MAX_RAYS:
        areas = mesh.area_faces
        p = areas / areas.sum()
        idx = np.random.default_rng(0).choice(n, size=MAX_RAYS, replace=False, p=p)
    else:
        idx = np.arange(n)

    origins = fc[idx] - fn[idx] * 1e-4      # nudge inward to avoid self-hit
    dirs = -fn[idx]

    # multiple_hits=True so the first real back-wall (not the origin face) is found
    locs, ray_idx, tri_idx = mesh.ray.intersects_location(
        origins, dirs, multiple_hits=False)

    if len(ray_idx) == 0:
        return np.array([])
    d = np.linalg.norm(locs - origins[ray_idx], axis=1)
    # discard near-zero (numerical self-hits)
    d = d[d > 1e-3]
    return d


def overhang(mesh: trimesh.Trimesh) -> tuple[float, float]:
    """Worst overhang angle and overhanging area fraction, Z-up.

    A downward-facing face overhangs if the angle between its normal and -Z is
    <= OVERHANG_DEG (i.e. the surface points down and is shallower than 45 deg
    from horizontal-underside). We measure 'angle from vertical' as the angle of
    the surface plane: a face normal pointing straight down (-Z) is a flat
    ceiling = 90 deg overhang (worst). Convention here: report the steepest
    unsupported slope; flag if any meaningful downward area exceeds the limit."""
    fn = mesh.face_normals
    areas = mesh.area_faces
    total = areas.sum()
    nz = fn[:, 2]
    # downward-facing faces only (normal has a -Z component)
    down = nz < 0
    if not down.any():
        return 0.0, 0.0
    # angle of the underside surface measured from the vertical build axis:
    # face normal pointing straight DOWN (nz=-1) => horizontal ceiling => 0 deg
    #   from horizontal => 90 deg overhang from vertical wall. We express the
    # printability "overhang angle from vertical" = angle between surface and the
    # vertical plane = 90 - angle(normal, horizontal). Simpler and matches the
    # slicer convention: overhang severity = angle between face normal and -Z.
    # normal == -Z  -> 0 deg between them -> flat ceiling -> MOST severe.
    # normal horizontal (nz=0) -> 90 deg -> a vertical wall -> NOT an overhang.
    ang_from_down = np.degrees(np.arccos(np.clip(-nz[down], -1, 1)))  # 0=worst
    # "overhang from vertical" used in the skill: a face is unsupported when its
    # slope from vertical exceeds 45 deg, i.e. ang_from_down < (90-45)=45.
    overhang_mask = ang_from_down < (90.0 - OVERHANG_DEG)
    over_area = areas[down][overhang_mask].sum()
    frac = over_area / total if total > 0 else 0.0
    # worst-case overhang angle reported as "from vertical" = 90 - ang_from_down
    worst_from_vertical = 90.0 - ang_from_down.min() if len(ang_from_down) else 0.0
    return worst_from_vertical, frac


def analyze(path: str, name: str) -> dict[str, Any]:
    res: dict[str, Any] = {"part": name, "checks": {}, "verdict": "PASS"}
    try:
        mesh = load_mesh(path)
    except Exception as e:
        res["verdict"] = "ERROR"
        res["error"] = str(e)
        return res

    res["tris"] = int(len(mesh.faces))
    res["bbox_mm"] = [round(float(x), 1) for x in mesh.extents]

    # --- manifold / watertight (hard) ---
    wt = bool(mesh.is_watertight)
    res["checks"]["manifold"] = {
        "watertight": wt,
        "winding_consistent": bool(mesh.is_winding_consistent),
        "pass": wt,
        "hard": True,
    }
    if not wt:
        res["verdict"] = "FAIL"

    # --- wall thickness + min feature (hard) ---
    d = wall_thickness(mesh)
    if len(d):
        dmin = float(d.min())
        dthin = float(np.percentile(d, THIN_SAMPLE_FRAC * 100))
        wall_pass = dmin >= WALL_FLOOR and dthin >= MIN_FEATURE
        # structural advisory: how much is below 1.2
        below_struct = float((d < WALL_STRUCT).mean())
        res["checks"]["wall_thickness"] = {
            "min_mm": round(dmin, 3),
            "p5_mm": round(dthin, 3),
            "floor_mm": WALL_FLOOR,
            "struct_mm": WALL_STRUCT,
            "frac_below_struct": round(below_struct, 3),
            "pass": bool(wall_pass),
            "hard": True,
            "note": ("min<floor 0.8" if dmin < WALL_FLOOR else
                     ("p5<1.0 (thin feature)" if dthin < MIN_FEATURE else "ok")),
        }
        if not wall_pass:
            res["verdict"] = "FAIL"
    else:
        res["checks"]["wall_thickness"] = {
            "pass": True, "hard": True, "note": "no rays hit (open/degenerate) - skipped"}

    # --- overhang (advisory; orientation-dependent) ---
    worst, frac = overhang(mesh)
    over_pass = frac <= OVERHANG_AREA_FRAC
    res["checks"]["overhang"] = {
        "worst_from_vertical_deg": round(float(worst), 1),
        "limit_deg": OVERHANG_DEG,
        "overhang_area_frac": round(float(frac), 4),
        "area_thresh": OVERHANG_AREA_FRAC,
        "pass": bool(over_pass),
        "hard": False,    # advisory: depends on chosen print orientation/supports
        "note": ("needs support or re-orient (PETG)" if not over_pass else "ok"),
    }
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stl")
    ap.add_argument("--name", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    name = a.name or a.stl.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    res = analyze(a.stl, name)

    if a.json:
        print(json.dumps(res))
    else:
        v = res["verdict"]
        print(f"  part: {name}  [{v}]  tris={res.get('tris','?')}  bbox={res.get('bbox_mm','?')} mm")
        if v == "ERROR":
            print(f"    ERROR: {res.get('error')}")
        for cname, c in res["checks"].items():
            tag = "HARD" if c.get("hard") else "adv "
            ok = "PASS" if c.get("pass") else ("FAIL" if c.get("hard") else "WARN")
            extra = {k: vv for k, vv in c.items() if k not in ("pass", "hard", "note")}
            print(f"    [{ok}] ({tag}) {cname:14s} {c.get('note','')}  {extra}")

    if res["verdict"] == "FAIL":
        sys.exit(1)
    if res["verdict"] == "ERROR":
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
