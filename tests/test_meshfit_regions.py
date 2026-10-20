"""Region masks: shapes, falloff smoothness, combinators, field ramps, spec validation."""

from __future__ import annotations

import numpy as np
import pytest
from errors import UsageError
from geometry.meshfit import meshmath
from geometry.meshfit.regions import RegionContext
from meshfit_fixtures import cube_sphere, spec


def _ctx(regions: dict[str, object]) -> tuple[RegionContext, np.ndarray]:
    V, F = cube_sphere(24, 20.0)
    return RegionContext(V, F, regions, "."), V  # type: ignore[arg-type]


def test_box_weight_is_one_inside_zero_far_and_ramps_between() -> None:
    ctx, V = _ctx({"b": {"box": [-30, 30, -30, 30, 5, 30], "falloff": 6}})
    w = ctx.weight("b")
    assert np.all(w[V[:, 2] >= 5] == 1.0)
    assert np.all(w[V[:, 2] <= -1.0] == 0.0)
    mid = (V[:, 2] > -1.0) & (V[:, 2] < 5.0)
    assert ((w[mid] > 0.0) & (w[mid] < 1.0)).all()


def test_falloff_has_no_hard_step_between_neighbouring_vertices() -> None:
    """Smooth falloff: across any mesh edge the weight changes by at most a small fraction (hard region: it would jump to 1)."""
    ctx, V = _ctx(
        {
            "soft": {"sphere": {"center": [20, 0, 0], "radius": 6}, "falloff": 8},
            "hard": {"sphere": {"center": [20, 0, 0], "radius": 6}},
        }
    )
    F = ctx.F
    edges = np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])
    soft, hard = ctx.weight("soft"), ctx.weight("hard")
    edge_len = np.linalg.norm(V[edges[:, 0]] - V[edges[:, 1]], axis=1)
    soft_jump = np.abs(soft[edges[:, 0]] - soft[edges[:, 1]]).max()
    # Hermite ramp over 8 mm: max slope 1.5/8 per mm -> across the longest edge.
    assert soft_jump <= 1.5 / 8.0 * edge_len.max() + 1e-9
    assert (
        np.abs(hard[edges[:, 0]] - hard[edges[:, 1]]).max() == 1.0
    )  # the control really is a step


def test_combinators_invert_and_difference() -> None:
    ctx, V = _ctx(
        {
            "top": {"zrange": [0, 30]},
            "right": {"halfspace": {"point": [0, 0, 0], "normal": [-1, 0, 0]}},
            "both": {"intersect": ["top", "right"]},
            "either": {"union": ["top", "right"]},
            "top_not_right": {"difference": ["top", "right"]},
            "bottom": {"zrange": [0, 30], "invert": True},
        }
    )
    top, right = ctx.weight("top"), ctx.weight("right")
    assert np.array_equal(ctx.weight("both"), np.minimum(top, right))
    assert np.array_equal(ctx.weight("either"), np.maximum(top, right))
    assert np.allclose(ctx.weight("top_not_right"), top * (1 - right))
    assert np.allclose(ctx.weight("bottom"), 1 - top)


def test_ramp_on_normal_z_selects_upward_facing_surface() -> None:
    ctx, V = _ctx(
        {"up": {"ramp": {"quantity": "nz", "from": 0.2, "to": 0.8, "smooth_iters": 0}}}
    )
    n = meshmath.vertex_normals(V, ctx.F)
    w = ctx.weight("up")
    assert np.all(w[n[:, 2] > 0.85] == 1.0) and np.all(w[n[:, 2] < 0.15] == 0.0)


def test_curvature_ramp_is_positive_on_a_convex_sphere() -> None:
    ctx, V = _ctx(
        {
            "convex": {
                "ramp": {
                    "quantity": "curvature",
                    "from": 0.0,
                    "to": 0.04,
                    "smooth_iters": 1,
                }
            }
        }
    )
    h = ctx.curvature(1)
    assert (
        abs(float(np.median(h)) - 1.0 / 20.0) < 0.01
    )  # mean curvature of a sphere is 1/r
    assert ctx.weight("convex").min() > 0.5


def test_points_region_uses_distance_to_a_point_cloud() -> None:
    ctx, V = _ctx(
        {
            "near": {
                "points": {"list": [[20.0, 0.0, 0.0]], "radius": 4.0},
                "falloff": 2.0,
            }
        }
    )
    w = ctx.weight("near")
    d = np.linalg.norm(V - np.array([20.0, 0.0, 0.0]), axis=1)
    assert np.all(w[d <= 4.0] == 1.0) and np.all(w[d >= 6.0] == 0.0)


def test_gap_region_zeroes_vertices_facing_a_close_wall() -> None:
    """Two parallel plates 1 mm apart: the facing sides are in a narrow gap (weight 0), the outer sides are free."""
    import trimesh

    a = trimesh.creation.box(
        extents=(20, 20, 4),
        transform=trimesh.transformations.translation_matrix((0, 0, 0)),
    )
    b = trimesh.creation.box(
        extents=(20, 20, 4),
        transform=trimesh.transformations.translation_matrix((0, 0, 5.0)),
    )
    both = trimesh.util.concatenate(
        [a.subdivide_to_size(2.0), b.subdivide_to_size(2.0)]
    )
    V, F = (
        np.asarray(both.vertices, dtype=np.float64),
        np.asarray(both.faces, dtype=np.int64),
    )
    ctx = RegionContext(
        V, F, {"gap": {"gap": {"min": 1.5, "max": 3.0, "rays": 1}}}, "."
    )
    w = ctx.weight("gap")
    inner = (np.abs(V[:, 2] - 2.0) < 1e-6) | (np.abs(V[:, 2] - 3.0) < 1e-6)
    outer = (np.abs(V[:, 2] + 2.0) < 1e-6) | (np.abs(V[:, 2] - 7.0) < 1e-6)
    interior = (np.abs(V[:, 0]) < 8.0) & (
        np.abs(V[:, 1]) < 8.0
    )  # corners have diagonal normals and look past the other plate
    assert w[inner & interior].max() == 0.0 and w[outer].min() == 1.0


@pytest.mark.parametrize(
    "regions, message",
    [
        (
            {
                "a": {
                    "box": [0, 1, 0, 1, 0, 1],
                    "sphere": {"center": [0, 0, 0], "radius": 1},
                }
            },
            "exactly one",
        ),
        ({"a": {"box": [0, 1, 0]}}, "6 finite numbers"),
        ({"a": {"union": ["missing"]}}, "unknown region"),
        ({"a": {"box": [0, 1, 0, 1, 0, 1], "typo": 1}}, "unknown keys"),
        ({"a": {"sphere": {"center": [0, 0, 0], "radius": -1}}}, "positive"),
    ],
)
def test_invalid_region_definitions_are_rejected_with_a_reason(
    regions: dict[str, object], message: str
) -> None:
    with pytest.raises(UsageError, match=message):
        spec(regions=regions)  # type: ignore[arg-type]


def test_circular_region_reference_is_detected() -> None:
    V, F = cube_sphere(4)
    ctx = RegionContext(V, F, {"a": {"union": ["b"]}, "b": {"union": ["a"]}}, ".")
    with pytest.raises(UsageError, match="circular"):
        ctx.weight("a")
