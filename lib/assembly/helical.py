"""helical.py — radial clearance of two coaxial features (threads, ribs, keys, bores) from their surface profiles.

WHAT: a feature is the surface of one part around a common axis (the wall of a bore, the flank of a boss). Its PROFILE is
  the surface radius as a function of the phase `u` (one turn = 1):
    thread (pitch p > 0)   u = z/p -+ theta/2pi  - constant along a helix, so a perfect single-start thread is one
                           number per u; the sign is the handedness (right: u = z/p - theta/2pi),
    ribs / keys (p = 0)    u = theta/2pi        - the radius against the angle, whatever the height.
  The female profile (the bore, material outside the surface) and the male profile (the boss, material inside) give
  the radial gap `rf(u) - rm(u)` - at the posed phase, and at every other relative phase `d`, which is exactly the
  screwing (rotation = axial advance for a thread; rotation for ribs). The best phase, how much of the turn is free
  (axial play = fraction * pitch) and the crest / root clearances are read off that.
WHY: interpenetration volume and the minimum 3D gap say a thread BINDS; they do not say which feature of it, nor
  whether it screws at all (wrong pitch or handedness, a root fuller than the crest of the partner). The per-phase
  radial clearance does, and it works the same on the model and on the printed wall paths (`printed.py`).
INVARIANTS: the radius of a bin is the MEDIAN of the points in it (robust against the first-layer flare and stray
  segments); `coherence_mm` is the 90th percentile of the distance of the points to their bin's median - a wrong pitch
  or handedness shows as a large value; empty bins are NaN and ignored by the gap search, and `coverage` says how
  many there are. All coordinates are in the AXIS frame (axis = +z, origin on the axis).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt

from gcode_walls import densify_segments

BINS = 360
MIN_POINTS_PER_BIN = 3
TIE_MM = 5e-4  # phases whose gap is within this of the best are equally good
HAND_DECISIVE_RATIO = 0.8  # the wrong handedness must be this much less coherent for the pick to count as decided

Array = npt.NDArray[np.float64]
Matrix = npt.NDArray[np.float64]


def axis_frame(origin: Array, direction: Array) -> Matrix:
    """4x4 matrix taking assembly coordinates to the axis frame: `origin` -> 0, `direction` -> +z."""
    z = np.asarray(direction, dtype=float)
    z = z / np.linalg.norm(z)
    helper = np.array([1.0, 0.0, 0.0]) if abs(z[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    x = helper - z * float(helper @ z)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    rot = np.vstack([x, y, z])
    out = np.eye(4)
    out[:3, :3] = rot
    out[:3, 3] = -rot @ np.asarray(origin, dtype=float)
    return out


def apply(matrix: Matrix, pts: Array) -> Array:
    return pts @ matrix[:3, :3].T + matrix[:3, 3]


def cylindrical(pts: Array) -> tuple[Array, Array, Array]:
    """(r, theta, z) of axis-frame points."""
    return np.hypot(pts[:, 0], pts[:, 1]), np.arctan2(pts[:, 1], pts[:, 0]), pts[:, 2]


def window(
    pts: Array, z_range: tuple[float, float] | None, r_range: tuple[float, float] | None
) -> Array:
    """The axis-frame points inside a z window and a radius window (None = unbounded)."""
    keep = np.ones(len(pts), dtype=bool)
    if z_range is not None:
        keep &= (pts[:, 2] >= z_range[0]) & (pts[:, 2] <= z_range[1])
    if r_range is not None:
        r = np.hypot(pts[:, 0], pts[:, 1])
        keep &= (r >= r_range[0]) & (r <= r_range[1])
    return pts[keep]


def phase(theta: Array, z: Array, pitch: float, hand: int) -> Array:
    """The phase u in [0, 1): `hand` +1 right-handed, -1 left-handed; pitch 0 -> the angle alone."""
    turns = theta / (2.0 * math.pi)
    if pitch <= 0.0:
        return np.asarray(turns % 1.0)
    return np.asarray((z / pitch - hand * turns) % 1.0)


@dataclass
class Profile:
    """Radius against phase for one feature (`radius[k]` for bin k of BINS; NaN = no data)."""

    radius: Array
    counts: Array
    coherence_mm: float
    pitch_mm: float
    hand: int  # +1 right, -1 left, 0 none (ribs)

    @property
    def coverage(self) -> float:
        return float(np.mean(np.isfinite(self.radius)))

    @property
    def r_min(self) -> float:
        return float(np.nanmin(self.radius))

    @property
    def r_max(self) -> float:
        return float(np.nanmax(self.radius))


ENVELOPE_PERCENTILE = {"outer": 90.0, "inner": 10.0}  # the blocking side of a bin: a boss's largest, a bore's smallest radius


def build_profile(
    pts: Array,
    pitch: float,
    hand: int,
    bins: int = BINS,
    offset: Array | None = None,
    side: str | None = None,
) -> Profile:
    """Radius per phase bin of axis-frame points; `offset` is added to each point's radius (half line width).

    A bin of a helical surface holds one radius (the points of a perfect thread agree to a few um; the spread left is
    mesh faceting and layer noise), so a thread takes the MEDIAN. Ribs and keys (pitch 0) have steep walls, where one
    angle bin spans a range of radii: there the envelope that blocks is kept - the 90th percentile of a boss
    (`side='outer'`), the 10th of a bore (`'inner'`)."""
    r, theta, z = cylindrical(pts)
    if offset is not None:
        r = r + offset
    u = phase(theta, z, pitch, hand)
    b = np.minimum((u * bins).astype(int), bins - 1)
    order = np.argsort(b, kind="stable")
    bs, rs = b[order], r[order]
    cuts = np.searchsorted(bs, np.arange(bins + 1))
    radius = np.full(bins, np.nan)
    counts = np.zeros(bins)
    spread: list[Array] = []
    for k in range(bins):
        seg = rs[cuts[k] : cuts[k + 1]]
        counts[k] = len(seg)
        if len(seg) >= MIN_POINTS_PER_BIN:
            med = float(np.median(seg))
            radius[k] = (
                med
                if pitch > 0 or side not in ENVELOPE_PERCENTILE
                else float(np.percentile(seg, ENVELOPE_PERCENTILE[side]))
            )
            spread.append(np.abs(seg - med))
    coherence = float(np.percentile(np.concatenate(spread), 90)) if spread else math.nan
    return Profile(radius, counts, coherence, pitch, hand)


def pick_handedness(
    pts: Array,
    pitch: float,
    requested: str,
    offset: Array | None = None,
    side: str | None = None,
) -> tuple[Profile, dict[str, Any]]:
    """The profile of the handedness that explains the points best (or the requested one), with how decisive it was."""
    if pitch <= 0.0:
        prof = build_profile(pts, 0.0, 0, offset=offset, side=side)
        return prof, {"hand": "none", "decided": True, "coherence_mm": {}}
    right = build_profile(pts, pitch, +1, offset=offset, side=side)
    left = build_profile(pts, pitch, -1, offset=offset, side=side)
    spreads = {
        "right": round(right.coherence_mm, 4),
        "left": round(left.coherence_mm, 4),
    }
    best = right if right.coherence_mm <= left.coherence_mm else left
    other = left if best is right else right
    decided = (
        np.isfinite(best.coherence_mm)
        and np.isfinite(other.coherence_mm)
        and best.coherence_mm < HAND_DECISIVE_RATIO * other.coherence_mm
    )
    if requested in ("right", "left"):
        chosen = right if requested == "right" else left
    else:
        chosen = best
    info = {
        "hand": "right" if chosen.hand > 0 else "left",
        "decided": bool(decided),
        "coherence_mm": spreads,
        "best_fit": "right" if best.hand > 0 else "left",
    }
    return chosen, info


def fit_axis_offset(pts: Array) -> tuple[float, float, float]:
    """Least-squares circle through the points' xy: (dx, dy, residual rms) - how far the feature's own axis is from
    the declared one (a feature that is not concentric with the axis it is judged on cannot be read radially)."""
    x, y = pts[:, 0], pts[:, 1]
    a = np.column_stack([2 * x, 2 * y, np.ones(len(x))])
    sol, *_ = np.linalg.lstsq(a, x * x + y * y, rcond=None)
    cx, cy = float(sol[0]), float(sol[1])
    r = math.sqrt(max(sol[2] + cx * cx + cy * cy, 0.0))
    rms = float(np.sqrt(np.mean((np.hypot(x - cx, y - cy) - r) ** 2)))
    return cx, cy, rms


@dataclass
class Gap:
    """The radial gap of the female over the male profile at one relative phase."""

    min_mm: float
    mean_mm: float
    at_bin: int


def gap_at(female: Array, male: Array, shift: int = 0) -> Gap:
    """Radial gap female - male with the male turned by `shift` bins; NaN bins on either side are skipped."""
    g = female - np.roll(male, shift)
    ok = np.isfinite(g)
    if not ok.any():
        return Gap(math.nan, math.nan, -1)
    k = int(np.nanargmin(g))
    return Gap(float(g[k]), float(np.nanmean(g)), k)


def sliding(female: Array, male: Array) -> Array:
    """The minimum radial gap at every relative phase (index = shift in bins)."""
    n = len(female)
    return np.array([gap_at(female, male, d).min_mm for d in range(n)])


@dataclass
class Clearance:
    """The per-feature radial clearance of a female/male profile pair."""

    pitch_mm: float
    at_pose: Gap
    best_shift_bins: int
    best_gap_mm: float
    free_fraction: float
    crest_male_to_root_female_mm: float
    crest_female_to_root_male_mm: float
    by_shift: Array = field(repr=False, default_factory=lambda: np.zeros(0))

    @property
    def free_play(self) -> tuple[float, str]:
        """The free travel at the best position: mm along the axis for a thread, degrees for ribs."""
        if self.pitch_mm > 0:
            return self.free_fraction * self.pitch_mm, "mm axial"
        return self.free_fraction * 360.0, "deg"


def clearance(female: Profile, male: Profile) -> Clearance:
    by = sliding(female.radius, male.radius)
    if np.isfinite(by).any():
        best_gap = float(np.nanmax(by))
        ties = np.flatnonzero(by >= best_gap - TIE_MM)  # a symmetric part is best at several phases: take the nearest
        best = int(ties[np.argmin(np.minimum(ties, len(by) - ties))])
        free = float(np.nansum(by > 0) / np.isfinite(by).sum())
    else:
        best, best_gap, free = 0, math.nan, math.nan
    return Clearance(
        female.pitch_mm,
        gap_at(female.radius, male.radius, 0),
        best,
        best_gap,
        free,
        float(np.nanmax(female.radius) - np.nanmax(male.radius)),
        float(np.nanmin(female.radius) - np.nanmin(male.radius)),
        by,
    )


def shift_to_motion(shift_bins: int, pitch: float, hand: int, bins: int = BINS) -> dict[str, float]:
    """What moving the MALE part by `shift_bins` bins of phase means, the shorter way round.

    A thread: an axial advance `axial_mm` (u = z/p - hand*theta/2pi: +dz moves a feature by +dz/p turns) or, with the
    axial position kept (a part that seats on a face), the turn `rotate_deg` that has the same effect (a turn by phi
    moves a feature by -hand*phi/360 turns). Ribs: only the turn (u = theta/2pi). The sign follows `roll(male, shift)`:
    bin k of the rolled profile is bin k - shift of the male, i.e. the male profile moves toward higher u."""
    turns = shift_bins / bins
    if turns > 0.5:
        turns -= 1.0
    if pitch > 0:
        return {
            "rotate_deg": round(-hand * turns * 360.0, 3) + 0.0,
            "axial_mm": round(turns * pitch, 4) + 0.0,
            "turns": round(turns, 4) + 0.0,
        }
    return {"rotate_deg": round(turns * 360.0, 3) + 0.0, "turns": round(turns, 4) + 0.0}


def mesh_wall_points(
    mesh_axis_frame: Any,
    z_range: tuple[float, float],
    r_range: tuple[float, float] | None,
    step_mm: float,
    spacing_mm: float,
) -> Array:
    """Surface points of a mesh already in the axis frame: its sections every `step_mm` along z, densified, windowed."""
    from trimesh import intersections

    z0, z1 = z_range
    heights = np.arange(z0 + step_mm / 2.0, z1, step_mm)
    if len(heights) == 0:
        heights = np.array([(z0 + z1) / 2.0])
    lines, to_3d, _ = intersections.mesh_multiplane(
        mesh_axis_frame, np.zeros(3), np.array([0.0, 0.0, 1.0]), heights
    )
    out: list[Array] = []
    for seg, mat in zip(lines, to_3d, strict=True):
        if len(seg) == 0:
            continue
        seg = np.asarray(seg, dtype=float)
        pts2, _ = densify_segments(seg[:, 0, :], seg[:, 1, :], spacing_mm)
        pts3 = (
            np.column_stack([pts2, np.zeros(len(pts2)), np.ones(len(pts2))])
            @ np.asarray(mat).T
        )
        out.append(pts3[:, :3])
    pts = np.vstack(out) if out else np.zeros((0, 3))
    return window(pts, None, r_range)
