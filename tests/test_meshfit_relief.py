"""Fur relief: reproduces the Chekhov strand field, stays within the amplitude, and (overhang-safe) never undercuts."""

from __future__ import annotations

import numpy as np
from geometry.meshfit import meshmath, noise, relief
from geometry.meshfit.regions import RegionContext
from meshfit_fixtures import box, cube_sphere

# Values of print/fur.py (Chekhov, strand_field / clump_field with its default constants) at these points.
POINTS = np.array(
    [
        [0.0, 0.0, 0.0],
        [12.3, -4.5, 30.1],
        [-7.7, 22.2, 5.5],
        [33.3, 1.1, -8.8],
        [5.0, 5.0, 5.0],
    ]
)
CHEKHOV_STRAND = [0.343081129, 0.35339441, 0.684357575, 0.364558993, 0.455106027]
CHEKHOV_CLUMP = [0.868616226, 0.424468991, 0.503029858, 0.694651019, 0.73738712]
FLOW_Z = np.array([0.0, 0.0, 1.0])


def test_noise_ports_the_chekhov_strand_and_clump_fields_exactly() -> None:
    assert np.allclose(
        noise.strand_field(POINTS, seed=1, flow=FLOW_Z), CHEKHOV_STRAND, atol=1e-9
    )
    assert np.allclose(
        noise.clump_field(POINTS, seed=1, flow=FLOW_Z), CHEKHOV_CLUMP, atol=1e-9
    )


def test_noise_is_a_pure_function_of_seed_and_position() -> None:
    a = noise.strand_field(POINTS, seed=4, flow=FLOW_Z)
    assert np.array_equal(a, noise.strand_field(POINTS, seed=4, flow=FLOW_Z))
    assert not np.allclose(
        a, noise.strand_field(POINTS, seed=5, flow=FLOW_Z)
    )  # the seed matters
    assert a.min() >= 0.0 and a.max() <= 1.0


def test_strands_are_stretched_along_the_flow() -> None:
    """Correlation along the flow axis is far higher than across it (that is what makes a strand)."""
    rng = np.random.default_rng(1)
    base = rng.uniform(-40, 40, (4000, 3))
    f0 = noise.strand_field(base, seed=2, flow=FLOW_Z)
    along = np.corrcoef(
        f0, noise.strand_field(base + [0, 0, 1.5], seed=2, flow=FLOW_Z)
    )[0, 1]
    across = np.corrcoef(
        f0, noise.strand_field(base + [1.5, 0, 0], seed=2, flow=FLOW_Z)
    )[0, 1]
    assert along > across + 0.25


def _ctx(n: int = 28) -> RegionContext:
    V, F = cube_sphere(n, 20.0)
    return RegionContext(V, F, {"all": {"all": True}}, ".")


def test_fur_displacement_equals_amplitude_times_strand_field_along_the_smoothed_normal() -> (
    None
):
    ctx = _ctx()
    w = np.ones(len(ctx.V))
    out, info = relief.fur(
        ctx, {"amplitude": 0.35, "flow": [0, 0, 1], "normal_smoothing": 6}, w, seed=1
    )
    n = meshmath.smooth_normals(ctx.V, ctx.F, 6)
    expect = noise.strand_field(ctx.V, seed=1, flow=FLOW_Z) * 0.35
    assert np.allclose(out - ctx.V, n * expect[:, None], atol=1e-12)
    assert info["max_relief_mm"] <= 0.35 + 1e-12


def test_fur_stays_outward_within_amplitude_and_inside_the_mask_only() -> None:
    ctx = _ctx()
    w = np.clip((ctx.V[:, 2] - 2.0) / 10.0, 0.0, 1.0)
    out, _ = relief.fur(ctx, {"amplitude": 0.4, "amplitude_regions": []}, w, seed=3)
    n = meshmath.smooth_normals(ctx.V, ctx.F, 6)
    along = np.einsum("ij,ij->i", out - ctx.V, n)
    assert along.min() >= -1e-12 and along.max() <= 0.4 + 1e-12  # outward only, bounded
    assert np.array_equal(out[w == 0.0], ctx.V[w == 0.0])


def test_amplitude_regions_blend_a_second_amplitude_in() -> None:
    ctx = RegionContext(*cube_sphere(28, 20.0), {"head": {"zrange": [10, 30]}}, ".")  # type: ignore[arg-type]
    w = np.ones(len(ctx.V))
    out, _ = relief.fur(
        ctx,
        {"amplitude": 0.4, "amplitude_regions": [{"region": "head", "amplitude": 0.1}]},
        w,
        seed=1,
    )
    d = np.linalg.norm(out - ctx.V, axis=1)
    head = ctx.V[:, 2] >= 10
    assert d[head].max() <= 0.1 + 1e-9 and d[~head].max() > 0.2


def _worst_overhang_angle_from_vertical(
    V: np.ndarray, F: np.ndarray, bed: float = 0.3
) -> float:
    """Angle (deg from vertical) of the worst downward-facing, off-bed face (the repo's overhang convention)."""
    nz = meshmath.face_normals(V, F)[:, 2]
    on_bed = V[F][:, :, 2].max(axis=1) < bed
    down = (nz < 0) & ~on_bed
    return float(np.degrees(np.arcsin(-nz[down].min()))) if down.any() else 0.0


def _bell() -> tuple[np.ndarray, np.ndarray]:
    """A 30 x 30 x 28 block standing on the bed: vertical walls and a flat top, so the input has no overhang."""
    V, F = box((30.0, 30.0, 28.0), n=30)
    return V + np.array([0.0, 0.0, 14.0]), F


def test_overhang_safe_mode_never_creates_a_face_past_the_limit_while_plain_fur_does() -> (
    None
):
    V, F = _bell()
    before = _worst_overhang_angle_from_vertical(V, F)
    assert before == 0.0  # the input is printable
    ctx = RegionContext(V, F, {"all": {"all": True}}, ".")
    w = np.ones(len(V))
    big = {
        "amplitude": 3.0,
        "pitch": 1.6,
        "length": 3.0,
        "flow": [1, 0, 0],
    }  # crosswise strands: steep flanks on purpose
    plain, _ = relief.fur(ctx, big, w, seed=7)
    safe, info = relief.fur(
        ctx, {**big, "overhang_safe": {"max_angle_deg": 45.0}}, w, seed=7
    )
    assert (
        _worst_overhang_angle_from_vertical(plain, F) > 45.0
    )  # teeth: without the mode the relief undercuts
    assert _worst_overhang_angle_from_vertical(safe, F) <= 45.0 + 1e-9
    assert (
        info["overhang_safe"]["violating_faces_first_pass"] > 0
        and info["overhang_safe"]["vertices_reduced"] > 0
    )
    assert np.abs(safe - V).max() > 0.2  # and it is still real relief, not zeroed out


def test_overhang_safe_agrees_with_the_repo_printability_overhang_tool() -> None:
    """Cross-check with lib/printability_mesh.overhang on the shell above the bed (its 45 degree convention)."""
    import trimesh
    from printability_mesh import overhang

    V, F = _bell()
    ctx = RegionContext(V, F, {"all": {"all": True}}, ".")
    safe, _ = relief.fur(
        ctx,
        {
            "amplitude": 3.0,
            "pitch": 1.6,
            "length": 3.0,
            "flow": [1, 0, 0],
            "overhang_safe": True,
        },
        np.ones(len(V)),
        seed=7,
    )
    above_bed = V[F][:, :, 2].max(axis=1) >= 0.3
    worst, _ = overhang(trimesh.Trimesh(safe, F[above_bed], process=False))
    assert worst <= 45.0 + 0.5


def test_limit_overhang_leaves_already_overhanging_faces_no_worse() -> None:
    V, F = cube_sphere(20, 20.0)  # the lower hemisphere already overhangs
    n = meshmath.vertex_normals(V, F)
    disp = np.full(len(V), 1.5)
    out, _ = relief.limit_overhang(V, F, n, disp, 45.0, 0.3)
    nz0 = meshmath.face_normals(V, F)[:, 2]
    nz1 = meshmath.face_normals(V + n * out[:, None], F)[:, 2]
    bed = V[F][:, :, 2].max(axis=1) < 0.3
    worse = (nz1 < -np.sin(np.radians(45.0))) & (nz1 < nz0 - 1e-9) & ~bed
    assert not worse.any()


def test_overhang_safe_fur_holds_its_limit_on_the_faded_displacement() -> None:
    """The keep-out fade scales the relief per vertex; the limiter must see that scaled field, not the unscaled one."""
    V, F = _bell()
    ctx = RegionContext(V, F, {"all": {"all": True}}, ".")
    w = np.ones(len(V))
    big = {
        "amplitude": 3.0,
        "pitch": 1.6,
        "length": 3.0,
        "flow": [1, 0, 0],
        "overhang_safe": {"max_angle_deg": 45.0},
    }
    scale = np.random.default_rng(5).uniform(0.0, 1.0, len(V))  # a ragged fade band
    unscaled, _ = relief.fur(ctx, big, w, seed=7)
    faded_after = V + (unscaled - V) * scale[:, None]  # what the engine used to do
    faded_inside, _ = relief.fur(ctx, big, w, seed=7, scale=scale)
    assert _worst_overhang_angle_from_vertical(faded_after, F) > 45.0  # teeth
    assert _worst_overhang_angle_from_vertical(faded_inside, F) <= 45.0 + 1e-9
    assert np.abs(faded_inside - V).max() > 0.2
