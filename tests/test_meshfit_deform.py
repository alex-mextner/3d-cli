"""Handle deformation: handles reach their targets, anchors never move, ARAP keeps detail rigid, locality holds."""

from __future__ import annotations

import numpy as np
import pytest
from errors import UsageError
from geometry.meshfit import deform, meshmath
from meshfit_fixtures import cube_sphere


def _bar(n_along: int = 40) -> tuple[np.ndarray, np.ndarray]:
    """A closed 40 x 6 x 6 mm bar along X, tessellated finely along its length."""
    V, F = cube_sphere(n_along, 1.0)
    m = np.abs(V).max(axis=1, keepdims=True)
    V = V / m
    V = V * np.array([20.0, 3.0, 3.0])
    return V.astype(np.float32).astype(np.float64), F


def _setup() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    V, F = _bar()
    handles = np.nonzero(V[:, 0] > 19.5)[0]
    anchors = V[:, 0] < -19.5
    targets = V[handles] + np.array([0.0, 0.0, 6.0])
    return V, F, handles, anchors, targets


@pytest.mark.parametrize("method", ["harmonic", "biharmonic", "arap"])
def test_handles_reach_their_targets_exactly_and_anchors_do_not_move(
    method: str,
) -> None:
    V, F, handles, anchors, targets = _setup()
    out, stats = deform.solve_handles(
        V, F, handles, targets, anchors, None, method, iterations=6
    )
    assert np.allclose(out[handles], targets, atol=1e-12)
    assert np.array_equal(out[anchors], V[anchors])  # 0.0 mm
    assert stats["free_vertices"] == len(V) - len(handles) - int(anchors.sum())


def test_surface_follows_smoothly_between_anchor_and_handle() -> None:
    """The deflection grows monotonically along the bar from the anchored end to the pulled end (no kinks, no overshoot)."""
    V, F, handles, anchors, targets = _setup()
    out, _ = deform.solve_handles(V, F, handles, targets, anchors, None, "biharmonic")
    # deflection of the centreline vertices (|y|,|z| smallest), binned along x
    x = V[:, 0]
    bins = np.linspace(-20, 20, 21)
    lift = [
        np.mean((out - V)[(x >= lo) & (x < hi), 2])
        for lo, hi in zip(bins[:-1], bins[1:], strict=True)
    ]
    assert all(b >= a - 1e-9 for a, b in zip(lift, lift[1:], strict=False))
    assert 0.0 <= lift[0] < lift[-1] <= 6.0 + 1e-9


def test_arap_preserves_local_edge_lengths_better_than_harmonic() -> None:
    """Teeth: twisting the free end of the bar by 90 degrees, ARAP keeps edges near their length, harmonic shears them."""
    V, F = _bar()
    handles = np.nonzero(V[:, 0] > 19.5)[0]
    anchors = V[:, 0] < -19.5
    spec = {"rotate": {"axis": [1, 0, 0], "deg": 90.0}, "pivot": [20.0, 0.0, 0.0]}
    targets = deform.handle_targets(V, handles, spec)
    edges = np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])
    base = np.linalg.norm(V[edges[:, 0]] - V[edges[:, 1]], axis=1)

    def distortion(method: str) -> float:
        out, _ = deform.solve_handles(
            V, F, handles, targets, anchors, None, method, iterations=15
        )
        return float(
            np.abs(
                np.linalg.norm(out[edges[:, 0]] - out[edges[:, 1]], axis=1) / base - 1.0
            ).mean()
        )

    assert distortion("arap") < 0.6 * distortion("harmonic")


def test_influence_zone_keeps_everything_outside_the_patch_bit_identical() -> None:
    V, F, handles, _, targets = _setup()
    zone = np.linalg.norm(V - V[handles].mean(axis=0), axis=1) <= 12.0
    anchors = np.zeros(len(V), dtype=bool)
    out, stats = deform.solve_handles(
        V, F, handles, targets, anchors, zone, "biharmonic"
    )
    assert np.array_equal(out[~zone], V[~zone])
    assert stats["free_vertices"] < len(V) // 2
    assert np.abs(out - V).max() >= 6.0 - 1e-9


def test_rotation_and_scale_targets_about_a_pivot() -> None:
    V, _, handles, _, _ = _setup()
    t = deform.handle_targets(
        V,
        handles,
        {
            "rotate": {"axis": [0, 0, 1], "deg": 90.0},
            "pivot": [0.0, 0.0, 0.0],
            "scale": 2.0,
            "translate": [0, 0, 1],
        },
    )
    p = V[handles][0]
    assert np.allclose(t[0], [-2 * p[1], 2 * p[0], 2 * p[2] + 1.0], atol=1e-9)


def test_unpinned_patch_is_refused_with_a_reason() -> None:
    V, F, handles, _, targets = _setup()
    with pytest.raises(UsageError, match="no anchors"):
        deform.solve_handles(
            V, F, handles, targets, np.zeros(len(V), dtype=bool), None, "arap"
        )


def test_biharmonic_response_is_linear_in_the_handle_motion() -> None:
    """Doubling the pull doubles the displacement everywhere (the property the silhouette fit relies on)."""
    V, F, handles, anchors, targets = _setup()
    one, _ = deform.solve_handles(V, F, handles, targets, anchors, None, "biharmonic")
    two, _ = deform.solve_handles(
        V,
        F,
        handles,
        V[handles] + 2.0 * (targets - V[handles]),
        anchors,
        None,
        "biharmonic",
    )
    assert np.allclose(two - V, 2.0 * (one - V), atol=1e-8)
    assert meshmath.is_watertight(F)
