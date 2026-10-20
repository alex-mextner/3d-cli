"""noise.py — seeded, vectorised 3-D gradient noise and the strand / clump relief fields built on it.

WHAT: Perlin gradient noise on an integer hash (no random state, no threads), ridged noise, a
  flow-aligned strand field (thin crests stretched along a flow direction, domain-warped across it)
  and a rounder clump field. All return float64 arrays for (n, 3) sample points.

WHY: fur / relief displacement must be a pure function of (position, seed, parameters) so the same
  spec gives a byte-identical mesh. The hash is the one used by the Chekhov fur script, so a spec
  with seed 1, flow (0,0,1) and the Chekhov constants reproduces that relief (pinned by a golden test).
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

Array = NDArray[np.float64]

_GRADIENTS = np.array(
    [
        [1, 1, 0],
        [-1, 1, 0],
        [1, -1, 0],
        [-1, -1, 0],
        [1, 0, 1],
        [-1, 0, 1],
        [1, 0, -1],
        [-1, 0, -1],
        [0, 1, 1],
        [0, -1, 1],
        [0, 1, -1],
        [0, -1, -1],
    ],
    dtype=np.float64,
)


def smoothstep(x: Array, a: float, b: float) -> Array:
    """Hermite ramp 0 at `a` to 1 at `b` (either order); constant 0/1 outside. a == b is a hard step at a."""
    if a == b:
        return (x >= a).astype(np.float64)
    t = np.clip((x - a) / (b - a), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _hash(
    ix: NDArray[np.int64], iy: NDArray[np.int64], iz: NDArray[np.int64], seed: int
) -> NDArray[np.int64]:
    with np.errstate(over="ignore"):  # int64 wrap-around is the hash
        h = (
            (ix * 73856093)
            ^ (iy * 19349663)
            ^ (iz * 83492791)
            ^ np.int64((seed * 2654435761) & 0x7FFFFFFFFFFFFFFF)
        )
        h = (h ^ (h >> 13)) * 1274126177
        return ((h ^ (h >> 16)) & 0x7FFFFFFF) % 12


def perlin(p: Array, seed: int = 1) -> Array:
    """3-D gradient noise, roughly in [-1, 1], for (n, 3) points."""
    i0 = np.floor(p).astype(np.int64)
    f = p - i0
    u = f * f * f * (f * (f * 6 - 15) + 10)
    res = np.zeros(len(p))
    for dx in (0, 1):
        for dy in (0, 1):
            for dz in (0, 1):
                g = _GRADIENTS[_hash(i0[:, 0] + dx, i0[:, 1] + dy, i0[:, 2] + dz, seed)]
                d = f - np.array([dx, dy, dz])
                val = g[:, 0] * d[:, 0] + g[:, 1] * d[:, 1] + g[:, 2] * d[:, 2]
                wx = u[:, 0] if dx else 1 - u[:, 0]
                wy = u[:, 1] if dy else 1 - u[:, 1]
                wz = u[:, 2] if dz else 1 - u[:, 2]
                res += val * wx * wy * wz
    return res * 1.7


def ridged(p: Array, seed: int, power: float) -> Array:
    """Crest noise in [0, 1]: sharp ridges where the gradient noise crosses zero."""
    return np.clip((1.0 - 2.0 * np.abs(perlin(p, seed))) * 0.5 + 0.5, 0.0, 1.0) ** power


def flow_basis(flow: Array) -> tuple[Array, Array, Array]:
    """Orthonormal (a, b, f): f along the flow, a/b across it. For a flow along ±Z this is (X, Y, Z)."""
    f = np.asarray(flow, dtype=np.float64)
    f = f / np.linalg.norm(f)
    helper = np.array([1.0, 0.0, 0.0]) if abs(f[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    a = np.cross(helper, f)
    a /= np.linalg.norm(a)
    b = np.cross(f, a)
    if (
        abs(f[2]) > 0.999
    ):  # keep the world axes for the common gravity flow so seeds match the Chekhov script
        a, b = np.array([1.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0])
    return a, b, f


def strand_field(
    p: Array,
    *,
    seed: int = 1,
    flow: Array | None = None,
    pitch: float = 1.9,
    length: float = 11.0,
    fine_pitch: float = 1.05,
    fine_weight: float = 0.22,
    warp_scale: float = 14.0,
    warp_strength: float = 1.6,
    sharpness: float = 1.7,
) -> Array:
    """Ridged strand crests stretched along `flow`, values in [0, 1].

    `pitch` is the across-flow cell (mm), `length` the along-flow cell. A domain warp that moves
    points only across the flow makes the strands wavy without tilting them. A fine octave
    (`fine_pitch`, weight `fine_weight`) adds grain; a slow modulation varies the crest height.
    """
    a, b, f = flow_basis(np.array([0.0, 0.0, 1.0]) if flow is None else flow)
    pa, pb, pf = p @ a, p @ b, p @ f
    warp_a = perlin(p / warp_scale, seed + 10) * warp_strength
    warp_b = perlin(p / warp_scale + 31.7, seed + 11) * warp_strength
    q = np.column_stack([(pa + warp_a) / pitch, (pb + warp_b) / pitch, pf / length])
    s1 = ridged(q, seed, sharpness)
    q2 = np.column_stack(
        [(pa + warp_a) / fine_pitch, (pb + warp_b) / fine_pitch, pf / (length * 0.6)]
    )
    s2 = ridged(q2, seed + 1, 1.4)
    mod = 0.75 + 0.25 * perlin(p / 7.0, seed + 2)
    return ((1 - fine_weight) * s1 + fine_weight * s2) * np.clip(mod, 0.4, 1.0)


def clump_field(
    p: Array,
    *,
    seed: int = 1,
    flow: Array | None = None,
    pitch: float = 2.6,
    length: float = 5.0,
    strand_kwargs: dict[str, float] | None = None,
) -> Array:
    """Rounder, shorter clumps (fluffy chest / belly): ridged noise with a short strand length."""
    a, b, f = flow_basis(np.array([0.0, 0.0, 1.0]) if flow is None else flow)
    q = np.column_stack([(p @ a) / pitch, (p @ b) / pitch, (p @ f) / length])
    c = ridged(q, seed + 4, 1.3)
    return c * (
        0.8 + 0.2 * strand_field(p, seed=seed, flow=flow, **(strand_kwargs or {}))
    )
