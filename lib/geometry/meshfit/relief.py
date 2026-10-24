"""relief.py — procedural surface relief (fur / strands) as real displacement, with an overhang-safe mode.

WHAT: displaces vertices along their smoothed normal by a seeded strand field (ridged noise stretched
  along a flow direction, wavy by domain warp), amplitude per region, optional rounder "clump" layers,
  masked by a region weight. Outward only (displacement >= 0), so walls never get thinner.
  `overhang_safe` then caps the displacement wherever it would turn a face past the print limit.

WHY: a fur texture that is geometry (not a bump map) has to print without supports. Strands run
  along the flow (default: down), so across-flow gradients only tilt a surface toward the horizontal
  and never create an overhang; the along-flow gradient is small by construction (long strands) and the
  overhang-safe pass makes it a guarantee: no face that was printable may end up steeper than the limit.
  Ported from the Chekhov fur script (pure function of seed + parameters; generic, no model constants).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
from numpy.typing import NDArray

from errors import GateFailure

from . import meshmath
from .noise import clump_field, smoothstep, strand_field
from .regions import RegionContext

Array = NDArray[np.float64]
IntArray = NDArray[np.int64]

MAX_ROUNDS = 5000


def fur(
    ctx: RegionContext,
    params: Mapping[str, Any],
    w: Array,
    seed: int,
    scale: Array | None = None,
) -> tuple[Array, dict[str, Any]]:
    """Fur displacement. `scale` (per vertex, e.g. the keep-out fade) multiplies the relief BEFORE the overhang limiter,
    so the limit holds for the displacement that is actually applied."""
    seed = int(params.get("seed", seed))
    flow = np.asarray(params.get("flow", [0.0, 0.0, 1.0]), dtype=np.float64)
    amplitude = float(params.get("amplitude", 0.35))
    pitch = float(params.get("pitch", 1.9))
    length = float(params.get("length", 11.0))
    fine = params.get("fine", {})
    warp = params.get("warp", {})
    strand_kw = {
        "pitch": pitch,
        "length": length,
        "fine_pitch": float(fine.get("pitch", 1.05)),
        "fine_weight": float(fine.get("weight", 0.22)),
        "warp_scale": float(warp.get("scale", 14.0)),
        "warp_strength": float(warp.get("strength", 1.6)),
        "sharpness": float(params.get("sharpness", 1.7)),
    }
    V = ctx.V
    n = ctx.normals(int(params.get("normal_smoothing", 6)))
    active = np.nonzero(w > 0.0)[0]
    disp = np.zeros(len(V))
    if len(active):
        P = V[active]
        amp = np.full(len(active), amplitude)
        for item in params.get("amplitude_regions", []):
            r = ctx.weight(item["region"])[active]
            amp = amp * (1.0 - r) + float(item["amplitude"]) * r
        d = strand_field(P, seed=seed, flow=flow, **strand_kw) * amp
        for clump in params.get("clumps", []):
            r = ctx.weight(clump["region"])[active]
            nz = n[active, 2]
            lo, hi = clump.get("nz", [-0.15, 0.05])
            cw = r * smoothstep(nz, float(lo), float(hi))
            c = clump_field(
                P,
                seed=seed,
                flow=flow,
                pitch=float(clump.get("pitch", 2.6)),
                length=float(clump.get("length", 5.0)),
                strand_kwargs=strand_kw,
            )
            d = d * (1.0 - cw) + c * float(clump.get("amplitude", amplitude)) * cw
        disp[active] = np.maximum(d, 0.0) * w[active]
    if scale is not None:
        disp *= scale
    details: dict[str, Any] = {
        "seed": seed,
        "amplitude_mm": amplitude,
        "max_relief_mm": float(disp.max()) if len(disp) else 0.0,
    }
    safe = params.get("overhang_safe")
    if safe:
        cfg = safe if isinstance(safe, Mapping) else {}
        disp, report = limit_overhang(
            V,
            ctx.F,
            n,
            disp,
            float(cfg.get("max_angle_deg", 45.0)),
            float(cfg.get("bed_z", 0.3)),
        )
        details["overhang_safe"] = report
    return V + n * disp[:, None], details


def overhang_threshold(max_angle_deg: float) -> float:
    """A face overhangs when its normal z is below this (the repo convention: nz < -sin(angle from vertical))."""
    return -float(np.sin(np.radians(max_angle_deg)))


def limit_overhang(
    V: Array, F: IntArray, n: Array, disp: Array, max_angle_deg: float, bed_z: float
) -> tuple[Array, dict[str, Any]]:
    """Shrink `disp` until no face that was printable becomes steeper than `max_angle_deg` (undercut-free).

    A face is violating when its new normal z is below the limit AND below its original normal z
    (faces that already overhang may not get worse). Violating faces halve, then zero, the displacement
    of their vertices; zero displacement restores the original normal, so the loop always terminates.
    """
    thr = overhang_threshold(max_angle_deg)
    nz0 = meshmath.face_normals(V, F)[:, 2]
    on_bed = V[F][:, :, 2].max(axis=1) < bed_z
    disp = disp.copy()
    reduced = np.zeros(len(V), dtype=bool)
    rounds = 0
    first_bad = 0
    for rounds in range(MAX_ROUNDS + 1):
        nz1 = meshmath.face_normals(V + n * disp[:, None], F)[:, 2]
        bad = (nz1 < thr) & (nz1 < nz0 - 1e-9) & ~on_bed
        if rounds == 0:
            first_bad = int(bad.sum())
        if not bad.any():
            break
        verts = np.unique(F[bad])
        reduced[verts] = True
        disp[verts] = disp[verts] * 0.5 if rounds < 6 else 0.0
    else:
        raise GateFailure(
            "fur: overhang-safe limiter did not converge",
            command="mesh deform",
            remediation=["Lower the amplitude or raise max_angle_deg."],
        )
    return disp, {
        "max_angle_deg": max_angle_deg,
        "rounds": rounds,
        "violating_faces_first_pass": first_bad,
        "vertices_reduced": int(reduced.sum()),
    }
