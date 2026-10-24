"""Cross-section properties of a mesh along a span axis, for the beam path on STL / 3MF / SCAD input.

The mesh is rotated so the span runs along local z and the bending (load) direction along local x;
manifold3d slices it at evenly spaced stations; each slice's rings give area, centroid, the
second moment of area about the bending axis, and the extreme-fibre distance (Green's theorem on
the signed rings: outers positive, holes negative). A variable-section Euler-Bernoulli beam is then
integrated over these stations, so tapered and stepped parts are handled by the same code that
handles a prismatic bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import trimesh


@dataclass(frozen=True)
class Profile:
    """Section properties at stations s (mm along the span, 0 at the low end)."""

    s: np.ndarray
    area: np.ndarray
    inertia: np.ndarray  # about the bending axis, mm^4
    c_max: (
        np.ndarray
    )  # extreme fibre distance from the section centroid in the bending direction, mm
    length: float
    solid_fraction: float  # fraction of stations with material

    def at(self, values: np.ndarray, x: np.ndarray) -> np.ndarray:
        return np.interp(x, self.s, values)  # type: ignore[no-any-return]


def _trapz(y: np.ndarray, x: np.ndarray) -> float:
    return float(np.sum((y[1:] + y[:-1]) * np.diff(x)) / 2.0)


def _frame(span: np.ndarray, bend: np.ndarray) -> np.ndarray:
    ez = np.asarray(span, dtype=float) / np.linalg.norm(span)
    ex = np.asarray(bend, dtype=float) - (np.asarray(bend, dtype=float) @ ez) * ez
    ex /= np.linalg.norm(ex)
    ey = np.cross(ez, ex)
    return np.vstack([ex, ey, ez])


def _ring_moments(ring: np.ndarray) -> tuple[float, float, float, float]:
    """Signed area, first moment about x (sum x dA), and second moment about origin  sum x^2 dA for one ring."""
    x, y = ring[:, 0], ring[:, 1]
    x2, y2 = np.roll(x, -1), np.roll(y, -1)
    cross = x * y2 - x2 * y
    area = 0.5 * float(cross.sum())
    qx = float(((x + x2) * cross).sum()) / 6.0
    ixx = float(((x * x + x * x2 + x2 * x2) * cross).sum()) / 12.0
    return area, qx, ixx, float(x.max() - x.min())


def profile(
    surface: trimesh.Trimesh, span: np.ndarray, bend: np.ndarray, stations: int = 41
) -> Profile:
    from manifold3d import Manifold, Mesh

    rot = _frame(span, bend)
    verts = np.asarray(surface.vertices) @ rot.T
    manifold = Manifold(
        Mesh(
            vert_properties=np.asarray(verts, dtype=np.float32),
            tri_verts=np.asarray(surface.faces, dtype=np.uint32),
        )
    )
    z0, z1 = float(verts[:, 2].min()), float(verts[:, 2].max())
    length = z1 - z0
    margin = 0.01 * length
    s = np.linspace(margin, length - margin, stations)
    area = np.zeros(stations)
    inertia = np.zeros(stations)
    c_max = np.zeros(stations)
    for i, si in enumerate(s):
        rings = manifold.slice(z0 + float(si)).to_polygons()
        a = q = ixx = 0.0
        xmin, xmax = np.inf, -np.inf
        for ring in rings:
            ra, rq, ri, _ = _ring_moments(np.asarray(ring, dtype=float))
            a += ra
            q += rq
            ixx += ri
            xmin, xmax = (
                min(xmin, float(ring[:, 0].min())),
                max(xmax, float(ring[:, 0].max())),
            )
        if a > 1e-9:
            cx = q / a
            area[i] = a
            inertia[i] = ixx - a * cx * cx
            c_max[i] = max(cx - xmin, xmax - cx)
    return Profile(
        s=s,
        area=area,
        inertia=inertia,
        c_max=c_max,
        length=length,
        solid_fraction=float(np.mean(area > 1e-9)),
    )


def cantilever(
    profile_: Profile, force: float, modulus: float
) -> tuple[float, float, float, float]:
    """Tip load P at s = L, clamp at s = 0. Returns (tip deflection, max bending stress, its station, max shear stress).

    delta = integral of M(s) (L - s) / (E I(s)) ds with M = P (L - s); sigma = M c / I; tau ~ 1.5 P / A (rectangular peak).
    """
    length = profile_.length
    s = np.linspace(0.0, length, 801)
    inertia = np.maximum(profile_.at(profile_.inertia, s), 1e-12)
    c = profile_.at(profile_.c_max, s)
    area = np.maximum(profile_.at(profile_.area, s), 1e-12)
    moment = force * (length - s)
    delta = _trapz(moment * (length - s) / (modulus * inertia), s)
    sigma = moment * c / inertia
    k = int(np.argmax(sigma))
    return delta, float(sigma[k]), float(s[k]), float(np.max(1.5 * force / area))


def simple_beam(
    profile_: Profile, force: float, modulus: float
) -> tuple[float, float, float, float]:
    """Centre point load F on a simply supported span. Returns (mid-span deflection, max stress, station, max shear)."""
    length = profile_.length
    s = np.linspace(0.0, length, 801)
    inertia = np.maximum(profile_.at(profile_.inertia, s), 1e-12)
    c = profile_.at(profile_.c_max, s)
    area = np.maximum(profile_.at(profile_.area, s), 1e-12)
    moment = force * np.minimum(s, length - s) / 2.0
    unit = np.minimum(s, length - s) / 2.0
    delta = _trapz(moment * unit / (modulus * inertia), s)
    sigma = moment * c / inertia
    k = int(np.argmax(sigma))
    return (
        delta,
        float(sigma[k]),
        float(s[k]),
        float(np.max(1.5 * (force / 2.0) / area)),
    )
