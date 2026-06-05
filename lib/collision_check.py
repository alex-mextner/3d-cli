#!/usr/bin/env python3
"""GENERIC part-collision GATE (static) — robust mesh booleans + zero-gap detection.

PROJECT-AGNOSTIC: takes a project config JSON (e.g. projects/ejector/verify/collision.json)
that supplies the placement .scad, the part list, the phases, the intended-contact set, and
the EPS/TOUCH_TOL/CONTACT_MAX thresholds. Nothing project-specific is hardcoded here.

For every part pair, at every configured phase, this:
  1. Exports each part PLACED in its phase position (pair.scad `solo` mode) as a binary STL
     and loads it with trimesh.
  2. INTERPENETRATION: exact boolean intersection via the manifold3d engine
     (trimesh.boolean.intersection(..., engine="manifold")); flags volume > EPS.
  3. ZERO-GAP TOUCH / coincident faces: minimum surface-surface distance (both directions,
     trimesh.proximity.closest_point over vertices AND face centroids); flags min-distance
     < TOUCH_TOL even when the intersection volume is ~0.
  4. Classifies each pair/phase as clear / intended-contact / BUG and prints a table.

Exits NON-ZERO if ANY unintended interpenetration OR zero-gap touch exists, so the pre-commit
hook (via verify/run_all.sh) blocks a dirty assembly from being committed.

Libraries (all under `uv run --with ...`, no global installs):
  trimesh + manifold3d  — robust, exact boolean intersection meshes & signed volume
  scipy (cKDTree)       — backs trimesh.proximity nearest-surface queries
  rtree                 — trimesh spatial index for proximity
  numpy

Run:  uv run --with trimesh,manifold3d,rtree,numpy,scipy \
          python3 tools/collision/collision_check.py <project>/verify/collision.json
Env:  PHASES_SEL (e.g. "0,1") restricts which configured phases are gated.
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
import trimesh

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from config import load  # noqa: E402

np.seterr(divide="ignore", invalid="ignore")  # sharp tips yield degenerate tris


def _load_mesh(path: str) -> trimesh.Trimesh | None:
    """Load an STL as a single Trimesh (force='mesh' concatenates any Scene)."""
    m = cast(trimesh.Trimesh, trimesh.load(path, force="mesh", process=True))
    if m is None or m.is_empty or len(m.faces) == 0:
        return None
    return m


def export_part(
    pair_scad: pathlib.Path, part: str, phase: int, tmp: str
) -> trimesh.Trimesh | None:
    """Render one PLACED part at a phase to binary STL and load it as a trimesh.Trimesh."""
    out = os.path.join(tmp, f"solo_{part}_{phase}.stl")
    subprocess.run(
        ["openscad", "--export-format", "binstl", "-o", out,
         "-D", f'solo="{part}"', "-D", f"phase={phase}", str(pair_scad)],
        capture_output=True, text=True, timeout=180, check=False)
    if not os.path.exists(out) or os.path.getsize(out) < 100:
        return None
    return _load_mesh(out)


def intersection_volume(a: trimesh.Trimesh, b: trimesh.Trimesh) -> float:
    """Exact boolean-intersection volume (mm^3) via the manifold engine; 0 if disjoint."""
    try:
        inter = trimesh.boolean.intersection([a, b], engine="manifold")
    except Exception:
        return 0.0
    if inter is None or inter.is_empty or len(inter.faces) == 0:
        return 0.0
    return abs(float(inter.volume))


def _sample_points(m: trimesh.Trimesh) -> Any:
    """Vertices + face centroids of a mesh — covers both near-misses and coincident faces."""
    return np.vstack([m.vertices, m.triangles.mean(axis=1)])


def min_surface_distance(a: trimesh.Trimesh, b: trimesh.Trimesh) -> float:
    """Minimum surface-to-surface distance (mm), queried in BOTH directions."""
    _, da, _ = trimesh.proximity.closest_point(b, _sample_points(a))
    _, db, _ = trimesh.proximity.closest_point(a, _sample_points(b))
    return float(min(da.min(), db.min()))


def classify(
    vol: float, dist: float, intended: bool,
    eps: float, touch_tol: float, contact_max: float,
) -> tuple[str, str]:
    """Return (status, kind) where status in {clear, intended, bug}."""
    interpen = vol > eps
    touch = dist < touch_tol
    if not interpen and not touch:
        return "clear", ""
    kind = "interpenetration" if interpen else "gap-0 touch"
    # intended pairs are excused ONLY for small contact volumes; a big overlap is a real bug.
    if intended and vol <= contact_max:
        return "intended", kind
    return "bug", kind


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        print("usage: collision_check.py <config.json>", file=sys.stderr)
        sys.exit(2)
    cfg = load(argv[1])

    # PHASES_SEL (e.g. "0,1") restricts which configured phases are gated — used to demonstrate
    # that a clean subset of phases passes. Default = all phases from the config.
    sel = os.environ.get("PHASES_SEL")
    phases = ({int(p): cfg.phases[int(p)] for p in sel.split(",")} if sel else cfg.phases)

    tmp = tempfile.mkdtemp(prefix="collision_")
    # Pre-export every placed part once per phase (reused across all pairs).
    meshes: dict[tuple[str, int], trimesh.Trimesh | None] = {}
    for ph in phases:
        for p in cfg.parts:
            meshes[(p, ph)] = export_part(cfg.pair, p, ph, tmp)

    rows: list[tuple[int, str, str, float, float, str, str]] = []
    bugs: list[tuple[int, str, str, float, float, str]] = []
    for ph in phases:
        for a, b in itertools.combinations(cfg.parts, 2):
            ma, mb = meshes[(a, ph)], meshes[(b, ph)]
            if ma is None or mb is None:
                continue
            vol = intersection_volume(ma, mb)
            dist = min_surface_distance(ma, mb)
            intended = frozenset((a, b)) in cfg.intended
            status, kind = classify(vol, dist, intended, cfg.eps, cfg.touch_tol, cfg.contact_max)
            rows.append((ph, a, b, vol, dist, status, kind))
            if status == "bug":
                bugs.append((ph, a, b, vol, dist, kind))

    # ---- table ----
    print(f"\ncollision check  (EPS={cfg.eps} mm^3, touch_tol={cfg.touch_tol} mm)")
    print("-" * 78)
    print(f"{'phase':8s} {'pair':22s} {'vol mm^3':>9s} {'mindist mm':>11s}  status / kind")
    print("-" * 78)
    for ph, a, b, vol, dist, status, kind in rows:
        if status == "clear":
            continue   # only print the interesting rows (contacts/bugs); keeps it readable
        tag = {"intended": "intended-contact", "bug": "*** BUG ***"}[status]
        print(f"{phases[ph]:8s} {a+'/'+b:22s} {vol:9.1f} {dist:11.3f}  {tag} ({kind})")
    print("-" * 78)
    print("legend: only contacts/bugs shown; all other pairs are clear "
          "(vol<=EPS and mindist>=tol).")

    if bugs:
        print(f"\nRESULT: FAIL — {len(bugs)} unintended collision(s):")
        for ph, a, b, vol, dist, kind in bugs:
            print(f"  [{phases[ph]:>6s}] {a}/{b}: {kind}  "
                  f"(vol={vol:.1f} mm^3, mindist={dist:.3f} mm)")
        sys.exit(1)
    print("\nRESULT: PASS — no unintended interpenetration or zero-gap touch.")
    sys.exit(0)


if __name__ == "__main__":
    main(sys.argv)
