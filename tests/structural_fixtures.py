"""Shared builders for the structural-solver tests: bar / tapered / pipe meshes and a one-call FEA solve."""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest
import trimesh
from materials import get_material
from structural import fdm, fea
from structural import mesh as meshing


def card(name: str = "PLA") -> fdm.FdmCard:
    return fdm.FdmCard.from_material(get_material(name))


def grid_tetrahedralize(
    surface: trimesh.Trimesh,
    edge_length_mm: float,
    *,
    min_thickness_mm: float | None = None,
) -> meshing.TetMesh:
    """A deterministic stand-in for `meshing.tetrahedralize` on a box: its bounding box in cells of about
    `edge_length_mm`, each cut into the 6 tets of its main diagonal (the mesh conforms across cells).

    fTetWild returns a different mesh on every call, even on one thread, and a peak stress at a clamp or a
    load patch moves by tens of percent with it: a test that compares two analyses by stress uses this.
    Only boxes: anything else is refused, and the mesh checks itself (positive tets filling the box)."""
    lo, hi = surface.bounds
    box_volume = float(np.prod(hi - lo))
    assert math.isclose(abs(surface.volume), box_volume, rel_tol=1e-6), (
        "the grid stand-in meshes boxes only"
    )
    # the epsilon keeps an extent that is an exact multiple of the edge from gaining a cell
    cells = [
        max(1, math.ceil((hi[a] - lo[a]) / edge_length_mm - 1e-9)) for a in range(3)
    ]
    dims = [c + 1 for c in cells]
    nodes = np.arange(math.prod(dims)).reshape(dims)
    axes = [np.linspace(lo[a], hi[a], dims[a]) for a in range(3)]
    points = np.stack(np.meshgrid(*axes, indexing="ij"), axis=-1).reshape(-1, 3)
    tets = []
    paths = list(
        itertools.permutations(range(3))
    )  # the 6 monotone paths corner 000 -> 111
    for cell in itertools.product(*(range(c) for c in cells)):
        for order in paths:
            corner = list(cell)
            path = [nodes[tuple(corner)]]
            for axis in order:
                corner[axis] += 1
                path.append(nodes[tuple(corner)])
            tets.append(path)
    elements = np.array(tets, dtype=np.int64)
    p = points[elements]
    volume = np.einsum(
        "ij,ij->i", np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), p[:, 3] - p[:, 0]
    )
    flip = volume < 0.0
    elements[flip] = elements[flip][:, [0, 2, 1, 3]]
    assert math.isclose(float(np.abs(volume).sum()) / 6.0, box_volume, rel_tol=1e-9)
    assert np.all(np.abs(volume) > 0.0)
    return meshing.TetMesh(
        points=points,
        tets=elements,
        edge_length_mm=edge_length_mm,
        min_thickness_mm=min_thickness_mm,
    )


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


@pytest.fixture
def grid_mesher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests that compare an analysis by stress or strain peaks run on the deterministic grid mesh of box parts.

    fTetWild meshes differ from call to call (even on one thread) and a peak at a clamp / load patch moves by
    tens of percent with them (40 runs of the assembled bracket: assembled / alone = 0.34 .. 1.05, bound 0.75;
    2 of 50 runs of the orientation test broke its bound of 0.6; 5 of 40 of the snap-hook band strain broke
    its 10 %). What such a test compares is then the model, not mesh luck."""
    monkeypatch.setattr(meshing, "tetrahedralize", grid_tetrahedralize)
