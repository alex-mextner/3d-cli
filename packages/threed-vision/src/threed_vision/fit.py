"""`fit_auto`: the cheapest fit that is good enough.

1. closed form (ms): per-view profile -> template by `kind` (box -> p=8, cylinder_capsule / lathe_profile / ring_lathe -> p=2,
   loft_flat -> p=4) -> pose candidates ranked by the independent hard rasteriser; the best candidate IS the parametric model
   (lathe / loft / box params straight from the profile) when every view's IoU >= `iou_accept`.
2. soft-silhouette refinement (torch, GPU->CPU) only otherwise.
3. CMA-ES (no GPU, ~3 s) only as a second attempt when the result is still low-confidence; the better attempt wins, selected by the
   held-out view when there is one (training IoU is ~equal for both, RESULT 4c) else by mean training IoU.

The output is the fit dict of `loft.make_fit` + `views`, `confidence`, `timing_s`, `attempts`, `closed_form`. See
docs/commands/fit-silhouette.md for the stable `params` schema the hsw-editor service maps to lathe / loft / box.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np

from .fit_init import ranked_candidates
from .errors import BadInput, MissingDependency
from .fit_silhouette import assemble, cma_refine, prepare_inputs, refine_candidates

CLOSED_FORM_RING = 12


def _summary(fit: dict[str, Any]) -> dict[str, Any]:
    c = fit["confidence"]
    return {
        "method": fit["method"],
        "train_min_iou": c["train_min_iou"],
        "train_mean_iou": float(np.mean([v["iou"] for v in fit["views"]])),
        "heldout_iou": c["heldout_iou"],
        "time_s": fit["timing_s"].get("total"),
    }


def _skipped(method: str, exc: MissingDependency) -> dict[str, Any]:
    """An `attempts` entry for an optional stage that could not run: explicit, never silent."""
    return {"method": method, "skipped": exc.render(), "train_min_iou": None, "train_mean_iou": None, "heldout_iou": None, "time_s": 0.0}


def _score(fit: dict[str, Any], min_iou: float) -> tuple[bool, float]:
    """Selection key: a fit whose TRAINING IoU clears `min_iou` first, then the mean TRAINING IoU. The held-out view never selects
    (it only reports): choosing the attempt by it would make `confidence.heldout_iou` a best-of-N number, not an independent one."""
    return (fit["confidence"]["train_min_iou"] >= min_iou, float(np.mean([v["iou"] for v in fit["views"]])))


def fit_auto(
    views: list[dict[str, Any]],
    *,
    template: str | float | None = None,
    K: int = 8,
    known_depth: float | None = None,
    planar: bool = False,
    fixed_dims: dict[str, float] | None = None,
    heldout_views: list[dict[str, Any]] | None = None,
    iou_accept: float = 0.9,
    min_iou: float = 0.8,
    max_tries: int = 3,
    device: str = "auto",
    seed: int = 0,
    cma: bool = True,
    refine: bool = True,
) -> dict[str, Any]:
    """Fit a lathe / flat loft / box to calibrated silhouettes (views = [{"mask","K","R","t"}], world -> camera, mm).

    template None picks by closed-form kind (or 'lathe' | 'flat' | 'box'). A single view needs `known_depth` (mm along camera z);
    `planar=True` = top-down object on a plane parallel to the image. `fixed_dims` {length_mm, max_width_mm, thickness_mm} are HARD
    user overrides (exact in the result). `heldout_views` are scored, never fitted: they make `confidence.heldout_iou`.
    `iou_accept` is the per-view hard-raster IoU at which the closed-form model is returned without any optimiser (a closed form that is already `low` - train IoU under `min_iou` or a held-out view under 0.75 - always triggers the refiner; the attempt is then chosen by training IoU, the held-out IoU is only reported);
    `min_iou` is the `confidence.status == 'low'` threshold. `refine=False` returns the closed-form fit whatever its confidence (no torch, no CMA). Raises BadInput / MissingDependency (refinement needs torch).
    """
    t_all = time.perf_counter()
    vs, hv, fixed = prepare_inputs(views, heldout_views, fixed_dims)
    if planar and len(vs) != 1:
        raise BadInput("planar applies to ONE top-down view (the object lies on a plane parallel to the image)", remediation="drop planar for several views")
    cands, info = ranked_candidates(
        vs,
        template,
        K=K,
        known_depth=known_depth,
        planar=planar,
        fixed_dims=fixed,
        ring=CLOSED_FORM_RING,
    )
    best = assemble(
        cands[0]["init"],
        vs,
        template=info["template"],
        K=K,
        kind=info["kind"],
        fixed=fixed,
        method="closed_form",
        ring=CLOSED_FORM_RING,
        min_iou=min_iou,
        timing=dict(info["timing_s"]),
        tries=0,
        device=None,
        heldout_views=hv,
    )
    attempts = [_summary(best)]
    t_refine = t_cma = 0.0
    # refine when the closed form is not accepted, or is already `low`: a train IoU under `min_iou` (a stricter --min-confidence than
    # --iou-accept) or a held-out view under 0.75 (flat/box fits reproduce their training views and miss the unseen one)
    if refine and (best["confidence"]["train_min_iou"] < iou_accept or best["confidence"]["status"] == "low"):
        try:
            soft = refine_candidates(
                vs, cands, info, K=K, device=device, planar=planar, fixed=fixed, min_iou=min_iou, max_tries=max_tries, seed=seed, heldout_views=hv
            )
        except MissingDependency as exc:
            if device == "cuda":  # an explicit GPU request is never silently downgraded
                raise
            attempts.append(_skipped("soft", exc))  # the closed-form fit stays: its confidence says how good it is
            soft = None
        if soft is not None:
            t_refine = soft["timing_s"]["refine"]
            attempts.append(_summary(soft))
            if _score(soft, min_iou) >= _score(best, min_iou):
                best = soft
        if cma and best["confidence"]["status"] == "low":
            try:
                second = cma_refine(vs, cands, info, K=K, planar=planar, fixed=fixed, min_iou=min_iou, seed=seed, heldout_views=hv)
            except MissingDependency as exc:
                attempts.append(_skipped("cma", exc))
            else:
                t_cma = second["timing_s"]["refine"]
                attempts.append(_summary(second))
                if _score(second, min_iou) > _score(best, min_iou):
                    best = second
    best["timing_s"] = {
        **info["timing_s"],
        "refine": t_refine,
        "cma": t_cma,
        "total": time.perf_counter() - t_all,
    }
    best["attempts"] = attempts
    best["closed_form"] = {
        "kind": info["kind"],
        "views": [
            {"kind": p["kind"], "iou_recon": p["iou_recon"]} for p in info["profiles"]
        ],
        "iou_accept": iou_accept,
    }
    return best
