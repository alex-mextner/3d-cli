#!/usr/bin/env python3
"""prep_mesh.py — the heavy geometry engine behind `3d prep`.

WHAT: takes a single-body mesh (.stl / .3mf) and prepares it for FDM printing in one pass:

  1. MESH REPAIR — merge coincident vertices, drop duplicate/degenerate faces, make the
     winding consistent, recompute outward normals, and fill boundary holes. Reports the
     BEFORE/AFTER watertight + manifold state and how many boundary loops were closed. It
     never claims success blindly: if the mesh is still non-watertight after repair, the
     report and the exit code say so.
  2. AUTO-ORIENT — evaluate candidate rest orientations (every convex-hull face normal, i.e.
     the "rest on this face" candidates, plus the 6 axis-aligned directions). Each candidate
     is scored by its SUPPORT-overhang area fraction (the printability overhang metric, but
     excluding the faces that rest ON the bed), tie-broken by lower build height then smaller
     footprint. The winner is applied (rotate the resting face down, drop min-Z to 0).
  3. FINAL REPORT — re-run the shared printability analyzer (`printability_mesh.analyze`) and
     the manifold/watertight gate (`mesh_check.check_with_trimesh`) on the prepared mesh.
  4. EMIT — write the repaired + oriented STL, and optionally a print-ready single-plate 3MF.

WHY: nothing in the CLI did auto-orientation, mesh repair, or a single print-prep
  orchestrator. A model that is non-manifold, or modeled resting on an overhang-heavy face,
  wastes filament and time. `prep` fixes the geometry and picks a low-support orientation
  before the part ever reaches the slicer.

This is a TOOL (run via `cli.pyrun` with its deps), NOT a command module, so it may import
trimesh/numpy at top level (mirrors `printability_mesh.py`). The overhang metric and the
final analyzer are REUSED from `printability_mesh`, not reimplemented.

Exit codes (mirror errors.py so the `prep` command can translate them):
  0   -> READY (watertight after repair AND printability verdict PASS)
  1   -> NOT READY (still non-watertight after repair, or a HARD printability rule failed)
  2   -> usage / IO error (missing / unreadable / empty mesh)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import trimesh

from printability_mesh import OVERHANG_DEG, analyze, load_mesh

_AXES: tuple[tuple[float, float, float], ...] = (
    (1.0, 0.0, 0.0), (-1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0), (0.0, -1.0, 0.0),
    (0.0, 0.0, 1.0), (0.0, 0.0, -1.0),
)
_MAX_HULL_CANDIDATES = 128  # cap candidate orientations so a busy hull can't blow up runtime
_DEFAULT_BED = 270.0        # square bed (mm), Snapmaker U1 default (mirrors `3d arrange`)


# --------------------------------------------------------------------------------------
# Mesh repair
# --------------------------------------------------------------------------------------
@dataclass
class RepairReport:
    """Before/after health of the mesh across the repair pass."""

    faces_before: int
    faces_after: int
    watertight_before: bool
    watertight_after: bool
    winding_before: bool
    winding_after: bool
    holes_before: int    # boundary loops before filling
    holes_after: int     # boundary loops still open after filling
    holes_filled: int    # loops closed by the repair


def _boundary_edges(mesh: "trimesh.Trimesh") -> np.ndarray:
    """Edges shared by exactly one face (the open boundary). Shape (K, 2), possibly empty."""
    edges = mesh.edges_sorted
    if len(edges) == 0:
        return np.empty((0, 2), dtype=int)
    uniq, counts = np.unique(edges, axis=0, return_counts=True)
    return uniq[counts == 1]


def _boundary_loops(mesh: "trimesh.Trimesh") -> int:
    """Count connected boundary loops (open holes) via union-find over boundary edges.

    Each connected group of boundary edges is one hole loop. Kept dependency-free (no
    networkx) so the .venv unit tests import and run cleanly.
    """
    boundary = _boundary_edges(mesh)
    if len(boundary) == 0:
        return 0
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # path compression
            parent[x], x = root, parent[x]
        return root

    for a, b in boundary:
        ra, rb = find(int(a)), find(int(b))
        if ra != rb:
            parent[rb] = ra
    return len({find(v) for v in parent})


def _boundary_rings(mesh: "trimesh.Trimesh") -> list[list[int]]:
    """Ordered vertex rings for each boundary loop (walk the boundary-edge adjacency)."""
    boundary = _boundary_edges(mesh)
    adj: dict[int, list[int]] = {}
    for a, b in boundary:
        adj.setdefault(int(a), []).append(int(b))
        adj.setdefault(int(b), []).append(int(a))
    rings: list[list[int]] = []
    visited: set[int] = set()
    for start in adj:
        if start in visited:
            continue
        ring = [start]
        visited.add(start)
        prev, cur = -1, start
        while True:
            nexts = [v for v in adj.get(cur, []) if v != prev]
            nexts = [v for v in nexts if v not in visited] or [v for v in nexts if v == start]
            if not nexts:
                break
            nxt = nexts[0]
            if nxt == start:
                break
            ring.append(nxt)
            visited.add(nxt)
            prev, cur = cur, nxt
        if len(ring) >= 3:
            rings.append(ring)
    return rings


def _fill_holes(mesh: "trimesh.Trimesh") -> None:
    """Close boundary loops: prefer trimesh's filler, fall back to a fan triangulation.

    `trimesh.repair.fill_holes` needs networkx; when it is unavailable (or leaves holes),
    fan-triangulate each remaining boundary loop from its first vertex. Fan fill closes the
    simple triangular/quad holes that repair typically leaves and keeps the tool usable
    without the optional networkx dep.
    """
    try:
        trimesh.repair.fill_holes(mesh)
    except Exception:  # noqa: BLE001 — missing networkx or a filler edge case; fan-fill below
        pass
    if mesh.is_watertight:
        return
    new_faces: list[list[int]] = []
    for ring in _boundary_rings(mesh):
        pivot = ring[0]
        for i in range(1, len(ring) - 1):
            new_faces.append([pivot, ring[i], ring[i + 1]])
    if new_faces:
        mesh.faces = np.vstack([np.asarray(mesh.faces), np.asarray(new_faces, dtype=int)])
        trimesh.repair.fix_winding(mesh)


def repair(mesh: "trimesh.Trimesh") -> tuple["trimesh.Trimesh", RepairReport]:
    """Repair a copy of `mesh` and report the before/after health.

    Order matters: merge coincident vertices first (so duplicate/degenerate detection sees
    the real topology), drop bad faces, make winding consistent, recompute outward normals,
    then fill remaining boundary holes.
    """
    m = mesh.copy()
    faces_before = int(len(m.faces))
    wt_before = bool(m.is_watertight)
    wind_before = bool(m.is_winding_consistent)
    holes_before = _boundary_loops(m)

    m.merge_vertices()
    m.update_faces(m.unique_faces())          # drop duplicate faces
    m.update_faces(m.nondegenerate_faces())   # drop zero-area faces
    m.remove_unreferenced_vertices()
    trimesh.repair.fix_winding(m)
    trimesh.repair.fix_normals(m)
    _fill_holes(m)

    holes_after = _boundary_loops(m)
    report = RepairReport(
        faces_before=faces_before,
        faces_after=int(len(m.faces)),
        watertight_before=wt_before,
        watertight_after=bool(m.is_watertight),
        winding_before=wind_before,
        winding_after=bool(m.is_winding_consistent),
        holes_before=holes_before,
        holes_after=holes_after,
        holes_filled=max(0, holes_before - holes_after),
    )
    return m, report


# --------------------------------------------------------------------------------------
# Auto-orientation
# --------------------------------------------------------------------------------------
@dataclass
class OrientResult:
    """One scored rest orientation: the down-direction, its rotation, and the score fields."""

    down: list[float]           # the face normal steered to point at the bed (-Z)
    matrix: list[list[float]]   # 4x4 transform that realizes it
    overhang_frac: float        # SUPPORT overhang area fraction (bed-contact faces excluded)
    build_height: float         # Z extent after orienting (mm)
    footprint_area: float       # XY bounding-box area after orienting (mm^2)
    source: str = "axis"        # "axis" or "hull" — where the candidate came from


def candidate_down_directions(mesh: "trimesh.Trimesh") -> list[tuple[np.ndarray, str]]:
    """Candidate rest directions: unique convex-hull face normals + the 6 axis directions.

    Each direction is the outward normal of the face that would rest on the bed. Duplicates
    (rounded to 2 decimals) are collapsed so a faceted hull does not explode the search.
    """
    out: list[tuple[np.ndarray, str]] = []
    seen: set[tuple[int, int, int]] = set()

    def add(vec: np.ndarray, source: str) -> None:
        norm = float(np.linalg.norm(vec))
        if norm < 1e-9:
            return
        unit = vec / norm
        key = (int(round(unit[0] * 100)), int(round(unit[1] * 100)), int(round(unit[2] * 100)))
        if key in seen:
            return
        seen.add(key)
        out.append((unit, source))

    try:
        hull_normals = np.asarray(mesh.convex_hull.face_normals, dtype=float)
    except Exception:  # noqa: BLE001 — a degenerate hull just means "axes only"
        hull_normals = np.empty((0, 3))
    for n in hull_normals[:_MAX_HULL_CANDIDATES]:
        add(n, "hull")
    for ax in _AXES:
        add(np.asarray(ax, dtype=float), "axis")
    return out


def _support_overhang_frac(mesh: "trimesh.Trimesh") -> float:
    """Overhang area fraction that would actually need support, in the CURRENT orientation.

    Same downward-face / angle test as `printability_mesh.overhang`, but it EXCLUDES the
    faces that rest on the bed (downward-facing faces at the minimum Z). Those carry the part
    and never need support — counting them (as the bare printability metric does) would
    perversely punish laying the part flat on its largest face, which is exactly what we want.
    """
    fn = mesh.face_normals
    areas = mesh.area_faces
    total = float(areas.sum())
    if total <= 0.0:
        return 0.0
    nz = fn[:, 2]
    zc = mesh.triangles_center[:, 2]
    zmin = float(mesh.bounds[0][2])
    tol = max(1e-4, 0.02 * float(max(mesh.extents)))

    down = nz < 0.0
    ang_from_down = np.degrees(np.arccos(np.clip(-nz, -1.0, 1.0)))  # 0 = straight-down = worst
    resting = down & (zc <= zmin + tol) & (nz <= -0.5)
    overhang_mask = down & (ang_from_down < (90.0 - OVERHANG_DEG)) & (~resting)
    return float(areas[overhang_mask].sum() / total)


def score_orientation(
    mesh: "trimesh.Trimesh", down: tuple[float, float, float] | np.ndarray, source: str = "axis"
) -> OrientResult:
    """Rotate `down` to point at the bed (-Z), drop to Z=0, and score the result."""
    d = np.asarray(down, dtype=float)
    transform = trimesh.geometry.align_vectors(d, np.array([0.0, 0.0, -1.0]))
    m = mesh.copy()
    m.apply_transform(transform)
    m.apply_translation([0.0, 0.0, -float(m.bounds[0][2])])
    ext = m.extents
    return OrientResult(
        down=[float(d[0]), float(d[1]), float(d[2])],
        matrix=[[float(v) for v in row] for row in transform],
        overhang_frac=_support_overhang_frac(m),
        build_height=float(ext[2]),
        footprint_area=float(ext[0] * ext[1]),
        source=source,
    )


def _orient_key(result: OrientResult) -> tuple[float, float, float]:
    """Selection order: least overhang, then lowest build, then smallest footprint."""
    return (round(result.overhang_frac, 4), round(result.build_height, 3), round(result.footprint_area, 3))


def best_orientation(mesh: "trimesh.Trimesh") -> tuple[OrientResult, OrientResult]:
    """Return (best, baseline). Baseline is the as-modeled orientation (no rotation)."""
    baseline = score_orientation(mesh, (0.0, 0.0, -1.0), source="baseline")
    results = [score_orientation(mesh, d, source=src) for d, src in candidate_down_directions(mesh)]
    best = min(results, key=_orient_key) if results else baseline
    return best, baseline


def apply_orientation(mesh: "trimesh.Trimesh", result: OrientResult) -> "trimesh.Trimesh":
    """Apply the chosen rotation, center in XY, and drop min-Z to 0 (rest on the bed)."""
    m = mesh.copy()
    m.apply_transform(np.asarray(result.matrix, dtype=float))
    center = m.bounds.mean(axis=0)
    m.apply_translation([-float(center[0]), -float(center[1]), -float(m.bounds[0][2])])
    return m


def rotation_angle_deg(result: OrientResult) -> float:
    """The magnitude of the applied rotation (degrees), for the human report."""
    rot = np.asarray(result.matrix, dtype=float)[:3, :3]
    cos_theta = (float(np.trace(rot)) - 1.0) / 2.0
    return float(math.degrees(math.acos(max(-1.0, min(1.0, cos_theta)))))


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------
def write_stl(mesh: "trimesh.Trimesh", out_path: str) -> None:
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    mesh.export(out_path, file_type="stl")


def write_3mf(mesh: "trimesh.Trimesh", out_path: str, *, name: str, bed: float) -> None:
    """Write a print-ready single-plate Orca/Bambu project 3MF (reuses `orca_project_3mf`)."""
    from orca_project_3mf import MeshPart, write_project_3mf

    verts = [(float(v[0]), float(v[1]), float(v[2])) for v in mesh.vertices]
    tris = [(int(f[0]), int(f[1]), int(f[2])) for f in mesh.faces]
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    write_project_3mf(out_path, [MeshPart(name=name, vertices=verts, triangles=tris, plate=0)], bed=bed)


# --------------------------------------------------------------------------------------
# Orchestration + report
# --------------------------------------------------------------------------------------
@dataclass
class PrepResult:
    """Everything the pipeline produced, for the human report and the --json summary."""

    input: str
    name: str
    repair: dict[str, Any]
    orientation: dict[str, Any]
    printability: dict[str, Any]
    mesh_check: dict[str, Any]
    outputs: dict[str, str]
    ready: bool
    reasons: list[str] = field(default_factory=list)


def _final_checks(out_stl: str, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reuse the shared printability analyzer + manifold gate on the prepared STL."""
    printability = analyze(out_stl, name)
    try:
        import mesh_check

        manifold = mesh_check.check_with_trimesh(out_stl)
    except Exception as exc:  # noqa: BLE001 — manifold detail is a bonus, never fatal
        manifold = {"error": str(exc)}
    return printability, manifold


def _orientation_summary(best: OrientResult, baseline: OrientResult) -> dict[str, Any]:
    improvement = baseline.overhang_frac - best.overhang_frac
    return {
        "chosen_down": best.down,
        "source": best.source,
        "rotation_deg": round(rotation_angle_deg(best), 2),
        "overhang_frac_before": round(baseline.overhang_frac, 4),
        "overhang_frac_after": round(best.overhang_frac, 4),
        "overhang_improvement": round(improvement, 4),
        "build_height_mm": round(best.build_height, 3),
        "footprint_mm2": round(best.footprint_area, 3),
    }


def run_pipeline(
    inp: str, *, out_stl: str, out_3mf: str | None, bed: float, name: str
) -> PrepResult:
    """Load -> repair -> orient -> write -> re-check. Returns the full result."""
    mesh = load_mesh(inp)
    repaired, rep = repair(mesh)
    best, baseline = best_orientation(repaired)
    oriented = apply_orientation(repaired, best)

    write_stl(oriented, out_stl)
    outputs = {"stl": out_stl}
    if out_3mf:
        write_3mf(oriented, out_3mf, name=name, bed=bed)
        outputs["3mf"] = out_3mf

    printability, manifold = _final_checks(out_stl, name)
    reasons: list[str] = []
    if not rep.watertight_after:
        reasons.append("still non-watertight after repair")
    if printability.get("verdict") == "FAIL":
        reasons.append("printability HARD rule failed")
    return PrepResult(
        input=inp,
        name=name,
        repair=asdict(rep),
        orientation=_orientation_summary(best, baseline),
        printability=printability,
        mesh_check=manifold,
        outputs=outputs,
        ready=not reasons,
        reasons=reasons,
    )


def _print_report(res: PrepResult) -> None:
    rep = res.repair
    ori = res.orientation
    print("================================================================")
    print(f" prep: {res.name}")
    print("================================================================")
    print("-- repair --")
    print(f"  faces        : {rep['faces_before']} -> {rep['faces_after']}")
    print(f"  watertight   : {_yn(rep['watertight_before'])} -> {_yn(rep['watertight_after'])}")
    print(f"  winding ok   : {_yn(rep['winding_before'])} -> {_yn(rep['winding_after'])}")
    print(f"  holes        : {rep['holes_before']} -> {rep['holes_after']}  (filled {rep['holes_filled']})")
    print("-- auto-orient --")
    print(f"  chosen down  : {_fmt_vec(ori['chosen_down'])}  ({ori['source']}, {ori['rotation_deg']} deg)")
    print(f"  overhang     : {ori['overhang_frac_before']:.4f} -> {ori['overhang_frac_after']:.4f}"
          f"  (improved {ori['overhang_improvement']:+.4f})")
    print(f"  build height : {ori['build_height_mm']:.2f} mm   footprint {ori['footprint_mm2']:.1f} mm^2")
    print("-- printability --")
    _print_printability(res.printability)
    print("-- output --")
    for kind, path in res.outputs.items():
        print(f"  {kind:<4} : {path}")
    print("================================================================")
    verdict = "READY" if res.ready else "NOT READY"
    print(f">>> PREP: {verdict}" + ("" if res.ready else f"  ({'; '.join(res.reasons)})"))
    print("================================================================")


def _print_printability(ana: dict[str, Any]) -> None:
    if ana.get("verdict") == "ERROR":
        print(f"  ERROR: {ana.get('error')}")
        return
    for cname, check in ana.get("checks", {}).items():
        hard = "HARD" if check.get("hard") else "adv "
        ok = "PASS" if check.get("pass") else ("FAIL" if check.get("hard") else "WARN")
        print(f"  [{ok}] ({hard}) {cname:14s} {check.get('note', '')}")
    print(f"  verdict      : {ana.get('verdict')}")


def _yn(value: bool) -> str:
    return "yes" if value else "NO"


def _fmt_vec(vec: list[float]) -> str:
    return "[" + ", ".join(f"{v:+.2f}" for v in vec) + "]"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="prep_mesh", add_help=False)
    ap.add_argument("input")
    ap.add_argument("-o", "--out", dest="out", default="")
    ap.add_argument("--3mf", dest="out_3mf", default="")
    ap.add_argument("--bed", type=float, default=_DEFAULT_BED)
    ap.add_argument("--name", default="")
    ap.add_argument("--json", action="store_true")
    return ap.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    inp = args.input
    if not os.path.isfile(inp):
        sys.stderr.write(f"prep: input not found: {inp}\n")
        return 2
    name = args.name or os.path.splitext(os.path.basename(inp))[0]
    out_stl = args.out or (os.path.splitext(inp)[0] + ".prep.stl")
    out_3mf = args.out_3mf or None

    try:
        res = run_pipeline(inp, out_stl=out_stl, out_3mf=out_3mf, bed=args.bed, name=name)
    except ValueError as exc:  # empty / unreadable mesh from load_mesh
        sys.stderr.write(f"prep: {exc}\n")
        return 2

    if args.json:
        print(json.dumps(asdict_result(res), indent=2, sort_keys=True))
    else:
        _print_report(res)
    return 0 if res.ready else 1


def asdict_result(res: PrepResult) -> dict[str, Any]:
    return {
        "input": res.input,
        "name": res.name,
        "repair": res.repair,
        "orientation": res.orientation,
        "printability": res.printability,
        "mesh_check": res.mesh_check,
        "outputs": res.outputs,
        "ready": res.ready,
        "reasons": res.reasons,
    }


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
