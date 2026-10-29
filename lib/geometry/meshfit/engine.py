"""engine.py — runs a validated spec on a mesh: ordered ops, keep-out enforcement, hard gate, report.

WHAT: `run_spec(V, F, spec)` applies each op to the current vertices, multiplies every op's displacement
  by (1 - keep-out weight) and pins fully-protected vertices to their exact bytes, then re-checks the
  keep-out contract against the ORIGINAL mesh at the end (`keepout_gate`): every protected original
  vertex must still be a vertex of the result, bit-identical (max displacement 0.0 mm), unless the spec
  grants an explicit `keepout_tolerance_mm`, in which case the surface distance is gated instead.
  The report records per-op moved counts / max displacement, watertight + volume before/after, the
  self-intersection count and timings.

WHY: the point of the tool is that a fit can never touch a functional zone (phone contact corridor,
  eyes, cable scoop) — a property that is verified from the final mesh, not trusted from the ops.
  Topology ops that cannot keep vertices (pymeshlab remesh, decimate) are refused when a keep-out zone
  exists and no tolerance is set, because the gate could not hold.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np
from numpy.typing import NDArray

from errors import GateFailure, UsageError

from . import brushes, deform, fit, relief, retopo
from .meshio import describe, mesh_sha256
from .regions import LOCK_WEIGHT, RegionContext
from .selfint import count_self_intersections
from .spec import TOPOLOGY_OPS

Array = NDArray[np.float64]
IntArray = NDArray[np.int64]
BoolArray = NDArray[np.bool_]
BrushFn = Callable[[RegionContext, Mapping[str, Any], Array], Array]

BRUSHES: dict[str, BrushFn] = {
    "smooth": brushes.smooth,
    "inflate": brushes.inflate,
    "draw": brushes.draw,
    "displace": brushes.displace,
    "pinch": brushes.pinch,
    "crease": brushes.crease,
    "scale_local": brushes.scale_local,
    "symmetrize": brushes.symmetrize,
}
# Points per closest-point query of the tolerance gate: bounds the memory of one query, never the count measured.
DISTANCE_CHUNK = 20000
# Ops that apply the keep-out fade themselves (the displacement they return is already faded and locked rows are
# zero): fur limits overhangs on the faded field, fit_silhouette scores the faded pose. The engine must not fade again.
SELF_FADING_OPS = frozenset({"fur", "fit_silhouette"})
TOPOLOGY: dict[str, Callable[[Array, IntArray, Mapping[str, Any]], retopo.Result]] = {
    "remesh": retopo.remesh,
    "decimate": retopo.decimate,
    "repair": retopo.repair,
}


class Pipeline:
    """Mutable run state: current mesh, persistent keep-out lock, baseline for the final gate."""

    def __init__(
        self,
        V: Array,
        F: IntArray,
        spec: Mapping[str, Any],
        carry_faces: bool = False,
    ) -> None:
        self.spec = spec
        self.carry_faces = carry_faces
        self.V0, self.F0 = V.copy(), F.copy()
        self.V, self.F = V.copy(), F.copy()
        self.face_parent: IntArray | None = np.arange(len(F), dtype=np.int64)
        self.records: list[dict[str, Any]] = []
        base = RegionContext(self.V, self.F, spec["regions"], spec["base_dir"])
        self.keepout_weight0 = self._keepout_weight(base)
        self.lock = self.keepout_weight0 >= LOCK_WEIGHT
        self.baseline_locked = self.V0[self.lock].copy()

    def _keepout_weight(self, ctx: RegionContext) -> Array:
        return (
            ctx.weight(list(self.spec["keepout"]))
            if self.spec["keepout"]
            else np.zeros(len(self.V))
        )

    def apply(self, index: int, op: Mapping[str, Any]) -> None:
        name = op["op"]
        started = time.perf_counter()
        if name in TOPOLOGY_OPS:
            record = self._apply_topology(name, op)
        else:
            record = self._apply_vertex_op(name, op)
        record.update(
            {
                "index": index,
                "op": name,
                "region": op.get("region"),
                "seconds": round(time.perf_counter() - started, 4),
            }
        )
        self.records.append(record)

    def _keepout_scale(self, wk: Array) -> Array:
        """Per-vertex factor a displacement is multiplied by: the keep-out fade, zero on locked vertices."""
        scale = 1.0 - wk
        scale[self.lock] = 0.0
        return scale

    def _apply_vertex_op(self, name: str, op: Mapping[str, Any]) -> dict[str, Any]:
        ctx = RegionContext(self.V, self.F, self.spec["regions"], self.spec["base_dir"])
        wk = self._keepout_weight(ctx)
        self.lock |= wk >= LOCK_WEIGHT
        w = ctx.weight(op.get("region"))
        details: dict[str, Any] = {}
        if name in BRUSHES:
            V_new = BRUSHES[name](ctx, op, w)
        elif name == "fur":
            V_new, details = relief.fur(
                ctx, op, w, int(self.spec["seed"]), self._keepout_scale(wk)
            )
        elif name == "fit_silhouette":
            V_new, details = fit.fit_silhouette(ctx, op, self.lock, wk)
        else:
            V_new, details = self._grab(ctx, op, w)
        delta = V_new - self.V
        if name not in SELF_FADING_OPS:
            delta *= (1.0 - wk)[:, None]
        delta[self.lock] = 0.0
        moved = np.linalg.norm(delta, axis=1)
        self.V = self.V + delta
        return {
            "vertices_moved": int((moved > 0.0).sum()),
            "max_displacement_mm": round(float(moved.max()) if len(moved) else 0.0, 6),
            "mean_displacement_mm": round(
                float(moved[moved > 0.0].mean()) if (moved > 0.0).any() else 0.0, 6
            ),
            "region_weight_vertices": int((w > 0.0).sum()),
            **details,
        }

    def _grab(
        self, ctx: RegionContext, op: Mapping[str, Any], w: Array
    ) -> tuple[Array, dict[str, Any]]:
        solver = str(op.get("solver", "arap"))
        handle_min = float(op.get("handle_min", LOCK_WEIGHT))
        V = ctx.V
        if (
            solver == "none"
        ):  # brush-style grab: translate/rotate/scale each handle region with its falloff weight
            out = V.copy()
            for h in op["handles"]:
                hw = ctx.weight(h["region"])
                idx = np.nonzero(hw > 0.0)[0]
                if len(idx):
                    out[idx] += hw[idx, None] * (
                        deform.handle_targets(V, idx, h) - V[idx]
                    )
            return out, {"solver": "none"}
        handles: dict[int, Array] = {}
        for h in op["handles"]:
            idx = np.nonzero((ctx.weight(h["region"]) >= handle_min) & ~self.lock)[0]
            handles.update(
                zip(idx.tolist(), deform.handle_targets(V, idx, h), strict=True)
            )
        if not handles:
            raise UsageError(
                "grab: no handle vertex has region weight >= handle_min outside the keep-out zones",
                command="mesh deform",
            )
        h_idx = np.array(sorted(handles), dtype=np.int64)
        targets = np.stack([handles[i] for i in h_idx.tolist()])
        fixed = self.lock.copy()
        for ref in op.get("anchors", []) or []:
            fixed |= ctx.weight(ref) >= handle_min
        fixed[h_idx] = False
        zone: BoolArray | None = None
        if "influence" in op:
            from scipy.spatial import cKDTree

            dist = cKDTree(V[h_idx]).query(V, workers=1)[0]
            zone = dist <= float(op["influence"])
        out, stats = deform.solve_handles(
            V, ctx.F, h_idx, targets, fixed, zone, solver, int(op.get("iterations", 10))
        )
        stats["handle_vertices"] = int(len(h_idx))
        return out, stats

    def _apply_topology(self, name: str, op: Mapping[str, Any]) -> dict[str, Any]:
        tolerance = float(self.spec["keepout_tolerance_mm"])
        refining = name == "remesh" and str(op.get("engine", "refine")) == "refine"
        if self.carry_faces and not refining:
            raise UsageError(
                f"{name} (engine {op.get('engine', 'default')}) cannot carry per-face labels: it does not map output faces to input faces",
                command="mesh deform",
                remediation=[
                    "Use remesh engine `refine` (children inherit the parent face), or run without --labels."
                ],
            )
        zone = None
        if op.get("region") is not None:
            if not refining:
                raise UsageError(
                    f"{name}: `region` is only honoured by remesh engine refine",
                    command="mesh deform",
                    remediation=["Drop `region` to run the op on the whole mesh."],
                )
            ctx = RegionContext(
                self.V, self.F, self.spec["regions"], self.spec["base_dir"]
            )
            zone = ctx.weight(op.get("region")) > 0.0
        parent = None
        if refining:
            V2, F2, info, parent = retopo.refine(self.V, self.F, op, zone)
            preserves = True
        else:
            V2, F2, info, preserves = TOPOLOGY[name](self.V, self.F, op)
        if not preserves and len(self.baseline_locked) and tolerance <= 0.0:
            raise GateFailure(
                f"{name} (engine {info.get('engine')}) moves vertices, so the keep-out zone cannot stay bit-identical",
                command="mesh deform",
                remediation=[
                    "Use remesh engine `refine` (keeps every original vertex), or run this op before the keep-out is defined",
                    "or set `keepout_tolerance_mm: <mm>` in the spec to gate surface distance instead of exact bytes.",
                ],
            )
        n_before = len(self.V)
        if preserves:
            self.lock = np.concatenate(
                [self.lock, np.zeros(len(V2) - n_before, dtype=bool)]
            )
        else:
            self.lock = _locate_rows(V2, self.baseline_locked)
        self.V, self.F = V2, F2
        self.face_parent = (
            None
            if parent is None or self.face_parent is None
            else self.face_parent[parent]
        )
        return {"vertices_before": n_before, "vertices_after": int(len(V2)), **info}

    def keepout_gate(self) -> dict[str, Any]:
        tol = float(self.spec["keepout_tolerance_mm"])
        locked = self.baseline_locked
        fade = int(
            ((self.keepout_weight0 > 0.0) & (self.keepout_weight0 < LOCK_WEIGHT)).sum()
        )
        result: dict[str, Any] = {
            "locked_vertices": int(len(locked)),
            "fade_vertices": fade,
            "tolerance_mm": tol,
        }
        if len(locked) == 0:
            result.update(
                max_displacement_mm=0.0,
                exact_vertices_preserved=0,
                gate="PASS",
                note="no keep-out zone defined"
                if not self.spec["keepout"]
                else "keep-out zone selects no vertex at weight 1",
            )
            return result
        survived = _locate_rows(
            locked, self.V
        )  # mask over locked originals that still exist, bit-for-bit, in the final mesh
        found = int(survived.sum())
        missing = len(locked) - found
        max_disp = 0.0
        if missing:
            max_disp = _surface_distance(self.V, self.F, locked[~survived])
        result.update(
            max_displacement_mm=round(max_disp, 9),
            exact_vertices_preserved=found,
            missing_vertices=int(missing),
            gate="PASS" if max_disp <= tol else "FAIL",
        )
        return result


def _row_key(P: Array) -> Any:
    return (
        np.ascontiguousarray(P).view(np.dtype((np.void, P.dtype.itemsize * 3))).ravel()
    )


def _locate_rows(V: Array, points: Array) -> BoolArray:
    """Mask over rows of V that equal (bit-for-bit) some row of `points`."""
    if len(points) == 0:
        return np.zeros(len(V), dtype=bool)
    return np.asarray(np.isin(_row_key(V), _row_key(points)), dtype=bool)


def _surface_distance(V: Array, F: IntArray, points: Array) -> float:
    """Largest distance from any of `points` to the surface (V, F); every point is measured, in chunks."""
    import trimesh

    mesh = trimesh.Trimesh(V, F, process=False)
    worst = 0.0
    for start in range(0, len(points), DISTANCE_CHUNK):
        _, dist, _ = trimesh.proximity.closest_point(
            mesh, points[start : start + DISTANCE_CHUNK]
        )
        worst = max(worst, float(dist.max()))
    return worst


def spec_sha256(spec: Mapping[str, Any]) -> str:
    keep = {k: v for k, v in spec.items() if k != "base_dir"}
    return hashlib.sha256(
        json.dumps(keep, sort_keys=True, default=str).encode()
    ).hexdigest()


def run_spec(
    V: Array, F: IntArray, spec: Mapping[str, Any], *, self_intersections: bool = True
) -> tuple[Array, IntArray, dict[str, Any]]:
    """Apply `spec` to (V, F). Returns the new mesh and the report; report["status"] is PASS or FAIL."""
    V2, F2, report, _ = run_spec_with_parents(
        V, F, spec, self_intersections=self_intersections
    )
    return V2, F2, report


def run_spec_with_parents(
    V: Array,
    F: IntArray,
    spec: Mapping[str, Any],
    *,
    self_intersections: bool = True,
    carry_faces: bool = False,
) -> tuple[Array, IntArray, dict[str, Any], IntArray | None]:
    """`run_spec` plus, for every output face, the index of the input face it was cut from.

    Per-face data (labels) follows the faces through vertex ops and remesh engine `refine`; any other
    topology op has no such mapping. With `carry_faces` such an op is refused up front."""
    started = time.perf_counter()
    pipe = Pipeline(V, F, spec, carry_faces=carry_faces)
    for index, op in enumerate(spec["ops"]):
        pipe.apply(index, op)
    gate = pipe.keepout_gate()
    before, after = describe(pipe.V0, pipe.F0), describe(pipe.V, pipe.F)
    report: dict[str, Any] = {
        "tool": "3d mesh deform",
        "status": "PASS" if gate["gate"] == "PASS" else "FAIL",
        "seed": int(spec["seed"]),
        "spec_sha256": spec_sha256(spec),
        "input": before,
        "output": after,
        "watertight_before": before["watertight"],
        "watertight_after": after["watertight"],
        "volume_before_mm3": before["volume_mm3"],
        "volume_after_mm3": after["volume_mm3"],
        "ops": pipe.records,
        "keepout": gate,
        "mesh_sha256": mesh_sha256(pipe.V, pipe.F),
    }
    if self_intersections:
        report["self_intersections"] = {
            "before": count_self_intersections(pipe.V0, pipe.F0),
            "after": count_self_intersections(pipe.V, pipe.F),
        }
    report["timing_s"] = round(time.perf_counter() - started, 4)
    # None after a topology op that does not map output faces to input faces (carry_faces refuses those)
    return pipe.V, pipe.F, report, pipe.face_parent
