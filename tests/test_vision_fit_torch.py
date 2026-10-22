"""Soft-silhouette fitter, CMA-ES and the optimiser regression tests. Needs torch (and cma for the gradient-free tests):
CI without the `fit` extra skips this whole file; run it in the vision venv (`~/.cache/3d-cli/vision/venv`)."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("cv2")
torch = pytest.importorskip("torch")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vision_fit_synth as S  # noqa: E402

from threed_vision import fit_silhouette, soft_raster  # noqa: E402
from threed_vision.errors import MissingDependency  # noqa: E402
from threed_vision.fit import fit_auto  # noqa: E402
from threed_vision.fit_init import ranked_candidates, validate_views  # noqa: E402
from threed_vision.fit_silhouette import fit_cma, fit_object, resolve_torch_device  # noqa: E402
from threed_vision.io import to_jsonable  # noqa: E402
from threed_vision.loft import loft_vertices  # noqa: E402

SHORT = ((48, 8, 1.2, 0.03), (64, 6, 0.8, 0.015))  # smoke schedule: ~1 s on CPU


@pytest.fixture
def short_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(soft_raster, "STAGES", SHORT)


def _strip(fit: dict[str, Any]) -> str:
    fit = json.loads(json.dumps(to_jsonable(fit)))
    fit.pop("timing_s", None)
    for a in fit.get("attempts", []):
        a.pop("time_s", None)
    return json.dumps(fit, sort_keys=True)


# ------------------------------------------------------------------ the optimiser fixes, each pinned
def test_fix_softplus_inverse_is_numerically_stable() -> None:
    x = torch.tensor([0.05, 1.0, 20.0, 80.0, 100.0, 1e3, 1e5])
    naive = torch.log(
        torch.expm1(x)
    )  # the spike's first version: overflows to inf for large widths
    assert torch.isinf(naive[-3:]).all()
    inv = soft_raster.softplus_inv(x)
    assert torch.isfinite(inv).all()
    back = torch.nn.functional.softplus(inv)
    assert torch.allclose(back, x, rtol=1e-4, atol=1e-5)


def test_fix_smoothness_scale_is_detached() -> None:
    w = torch.tensor([3.0, 5.0, 4.0, 9.0, 6.0, 7.0, 8.0, 5.0], requires_grad=True)
    c = torch.tensor([0.0, 1.0, -1.0, 2.0, 0.0, 0.5, -0.5, 0.0], requires_grad=True)
    soft_raster.smoothness_penalty(w, c).backward()
    # reference: the same penalty with a CONSTANT scale (what "detached" means)
    w2, c2 = (
        w.detach().clone().requires_grad_(True),
        c.detach().clone().requires_grad_(True),
    )
    sc = float(w2.detach().mean()) + 1.0
    d2w, d2c = w2[2:] - 2 * w2[1:-1] + w2[:-2], c2[2:] - 2 * c2[1:-1] + c2[:-2]
    (
        soft_raster.SMOOTH_WEIGHT
        * (((d2w / sc) ** 2).mean() + ((d2c / sc) ** 2).mean())
    ).backward()
    assert torch.allclose(w.grad, w2.grad, rtol=1e-5, atol=1e-9)
    assert torch.allclose(c.grad, c2.grad, rtol=1e-5, atol=1e-9)
    # with a learnable scale the gradient would carry an extra term pushing w to inflate (the scale grows with w): equality with the
    # constant-scale gradient above is the pin
    w3 = w.detach().clone().requires_grad_(True)
    sc_live = w3.mean() + 1.0  # the rejected variant: the scale is part of the graph
    (
        soft_raster.SMOOTH_WEIGHT
        * ((((w3[2:] - 2 * w3[1:-1] + w3[:-2]) / sc_live) ** 2).mean())
    ).backward()
    assert not torch.allclose(w3.grad, w2.grad, rtol=1e-3), (
        "negative control: a live scale changes the gradient"
    )


def test_fix_max_union_boundary_sits_at_half_and_does_not_inflate() -> None:
    tri = torch.tensor([[0.0, 0.0], [10.0, 0.0], [0.0, 10.0]])
    uv = torch.cat([tri] * 5)  # five coincident triangles
    faces = torch.tensor([[3 * i, 3 * i + 1, 3 * i + 2] for i in range(5)])
    on_edge = torch.tensor(
        [[5.0, 0.0]]
    )  # a pixel exactly on the hypotenuse-free edge y = 0
    single = soft_raster.soft_silhouette(tri, faces[:1], on_edge, 1.0)
    five = soft_raster.soft_silhouette(uv, faces, on_edge, 1.0)
    assert float(single) == pytest.approx(0.5, abs=1e-6)
    assert float(five) == pytest.approx(
        0.5, abs=1e-6
    )  # 1 - prod(1 - p) would give 1 - 0.5**5 = 0.97
    assert float(1 - (1 - single) ** 5) > 0.9  # what the rejected formulation does


def test_fix_nonfinite_loss_restores_the_last_finite_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    views = validate_views(S.topdown_scene("screwdriver", 30)["views"])
    cands, info = ranked_candidates(views, "lathe", known_depth=650.0, planar=True)
    lf = soft_raster.LoftFit(
        views, template="lathe", p=2.0, K=8, device="cpu", planar=True
    )
    lf.set_init(cands[0]["init"])
    real_loss, calls = lf.loss, {"n": 0}

    def poisoned(S_: int, sigma: float) -> Any:
        calls["n"] += 1
        return (
            real_loss(S_, sigma) * float("nan")
            if calls["n"] > 3
            else real_loss(S_, sigma)
        )

    monkeypatch.setattr(lf, "loss", poisoned)
    info_run = lf.run(SHORT)
    assert info_run["finite"] is False
    core = lf.result()
    assert all(np.isfinite(np.asarray(v)).all() for v in core.values())
    assert all(bool(torch.isfinite(p).all()) for p in lf.params.values())


def test_huge_initial_widths_do_not_overflow() -> None:
    views = validate_views(S.topdown_scene("box_elongated", 30)["views"])
    cands, _ = ranked_candidates(views, "box", known_depth=650.0, planar=True)
    init = dict(
        cands[0]["init"], w=np.full(8, 5e4)
    )  # 100 m wide: softplus_inv input far above exp overflow
    lf = soft_raster.LoftFit(
        views, template="box", p=8.0, K=8, device="cpu", planar=True
    )
    lf.set_init(init)
    lf.run(((48, 4, 1.2, 0.03),))
    core = lf.result()
    assert all(np.isfinite(np.asarray(v)).all() for v in core.values())
    json.dumps(to_jsonable(core), allow_nan=False)


# ------------------------------------------------------------------ rasteriser equivalence
def test_hull_union_equals_face_union_and_the_hard_raster() -> None:
    """The per-station convex-hull union is the same silhouette as the union of all mesh triangles (thresholded at 0.5) and
    agrees with the independent cv2 rasteriser up to its inclusive boundary pixels."""
    from threed_vision.loft import build_faces
    from threed_vision.render import hard_mask

    views = validate_views(S.scene("screwdriver", 2)["views"])
    cands, _ = ranked_candidates(views, "lathe")
    lf = soft_raster.LoftFit(views, template="lathe", p=2.0, K=8, device="cpu")
    lf.set_init(cands[0]["init"])
    Vw, _ = lf.mesh_world()
    faces_all = torch.tensor(build_faces(8, lf.M))
    pix = soft_raster.pixel_grid(96, "cpu")
    for vw in lf._views(96)[0]:
        uv = soft_raster.project(Vw, vw["K"], vw["R"], vw["t"])
        tris = (
            (soft_raster.soft_silhouette(uv, faces_all, pix, 0.5) > 0.5)
            .reshape(96, 96)
            .numpy()
        )
        idx = soft_raster.hull_indices(uv, 8, lf.M)
        hulls = (
            (soft_raster.soft_silhouette_hulls(uv, idx, pix, 0.5) > 0.5)
            .reshape(96, 96)
            .numpy()
        )
        assert (tris & hulls).sum() / max((tris | hulls).sum(), 1) > 0.985
        hard = hard_mask(
            Vw.detach().numpy().astype(float),
            faces_all.numpy(),
            vw["K"].numpy().astype(float),
            vw["R"].numpy().astype(float),
            vw["t"].numpy().astype(float),
            (96, 96),
        )
        assert (hard & hulls).sum() / max((hard | hulls).sum(), 1) > 0.85


def test_torch_mesh_matches_numpy_template() -> None:
    views = validate_views(S.topdown_scene("pliers_flat", 30)["views"])
    cands, _ = ranked_candidates(views, "flat", known_depth=650.0, planar=True)
    init = cands[0]["init"]
    lf = soft_raster.LoftFit(
        views, template="flat", p=4.0, K=8, device="cpu", planar=True
    )
    lf.set_init(init)
    Vt, _ = lf.mesh_world()
    Vn = (
        loft_vertices(init["L"], init["c"], init["w"], init["aspect"], 4.0, lf.M)
        @ init["R0"].T
        + init["t0"]
    )
    assert np.allclose(Vt.detach().numpy(), Vn, atol=5e-3)


# ------------------------------------------------------------------ fit_object
def test_scale_recovery_three_view_lathe_within_3_percent() -> None:
    sc = S.scene("screwdriver", 1)
    fit = fit_object(
        sc["views"], "lathe", device="cpu", heldout_views=sc["heldout"], max_tries=1
    )
    assert fit["method"] == "soft" and fit["device"] == "cpu"
    assert fit["dims_mm"]["length_mm"] == pytest.approx(sc["spec"]["L"], rel=0.03)
    assert fit["confidence"]["train_min_iou"] >= 0.85
    assert (
        fit["confidence"]["heldout_iou"] >= 0.8 and fit["confidence"]["status"] == "ok"
    )
    assert fit["params"]["aspect"] == 1.0  # a lathe stays axisymmetric
    json.dumps(to_jsonable(fit), allow_nan=False)


@pytest.mark.parametrize(
    "fixed",
    [
        {"length_mm": 200.0},
        {"max_width_mm": 33.0},
        {"thickness_mm": 7.5},
        {"length_mm": 175.0, "max_width_mm": 26.5, "thickness_mm": 4.25},
    ],
    ids=["length", "width", "thickness", "all"],
)
def test_fixed_dims_are_exact_after_optimisation(
    fixed: dict[str, float], short_schedule: None
) -> None:
    sc = S.topdown_scene("pliers_flat", 25)
    fit = fit_object(
        sc["views"],
        "flat",
        known_depth=sc["depth"],
        planar=sc is not None,
        fixed_dims=fixed,
        device="cpu",
        max_tries=1,
    )
    assert fit["method"] == "soft"
    for k, v in fixed.items():
        assert fit["dims_mm"][k] == pytest.approx(v, rel=1e-9), k
    assert fit["fixed_dims"] == fixed


def test_fixed_dims_hold_with_three_views_and_survive_the_mesh(
    short_schedule: None,
) -> None:
    sc = S.scene("box_elongated", 0)
    fixed = {"length_mm": 230.0, "thickness_mm": 14.0}
    fit = fit_object(sc["views"], "box", fixed_dims=fixed, device="cpu", max_tries=1)
    from threed_vision.mesh_export import export_mesh

    V, _ = export_mesh(fit)
    ext = V.max(0) - V.min(0)
    assert ext[0] == pytest.approx(230.0, rel=1e-9) and ext[2] == pytest.approx(
        14.0, rel=1e-9
    )


def test_same_seed_same_result_on_cpu(short_schedule: None) -> None:
    sc = S.scene("pliers_flat", 1)
    a = fit_object(sc["views"], "flat", device="cpu", seed=3, max_tries=2)
    b = fit_object(sc["views"], "flat", device="cpu", seed=3, max_tries=2)
    assert _strip(a) == _strip(b)


def test_single_view_non_planar_needs_depth_and_runs(short_schedule: None) -> None:
    sc = S.scene("screwdriver", 0, azimuths=(0.0,), elevations=(20.0,), heldout=None)
    fit = fit_object(
        sc["views"], "lathe", known_depth=sc["depth"], device="cpu", max_tries=1
    )
    assert fit["confidence"]["n_views"] == 1 and fit["confidence"]["status"] in (
        "single_view",
        "low",
    )


def test_fit_auto_refines_only_when_closed_form_is_not_good_enough(
    short_schedule: None,
) -> None:
    sc = S.topdown_scene("screwdriver", 30)
    cheap = fit_auto(sc["views"], known_depth=sc["depth"], planar=True)
    assert cheap["method"] == "closed_form" and len(cheap["attempts"]) == 1
    forced = fit_auto(
        sc["views"],
        known_depth=sc["depth"],
        planar=True,
        iou_accept=1.01,
        cma=False,
        max_tries=1,
        device="cpu",
    )
    assert [a["method"] for a in forced["attempts"]] == ["closed_form", "soft"]
    assert forced["timing_s"]["refine"] > 0 and forced["timing_s"]["cma"] == 0.0


def test_fit_auto_cma_is_the_second_attempt_for_low_confidence(
    short_schedule: None,
) -> None:
    pytest.importorskip("cma")
    sc = S.topdown_scene("pliers_flat", 30)
    res = fit_auto(
        sc["views"],
        known_depth=sc["depth"],
        planar=True,
        iou_accept=1.01,
        min_iou=0.999,
        max_tries=1,
        device="cpu",
    )
    assert [a["method"] for a in res["attempts"]] == ["closed_form", "soft", "cma"]
    assert res["confidence"]["status"] == "low"
    assert res["timing_s"]["cma"] > 0


# ------------------------------------------------------------------ CMA-ES
def test_cma_is_deterministic_per_seed_and_seed_matters() -> None:
    pytest.importorskip("cma")
    sc = S.topdown_scene("box_elongated", 30)
    kw = {"known_depth": sc["depth"], "planar": True, "max_evals": 250, "popsize": 8}
    a = fit_cma(sc["views"], "box", seed=5, **kw)
    b = fit_cma(sc["views"], "box", seed=5, **kw)
    c = fit_cma(sc["views"], "box", seed=6, **kw)
    assert _strip(a) == _strip(b)
    assert _strip(a) != _strip(c)
    assert a["method"] == "cma" and a["device"] == "cpu"


def test_cma_honours_fixed_dims() -> None:
    pytest.importorskip("cma")
    sc = S.scene("box_elongated", 1)
    fixed = {"length_mm": 222.0, "max_width_mm": 29.0, "thickness_mm": 11.0}
    fit = fit_cma(sc["views"], "box", fixed_dims=fixed, max_evals=200, popsize=8)
    for k, v in fixed.items():
        assert fit["dims_mm"][k] == pytest.approx(v, rel=1e-9)


# ------------------------------------------------------------------ device selection
def test_device_cpu_is_always_available_and_cuda_is_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert resolve_torch_device("cpu") == "cpu"
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    assert resolve_torch_device("auto") == "cpu"
    with pytest.raises(MissingDependency, match="CUDA"):
        resolve_torch_device("cuda")


def test_auto_falls_back_to_cpu_when_the_gpu_run_fails(
    monkeypatch: pytest.MonkeyPatch, short_schedule: None
) -> None:
    real = fit_silhouette._refine_on
    seen: list[str] = []

    def flaky(device: str, *a: Any, **kw: Any) -> Any:
        seen.append(device)
        if device == "cuda":
            raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")
        return real(device, *a, **kw)

    monkeypatch.setattr(
        fit_silhouette,
        "resolve_torch_device",
        lambda pref="auto": "cuda" if pref in ("auto", "cuda") else "cpu",
    )
    monkeypatch.setattr(fit_silhouette, "_refine_on", flaky)
    sc = S.topdown_scene("screwdriver", 30)
    fit = fit_object(
        sc["views"],
        "lathe",
        known_depth=sc["depth"],
        planar=True,
        device="auto",
        max_tries=1,
    )
    assert (
        seen == ["cuda", "cpu"]
        and fit["device"] == "cpu"
        and "cuda failed" in fit["device_fallback"]
    )
    seen.clear()
    with pytest.raises(
        RuntimeError, match="CUDA out of memory"
    ):  # an explicit device is never silently replaced
        fit_object(
            sc["views"],
            "lathe",
            known_depth=sc["depth"],
            planar=True,
            device="cuda",
            max_tries=1,
        )
    assert seen == ["cuda"]


@pytest.mark.skipif(not torch.cuda.is_available(), reason="no CUDA GPU")
def test_real_gpu_fit_runs_on_cuda(short_schedule: None) -> None:
    sc = S.topdown_scene("screwdriver", 30)
    fit = fit_object(
        sc["views"],
        "lathe",
        known_depth=sc["depth"],
        planar=True,
        device="cuda",
        max_tries=1,
    )
    assert fit["device"] == "cuda" and fit["confidence"]["train_min_iou"] > 0.8
    assert math.isfinite(fit["dims_mm"]["length_mm"])


@pytest.mark.skipif(not torch.cuda.is_available(), reason="no CUDA GPU")
def test_gpu_fit_is_repeatable(short_schedule: None) -> None:
    """Measured bitwise-identical on an RTX 4060 (3 runs); the contract asserted here is the weaker 'equal to 1e-4'."""
    sc = S.scene("pliers_flat", 1)
    a = fit_object(sc["views"], "flat", device="cuda", seed=3, max_tries=1)
    b = fit_object(sc["views"], "flat", device="cuda", seed=3, max_tries=1)
    assert a["dims_mm"]["length_mm"] == pytest.approx(b["dims_mm"]["length_mm"], rel=1e-4)
    assert np.allclose(a["params"]["w_mm"], b["params"]["w_mm"], rtol=1e-4, atol=1e-4)


def test_failed_heldout_view_triggers_the_refiner_even_when_closed_form_is_accepted(short_schedule: None) -> None:
    sc = S.topdown_scene("pliers_flat", 30)
    other = S.topdown_scene("box_elongated", 70)["views"][0]  # a different object as the "held-out" view: IoU ~ 0
    res = fit_auto(sc["views"], known_depth=sc["depth"], planar=True, iou_accept=0.0, heldout_views=[other], cma=False, max_tries=1, device="cpu")
    assert [a["method"] for a in res["attempts"]] == ["closed_form", "soft"]


def test_cma_negative_seed_is_reproducible() -> None:
    pytest.importorskip("cma")
    sc = S.topdown_scene("box_elongated", 30)
    kw = {"known_depth": sc["depth"], "planar": True, "max_evals": 150, "popsize": 8, "seed": -1}
    assert _strip(fit_cma(sc["views"], "box", **kw)) == _strip(fit_cma(sc["views"], "box", **kw))
