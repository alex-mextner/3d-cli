"""Sculpt-brush kernels: each one does what its definition says, only where the mask says."""

from __future__ import annotations

import numpy as np
from geometry.meshfit import brushes, meshmath
from geometry.meshfit.regions import RegionContext
from meshfit_fixtures import cube_sphere

REGIONS: dict[str, object] = {
    "cap": {"zrange": [8, 30], "falloff": 6},
    "all": {"all": True},
}


def _setup() -> tuple[RegionContext, np.ndarray, np.ndarray]:
    V, F = cube_sphere(20, 20.0)
    ctx = RegionContext(V, F, REGIONS, ".")  # type: ignore[arg-type]
    return ctx, V, ctx.weight("cap")


def test_inflate_moves_exactly_amount_times_weight_along_the_normal() -> None:
    ctx, V, w = _setup()
    out = brushes.inflate(ctx, {"amount": 0.8, "normal_smoothing": 0}, w)
    n = meshmath.vertex_normals(V, ctx.F)
    assert np.allclose(out - V, (0.8 * w)[:, None] * n)
    assert np.all(out[w == 0.0] == V[w == 0.0])  # outside the mask: bit-identical


def test_negative_inflate_shrinks_the_volume_positive_grows_it() -> None:
    ctx, V, w = _setup()
    base = meshmath.signed_volume(V, ctx.F)
    assert (
        meshmath.signed_volume(brushes.inflate(ctx, {"amount": 0.5}, w), ctx.F) > base
    )
    assert (
        meshmath.signed_volume(brushes.inflate(ctx, {"amount": -0.5}, w), ctx.F) < base
    )


def test_taubin_smooth_removes_roughness_without_shrinking_but_plain_laplacian_shrinks() -> (
    None
):
    ctx, V, w = _setup()
    rng = np.random.default_rng(0)
    n = meshmath.vertex_normals(V, ctx.F)
    rough = V + n * rng.normal(0, 0.15, len(V))[:, None]
    rctx = RegionContext(rough, ctx.F, REGIONS, ".")  # type: ignore[arg-type]
    all_w = rctx.weight("all")
    err = lambda P: float(np.abs(np.linalg.norm(P, axis=1) - 20.0).std())  # noqa: E731 - radial roughness of a sphere
    taubin = brushes.smooth(rctx, {"iterations": 20, "method": "taubin"}, all_w)
    plain = brushes.smooth(rctx, {"iterations": 20, "method": "laplacian"}, all_w)
    assert err(taubin) < 0.4 * err(rough)
    v0 = meshmath.signed_volume(rough, ctx.F)
    assert abs(meshmath.signed_volume(taubin, ctx.F) - v0) / v0 < 0.03
    assert (
        v0 - meshmath.signed_volume(plain, ctx.F)
    ) / v0 > 0.05  # teeth: Laplacian smoothing does shrink


def test_scale_local_scales_about_the_pivot_only_inside_the_mask() -> None:
    ctx, V, w = _setup()
    out = brushes.scale_local(ctx, {"factor": [1.0, 1.0, 1.5], "pivot": [0, 0, 0]}, w)
    inside = w == 1.0
    assert np.allclose(out[inside, 2], V[inside, 2] * 1.5) and np.allclose(
        out[inside, :2], V[inside, :2]
    )
    assert np.array_equal(out[w == 0.0], V[w == 0.0])


def test_pinch_pulls_toward_the_target_in_the_tangent_plane_and_crease_adds_a_groove() -> (
    None
):
    ctx, V, w = _setup()
    target = [0.0, 0.0, 20.0]
    pinched = brushes.pinch(
        ctx, {"strength": 0.5, "toward": {"point": target}, "keep_normal": False}, w
    )
    before = np.linalg.norm(V - target, axis=1)
    after = np.linalg.norm(pinched - target, axis=1)
    inside = w == 1.0
    assert (
        np.all(after[inside] <= before[inside])
        and after[inside].sum() < 0.8 * before[inside].sum()
    )
    creased = brushes.crease(
        ctx, {"strength": 0.0, "depth": 0.4, "sharpness": 1.0, "normal_smoothing": 0}, w
    )
    n = meshmath.vertex_normals(V, ctx.F)
    assert np.allclose(np.einsum("ij,ij->i", creased - V, n), -0.4 * w)


def test_symmetrize_average_makes_an_asymmetric_dent_mirror_symmetric() -> None:
    ctx, V, _ = _setup()
    dented = V.copy()
    dented[V[:, 0] > 10] *= 1.05  # push the +x side outward only
    dctx = RegionContext(dented, ctx.F, REGIONS, ".")  # type: ignore[arg-type]
    out = brushes.symmetrize(dctx, {"axis": "x", "mode": "average"}, dctx.weight("all"))
    mirrored = out * np.array([-1.0, 1.0, 1.0])
    from scipy.spatial import cKDTree

    gap_before = cKDTree(dented).query(dented * np.array([-1.0, 1.0, 1.0]))[0].max()
    gap_after = cKDTree(out).query(mirrored)[0].max()
    assert gap_after < 0.35 * gap_before


def test_symmetrize_mirror_positive_copies_the_positive_side_over_the_negative() -> (
    None
):
    ctx, V, _ = _setup()
    dented = V.copy()
    dented[V[:, 0] > 10] *= 1.05
    dctx = RegionContext(dented, ctx.F, REGIONS, ".")  # type: ignore[arg-type]
    out = brushes.symmetrize(
        dctx, {"axis": "x", "mode": "mirror_positive"}, dctx.weight("all")
    )
    positive = dented[:, 0] >= 0
    assert np.array_equal(out[positive], dented[positive])  # the kept side never moves
    from scipy.spatial import cKDTree

    d_mirror = cKDTree(out[positive]).query(
        out[~positive] * np.array([-1.0, 1.0, 1.0])
    )[0]
    assert (
        d_mirror.max() < 1.5
    )  # about one edge length: the negative side now follows the +x bulge
