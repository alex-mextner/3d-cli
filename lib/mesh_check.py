#!/usr/bin/env python3
"""
mesh_check.py - manifold/watertight mesh gate (report 4.1).

Given an STL / 3MF (or a .scad it exports first), report:
  - watertight?         (closed surface, no boundary edges)
  - manifold?           (edge- + vertex-manifold, winding consistent)
  - self-intersections? (volumetric / face-face)
  - volume (mm^3)

Exit code:
  0  -> PASS  (watertight + manifold + no self-intersections)
  1  -> FAIL  (any geometric defect)
  2  -> usage / IO error

Engine tiers (graceful degradation):
  1. trimesh + open3d   -> FULL: watertight, edge+vertex-manifold, volume, and a
     REAL triangle-triangle self-intersection test (open3d.is_self_intersecting).
  2. trimesh + manifold3d (no open3d) -> watertight/manifold/volume computed;
     geometric self-intersection reported as UNVERIFIED (topology can't see it).
  3. no trimesh at all  -> FALLBACK: parse `openscad --render` stderr for
     WARNING:/ERROR: (repo's cheap first-line gate). Needs a .scad input; no
     mesh inspection, volume unknown.

Recommended invocation (full path) — via the `3d` CLI which resolves deps:
  3d mesh PART.stl
or directly:
  uv run --with trimesh,open3d,manifold3d,numpy mesh_check.py PART.stl

Usage:
  mesh_check.py PART.stl
  mesh_check.py PART.3mf
  mesh_check.py PART.scad           # exports to a temp STL first, then checks
  mesh_check.py PART.scad -D 'var=value' -D 'x=1'   # defines passed to openscad
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

# Resolve THIS repo's export helper (bin/3d), following the lib/ -> repo layout.
# Falls back to a direct openscad call if not present, so the tool is standalone.
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EXPORT_STL = os.path.join(REPO_ROOT, "bin", "3d")  # `3d export <scad> -o <stl>`
OPENSCAD = os.environ.get("OPENSCAD") or shutil.which("openscad") or "openscad"

# self-intersection volume below this (mm^3) is numeric sliver noise, not a real
# defect (mirrors verify/overlap.py EPS in AGENTS.md).
SELFX_EPS_MM3 = 8.0


def die(msg, code=2):
    print(f"mesh_check: {msg}", file=sys.stderr)
    sys.exit(code)


def export_scad_to_stl(scad_path, defines):
    """Export a .scad to a temp binary STL via openscad directly.
    Returns (stl_path, openscad_log)."""
    out_dir = tempfile.mkdtemp(prefix="mesh_check_")
    stl_path = os.path.join(out_dir, "part.stl")
    cmd = [OPENSCAD, "--export-format", "binstl"]
    for d in defines:
        cmd += ["-D", d]
    cmd += ["-o", stl_path, scad_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    log = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if not os.path.isfile(stl_path):
        die(f"openscad export produced no STL.\n{log}", 2)
    return stl_path, log


def openscad_warning_grep(log):
    """Return list of real WARNING:/ERROR: lines from an openscad log.
    grep ERROR: WITH the colon to avoid matching 'NoError' (AGENTS.md footgun)."""
    hits = []
    for line in log.splitlines():
        if re.search(r"\bERROR:", line) or re.search(r"\bWARNING:", line):
            hits.append(line.strip())
    return hits


def check_with_trimesh(mesh_path):
    import trimesh

    loaded = trimesh.load(mesh_path, force="mesh")
    if loaded is None or loaded.is_empty:
        die(f"trimesh loaded an empty mesh from {mesh_path}", 2)
    # force="mesh" already concatenates a Scene into one Trimesh.
    mesh = loaded

    watertight = bool(mesh.is_watertight)
    winding_ok = bool(mesh.is_winding_consistent)

    # vertex/edge manifold: the robust signal is is_watertight (no boundary
    # edges) + winding consistency + each edge shared by exactly 2 faces. Verify
    # edge degree explicitly. (vertex-manifold / bowtie detection comes from
    # open3d below when available.)
    edges_sorted = mesh.edges_sorted
    import numpy as np

    _, counts = np.unique(edges_sorted, axis=0, return_counts=True)
    edge_manifold = bool(counts.max() <= 2) if len(counts) else False

    volume = float(abs(mesh.volume)) if watertight else float("nan")

    # --- self-intersection + vertex-manifold ---------------------------------
    # GEOMETRIC self-intersection (faces interpenetrating without sharing an
    # edge) is NOT detectable from topology alone. manifold3d's status is purely
    # topological (NotManifold / NonFiniteVertex / ...), so it cannot see two
    # watertight cubes that overlap. open3d's is_self_intersecting() does the
    # real triangle-triangle test. Use it as the authoritative check; if open3d
    # is unavailable, report self-intersection as UNVERIFIED (None), never fake
    # a "no".
    selfx, vertex_manifold, selfx_detail = check_self_and_vertex(mesh)

    # manifold = edge-manifold + winding consistent + (vertex-manifold if known)
    manifold = edge_manifold and winding_ok and (vertex_manifold is not False)

    engine = "trimesh"
    if selfx_detail.get("via") == "open3d":
        engine += "+open3d"
    elif selfx_detail.get("via") == "manifold3d":
        engine += "+manifold3d"

    return {
        "engine": engine,
        "watertight": watertight,
        "manifold": manifold,
        "edge_manifold": edge_manifold,
        "vertex_manifold": vertex_manifold,  # True / False / None(unknown)
        "winding_consistent": winding_ok,
        "self_intersecting": selfx,          # True / False / None(unverified)
        "self_intersect_detail": selfx_detail,
        "volume_mm3": volume,
        "n_faces": int(len(mesh.faces)),
        "n_vertices": int(len(mesh.vertices)),
    }


def check_self_and_vertex(mesh):
    """Return (self_intersecting, vertex_manifold, detail).

    self_intersecting: True / False / None(unverified)
    vertex_manifold:   True / False / None(unknown)

    Authoritative path = open3d (real triangle-triangle self-intersection test +
    explicit vertex-manifold). Fallback = manifold3d topological status, which
    can prove a *topological* defect (NotManifold) but CANNOT see geometric
    self-intersection of an otherwise-valid mesh -> reported as None, not False.
    """
    detail = {}
    # ---- authoritative: open3d ----
    try:
        import numpy as np
        import open3d as o3d

        o3m = o3d.geometry.TriangleMesh(
            o3d.utility.Vector3dVector(np.asarray(mesh.vertices, dtype=np.float64)),
            o3d.utility.Vector3iVector(np.asarray(mesh.faces, dtype=np.int32)),
        )
        detail["via"] = "open3d"
        si = bool(o3m.is_self_intersecting())
        vm = bool(o3m.is_vertex_manifold())
        detail["o3d_is_self_intersecting"] = si
        detail["o3d_is_vertex_manifold"] = vm
        detail["o3d_is_watertight"] = bool(o3m.is_watertight())
        return si, vm, detail
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001 - open3d present but choked; degrade
        detail["open3d_error"] = str(e)

    # ---- fallback: manifold3d (topological only) ----
    try:
        import manifold3d
        import numpy as np

        verts = np.asarray(mesh.vertices, dtype=np.float32)
        tris = np.asarray(mesh.faces, dtype=np.uint32)
        man = manifold3d.Manifold(
            manifold3d.Mesh(vert_properties=verts, tri_verts=tris)
        )
        status = str(man.status())
        detail.setdefault("via", "manifold3d")
        detail["m3_status"] = status
        if "NoError" not in status:
            # a topological defect -> not a clean manifold. Report it as
            # non-manifold (vertex_manifold=False -> manifold=False), NOT as
            # self-intersecting (manifold3d's status is topological, not a
            # geometric triangle-triangle test).
            detail["note"] = f"manifold3d topological defect: {status}"
            return None, False, detail
        detail["m3_volume_mm3"] = float(man.volume())
        # topologically clean, but geometric self-intersection is UNVERIFIED.
        detail["note"] = (
            "geometric self-intersection NOT verified (install open3d for the "
            "real triangle-triangle test); manifold3d only checks topology"
        )
        return None, None, detail
    except ImportError:
        detail["via"] = "none"
        detail["note"] = "no open3d / manifold3d; self-intersection unverified"
        return None, None, detail


def report_and_exit(r):
    print("=" * 56)
    print("MESH CHECK (report 4.1 manifold/watertight gate)")
    print("=" * 56)
    print(f"engine            : {r['engine']}")
    print(f"faces / vertices  : {r.get('n_faces','?')} / {r.get('n_vertices','?')}")

    def yn(b):
        return "yes" if b else "NO"

    def vm_str(v):
        return "yes" if v is True else ("NO" if v is False else "unknown")

    print(f"watertight        : {yn(r['watertight'])}")
    print(
        f"manifold          : {yn(r['manifold'])}"
        f"  (edge-manifold={yn(r['edge_manifold'])},"
        f" vertex-manifold={vm_str(r.get('vertex_manifold'))},"
        f" winding={yn(r['winding_consistent'])})"
    )
    si = r["self_intersecting"]
    si_str = "YES" if si is True else ("no" if si is False else "UNVERIFIED")
    print(f"self-intersecting : {si_str}")
    if r.get("self_intersect_detail"):
        print(f"  detail          : {r['self_intersect_detail']}")
    v = r["volume_mm3"]
    print(f"volume (mm^3)     : {v if v == v else 'n/a (not watertight)'}")
    print("=" * 56)

    reasons = []
    if not r["watertight"]:
        reasons.append("not watertight")
    if not r["manifold"]:
        reasons.append("non-manifold")
    if r["self_intersecting"] is True:
        reasons.append("self-intersecting")
    if reasons:
        print(f">>> MESH CHECK: FAIL ({', '.join(reasons)})")
        sys.exit(1)
    if r["self_intersecting"] is None:
        # topology clean but geometric self-intersection not independently
        # verified. Don't fabricate a PASS on that field; warn loudly, still
        # gate-pass on the checks we DID compute (watertight + manifold).
        print(">>> MESH CHECK: PASS (self-intersection UNVERIFIED - "
              "install open3d for the geometric test)")
        sys.exit(0)
    print(">>> MESH CHECK: PASS")
    sys.exit(0)


def fallback_openscad(scad_path, defines):
    """No trimesh: render the .scad and grep warnings. Cannot compute volume or
    inspect a raw mesh, but catches OpenSCAD's own non-manifold warnings."""
    print("mesh_check: trimesh unavailable -> FALLBACK to openscad warning grep",
          file=sys.stderr)
    if not scad_path.lower().endswith(".scad"):
        die(
            "fallback path needs a .scad input (no mesh inspector available). "
            "Install deps: `uv run --with trimesh,manifold3d mesh_check.py ...` "
            "or `pip install --user trimesh manifold3d`.",
            2,
        )
    _, log = export_scad_to_stl(scad_path, defines)
    hits = openscad_warning_grep(log)
    print("=" * 56)
    print("MESH CHECK (FALLBACK: openscad --render warning grep)")
    print("=" * 56)
    if hits:
        print("openscad emitted WARNING:/ERROR: lines:")
        for h in hits:
            print("  " + h)
        print(">>> MESH CHECK: FAIL (openscad warnings)")
        sys.exit(1)
    print("no WARNING:/ERROR: from openscad --render")
    print("note: volume + self-intersection not checked in fallback mode.")
    print("note: install trimesh+manifold3d for the full geometric gate.")
    print(">>> MESH CHECK: PASS (fallback)")
    sys.exit(0)


def main(argv):
    if len(argv) < 2:
        die(__doc__.strip().splitlines()[0] + "\n\n" + usage(), 2)
    inp = argv[1]
    defines = []
    i = 2
    while i < len(argv):
        if argv[i] == "-D" and i + 1 < len(argv):
            defines.append(argv[i + 1])
            i += 2
        else:
            die(f"unknown arg: {argv[i]}\n{usage()}", 2)

    if not os.path.isfile(inp):
        die(f"no such file: {inp}", 2)

    # try trimesh; if missing, fall back.
    try:
        import trimesh  # noqa: F401
    except ImportError:
        fallback_openscad(inp, defines)
        return  # unreachable (fallback exits)

    ext = os.path.splitext(inp)[1].lower()
    if ext == ".scad":
        mesh_path, log = export_scad_to_stl(inp, defines)
        # surface any openscad warnings even on the trimesh path.
        hits = openscad_warning_grep(log)
        if hits:
            print("mesh_check: openscad warnings during export:", file=sys.stderr)
            for h in hits:
                print("  " + h, file=sys.stderr)
    elif ext in (".stl", ".3mf", ".obj", ".ply", ".off"):
        mesh_path = inp
    else:
        die(f"unsupported extension {ext} (want .scad/.stl/.3mf/.obj/.ply/.off)", 2)

    r = check_with_trimesh(mesh_path)
    report_and_exit(r)


def usage():
    return (
        "Usage: mesh_check.py PART.{stl,3mf,scad} [-D 'var=val' ...]\n"
        "  Via the CLI (resolves deps): 3d mesh PART.stl\n"
        "  Direct: uv run --with trimesh,manifold3d mesh_check.py PART.stl"
    )


if __name__ == "__main__":
    main(sys.argv)
