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


def remesh(V: Array, F: IntArray, params: Mapping[str, Any]) -> Result:
    engine = str(params.get("engine", "refine"))
    target = float(params["target_edge"]) if "target_edge" in params else None
    if target is None or target <= 0:
        raise UsageError(
            "remesh: target_edge (mm) must be a positive number", command="mesh deform"
        )
    before = meshmath.mean_edge_length(V, F)
    if engine == "refine":
        V2, F2, rounds = meshmath.refine_long_edges(
            V, F, target * 4.0 / 3.0, int(params.get("max_rounds", 10))
        )
        info = {
            "engine": "refine",
            "rounds": rounds,
            "faces_before": len(F),
            "faces_after": len(F2),
            "mean_edge_before": before,
            "mean_edge_after": meshmath.mean_edge_length(V2, F2),
        }
        return V2, F2, info, True
    if engine == "pymeshlab":
        V2, F2 = _pymeshlab_remesh(V, F, params, target)
        info = {
            "engine": "pymeshlab",
            "faces_before": len(F),
            "faces_after": len(F2),
            "mean_edge_before": before,
            "mean_edge_after": meshmath.mean_edge_length(V2, F2),
        }
        return V2, F2, info, False
    raise UsageError(
        f"remesh: engine must be refine or pymeshlab, got {engine!r}",
        command="mesh deform",
    )


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
    engine = str(params.get("engine", "manifold3d"))
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
