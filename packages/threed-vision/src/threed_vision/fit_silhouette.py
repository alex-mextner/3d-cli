"""Silhouette fitting of the loft template to calibrated views: `fit_object` (soft-rasteriser Adam, torch) and `fit_cma`
(gradient-free CMA-ES on the hard rasteriser, no torch). torch / cma are imported lazily; importing this module is cheap.

views = [{"mask": HxW bool (full-res), "K": 3x3, "R": 3x3, "t": 3}]  with (R, t) world -> camera, OpenCV convention, mm.
Both return the fit dict of `loft.make_fit` plus `views[{iou}]`, `confidence`, `timing_s`, `tries`, `device`.
"""

from __future__ import annotations

import math
import time
from typing import Any

import numpy as np

from .errors import BadInput, MissingDependency
from .fit_init import (
    CROP_S,
    FLAG_BELOW_IOU,
    camera_center,
    check_fixed_dims,
    crop_all,
    mesh_world,
    ranked_candidates,
    validate_views,
)
from .loft import (
    ASPECT_MAX,
    ASPECT_MIN,
    RING_M,
    apply_fixed_dims,
    build_faces,
    make_fit,
    object_view_angles,
)
from .render import hull_mask, view_iou

__all__ = [
    "assemble",
    "cma_refine",
    "confidence",
    "prepare_inputs",
    "fit_cma",
    "fit_object",
    "refine_candidates",
    "resolve_torch_device",
]

GPU_NEED_MB = (
    700  # soft fit peak is ~350 MB (measured); leave headroom on a shared card
)
OPT_RING = RING_M


def resolve_torch_device(preference: str = "auto") -> str:
    """'cpu' | 'cuda' (strict: MissingDependency when torch sees no GPU) | 'auto' (GPU when torch sees one with enough free VRAM, else CPU)."""
    if preference not in ("auto", "cuda", "cpu"):
        raise BadInput(
            f"unknown device {preference!r}", remediation="use auto, cuda or cpu"
        )
    if preference == "cpu":
        return "cpu"
    torch = _torch()
    if not torch.cuda.is_available():
        if preference == "cuda":
            raise MissingDependency(
                "a CUDA GPU visible to torch",
                remediation="use --device auto|cpu, or install a CUDA build of torch",
            )
        return "cpu"
    if preference == "cuda":
        return "cuda"
    from .device import resolve_device

    return resolve_device("auto", GPU_NEED_MB)


def _torch() -> Any:
    try:
        import torch
    except ImportError as exc:
        raise MissingDependency(
            "torch (the soft-silhouette refiner)",
            remediation="python -m pip install 'threed-vision[fit]' (CPU or cu128 build of torch), or accept the closed-form result",
        ) from exc
    return torch


def confidence(
    train_ious: list[float], heldout_ious: list[float] | None, min_iou: float
) -> dict[str, Any]:
    """Confidence block. status: `low` (a training view below min_iou, or a held-out view below 0.75), `single_view` (reproduces its
    one photo; 3-D shape is NOT verified), `ok` (>= 2 views reproduced and any held-out view predicted)."""
    n = len(train_ious)
    tmin = float(min(train_ious)) if train_ious else 0.0
    held = float(min(heldout_ious)) if heldout_ious else None
    if tmin < min_iou or (held is not None and held < FLAG_BELOW_IOU):
        status = "low"
    elif n == 1 and held is None:
        status = "single_view"
    else:
        status = "ok"
    return {"train_min_iou": tmin, "heldout_iou": held, "n_views": n, "status": status}


def assemble(
    core: dict[str, Any],
    views: list[dict[str, Any]],
    *,
    template: str,
    K: int,
    kind: str | None,
    fixed: dict[str, float],
    method: str,
    ring: int,
    min_iou: float,
    timing: dict[str, float],
    tries: int,
    device: str | None,
    heldout_views: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the public fit dict from a solved core {L, w, c, aspect, R0, t0} and score it with the independent hard rasteriser."""
    fit = make_fit(
        template=template,
        K=K,
        L=core["L"],
        c=core["c"],
        w=core["w"],
        aspect=core["aspect"],
        R0=core["R0"],
        t_mm=core["t0"],
        method=method,
        kind=kind,
        fixed_dims=fixed,
        ring=ring,
    )
    from .render import mesh_from_fit

    V, F = mesh_from_fit(fit)
    entries, ious = [], []
    for v in views:
        iou = view_iou(v["mask"], V, F, v["K"], v["R"], v["t"])
        ious.append(iou)
        entries.append(
            {
                "name": v.get("name"),
                "iou": iou,
                **object_view_angles(core["R0"], core["t0"], camera_center(v)),
            }
        )
    held = [
        view_iou(v["mask"], V, F, v["K"], v["R"], v["t"]) for v in heldout_views or []
    ]
    fit["views"] = entries
    fit["heldout_views"] = [
        {"name": v.get("name"), "iou": i}
        for v, i in zip(heldout_views or [], held, strict=True)
    ]
    fit["confidence"] = confidence(ious, held or None, min_iou)
    timing = dict(timing)
    timing["total"] = sum(v for k, v in timing.items() if k != "total")
    fit["timing_s"] = timing
    fit["tries"] = tries
    fit["device"] = device
    return fit


def prepare_inputs(
    views: Any, heldout_views: Any, fixed_dims: dict[str, float] | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None, dict[str, float]]:
    vs = validate_views(views)
    hv = validate_views(heldout_views) if heldout_views else None
    return vs, hv, check_fixed_dims(fixed_dims)


def _seen_key(init: dict[str, Any]) -> tuple[Any, ...]:
    return (
        round(float(init["aspect"]), 2),
        tuple(np.round(np.asarray(init["R0"]).ravel(), 1)),  # the whole pose: roll differs in rows 2-3 when the axis is along world X
        bool(init["c"][0] >= 0),
    )


def _core_ious(
    core: dict[str, Any], views: list[dict[str, Any]], p: float, K: int, ring: int
) -> list[float]:
    from .loft import loft_vertices

    V = (
        loft_vertices(core["L"], core["c"], core["w"], core["aspect"], p, ring)
        @ core["R0"].T
        + core["t0"]
    )
    F = build_faces(K, ring)
    return [view_iou(v["mask"], V, F, v["K"], v["R"], v["t"]) for v in views]


def _refine_on(
    device: str,
    views: list[dict[str, Any]],
    cands: list[dict[str, Any]],
    *,
    name: str,
    p: float,
    K: int,
    planar: bool,
    fixed: dict[str, float],
    min_iou: float,
    max_tries: int,
    seed: int,
) -> tuple[dict[str, Any], int, float]:
    torch = _torch()
    from .soft_raster import LoftFit

    torch.manual_seed(seed)
    t0 = time.perf_counter()
    fit = LoftFit(
        views,
        template=name,
        p=p,
        K=K,
        device=device,
        M=OPT_RING,
        planar=planar,
        fixed_dims=fixed,
    )
    best: tuple[float, dict[str, Any]] | None = None
    seen: set[tuple[Any, ...]] = set()
    tries = 0
    for cand in cands:
        key = _seen_key(cand["init"])
        if key in seen:
            continue
        seen.add(key)
        tries += 1
        fit.set_init(cand["init"])
        fit.run()
        core = fit.result()
        ious = _core_ious(core, views, p, K, OPT_RING)
        score = float(np.mean(ious))
        if best is None or score > best[0]:
            best = (score, core)
        if min(ious) >= min_iou or tries >= max_tries:
            break
    if best is None:
        raise BadInput("no pose candidate to refine")
    return best[1], tries, time.perf_counter() - t0


def refine_candidates(
    views: list[dict[str, Any]],
    cands: list[dict[str, Any]],
    info: dict[str, Any],
    *,
    K: int,
    device: str = "auto",
    planar: bool = False,
    fixed: dict[str, float] | None = None,
    min_iou: float = 0.8,
    max_tries: int = 3,
    seed: int = 0,
    heldout_views: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Soft-silhouette refinement of the best ranked candidates (see `ranked_candidates`); falls back GPU -> CPU for device='auto'."""
    fixed = fixed or {}
    if max_tries < 1:
        raise BadInput(f"max_tries must be >= 1, got {max_tries}", remediation="use fit_auto(iou_accept=0) / --iou-accept 0 for closed form only")
    dev = resolve_torch_device(device)
    fallback = None
    kw = {
        "name": info["template"],
        "p": info["p"],
        "K": K,
        "planar": planar,
        "fixed": fixed,
        "min_iou": min_iou,
        "max_tries": max_tries,
        "seed": seed,
    }
    try:
        core, tries, dt = _refine_on(dev, views, cands, **kw)
    except RuntimeError as exc:
        if dev != "cuda" or device != "auto":
            raise
        fallback = f"cuda failed ({str(exc).splitlines()[0][:120]}); refit on cpu"
        _torch().cuda.empty_cache()
        dev = "cpu"
        core, tries, dt = _refine_on(dev, views, cands, **kw)
    timing = {**info["timing_s"], "refine": dt}
    out = assemble(
        core,
        views,
        template=info["template"],
        K=K,
        kind=info["kind"],
        fixed=fixed,
        method="soft",
        ring=OPT_RING,
        min_iou=min_iou,
        timing=timing,
        tries=tries,
        device=dev,
        heldout_views=heldout_views,
    )
    if fallback:
        out["device_fallback"] = fallback
    return out


def fit_object(
    views: list[dict[str, Any]],
    template: str | float | None,
    *,
    K: int = 8,
    device: str = "auto",
    known_depth: float | None = None,
    planar: bool = False,
    fixed_dims: dict[str, float] | None = None,
    min_iou: float = 0.8,
    max_tries: int = 3,
    seed: int = 0,
    heldout_views: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Closed-form start -> ranked pose candidates -> soft-silhouette refinement (torch).

    template: 'lathe' | 'flat' | 'box' (p = 2 / 4 / 8) or None/'auto' to pick by closed-form kind. A single view needs `known_depth`
    (mm along the camera z); `planar=True` additionally constrains the object to a plane parallel to the image (top-down photo).
    `fixed_dims` {length_mm, max_width_mm (full width), thickness_mm (full)} are HARD constraints. Deterministic for a given input
    on CPU; `seed` seeds torch. Raises BadInput / MissingDependency (no torch); never returns NaN.
    """
    vs, hv, fixed = prepare_inputs(views, heldout_views, fixed_dims)
    cands, info = ranked_candidates(
        vs,
        template,
        K=K,
        known_depth=known_depth,
        planar=planar,
        fixed_dims=fixed,
        ring=OPT_RING,
    )
    return refine_candidates(
        vs,
        cands,
        info,
        K=K,
        device=device,
        planar=planar,
        fixed=fixed,
        min_iou=min_iou,
        max_tries=max_tries,
        seed=seed,
        heldout_views=hv,
    )


# ----------------------------------------------------------------------------- CMA-ES (gradient free)
def _softplus_inv_np(w: np.ndarray) -> np.ndarray:
    t = np.maximum(np.asarray(w, np.float64), 0.05)
    return t + np.log(-np.expm1(-t))


def _decode(
    x: np.ndarray,
    K: int,
    template: str,
    fixed: dict[str, float],
    planar_n: np.ndarray | None,
    init: dict[str, Any],
) -> dict[str, Any]:
    rot, tr = x[0:3], x[3:6]
    if planar_n is not None:
        rot = float(rot @ planar_n) * planar_n
        tr = tr - float(tr @ planar_n) * planar_n
    from .loft import rodrigues

    w = np.logaddexp(0.0, x[7 + K : 7 + 2 * K])
    a = (
        1.0
        if template == "lathe"
        else float(np.clip(math.exp(x[-1]), ASPECT_MIN, ASPECT_MAX))
    )
    L, w, a = apply_fixed_dims(math.exp(float(np.clip(x[6], -20, 20))), w, a, fixed)
    return {
        "L": L,
        "w": w,
        "c": x[7 : 7 + K],
        "aspect": a,
        "R0": rodrigues(rot) @ init["R0"],
        "t0": init["t0"] + tr,
    }


def cma_refine(
    views: list[dict[str, Any]],
    cands: list[dict[str, Any]],
    info: dict[str, Any],
    *,
    K: int,
    planar: bool = False,
    fixed: dict[str, float] | None = None,
    min_iou: float = 0.8,
    seed: int = 0,
    max_evals: int = 2500,
    popsize: int = 24,
    init: dict[str, Any] | None = None,
    heldout_views: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """CMA-ES on the HARD rasteriser from the best ranked candidate (or `init`); CPU only, no torch, deterministic per `seed`."""
    try:
        import cma
    except ImportError as exc:
        raise MissingDependency(
            "cma (CMA-ES)", remediation="python -m pip install 'threed-vision[fit]'"
        ) from exc
    fixed = fixed or {}
    name, p = info["template"], info["p"]
    start = init or cands[0]["init"]
    t = time.perf_counter()
    planar_n = views[0]["R"].T @ np.array([0.0, 0.0, 1.0]) if planar else None
    x0 = np.concatenate(
        [
            np.zeros(6),
            [math.log(max(start["L"], 1e-3))],
            np.asarray(start["c"], np.float64),
            _softplus_inv_np(start["w"]),
            [math.log(float(np.clip(start["aspect"], ASPECT_MIN, ASPECT_MAX)))],
        ]
    )
    sig = np.concatenate([[0.1] * 3, [8.0] * 3, [0.1], [1.5] * K, [0.3] * K, [0.2]])
    frozen: set[int] = set()
    if len(views) == 1 and not planar:
        frozen.update({3, 4, 5})  # depth is defined by known_depth
    if "length_mm" in fixed:
        frozen.add(6)
    if name == "lathe" or "thickness_mm" in fixed or (planar and len(views) == 1):
        frozen.add(x0.size - 1)
    free = np.array([i for i in range(x0.size) if i not in frozen])
    crops = crop_all(views)
    S = crops[0]["mask"].shape[0] if crops else CROP_S
    tgt = [c["mask"] > 0.5 for c in crops]

    def full(xf: np.ndarray) -> np.ndarray:
        x = x0.copy()
        x[free] = xf
        return x

    def cost(xf: np.ndarray) -> float:
        V = mesh_world(
            _decode(full(xf), K, name, fixed, planar_n, start), p, K, OPT_RING
        )
        tot = 0.0
        for c, m in zip(crops, tgt, strict=True):
            h = hull_mask(V, K, OPT_RING, c["K"], c["R"], c["t"], (S, S))
            tot += 1.0 - float((h & m).sum()) / max(int((h | m).sum()), 1)
        return tot / len(crops)

    # cma treats seed 0 as "seed from the clock": map every user seed (negative too) to a positive one so runs are reproducible
    opts: dict[str, Any] = {
        "CMA_stds": sig[free],
        "maxfevals": max_evals,
        "popsize": popsize,
        "verbose": -9,
        "seed": seed % (2**31 - 1) + 1,
    }
    es = cma.CMAEvolutionStrategy(x0[free], 1.0, opts)
    es.optimize(cost)
    core = _decode(
        full(np.asarray(es.result.xbest, np.float64)), K, name, fixed, planar_n, start
    )
    timing = {**info["timing_s"], "refine": time.perf_counter() - t}
    out = assemble(
        core,
        views,
        template=name,
        K=K,
        kind=info["kind"],
        fixed=fixed,
        method="cma",
        ring=OPT_RING,
        min_iou=min_iou,
        timing=timing,
        tries=1,
        device="cpu",
        heldout_views=heldout_views,
    )
    out["cma_evals"] = int(es.result.evaluations)
    return out


def fit_cma(
    views: list[dict[str, Any]],
    template: str | float | None,
    *,
    K: int = 8,
    known_depth: float | None = None,
    planar: bool = False,
    fixed_dims: dict[str, float] | None = None,
    min_iou: float = 0.8,
    seed: int = 0,
    max_evals: int = 2500,
    popsize: int = 24,
    heldout_views: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Gradient-free fit: closed-form start -> ranked candidates -> CMA-ES on the hard rasteriser (CPU, no torch).

    Better than Adam on flat/boxy shapes, worse on lathe-like ones (RESULT 4c); ~3 s per object. Deterministic per `seed`.
    """
    vs, hv, fixed = prepare_inputs(views, heldout_views, fixed_dims)
    cands, info = ranked_candidates(
        vs,
        template,
        K=K,
        known_depth=known_depth,
        planar=planar,
        fixed_dims=fixed,
        ring=OPT_RING,
    )
    return cma_refine(
        vs,
        cands,
        info,
        K=K,
        planar=planar,
        fixed=fixed,
        min_iou=min_iou,
        seed=seed,
        max_evals=max_evals,
        popsize=popsize,
        heldout_views=hv,
    )
