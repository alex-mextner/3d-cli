"""retopo.py — topology-changing ops: remesh, decimate, repair (engines behind one deterministic interface).

WHAT: each function takes (V, F, params) and returns (V, F, details, preserves_vertices).
  remesh    engine `refine` (built in, conforming midpoint splits to a target edge length; every
            original vertex keeps its index and position), engine `pymeshlab` (isotropic explicit
            remeshing; best triangle quality, moves vertices)
  decimate  engine `manifold3d` (tolerance-driven simplify, a core dependency) or `quadric`
            (fast-simplification, target face count)
  repair    engine `builtin` (trimesh: merge, drop degenerate/duplicate faces and unreferenced
            vertices, fix winding, fill holes) or `meshfix` (the public meshfix library adapter)

WHY: a spec chains sculpt ops with remesh/decimate/repair; the keep-out gate needs to know which ops
  preserve vertices (so the zone survives bit-identically) and which do not (the engine then refuses
  unless the spec sets a tolerance).
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from typing import Any

import numpy as np
from numpy.typing import NDArray

from errors import MissingDependency, UsageError

from . import meshmath

Array = NDArray[np.float64]
IntArray = NDArray[np.int64]
Result = tuple[Array, IntArray, dict[str, Any], bool]


def refine(
    V: Array,
    F: IntArray,
    params: Mapping[str, Any],
    zone: NDArray[np.bool_] | None = None,
) -> tuple[Array, IntArray, dict[str, Any], IntArray]:
    """remesh engine `refine`: conforming midpoint splits, optionally only around the vertices of `zone`.

    `target_edge` splits edges longer than 4/3 of it (the mean edge ends near the target); `max_edge`
    splits every edge longer than exactly that and fails when `max_rounds` is not enough to get there.
    Returns (V, F, info, parent) with parent = the input face every output face was cut from."""
    has_target, has_max = "target_edge" in params, "max_edge" in params
    if has_target == has_max:
        raise UsageError(
            "remesh refine: give exactly one of target_edge (mean edge, mm) or max_edge (longest edge, mm)",
            command="mesh deform",
        )
    value = float(params["max_edge" if has_max else "target_edge"])
    if value <= 0:
        raise UsageError(
            f"remesh refine: {'max_edge' if has_max else 'target_edge'} (mm) must be a positive number",
            command="mesh deform",
        )
    limit = value if has_max else value * 4.0 / 3.0
    before = meshmath.mean_edge_length(V, F)
    V2, F2, rounds, parent, zone2 = meshmath.refine_edges(
        V, F, limit, int(params.get("max_rounds", 10)), zone
    )
    tri = V2[F2]
    longest = np.linalg.norm(tri[:, [1, 2, 0]] - tri, axis=2).max(axis=1)
    tested = np.ones(len(F2), dtype=bool) if zone2 is None else zone2[F2].any(axis=1)
    longest_tested = float(longest[tested].max()) if tested.any() else 0.0
    if has_max and longest_tested > limit:
        raise UsageError(
            f"remesh refine: {rounds} round(s) left an edge of {longest_tested:.4f} mm above max_edge {limit} mm",
            command="mesh deform",
            remediation=["Raise max_rounds on the op (default 10)."],
        )
    info: dict[str, Any] = {
        "engine": "refine",
        "rounds": rounds,
        "faces_before": len(F),
        "faces_after": len(F2),
        "mean_edge_before": before,
        "mean_edge_after": meshmath.mean_edge_length(V2, F2),
        "edge_limit_mm": limit,
        "longest_edge_in_zone_mm": round(longest_tested, 6),
    }
    if zone is not None:
        info["zone_vertices"] = int(np.asarray(zone).sum())
        info["zone_faces_after"] = int(tested.sum())
    return V2, F2, info, parent


def remesh(
    V: Array,
    F: IntArray,
    params: Mapping[str, Any],
    zone: NDArray[np.bool_] | None = None,
) -> Result:
    engine = str(params.get("engine", "refine"))
    if engine == "refine":
        V2, F2, info, _ = refine(V, F, params, zone)
        return V2, F2, info, True
    if zone is not None:
        raise UsageError(
            f"remesh: a region is only honoured by engine refine, not {engine!r}",
            command="mesh deform",
            remediation=[
                "Use engine refine, or drop `region` to remesh the whole mesh."
            ],
        )
    target = float(params["target_edge"]) if "target_edge" in params else None
    if target is None or target <= 0:
        raise UsageError(
            "remesh: target_edge (mm) must be a positive number", command="mesh deform"
        )
    before = meshmath.mean_edge_length(V, F)
    if engine in ("pymeshlab", "botsch"):
        V2, F2 = (_pymeshlab_remesh if engine == "pymeshlab" else _botsch_remesh)(
            V, F, params, target
        )
        info = {
            "engine": engine,
            "faces_before": len(F),
            "faces_after": len(F2),
            "mean_edge_before": before,
            "mean_edge_after": meshmath.mean_edge_length(V2, F2),
        }
        return V2, F2, info, False
    raise UsageError(
        f"remesh: engine must be refine, botsch or pymeshlab, got {engine!r}",
        command="mesh deform",
    )


def _botsch_remesh(
    V: Array, F: IntArray, params: Mapping[str, Any], target: float
) -> tuple[Array, IntArray]:
    """Botsch & Kobbelt isotropic remeshing (gpytoolbox, MIT): best triangle quality in the research benchmark."""
    try:
        import gpytoolbox
    except ImportError as err:
        raise MissingDependency(
            "gpytoolbox",
            install="pip install gpytoolbox   (MIT; pulls scikit-image and scs)",
            degrades="remesh engine botsch (the built-in `refine` engine still works)",
            command="mesh deform",
        ) from err
    V2, F2 = gpytoolbox.remesh_botsch(
        V,
        F.astype(np.int32),
        i=int(params.get("iterations", 10)),
        h=target,
        project=True,
        detect_feature_edges=bool(params.get("feature_deg")),
        feature_dihedral_threshold=float(params.get("feature_deg", 45.0)),
    )
    return np.asarray(V2, dtype=np.float64), np.asarray(F2, dtype=np.int64)


def _pymeshlab_remesh(
    V: Array, F: IntArray, params: Mapping[str, Any], target: float
) -> tuple[Array, IntArray]:
    try:
        import pymeshlab
    except ImportError as err:
        raise MissingDependency(
            "pymeshlab",
            install="pip install pymeshlab   (needs libGL on headless Linux)",
            degrades="remesh engine pymeshlab (the built-in `refine` engine still works)",
            command="mesh deform",
        ) from err
    ms = pymeshlab.MeshSet()
    ms.add_mesh(pymeshlab.Mesh(vertex_matrix=V, face_matrix=F.astype(np.int32)))
    ms.apply_filter(
        "meshing_isotropic_explicit_remeshing",
        iterations=int(params.get("iterations", 3)),
        adaptive=False,
        targetlen=pymeshlab.PureValue(target),
        featuredeg=float(params.get("feature_deg", 35.0)),
        checksurfdist=True,
        maxsurfdist=pymeshlab.PureValue(
            float(params.get("max_surface_dist", target / 10.0))
        ),
        splitflag=True,
        collapseflag=True,
        swapflag=True,
        smoothflag=True,
        reprojectflag=True,
    )
    m = ms.current_mesh()
    return np.asarray(m.vertex_matrix(), dtype=np.float64), np.asarray(
        m.face_matrix(), dtype=np.int64
    )


def decimate(V: Array, F: IntArray, params: Mapping[str, Any]) -> Result:
    engine = str(
        params.get("engine", "quadric" if "target_faces" in params else "manifold3d")
    )
    if engine == "manifold3d":
        tol = float(params.get("tolerance", 0.01))
        try:
            import manifold3d
        except ImportError as err:
            raise MissingDependency(
                "manifold3d",
                install="pip install manifold3d",
                degrades="decimate",
                command="mesh deform",
            ) from err
        man = manifold3d.Manifold(
            manifold3d.Mesh(
                vert_properties=V.astype(np.float32), tri_verts=F.astype(np.uint32)
            )
        )
        if str(man.status()) != "Error.NoError" and "NoError" not in str(man.status()):
            raise UsageError(
                f"decimate: manifold3d rejects the mesh ({man.status()}); run a `repair` op first",
                command="mesh deform",
            )
        out = man.simplify(tol).to_mesh()
        V2, F2 = (
            np.asarray(out.vert_properties, dtype=np.float64)[:, :3],
            np.asarray(out.tri_verts, dtype=np.int64),
        )
    elif engine == "quadric":
        try:
            import fast_simplification
        except ImportError as err:
            raise MissingDependency(
                "fast-simplification",
                install="pip install fast-simplification",
                degrades="decimate engine quadric (engine manifold3d still works)",
                command="mesh deform",
            ) from err
        if "target_faces" not in params:
            raise UsageError(
                "decimate: engine quadric needs target_faces", command="mesh deform"
            )
        reduction = max(
            0.0, min(0.999, 1.0 - float(params["target_faces"]) / max(len(F), 1))
        )
        V2, F2 = fast_simplification.simplify(
            V.astype(np.float32),
            F.astype(np.int32),
            target_reduction=reduction,
            agg=int(params.get("aggressiveness", 5)),
        )
        V2, F2 = np.asarray(V2, dtype=np.float64), np.asarray(F2, dtype=np.int64)
    else:
        raise UsageError(
            f"decimate: engine must be manifold3d or quadric, got {engine!r}",
            command="mesh deform",
        )
    return (
        V2,
        F2,
        {"engine": engine, "faces_before": len(F), "faces_after": len(F2)},
        False,
    )


def repair(V: Array, F: IntArray, params: Mapping[str, Any]) -> Result:
    engine = str(params.get("engine", "builtin"))
    if engine == "builtin":
        import trimesh

        mesh = trimesh.Trimesh(V, F, process=False)
        mesh.merge_vertices()
        mesh.update_faces(mesh.nondegenerate_faces())
        mesh.update_faces(mesh.unique_faces())
        mesh.remove_unreferenced_vertices()
        trimesh.repair.fix_winding(mesh)
        trimesh.repair.fix_inversion(mesh)
        trimesh.repair.fill_holes(mesh)
        info = {
            "engine": "builtin",
            "faces_before": len(F),
            "faces_after": len(mesh.faces),
            "watertight_after": bool(mesh.is_watertight),
        }
        return (
            np.asarray(mesh.vertices, dtype=np.float64),
            np.asarray(mesh.faces, dtype=np.int64),
            info,
            False,
        )
    if engine == "meshfix":
        try:
            mesh_repair = importlib.import_module(
                "mesh_repair"
            )  # the meshfix adapter of `3d mesh --repair`
        except ImportError as err:
            raise MissingDependency(
                "lib/mesh_repair.py (the meshfix adapter of `3d mesh --repair`)",
                install="merge the `3d mesh --repair` PR (#438) or use repair engine `builtin`",
                degrades="repair engine meshfix",
                command="mesh deform",
            ) from err
        V2, F2, report = mesh_repair.repair_mesh(V, F)
        return (
            np.asarray(V2, dtype=np.float64),
            np.asarray(F2, dtype=np.int64),
            {"engine": "meshfix", **{k: report[k] for k in sorted(report)}},
            False,
        )
    raise UsageError(
        f"repair: engine must be builtin or meshfix, got {engine!r}",
        command="mesh deform",
    )
