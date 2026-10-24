"""Tet meshing for the FEA path: fTetWild (pytetwild) on a trimesh surface, sized from the part.

Sizing is driven by the thinnest wall, not only by an element budget: a snap arm one element
thick has no bending stiffness worth trusting. The estimate is the 10th percentile of the
inward ray-hit distance from surface samples (robust to a few thin fillets). When resolving the
thinnest wall would exceed the element budget, the budget wins and the report carries a warning.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
from cli.resources import max_workers
from errors import MissingDependency, ThreeDError

if TYPE_CHECKING:
    import trimesh

# Edge length of the regular tetrahedron with unit volume factor: V = a^3 / (6 sqrt 2).
_TET_VOLUME_FACTOR = 1.0 / (6.0 * math.sqrt(2.0))
# fTetWild yields ~2x more tets than the regular-tet estimate once it grades toward the surface.
_FTETWILD_DENSITY = 2.0


class MeshError(ThreeDError):
    """The surface could not be tetrahedralised. Exit 2 (bad input geometry)."""

    exit_code = 2


@dataclass(frozen=True)
class TetMesh:
    """A tetrahedral mesh: points (n, 3) in mm, tets (m, 4) positively oriented."""

    points: np.ndarray
    tets: np.ndarray
    edge_length_mm: float
    min_thickness_mm: float | None
    envelope_mm: float = 0.0
    warnings: list[str] = field(default_factory=list)


def estimate_min_thickness(
    surface: trimesh.Trimesh, samples: int = 2000
) -> float | None:
    """10th percentile of the wall thickness measured by casting rays inward from surface samples."""
    import trimesh

    if surface.is_empty or surface.area <= 0.0:
        return None
    points, face_index = trimesh.sample.sample_surface(surface, samples, seed=7)
    normals = surface.face_normals[face_index]
    origins = points - normals * 1e-4
    try:
        locations, ray_index, _ = surface.ray.intersects_location(
            origins, -normals, multiple_hits=False
        )
    except (
        Exception
    ):  # ray backend missing/failing: sizing falls back to the element budget
        return None
    if len(ray_index) == 0:
        return None
    distances = np.linalg.norm(locations - origins[ray_index], axis=1)
    distances = distances[distances > 1e-3]
    if distances.size == 0:
        return None
    return float(np.percentile(distances, 10.0))


def edge_length_for_budget(volume_mm3: float, target_tets: int) -> float:
    """Edge length that gives roughly `target_tets` tets in `volume_mm3` with fTetWild's grading."""
    return float(
        (volume_mm3 * _FTETWILD_DENSITY / (_TET_VOLUME_FACTOR * max(target_tets, 1)))
        ** (1.0 / 3.0)
    )


def choose_edge_length(
    surface: trimesh.Trimesh,
    *,
    target_tets: int,
    explicit_mm: float | None,
    elements_per_wall: float,
) -> tuple[float, float | None, list[str]]:
    """Pick the edge length (mm). Returns (edge, thinnest wall or None, warnings)."""
    warnings: list[str] = []
    thin = estimate_min_thickness(surface)
    if explicit_mm is not None:
        if thin is not None and explicit_mm > thin / 1.5:
            warnings.append(
                f"mesh edge {explicit_mm:g} mm is coarse for the thinnest wall (~{thin:.2f} mm): "
                "bending stiffness and stress there are under-resolved"
            )
        return explicit_mm, thin, warnings
    budget_edge = edge_length_for_budget(abs(float(surface.volume)), target_tets)
    if thin is None:
        return budget_edge, None, warnings
    wanted = thin / elements_per_wall
    if wanted >= budget_edge:
        return budget_edge, thin, warnings
    cost_ratio = (budget_edge / wanted) ** 3
    if cost_ratio <= 2.5:
        return wanted, thin, warnings
    warnings.append(
        f"thinnest wall ~{thin:.2f} mm would need a {wanted:.2f} mm mesh ({cost_ratio:.0f}x the element budget "
        f"of {target_tets}); using {budget_edge:.2f} mm, so thin regions are under-resolved. "
        "Pass --mesh-size or --max-elements to refine, or use --crop to analyse the thin feature alone"
    )
    return budget_edge, thin, warnings


def tetrahedralize(
    surface: trimesh.Trimesh,
    edge_length_mm: float,
    *,
    min_thickness_mm: float | None = None,
) -> TetMesh:
    """fTetWild tet mesh of a closed surface. Raises MissingDependency / MeshError."""
    try:
        import pytetwild
    except ImportError as exc:
        raise MissingDependency(
            "pytetwild",
            install='uv sync --extra fea   # or: uv pip install "pytetwild>=0.2,<0.3" scikit-fem',
            degrades="`3d strength --method fea` (the closed-form beam method still works)",
            command="strength",
        ) from exc
    bounds = surface.bounds
    diagonal = float(np.linalg.norm(bounds[1] - bounds[0]))
    if diagonal <= 0.0:
        raise MeshError("the part has zero extent", command="strength")
    # Envelope: how far fTetWild may move the surface. 3 % of the smaller of wall thickness and edge length.
    envelope_abs = 0.03 * min(
        edge_length_mm, min_thickness_mm if min_thickness_mm else edge_length_mm
    )
    epsilon = max(envelope_abs / diagonal, 1e-6)
    try:
        points, tets = pytetwild.tetrahedralize(
            np.ascontiguousarray(surface.vertices, dtype=np.float64),
            np.ascontiguousarray(surface.faces, dtype=np.int32),
            edge_length_abs=edge_length_mm,
            epsilon=epsilon,
            optimize=True,
            num_opt_iter=30,
            num_threads=max_workers(),
            quiet=True,
        )
    except (
        Exception
    ) as exc:  # fTetWild raises RuntimeError/ValueError on degenerate input
        raise MeshError(
            f"tet meshing failed: {exc}",
            command="strength",
            remediation=[
                "Check the part with `3d check --mesh`; repair it with `3d mesh repair`."
            ],
        ) from exc
    points = np.asarray(points, dtype=np.float64)
    tets = np.asarray(tets, dtype=np.int64)
    if len(tets) == 0:
        raise MeshError("tet meshing produced no elements", command="strength")
    p = points[tets]
    volume = np.einsum(
        "ij,ij->i", np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), p[:, 3] - p[:, 0]
    )
    flip = volume < 0.0
    if np.any(flip):
        tets[flip] = tets[flip][:, [0, 2, 1, 3]]
    return TetMesh(
        points=points,
        tets=tets,
        edge_length_mm=edge_length_mm,
        min_thickness_mm=min_thickness_mm,
        envelope_mm=envelope_abs,
    )
