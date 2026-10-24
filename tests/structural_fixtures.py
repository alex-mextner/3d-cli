"""Shared builders for the structural-solver tests: bar / tapered / pipe meshes and a one-call FEA solve."""

from __future__ import annotations

import numpy as np
import trimesh
from materials import get_material
from structural import fdm, fea
from structural import mesh as meshing


def card(name: str = "PLA") -> fdm.FdmCard:
    return fdm.FdmCard.from_material(get_material(name))


def bar(
    length: float, width: float, height: float, *, x0: float = 0.0
) -> trimesh.Trimesh:
    """Box spanning x in [x0, x0 + length], centred in y and z."""
    box = trimesh.creation.box(extents=[length, width, height])
    box.apply_translation([x0 + length / 2.0, 0.0, 0.0])
    return box


def tapered_bar(
    length: float, width: float, root_h: float, tip_h: float
) -> trimesh.Trimesh:
    """Bar whose thickness (z) tapers linearly from root_h at x=0 to tip_h at x=length."""
    pts = [
        [x, y, z]
        for x, h in ((0.0, root_h), (length, tip_h))
        for y in (-width / 2, width / 2)
        for z in (-h / 2, h / 2)
    ]
    return trimesh.convex.convex_hull(np.array(pts))


def pipe(r_in: float, r_out: float, height: float) -> trimesh.Trimesh:
    """Hollow cylinder about z, centred on the origin, z from 0 to height."""
    pipe_mesh = trimesh.creation.annulus(
        r_min=r_in, r_max=r_out, height=height, sections=48
    )
    pipe_mesh.apply_translation([0.0, 0.0, height / 2.0])
    return pipe_mesh


def end_slab(axis: tuple[float, float, float], at: float) -> fea.Region:
    """Boundary facets of a planar end cap at coordinate `at` along `axis`."""
    return fea.Region("slab", axis=axis, lo=at - 1e-6, hi=at + 1e-6, min_alignment=0.9)


def solve(
    parts: list[tuple[str, trimesh.Trimesh]],
    case: fea.LoadCase,
    *,
    material: str = "PLA",
    build_axis: tuple[float, float, float] = (0.0, 0.0, 1.0),
    edge: float,
    order: int = 2,
    pairs: list[fea.ContactPair] | None = None,
    repeated_use: bool = False,
) -> tuple[fea.CaseSolution, fea.Problem]:
    bodies = []
    for name, surface in parts:
        tet = meshing.tetrahedralize(surface, edge)
        bodies.append(
            fea.build_body(name, tet, card(material), np.array(build_axis), order=order)
        )
    problem = fea.assemble_problem(bodies, pairs or [])
    return fea.solve_case(problem, case, repeated_use=repeated_use), problem


def tip_deflection(sol: fea.CaseSolution, body: str, x_tip: float, comp: int) -> float:
    """Mean displacement component at the nodes of `body` on the plane x = x_tip."""
    field = next(f for f in sol.fields if f.body.name == body)
    nodes = field.body.mesh.p
    return float(field.u[nodes[0] > x_tip - 1e-6][:, comp].mean())
