#!/usr/bin/env python3
"""mesh_metrics.py -- pinned-convention 3D shape metrics between two meshes.

ACCESSED VIA: `3d metrics geometry <mesh_a> <mesh_b> [options]`
(lib/commands/metrics.py runs THIS file through cli.pyrun so the resolved runtime
carries trimesh/scipy/numpy; the command module itself stays stdlib-only).

WHAT IT COMPUTES (APPLY-RESEARCH P1.1 / benchmarks-and-metrics §3):
  - Chamfer distance (L1 + L2, bidirectional, mean reduction).
  - F-score@tau (tau = 1% of the TARGET bbox diagonal) -- the PRIMARY, least-gameable
    surface metric (Tatarchenko et al. CVPR 2019).
  - Hausdorff distance (both directed sweeps + symmetric).
  - Normal consistency (absolute dot of nearest-neighbour surface normals).
  - Volumetric IoU (occupancy over a shared voxel grid via trimesh `contains`).

WHY EVERY NUMBER CARRIES ITS CONVENTION:
  benchmarks-and-metrics §4.4 warns "a longitudinal store is worthless if the
  convention silently drifts between runs." So every reported metric records its
  sense (higher/lower-better) AND the knobs that change the number: sample count,
  seed, tau (fraction + absolute), Chamfer reduction, Hausdorff direction, voxel
  resolution. `3d metrics show` / `3d ai bench --compare` can then diff like-for-like.

CONVENTIONS / ASSUMPTIONS:
  - Inputs are assumed to share a coordinate frame (same units, pre-aligned). This
    module does NOT run ICP; alignment is recorded as "none" so a caller that skipped
    alignment cannot mistake a pose offset for a shape error.
  - `mesh_b` is treated as the TARGET / reference: tau is 1% of its bbox diagonal and
    F-score precision is "fraction of mesh_a points near mesh_b".

INVARIANTS:
  - Module top level is stdlib-only; numpy / scipy / trimesh are lazy-imported inside
    the functions that need them (keeps the module importable under the stdlib-only
    contract and standalone-testable at the point-set level without trimesh).
  - Point-set level functions take plain numpy arrays and return plain floats/dicts,
    so unit tests can feed hand-built point sets with EXACT known answers.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

# --------------------------------------------------------------------------- #
# Default conventions (surface here so the store + docs cite one source).
# --------------------------------------------------------------------------- #
DEFAULT_SAMPLES = 50_000
DEFAULT_TAU_FRAC = 0.01  # F-score tau = 1% of target bbox diagonal (Tatarchenko).
DEFAULT_VOXEL_RES = 48   # grid cells along the longest union-bbox extent for vol-IoU.
DEFAULT_SEED = 0

SENSE_LOWER = "lower_better"
SENSE_HIGHER = "higher_better"


# --------------------------------------------------------------------------- #
# Point-set core (numpy + scipy.spatial.cKDTree). Pure, exact, testable.
# --------------------------------------------------------------------------- #
def _nn_query(query: Any, reference: Any) -> tuple[Any, Any]:
    """Nearest-neighbour distances + indices from each `query` point into `reference`."""
    from scipy.spatial import cKDTree

    tree = cKDTree(reference)
    dist, idx = tree.query(query, k=1)
    return dist, idx


def chamfer_distance(points_a: Any, points_b: Any) -> dict[str, float]:
    """Bidirectional Chamfer distance (mean reduction) between two point sets.

    L1 = mean_a min_b ||a-b|| + mean_b min_a ||a-b||   (unsquared).
    L2 = mean_a min_b ||a-b||^2 + mean_b min_a ||a-b||^2 (squared, benchmarks §3.1).
    Identical point sets score exactly 0.
    """
    import numpy as np

    d_ab, _ = _nn_query(points_a, points_b)
    d_ba, _ = _nn_query(points_b, points_a)
    d_ab = np.asarray(d_ab, dtype=np.float64)
    d_ba = np.asarray(d_ba, dtype=np.float64)
    return {
        "l1": float(d_ab.mean() + d_ba.mean()),
        "l2": float((d_ab**2).mean() + (d_ba**2).mean()),
        "a_to_b_mean": float(d_ab.mean()),
        "b_to_a_mean": float(d_ba.mean()),
    }


def f_score(points_a: Any, points_b: Any, tau: float) -> dict[str, float]:
    """F-score@tau. `points_a` = generated, `points_b` = target (benchmarks §3.2).

    Precision = fraction of generated points within tau of a target point.
    Recall    = fraction of target points within tau of a generated point.
    F = 2PR/(P+R). Range 0..1, 1 best. Identical sets score exactly 1.
    """
    import numpy as np

    d_ab, _ = _nn_query(points_a, points_b)
    d_ba, _ = _nn_query(points_b, points_a)
    precision = float((np.asarray(d_ab) <= tau).mean())
    recall = float((np.asarray(d_ba) <= tau).mean())
    denom = precision + recall
    fscore = (2.0 * precision * recall / denom) if denom > 0 else 0.0
    return {"f_score": fscore, "precision": precision, "recall": recall, "tau": float(tau)}


def hausdorff_distance(points_a: Any, points_b: Any) -> dict[str, float]:
    """Directed (a->b, b->a) + symmetric Hausdorff distance. Lower better."""
    import numpy as np

    d_ab, _ = _nn_query(points_a, points_b)
    d_ba, _ = _nn_query(points_b, points_a)
    directed_ab = float(np.asarray(d_ab).max())
    directed_ba = float(np.asarray(d_ba).max())
    return {
        "directed_ab": directed_ab,
        "directed_ba": directed_ba,
        "symmetric": max(directed_ab, directed_ba),
    }


def normal_consistency(
    points_a: Any, normals_a: Any, points_b: Any, normals_b: Any
) -> float:
    """Mean absolute dot of nearest-neighbour surface normals (both directions).

    Range 0..1, 1 best. abs() makes it orientation-agnostic (benchmarks §3.4).
    Matching unit normals score 1; perpendicular normals score 0.
    """
    import numpy as np

    na = np.asarray(normals_a, dtype=np.float64)
    nb = np.asarray(normals_b, dtype=np.float64)
    _, idx_ab = _nn_query(points_a, points_b)
    _, idx_ba = _nn_query(points_b, points_a)
    nc_ab = float(np.abs((na * nb[idx_ab]).sum(axis=1)).mean())
    nc_ba = float(np.abs((nb * na[idx_ba]).sum(axis=1)).mean())
    return (nc_ab + nc_ba) / 2.0


# --------------------------------------------------------------------------- #
# Mesh level (trimesh): loading, area-weighted sampling, volumetric IoU.
# --------------------------------------------------------------------------- #
def load_mesh(path: str) -> Any:
    """Load a mesh (STL / 3MF / OBJ / PLY / ...) as a single trimesh.Trimesh."""
    import trimesh

    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    mesh: Any = trimesh.load(path, force="mesh")
    vertices = getattr(mesh, "vertices", None)
    if vertices is None or len(vertices) == 0:
        raise ValueError(f"{path}: no geometry loaded")
    return mesh


def sample_surface(mesh: Any, n_samples: int, seed: int) -> tuple[Any, Any]:
    """Area-weighted surface samples + their face normals (deterministic given seed)."""
    from trimesh.sample import sample_surface as _tss

    try:
        points, face_idx = _tss(mesh, n_samples, seed=seed)
    except TypeError:  # older trimesh without the seed kwarg
        import numpy as np

        # Save/restore the global RNG state so a library caller's stochastic
        # pipeline is not silently reseeded by this determinism shim.
        state = np.random.get_state()
        try:
            np.random.seed(seed)
            points, face_idx = _tss(mesh, n_samples)
        finally:
            np.random.set_state(state)
    normals = mesh.face_normals[face_idx]
    return points, normals


def bbox_diagonal(mesh: Any) -> float:
    """Euclidean length of the mesh axis-aligned bounding-box diagonal."""
    import numpy as np

    lo, hi = mesh.bounds
    return float(np.linalg.norm(np.asarray(hi) - np.asarray(lo)))


def volumetric_iou(mesh_a: Any, mesh_b: Any, voxel_res: int = DEFAULT_VOXEL_RES) -> dict[str, Any]:
    """Occupancy IoU over a shared voxel grid (benchmarks §3.5).

    Both meshes are sampled on ONE common grid spanning the union bounding box, so the
    occupancies are directly comparable. `trimesh.contains` needs watertight meshes; the
    watertight flags are recorded so a caller can distrust the number when they are false.
    """
    import numpy as np

    lo = np.minimum(mesh_a.bounds[0], mesh_b.bounds[0])
    hi = np.maximum(mesh_a.bounds[1], mesh_b.bounds[1])
    extent = hi - lo
    pitch = float(extent.max()) / float(voxel_res)
    if pitch <= 0:
        return {"value": 0.0, "sense": SENSE_HIGHER, "voxel_res": voxel_res, "pitch": 0.0,
                "watertight_a": bool(mesh_a.is_watertight), "watertight_b": bool(mesh_b.is_watertight),
                "method": "contains-grid"}
    centers = _voxel_centers(lo, hi, pitch)
    occ_a = np.asarray(mesh_a.contains(centers), dtype=bool)
    occ_b = np.asarray(mesh_b.contains(centers), dtype=bool)
    union = int((occ_a | occ_b).sum())
    inter = int((occ_a & occ_b).sum())
    iou = (inter / union) if union > 0 else 0.0
    return {
        "value": float(iou), "sense": SENSE_HIGHER, "voxel_res": voxel_res,
        "pitch": pitch, "method": "contains-grid",
        "watertight_a": bool(mesh_a.is_watertight), "watertight_b": bool(mesh_b.is_watertight),
    }


def _voxel_centers(lo: Any, hi: Any, pitch: float) -> Any:
    """Grid of voxel-center points spanning [lo, hi] at the given pitch.

    A flat axis (hi == lo, e.g. a planar sheet) would otherwise make `np.arange`
    empty and collapse the whole grid to zero points; such an axis gets a single
    center so a degenerate-but-real mesh still yields an occupancy sample.
    """
    import numpy as np

    axes = []
    for i in range(3):
        start = lo[i] + pitch / 2.0
        stop = hi[i] + pitch / 2.0
        axis = np.arange(start, stop, pitch)
        axes.append(axis if axis.size else np.array([lo[i] + (hi[i] - lo[i]) / 2.0]))
    grid = np.meshgrid(*axes, indexing="ij")
    return np.stack([g.ravel() for g in grid], axis=1)


# --------------------------------------------------------------------------- #
# Battery: run everything on two mesh files, annotated with senses + convention.
# --------------------------------------------------------------------------- #
def geometry_battery(
    path_a: str,
    path_b: str,
    *,
    n_samples: int = DEFAULT_SAMPLES,
    tau_frac: float = DEFAULT_TAU_FRAC,
    voxel_res: int = DEFAULT_VOXEL_RES,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Compute the full geometry battery between two mesh files (b = target).

    Returns a JSON-ready dict: a `convention` block plus one entry per metric that
    carries its value(s) and `sense`. Never returns a bare number without its sense.
    """
    mesh_a = load_mesh(path_a)
    mesh_b = load_mesh(path_b)
    pts_a, norm_a = sample_surface(mesh_a, n_samples, seed)
    pts_b, norm_b = sample_surface(mesh_b, n_samples, seed)

    tau_abs = tau_frac * bbox_diagonal(mesh_b)
    chamfer = chamfer_distance(pts_a, pts_b)
    fsc = f_score(pts_a, pts_b, tau_abs)
    haus = hausdorff_distance(pts_a, pts_b)
    nc = normal_consistency(pts_a, norm_a, pts_b, norm_b)
    vol = volumetric_iou(mesh_a, mesh_b, voxel_res)

    return {
        "convention": {
            "n_samples": n_samples, "seed": seed, "tau_frac": tau_frac, "tau_abs": tau_abs,
            "voxel_res": voxel_res, "alignment": "none", "units": "model",
            "chamfer_reduction": "mean", "chamfer_direction": "symmetric",
            "normal_consistency_abs_dot": True, "target": "mesh_b",
        },
        "f_score": {**fsc, "sense": SENSE_HIGHER, "primary": True},
        "chamfer": {**chamfer, "sense": SENSE_LOWER},
        "hausdorff": {**haus, "sense": SENSE_LOWER},
        "normal_consistency": {"value": nc, "sense": SENSE_HIGHER, "abs_dot": True},
        "volumetric_iou": vol,
    }


# --------------------------------------------------------------------------- #
# CLI entry point (invoked by `3d metrics geometry` via cli.pyrun).
# --------------------------------------------------------------------------- #
_USAGE = """usage: mesh_metrics.py <mesh_a> <mesh_b> [options]
  Geometry battery: F-score@tau (primary), Chamfer L1/L2, Hausdorff, normal
  consistency, volumetric IoU. mesh_b is the target (tau = 1% of its bbox diagonal).

Options:
  --samples N       area-weighted surface samples per mesh (default 50000)
  --tau-frac F      F-score tau as a fraction of target bbox diagonal (default 0.01)
  --voxel-res R     voxel grid resolution for volumetric IoU (default 48)
  --seed S          sampling seed for determinism (default 0)
  --json            print the full JSON report (senses + convention)
  --no-store        do not append a record to the metrics store"""


def _parse_args(argv: list[str]) -> dict[str, Any]:
    """Parse the geometry-tool argv into a plain options dict (no argparse dep churn)."""
    opts: dict[str, Any] = {
        "positional": [], "samples": DEFAULT_SAMPLES, "tau_frac": DEFAULT_TAU_FRAC,
        "voxel_res": DEFAULT_VOXEL_RES, "seed": DEFAULT_SEED, "json": False, "store": True,
    }
    flag_map = {"--samples": ("samples", int), "--tau-frac": ("tau_frac", float),
                "--voxel-res": ("voxel_res", int), "--seed": ("seed", int)}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            opts["json"] = True
            i += 1
        elif a == "--no-store":
            opts["store"] = False
            i += 1
        elif a in flag_map:
            key, conv = flag_map[a]
            if i + 1 >= len(argv):
                raise ValueError(f"option {a} needs a value")
            opts[key] = conv(argv[i + 1])
            i += 2
        elif a.startswith("-") and a not in ("-h", "--help"):
            raise ValueError(f"unknown option {a}")
        else:
            opts["positional"].append(a)
            i += 1
    return opts


def _print_report(report: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, sort_keys=True, indent=2))
        return
    fsc = report["f_score"]
    print(f"F_SCORE={fsc['f_score']:.4f}")
    print(f"F_SCORE_TAU={fsc['tau']:.6f}")
    print(f"PRECISION={fsc['precision']:.4f}")
    print(f"RECALL={fsc['recall']:.4f}")
    print(f"CHAMFER_L1={report['chamfer']['l1']:.6f}")
    print(f"CHAMFER_L2={report['chamfer']['l2']:.6f}")
    print(f"HAUSDORFF={report['hausdorff']['symmetric']:.6f}")
    print(f"NORMAL_CONSISTENCY={report['normal_consistency']['value']:.4f}")
    vol = report["volumetric_iou"]
    print(f"VOLUMETRIC_IOU={vol['value']:.4f}")
    if not (vol["watertight_a"] and vol["watertight_b"]):
        print("VOLUMETRIC_IOU_WARNING=non-watertight input; occupancy IoU is approximate")


def _store(report: dict[str, Any], opts: dict[str, Any]) -> None:
    try:
        from registries.metrics import append_record

        append_record(
            command="geometry", tool="mesh_metrics",
            inputs={"mesh_a": opts["positional"][0], "mesh_b": opts["positional"][1],
                    "samples": opts["samples"], "seed": opts["seed"]},
            metrics=report,
        )
    except Exception:  # a store failure must never fail the measurement itself.
        pass


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(_USAGE)
        return 0 if argv else 1
    try:
        opts = _parse_args(argv)
    except ValueError as exc:
        print(f"mesh_metrics: {exc}", file=sys.stderr)
        return 2
    if len(opts["positional"]) != 2:
        print(_USAGE, file=sys.stderr)
        return 2
    try:
        report = geometry_battery(
            opts["positional"][0], opts["positional"][1],
            n_samples=opts["samples"], tau_frac=opts["tau_frac"],
            voxel_res=opts["voxel_res"], seed=opts["seed"],
        )
    except FileNotFoundError as exc:
        print(f"mesh_metrics: file not found: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"mesh_metrics: {exc}", file=sys.stderr)
        return 2
    _print_report(report, bool(opts["json"]))
    if opts["store"]:
        _store(report, opts)
    return 0


if __name__ == "__main__":
    # Run standalone via cli.pyrun: sys.path[0] is lib/geometry/, so put lib/ on the
    # path too, otherwise the lazy `registries.metrics` import in _store cannot resolve.
    _LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _LIB not in sys.path:
        sys.path.insert(0, _LIB)
    sys.exit(main(sys.argv[1:]))
