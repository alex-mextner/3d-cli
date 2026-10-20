"""voxel.py — fast voxel occupancy of a closed mesh, with no ray-tracing dependency.

WHAT: `voxelize(mesh, pitch)` -> `Grid` (boolean occupancy, index order x, y, z, cell centres at
  origin + (i + 0.5) * pitch). `fill_enclosed` fills every air pocket that does not reach the outside;
  `disc` is the circular structuring element the overhang/adaptation code uses per layer.
HOW: scan conversion along Z. Every triangle is rasterised onto the XY grid of rays; each ray/triangle
  hit toggles the parity of all cells below it, and a reverse cumulative XOR along Z turns the toggles
  into occupancy. Vectorised numpy only (no embree/rtree), so 411k faces at 0.25 mm take seconds.
ASSUMPTIONS: the mesh is closed (run the repair step first); rays are offset by a tiny irrational
  amount so they never graze a shared edge on the regular grid.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import trimesh
from numpy.typing import NDArray
from scipy import ndimage

#: ray offset in cell units; irrational-ish so rays never sit exactly on a mesh edge or vertex
_JITTER = (0.1234567, 0.2345678)
#: candidate (triangle, ray) pairs processed per chunk; bounds peak memory (~100 bytes per pair)
_CHUNK_PAIRS = 4_000_000


@dataclass
class Grid:
    """Voxel occupancy; `occ[i, j, k]` is the cell centred at `origin + (i, j, k + 0.5 each) * pitch`."""

    occ: NDArray[np.bool_]
    origin: NDArray[np.float64]
    pitch: float

    @property
    def shape(self) -> tuple[int, int, int]:
        return (int(self.occ.shape[0]), int(self.occ.shape[1]), int(self.occ.shape[2]))

    def centres(self, axis: int) -> NDArray[np.float64]:
        """World coordinates of the cell centres along `axis`."""
        return self.origin[axis] + (np.arange(self.occ.shape[axis]) + 0.5) * self.pitch

    def index(self, p: Any, axis: int) -> NDArray[np.intp]:
        """Cell index containing world coordinate(s) `p` on `axis` (not clipped)."""
        return np.floor((np.asarray(p, float) - self.origin[axis]) / self.pitch).astype(
            np.intp
        )

    def volume_mm3(self) -> float:
        return float(self.occ.sum()) * self.pitch**3


def voxelize(mesh: trimesh.Trimesh, pitch: float = 0.25, margin: float = 2.0) -> Grid:
    """Occupancy of `mesh` on a grid of `pitch` mm with `margin` mm of air around it."""
    if pitch <= 0:
        raise ValueError(f"pitch must be positive, got {pitch}")
    lo, hi = mesh.bounds
    origin = lo - margin
    shape = np.ceil((hi + margin - origin) / pitch).astype(np.int64)
    nx, ny, nz = (int(s) for s in shape)
    # vertices in "ray index" coordinates: rays sit at integer (i, j); z of cell k's centre is w == k
    u = (np.asarray(mesh.vertices, np.float64) - origin) / pitch
    u[:, 0] -= 0.5 + _JITTER[0]
    u[:, 1] -= 0.5 + _JITTER[1]
    u[:, 2] -= 0.5
    tri = u[np.asarray(mesh.faces, np.int64)]  # (F, 3, 3)
    parity = np.zeros(nx * ny * nz, dtype=np.uint8)
    for chunk in _chunks(tri):
        keys = _hit_keys(chunk, nx, ny, nz)
        if len(keys) == 0:
            continue
        uniq, counts = np.unique(keys, return_counts=True)
        parity[uniq[counts % 2 == 1]] ^= 1
    vol = parity.reshape(nx, ny, nz)
    # cell k is inside iff an odd number of hits lie above it
    occ = np.bitwise_xor.accumulate(vol[:, :, ::-1], axis=2)[:, :, ::-1].astype(bool)
    return Grid(occ, origin.astype(np.float64), float(pitch))


def _chunks(tri: NDArray[np.float64]) -> Any:
    """Split triangles so each chunk produces about `_CHUNK_PAIRS` candidate rays."""
    span = np.ceil(tri[:, :, :2].max(axis=1)) - np.ceil(tri[:, :, :2].min(axis=1)) + 1
    pairs = np.maximum(span[:, 0], 1) * np.maximum(span[:, 1], 1)
    cum = np.cumsum(pairs)
    start = 0
    n = len(tri)
    while start < n:
        base = cum[start - 1] if start else 0.0
        stop = int(np.searchsorted(cum, base + _CHUNK_PAIRS, side="right"))
        stop = min(max(stop, start + 1), n)
        yield tri[start:stop]
        start = stop


def _hit_keys(tri: NDArray[np.float64], nx: int, ny: int, nz: int) -> NDArray[np.int64]:
    """Flat (ray, cell) keys of every ray/triangle hit; the cell is the highest one below the hit."""
    xy = tri[:, :, :2]
    i0 = np.ceil(xy[:, :, 0].min(axis=1)).astype(np.int64)
    i1 = np.floor(xy[:, :, 0].max(axis=1)).astype(np.int64)
    j0 = np.ceil(xy[:, :, 1].min(axis=1)).astype(np.int64)
    j1 = np.floor(xy[:, :, 1].max(axis=1)).astype(np.int64)
    ni = np.maximum(i1 - i0 + 1, 0)
    nj = np.maximum(j1 - j0 + 1, 0)
    counts = ni * nj
    total = int(counts.sum())
    if total == 0:
        return np.empty(0, dtype=np.int64)
    t = np.repeat(np.arange(len(tri)), counts)
    local = np.arange(total) - np.repeat(np.cumsum(counts) - counts, counts)
    di = local % ni[t]
    dj = local // ni[t]
    ri = i0[t] + di
    rj = j0[t] + dj
    a, b, c = tri[t, 0], tri[t, 1], tri[t, 2]
    # barycentric coordinates of the ray (ri, rj) in the XY projection of the triangle
    d = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (
        b[:, 1] - a[:, 1]
    )
    ok = d != 0.0
    d = np.where(ok, d, 1.0)
    px = ri - a[:, 0]
    py = rj - a[:, 1]
    l1 = (px * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * py) / d
    l2 = ((b[:, 0] - a[:, 0]) * py - px * (b[:, 1] - a[:, 1])) / d
    l0 = 1.0 - l1 - l2
    inside = ok & (l0 >= 0) & (l1 >= 0) & (l2 >= 0)
    w = l0 * a[:, 2] + l1 * b[:, 2] + l2 * c[:, 2]
    cell = (
        np.ceil(w).astype(np.int64) - 1
    )  # highest cell with centre strictly below the hit
    inside &= (ri >= 0) & (ri < nx) & (rj >= 0) & (rj < ny) & (cell >= 0)
    cell = np.minimum(cell, nz - 1)
    return (ri[inside] * ny + rj[inside]) * nz + cell[inside]


def fill_enclosed(occ: NDArray[np.bool_]) -> NDArray[np.bool_]:
    """`occ` plus every air cell that cannot reach the grid border through 6-connected air."""
    pad = np.pad(~occ, 1, constant_values=True)
    lab, _ = ndimage.label(pad)
    outside = lab == lab[0, 0, 0]
    result: NDArray[np.bool_] = ~outside[1:-1, 1:-1, 1:-1]
    return result


def disc(radius: float) -> NDArray[np.bool_]:
    """Boolean disc of `radius` cells as a 2-D structuring element (odd side, centred)."""
    k = int(np.ceil(radius))
    y, x = np.mgrid[-k : k + 1, -k : k + 1]
    result: NDArray[np.bool_] = (x * x + y * y) <= radius * radius + 1e-9
    return result
