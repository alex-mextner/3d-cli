"""Linear-elastic FEA of an assembled model: tet meshes, anisotropic FDM material, tied / frictionless contact.

scikit-fem supplies the mesh topology, the P1/P2 vector basis, DOF numbering and facet
quadrature; the element stiffness, contact penalties and stress recovery are assembled here with
numpy so the transversely isotropic stiffness, per-element infill scaling and the node-to-surface
contact need no custom scikit-fem forms. Units: mm, N, MPa (so density is in tonne/mm^3).

Method (all of it is stated in the report's `assumptions[]`):
  * small strain, linear elastic, static; no creep / fatigue / plasticity / buckling
  * contact is node-to-surface at element corner nodes, enforced by a penalty: `tied` bonds the
    slave node to the master surface point (3 components); `frictionless` keeps the normal gap
    non-negative with an active-set iteration and lets the surfaces slide
  * stress is evaluated at the four vertices of every tet (exact for P2, constant for P1)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla
from errors import MissingDependency, ThreeDError
from structural import fdm
from structural.mesh import TetMesh

if TYPE_CHECKING:
    from skfem import Basis, MeshTet

MAX_DOFS = 450_000
_GRAVITY_MM_S2 = 9810.0
_FLUID_DENSITY_UNIT = 1.0e-9  # g/cm^3 -> tonne/mm^3


class SolveError(ThreeDError):
    """The model cannot be solved (under-constrained, too large). Exit 2: the input is wrong."""

    exit_code = 2


# ---- regions and specs ------------------------------------------------------------------------
@dataclass(frozen=True)
class Region:
    """Where on a body's surface a load or support acts.

    kind "sphere": boundary facets whose centroid is within `radius` of `center`.
    kind "line": boundary NODES within `radius` (0 = 0.4 x the mesh edge) of the line through `center` along
    `axis`, optionally limited to [lo, hi] along it; kind "point": the single boundary node nearest `center`.
    Both are for supports only (a roller line / a rigid-body pin): a facet patch would clamp the rotation.
    kind "annulus": boundary facets whose centroid lies between radii `lo` and `hi` from the line through
    `center` along `axis` (the bore or the outside of a pipe).
    kind "slab": boundary facets whose centroid along `axis` lies in [lo, hi] and whose outward
    normal makes |cos| >= `min_alignment` with `normal_axis` (default: `axis`), optionally with a
    required sign (end faces of a beam, or the top / bottom faces of a strip).
    """

    kind: str
    center: tuple[float, float, float] = (0.0, 0.0, 0.0)
    radius: float = 0.0
    axis: tuple[float, float, float] = (0.0, 0.0, 1.0)
    lo: float = 0.0
    hi: float = 0.0
    min_alignment: float = 0.0
    normal_axis: tuple[float, float, float] | None = None
    normal_sign: float = 0.0  # +1 / -1: also require sign(normal . normal_axis); 0 = either

    def mask(self, centroids: np.ndarray, normals: np.ndarray, slack: float = 0.0) -> np.ndarray:
        """Boolean facet mask; `slack` (mm) widens the region by the meshing envelope the surface may have moved."""
        if self.kind == "sphere":
            distance = np.linalg.norm(centroids - np.asarray(self.center), axis=1)
            return np.asarray(distance <= self.radius + slack)
        axis = np.asarray(self.axis, dtype=float)
        if self.kind == "annulus":
            rel = centroids - np.asarray(self.center)
            radius = np.linalg.norm(rel - np.outer(rel @ axis, axis), axis=1)
            return np.asarray((radius >= self.lo - slack) & (radius <= self.hi + slack))
        coord = centroids @ axis
        normal_axis = np.asarray(self.normal_axis if self.normal_axis is not None else self.axis, dtype=float)
        cos = normals @ normal_axis
        ok = (coord >= self.lo - slack) & (coord <= self.hi + slack) & (np.abs(cos) >= self.min_alignment)
        if self.normal_sign:
            ok &= cos * self.normal_sign > 0.0
        return np.asarray(ok)

    def describe(self) -> str:
        if self.kind == "line":
            return f"line through ({self.center[0]:g}, {self.center[1]:g}, {self.center[2]:g}) along ({self.axis[0]:g}, {self.axis[1]:g}, {self.axis[2]:g})"
        if self.kind == "point":
            return f"node nearest ({self.center[0]:g}, {self.center[1]:g}, {self.center[2]:g})"
        if self.kind == "annulus":
            return f"annulus r={self.lo:g}..{self.hi:g} mm about ({self.axis[0]:g}, {self.axis[1]:g}, {self.axis[2]:g})"
        if self.kind == "sphere":
            return f"sphere r={self.radius:g} mm at ({self.center[0]:g}, {self.center[1]:g}, {self.center[2]:g})"
        return f"slab {self.lo:g}..{self.hi:g} mm along ({self.axis[0]:g}, {self.axis[1]:g}, {self.axis[2]:g})"


@dataclass(frozen=True)
class Load:
    """One load on a region. kind: force (N, total), pressure (MPa, normal inward), displacement (mm), gravity (m/s^2)."""

    name: str
    kind: str
    region: Region | None
    vector: tuple[float, float, float] = (0.0, 0.0, 0.0)
    pressure_mpa: float = 0.0
    part: str | None = None
    components: str = "xyz"  # displacement loads prescribe only these components


@dataclass(frozen=True)
class Support:
    """Fixed support: displacement components held at zero on a region ("xyz" = fully fixed)."""

    name: str
    region: Region
    components: str = "xyz"
    part: str | None = None


@dataclass(frozen=True)
class LoadCase:
    name: str
    loads: tuple[Load, ...]
    supports: tuple[Support, ...]


@dataclass(frozen=True)
class ContactPair:
    a: str
    b: str
    kind: str  # tied | frictionless
    tolerance_mm: float | None = None
    origin: str = "declared"
    fit_type: str | None = None


# ---- bodies -----------------------------------------------------------------------------------
@dataclass
class BodyModel:
    name: str
    card: fdm.FdmCard
    build_axis: np.ndarray
    tet: TetMesh
    mesh: MeshTet
    basis: Basis
    cmat: np.ndarray
    stiff_scale: np.ndarray
    strength_scale: np.ndarray
    offset: int = 0
    facet_ids: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    facet_centroids: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))
    facet_normals: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))
    facet_areas: np.ndarray = field(default_factory=lambda: np.zeros(0))
    dof_component: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int8))

    @property
    def ndof(self) -> int:
        return int(self.basis.N)


def _require_skfem() -> Any:
    try:
        import skfem
    except ImportError as exc:
        raise MissingDependency(
            "scikit-fem",
            install='uv sync --extra fea   # or: uv pip install scikit-fem "pytetwild>=0.2,<0.3"',
            degrades="`3d strength --method fea` (the closed-form beam method still works)",
            command="strength",
        ) from exc
    return skfem


def _boundary_geometry(
    mesh: MeshTet,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Boundary facet ids, centroids (n,3), outward unit normals (n,3), areas (n,)."""
    ids = mesh.boundary_facets()
    nodes = mesh.facets[:, ids]
    p = mesh.p
    a, b, c = p[:, nodes[0]].T, p[:, nodes[1]].T, p[:, nodes[2]].T
    cross = np.cross(b - a, c - a)
    area = 0.5 * np.linalg.norm(cross, axis=1)
    normal = cross / np.maximum(2.0 * area, 1e-300)[:, None]
    centroid = (a + b + c) / 3.0
    element = mesh.f2t[0, ids]
    opposite = np.empty((len(ids), 3))
    for i in range(len(ids)):
        verts = mesh.t[:, element[i]]
        other = verts[~np.isin(verts, nodes[:, i])][0]
        opposite[i] = p[:, other]
    flip = np.einsum("ij,ij->i", normal, centroid - opposite) < 0.0
    normal[flip] *= -1.0
    return np.asarray(ids), centroid, normal, area


def build_body(
    name: str,
    tet: TetMesh,
    card: fdm.FdmCard,
    build_axis: np.ndarray,
    *,
    order: int,
    stiff_scale: np.ndarray | None = None,
    strength_scale: np.ndarray | None = None,
) -> BodyModel:
    skfem = _require_skfem()
    mesh = skfem.MeshTet(
        np.ascontiguousarray(tet.points.T), np.ascontiguousarray(tet.tets.T)
    )
    element = skfem.ElementTetP1() if order == 1 else skfem.ElementTetP2()
    basis = skfem.Basis(mesh, skfem.ElementVector(element), intorder=2)
    nel = int(mesh.t.shape[1])
    body = BodyModel(
        name=name,
        card=card,
        build_axis=np.asarray(build_axis, dtype=float),
        tet=tet,
        mesh=mesh,
        basis=basis,
        cmat=card.stiffness_voigt_part(np.asarray(build_axis, dtype=float)),
        stiff_scale=np.ones(nel) if stiff_scale is None else stiff_scale,
        strength_scale=np.ones(nel) if strength_scale is None else strength_scale,
    )
    body.facet_ids, body.facet_centroids, body.facet_normals, body.facet_areas = (
        _boundary_geometry(mesh)
    )
    body.dof_component = np.zeros(basis.N, dtype=np.int8)
    for comp in range(3):
        body.dof_component[basis.nodal_dofs[comp]] = comp
        if basis.edge_dofs.size:
            body.dof_component[basis.edge_dofs[comp]] = comp
    return body


def _strain_operator(basis: Basis) -> np.ndarray:
    """B (nel, nq, 6, nloc): Voigt engineering strain of every local basis function at every quadrature point."""
    first = basis.basis[0][0].grad
    nel, nq = first.shape[2], first.shape[3]
    nloc = len(basis.basis)
    b = np.empty((nel, nq, 6, nloc))
    for i, field_i in enumerate(basis.basis):
        g = field_i[0].grad  # (3, 3, nel, nq): g[a, b] = d u_a / d x_b
        b[:, :, 0, i] = g[0, 0]
        b[:, :, 1, i] = g[1, 1]
        b[:, :, 2, i] = g[2, 2]
        b[:, :, 3, i] = g[1, 2] + g[2, 1]
        b[:, :, 4, i] = g[0, 2] + g[2, 0]
        b[:, :, 5, i] = g[0, 1] + g[1, 0]
    return b


def assemble_stiffness(body: BodyModel) -> sp.csr_matrix:
    b = _strain_operator(body.basis)
    dx = body.basis.dx  # (nel, nq)
    cb = np.einsum("ab,eqbj->eqaj", body.cmat, b, optimize=True)
    ke = (
        np.einsum("eqai,eqaj,eq->eij", b, cb, dx, optimize=True)
        * body.stiff_scale[:, None, None]
    )
    dofs = body.basis.element_dofs  # (nloc, nel)
    nloc = dofs.shape[0]
    rows = np.repeat(dofs.T[:, :, None], nloc, axis=2).ravel()
    cols = np.repeat(dofs.T[:, None, :], nloc, axis=1).ravel()
    k = sp.coo_matrix((ke.ravel(), (rows, cols)), shape=(body.ndof, body.ndof)).tocsr()
    return k


# ---- facet selection and loads ----------------------------------------------------------------
def select_nodes(bodies: list[BodyModel], region: Region, part: str | None) -> list[tuple[BodyModel, np.ndarray]]:
    """Boundary corner nodes (per body) picked by a `line` or `point` region."""
    hits: list[tuple[BodyModel, np.ndarray]] = []
    best: tuple[float, BodyModel, int] | None = None
    centre = np.asarray(region.center, dtype=float)
    axis = np.asarray(region.axis, dtype=float)
    axis = axis / max(float(np.linalg.norm(axis)), 1e-12)
    for body in bodies:
        if part is not None and body.name != part:
            continue
        nodes = np.unique(body.mesh.facets[:, body.facet_ids])
        xyz = body.mesh.p[:, nodes].T
        rel = xyz - centre
        if region.kind == "point":
            dist = np.linalg.norm(rel, axis=1)
            k = int(np.argmin(dist))
            if region.radius > 0.0 and dist[k] > region.radius + body.tet.envelope_mm:
                continue
            if best is None or dist[k] < best[0]:
                best = (float(dist[k]), body, int(nodes[k]))
            continue
        along = rel @ axis
        perp = np.linalg.norm(rel - np.outer(along, axis), axis=1)
        tol = max(region.radius, 0.4 * body.tet.edge_length_mm) + body.tet.envelope_mm
        ok = perp <= tol
        if region.hi > region.lo:
            ok &= (along >= region.lo - tol) & (along <= region.hi + tol)
        if np.any(ok):
            hits.append((body, nodes[ok]))
    if best is not None:
        hits.append((best[1], np.array([best[2]])))
    return hits


def select_facets(
    bodies: list[BodyModel], region: Region, part: str | None
) -> list[tuple[BodyModel, np.ndarray]]:
    """Boundary facets (per body) matched by `region`. A sphere region without `part` binds to the nearest body only."""
    hits: list[tuple[BodyModel, np.ndarray]] = []
    for body in bodies:
        if part is not None and body.name != part:
            continue
        mask = region.mask(body.facet_centroids, body.facet_normals, body.tet.envelope_mm)
        if np.any(mask):
            hits.append((body, body.facet_ids[mask]))
    if region.kind == "sphere" and part is None and len(hits) > 1:
        centre = np.asarray(region.center)

        def nearest(item: tuple[BodyModel, np.ndarray]) -> float:
            body, ids = item
            sel = np.isin(body.facet_ids, ids)
            return float(
                np.min(np.linalg.norm(body.facet_centroids[sel] - centre, axis=1))
            )

        hits = [min(hits, key=nearest)]
    return hits


def _facet_area(body: BodyModel, ids: np.ndarray) -> float:
    return float(body.facet_areas[np.isin(body.facet_ids, ids)].sum())


def _traction_vector(
    body: BodyModel, facets: np.ndarray, traction: np.ndarray
) -> np.ndarray:
    skfem = _require_skfem()
    fbasis = skfem.FacetBasis(body.mesh, body.basis.elem, facets=facets, intorder=2)
    tx, ty, tz = (float(c) for c in traction)

    @skfem.LinearForm
    def form(v: Any, w: Any) -> Any:
        return tx * v[0] + ty * v[1] + tz * v[2]

    return np.asarray(form.assemble(fbasis))


def _pressure_vector(
    body: BodyModel, facets: np.ndarray, pressure: float
) -> np.ndarray:
    skfem = _require_skfem()
    fbasis = skfem.FacetBasis(body.mesh, body.basis.elem, facets=facets, intorder=2)

    @skfem.LinearForm
    def form(v: Any, w: Any) -> Any:
        return -pressure * (
            w.n[0] * v[0] + w.n[1] * v[1] + w.n[2] * v[2]
        )

    return np.asarray(form.assemble(fbasis))


def _gravity_vector(body: BodyModel, accel_mm_s2: np.ndarray) -> np.ndarray:
    skfem = _require_skfem()
    rho = body.card.density_g_cm3 * _FLUID_DENSITY_UNIT
    bx, by, bz = (float(rho * c) for c in accel_mm_s2)

    @skfem.LinearForm
    def form(v: Any, w: Any) -> Any:
        return bx * v[0] + by * v[1] + bz * v[2]

    return np.asarray(form.assemble(body.basis))


# ---- contact ----------------------------------------------------------------------------------
@dataclass
class ContactData:
    """Node-to-surface constraint rows for one pair.

    Every row ties one slave dof triple to a master surface point: `slave_dofs` (3, n) and
    `master_dofs` (3, n, m) are global dof ids per Cartesian component, `weights` (n, m) the master shape
    functions at the projected point (m = 3 corner nodes for P1, 6 corner + edge nodes for P2).
    """

    pair: ContactPair
    slave: str
    master: str
    slave_dofs: np.ndarray
    master_dofs: np.ndarray
    weights: np.ndarray
    normals: np.ndarray           # (n, 3) outward master normals
    gaps: np.ndarray              # (n,) initial signed gap along the normal
    penalty: np.ndarray           # (n,) stiffness per constraint row
    positions: np.ndarray         # (n, 3) slave point coordinates

    @property
    def count(self) -> int:
        return int(self.slave_dofs.shape[1])


def _surface_trimesh(body: BodyModel) -> Any:
    import trimesh

    nodes = body.mesh.facets[:, body.facet_ids].T
    return trimesh.Trimesh(vertices=body.mesh.p.T, faces=nodes, process=False)


def _facets_trimesh(body: BodyModel, facet_ids: np.ndarray) -> Any:
    import trimesh

    return trimesh.Trimesh(vertices=body.mesh.p.T, faces=body.mesh.facets[:, facet_ids].T, process=False)


def _edge_ids(mesh: MeshTet, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Global edge ids for node pairs (a, b) (arrays of equal length)."""
    n = int(mesh.p.shape[1])
    edges = np.sort(mesh.edges, axis=0)
    keys = edges[0].astype(np.int64) * n + edges[1]
    order = np.argsort(keys)
    want = np.minimum(a, b).astype(np.int64) * n + np.maximum(a, b)
    pos = np.searchsorted(keys[order], want)
    return np.asarray(order[pos])


def build_contact(
    bodies: dict[str, BodyModel], pair: ContactPair, *, penalty_factor: float, default_tol_mm: float
) -> ContactData | None:
    """Find slave points within tolerance of the master surface and their interpolation. None if the bodies do not touch."""
    from trimesh.triangles import points_to_barycentric

    a, b = bodies[pair.a], bodies[pair.b]
    # the slave is the body with the finer boundary mesh; the other one is the master surface
    slave, master = (a, b) if a.tet.edge_length_mm <= b.tet.edge_length_mm else (b, a)
    tol = pair.tolerance_mm if pair.tolerance_mm is not None else default_tol_mm
    master_surface = _surface_trimesh(master)

    _, dist, tri = _closest(master_surface, slave.facet_centroids)
    facing = np.einsum("ij,ij->i", slave.facet_normals, master.facet_normals[tri]) < -0.3
    contact_facets = (dist <= tol) & facing
    if not np.any(contact_facets):
        return None
    facet_nodes = slave.mesh.facets[:, slave.facet_ids[contact_facets]]  # (3, nf)
    areas = slave.facet_areas[contact_facets]
    quadratic = slave.basis.edge_dofs.size > 0

    nodes, inverse = np.unique(facet_nodes.ravel(), return_inverse=True)
    tributary = np.zeros(len(nodes))
    np.add.at(tributary, inverse, np.repeat(areas / 3.0, 3))
    points = slave.mesh.p[:, nodes].T
    s_dofs = slave.offset + slave.basis.nodal_dofs[:, nodes]  # (3, n)
    if quadratic:
        pairs_a = np.concatenate([facet_nodes[0], facet_nodes[1], facet_nodes[0]])
        pairs_b = np.concatenate([facet_nodes[1], facet_nodes[2], facet_nodes[2]])
        edge_ids = _edge_ids(slave.mesh, pairs_a, pairs_b)
        uniq_edges, inv_e = np.unique(edge_ids, return_inverse=True)
        edge_trib = np.zeros(len(uniq_edges))
        np.add.at(edge_trib, inv_e, np.tile(areas / 3.0, 3))
        first = np.zeros(len(uniq_edges), dtype=np.int64)
        first[inv_e] = np.arange(len(edge_ids))
        mids = 0.5 * (slave.mesh.p[:, pairs_a[first]] + slave.mesh.p[:, pairs_b[first]]).T
        points = np.vstack([points, mids])
        tributary = np.concatenate([tributary, edge_trib])
        s_dofs = np.hstack([s_dofs, slave.offset + slave.basis.edge_dofs[:, uniq_edges]])

    # project onto the master facets that face the slave patch (not onto a side face at the patch border)
    patch = _facets_trimesh(slave, slave.facet_ids[contact_facets])
    patch_normals = slave.facet_normals[contact_facets]
    _, dist_m, tri_m = _closest(patch, master.facet_centroids)
    candidates = np.flatnonzero(
        (dist_m <= tol) & (np.einsum("ij,ij->i", master.facet_normals, patch_normals[tri_m]) < -0.3)
    )
    if len(candidates) == 0:
        return None
    master_surface = _facets_trimesh(master, master.facet_ids[candidates])
    closest, dist_n, tri_n = _closest(master_surface, points)
    keep = dist_n <= 2.0 * tol
    if not np.any(keep):
        return None
    points, closest, tri_n, tributary, s_dofs = points[keep], closest[keep], tri_n[keep], tributary[keep], s_dofs[:, keep]
    corners = master_surface.faces[tri_n]  # (n, 3) master node ids
    bary = np.clip(points_to_barycentric(master_surface.vertices[corners], closest), 0.0, None)
    bary /= bary.sum(axis=1, keepdims=True)
    m_dofs = master.offset + master.basis.nodal_dofs[:, corners]  # (3, n, 3)
    weights = bary
    if master.basis.edge_dofs.size > 0:
        e01 = _edge_ids(master.mesh, corners[:, 0], corners[:, 1])
        e12 = _edge_ids(master.mesh, corners[:, 1], corners[:, 2])
        e02 = _edge_ids(master.mesh, corners[:, 0], corners[:, 2])
        l0, l1, l2 = bary[:, 0], bary[:, 1], bary[:, 2]
        weights = np.stack([l0 * (2 * l0 - 1), l1 * (2 * l1 - 1), l2 * (2 * l2 - 1), 4 * l0 * l1, 4 * l1 * l2, 4 * l0 * l2], axis=1)
        edge_dofs = master.offset + master.basis.edge_dofs[:, np.stack([e01, e12, e02], axis=1)]  # (3, n, 3)
        m_dofs = np.concatenate([m_dofs, edge_dofs], axis=2)
    normal = master.facet_normals[candidates[tri_n]]
    gap = np.einsum("ij,ij->i", points - closest, normal)
    h_snap = 0.1 * min(slave.tet.edge_length_mm, master.tet.edge_length_mm)
    gap = np.where(np.abs(gap) <= h_snap, 0.0, gap)  # meshing noise, not a designed clearance
    e_ref = min(slave.card.e_p, master.card.e_p)
    h_ref = max(min(slave.tet.edge_length_mm, master.tet.edge_length_mm), 1e-6)
    return ContactData(
        pair=pair,
        slave=slave.name,
        master=master.name,
        slave_dofs=s_dofs,
        master_dofs=m_dofs,
        weights=weights,
        normals=normal,
        gaps=gap,
        penalty=penalty_factor * e_ref * tributary / h_ref,
        positions=points,
    )


def _closest(surface: Any, points: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    import trimesh

    closest, distance, tri = trimesh.proximity.closest_point(surface, points)
    return np.asarray(closest), np.asarray(distance), np.asarray(tri)


def _contact_rows(
    data: ContactData, active: np.ndarray | None, ndof: int
) -> tuple[sp.csr_matrix, np.ndarray, np.ndarray]:
    """G (rows x ndof), per-row penalty, per-row initial gap. `active` selects the rows of a frictionless pair (None = all);
    a tied pair always has three rows (one per component) per slave point."""
    n = data.count
    m = data.weights.shape[1]
    rows: list[np.ndarray] = []
    cols: list[np.ndarray] = []
    vals: list[np.ndarray] = []
    if data.pair.kind == "tied":
        idx = np.arange(n)
        for comp in range(3):
            row = comp * n + idx
            rows += [row, np.repeat(row, m)]
            cols += [data.slave_dofs[comp], data.master_dofs[comp].ravel()]
            vals += [np.ones(n), -data.weights.ravel()]
        count = 3 * n
        penalty = np.tile(data.penalty, 3)
        gap = np.zeros(3 * n)
    else:
        sel = np.arange(n) if active is None else np.flatnonzero(active)
        k = len(sel)
        for comp in range(3):
            c = data.normals[sel, comp]
            row = np.arange(k)
            rows += [row, np.repeat(row, m)]
            cols += [data.slave_dofs[comp, sel], data.master_dofs[comp][sel].ravel()]
            vals += [c, -(data.weights[sel] * c[:, None]).ravel()]
        count = k
        penalty = data.penalty[sel]
        gap = data.gaps[sel]
    g = sp.coo_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(count, ndof)).tocsr()
    return g, penalty, gap


def _tangent_rows(data: ContactData, ndof: int) -> tuple[sp.csr_matrix, np.ndarray]:
    """Two tangential relative-displacement rows per slave point with a negligible stiffness (1e-6 x normal).

    A frictionless pair has no tangential resistance at all, so a body held only by frictionless contact would be a
    mechanism and the matrix singular; this soft spring keeps the solve well-posed without carrying real load.
    """
    n = data.count
    m = data.weights.shape[1]
    normal = data.normals
    helper = np.where(np.abs(normal[:, [0]]) < 0.9, np.array([[1.0, 0.0, 0.0]]), np.array([[0.0, 1.0, 0.0]]))
    t1 = np.cross(normal, helper)
    t1 /= np.linalg.norm(t1, axis=1, keepdims=True)
    t2 = np.cross(normal, t1)
    rows: list[np.ndarray] = []
    cols: list[np.ndarray] = []
    vals: list[np.ndarray] = []
    for k, tangent in enumerate((t1, t2)):
        for comp in range(3):
            c = tangent[:, comp]
            row = k * n + np.arange(n)
            rows += [row, np.repeat(row, m)]
            cols += [data.slave_dofs[comp], data.master_dofs[comp].ravel()]
            vals += [c, -(data.weights * c[:, None]).ravel()]
    g = sp.coo_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(2 * n, ndof)).tocsr()
    return g, np.tile(1.0e-6 * data.penalty, 2)


# ---- assembled problem and solve --------------------------------------------------------------
@dataclass
class Problem:
    bodies: list[BodyModel]
    contacts: list[ContactData]
    k: sp.csr_matrix
    ndof: int
    warnings: list[str] = field(default_factory=list)
    size_mm: float = 0.0

    @property
    def by_name(self) -> dict[str, BodyModel]:
        return {b.name: b for b in self.bodies}


def assemble_problem(
    bodies: list[BodyModel],
    pairs: list[ContactPair],
    *,
    penalty_factor: float = 1.0e3,
    default_tol_mm: float | None = None,
) -> Problem:
    offset = 0
    for body in bodies:
        body.offset = offset
        offset += body.ndof
    if offset > MAX_DOFS:
        raise SolveError(
            f"model has {offset} degrees of freedom (limit {MAX_DOFS}): the mesh is too fine for the direct solver",
            command="strength",
            remediation=[
                "Raise --mesh-size, lower --max-elements, use --order 1, or analyse a --crop."
            ],
        )
    k = sp.block_diag([assemble_stiffness(b) for b in bodies], format="csr")
    by_name = {b.name: b for b in bodies}
    contacts: list[ContactData] = []
    warnings: list[str] = []
    for pair in pairs:
        tol = default_tol_mm
        if tol is None:
            tol = max(
                0.1,
                0.6
                * min(
                    by_name[pair.a].tet.edge_length_mm,
                    by_name[pair.b].tet.edge_length_mm,
                ),
            )
        data = build_contact(
            by_name, pair, penalty_factor=penalty_factor, default_tol_mm=tol
        )
        if data is None:
            warnings.append(
                f"contact {pair.a} <-> {pair.b} ({pair.kind}): no faces within {tol:g} mm of each other, "
                "so the bodies are NOT coupled through this mate"
            )
            continue
        contacts.append(data)
    extent = np.vstack([b.mesh.p.T for b in bodies])
    size = float(np.linalg.norm(extent.max(axis=0) - extent.min(axis=0)))
    return Problem(bodies=bodies, contacts=contacts, k=k, ndof=offset, warnings=warnings, size_mm=size)


@dataclass
class BodyField:
    """Per-body solution fields for reporting and the stress picture."""

    body: BodyModel
    u: np.ndarray  # (nnodes, 3) displacement at mesh nodes (corner + midside)
    vertex_stress: np.ndarray  # (nel, 4, 3, 3) MPa at the tet vertices
    vertex_strain: np.ndarray  # (nel, 4, 3, 3)
    failure: dict[str, np.ndarray]  # fdm.failure_fields on vertex stress, each (nel, 4)
    principal_strain: np.ndarray  # (nel, 4) max principal strain
    strain_sf: np.ndarray  # (nel, 4) permissible / principal strain


@dataclass
class CaseSolution:
    name: str
    fields: list[BodyField]
    reaction_n: np.ndarray
    applied_n: np.ndarray
    contact_iterations: int
    active_contacts: dict[str, int]
    solve_seconds: float
    force_controlled: bool
    constrained_dofs: int
    notes: list[str]
    constraint_points: np.ndarray  # (m, 3) centroids of supported / loaded facets
    contact_points: np.ndarray  # (c, 3) slave points of every contact pair


def _fixed_dofs(problem: Problem, case: LoadCase) -> tuple[np.ndarray, np.ndarray, list[str], list[np.ndarray]]:
    """(dof ids, prescribed values, notes, centroids of the supported / displaced facets)."""
    dof_val: dict[int, float] = {}
    notes: list[str] = []
    points: list[np.ndarray] = []
    for support in case.supports:
        if support.region.kind in ("line", "point"):
            node_hits = select_nodes(problem.bodies, support.region, support.part)
            if not node_hits:
                raise SolveError(
                    f"support {support.name!r} ({support.region.describe()}) touches no body surface",
                    command="strength",
                    remediation=["Check the anchor position; positions are in the model's mm frame."],
                )
            for body, nodes in node_hits:
                for i, comp in enumerate("xyz"):
                    if comp in support.components:
                        for d in body.offset + body.basis.nodal_dofs[i, nodes]:
                            dof_val[int(d)] = 0.0
                points.append(body.mesh.p[:, nodes].T)
                notes.append(f"support {support.name}: {support.components} fixed on {body.name} ({len(nodes)} nodes)")
            continue
        hits = select_facets(problem.bodies, support.region, support.part)
        if not hits:
            raise SolveError(
                f"support {support.name!r} ({support.region.describe()}) touches no body surface",
                command="strength",
                remediation=["Check the anchor position or radius_mm; positions are in the model's mm frame."],
            )
        for body, facets in hits:
            keys = [f"u^{i + 1}" for i, c in enumerate("xyz") if c in support.components]
            for d in body.offset + body.basis.get_dofs(facets=facets).all(keys):
                dof_val[int(d)] = 0.0
            points.append(body.facet_centroids[np.isin(body.facet_ids, facets)])
            notes.append(f"support {support.name}: {support.components} fixed on {body.name} ({len(facets)} facets)")
    for load in case.loads:
        if load.kind != "displacement" or load.region is None:
            continue
        hits = select_facets(problem.bodies, load.region, load.part)
        if not hits:
            raise SolveError(
                f"displacement load {load.name!r} ({load.region.describe()}) touches no body surface",
                command="strength",
                remediation=["Check the anchor position or radius_mm; positions are in the model's mm frame."],
            )
        for body, facets in hits:
            for i in range(3):
                if "xyz"[i] not in load.components:
                    continue
                for d in body.offset + body.basis.get_dofs(facets=facets).all([f"u^{i + 1}"]):
                    dof_val[int(d)] = float(load.vector[i])
            points.append(body.facet_centroids[np.isin(body.facet_ids, facets)])
            notes.append(f"prescribed displacement {load.name} on {body.name} ({len(facets)} facets)")
    ids = np.array(sorted(dof_val), dtype=np.int64)
    vals = np.array([dof_val[int(i)] for i in ids])
    return ids, vals, notes, points


def _load_vector(problem: Problem, case: LoadCase) -> tuple[np.ndarray, list[str], list[np.ndarray]]:
    f = np.zeros(problem.ndof)
    notes: list[str] = []
    points: list[np.ndarray] = []
    for load in case.loads:
        if load.kind == "displacement":
            continue
        if load.kind == "gravity":
            accel = np.asarray(load.vector, dtype=float) * 1000.0  # m/s^2 -> mm/s^2
            for body in problem.bodies:
                f[body.offset : body.offset + body.ndof] += _gravity_vector(body, accel)
            notes.append(f"gravity {load.name}: {np.linalg.norm(load.vector):g} m/s^2")
            continue
        assert load.region is not None
        hits = select_facets(problem.bodies, load.region, load.part)
        if not hits:
            raise SolveError(
                f"load {load.name!r} ({load.region.describe()}) touches no body surface",
                command="strength",
                remediation=["Check the anchor position or radius_mm; positions are in the model's mm frame."],
            )
        total_area = sum(_facet_area(body, ids) for body, ids in hits)
        for body, ids in hits:
            area = _facet_area(body, ids)
            if load.kind == "force":
                traction = np.asarray(load.vector, dtype=float) * (area / total_area) / area
                vec = _traction_vector(body, ids, traction)
            else:
                vec = _pressure_vector(body, ids, load.pressure_mpa)
            f[body.offset : body.offset + body.ndof] += vec
            points.append(body.facet_centroids[np.isin(body.facet_ids, ids)])
            notes.append(f"{load.kind} {load.name} on {body.name}: {len(ids)} facets, {total_area:.2f} mm^2")
    return f, notes, points


def _solve_reduced(
    k: sp.csr_matrix, f: np.ndarray, fixed: np.ndarray, values: np.ndarray, size_mm: float = 0.0
) -> np.ndarray:
    n = k.shape[0]
    free = np.ones(n, dtype=bool)
    free[fixed] = False
    idx = np.flatnonzero(free)
    u = np.zeros(n)
    u[fixed] = values
    rhs = f[idx]
    if len(fixed):
        rhs = rhs - k[idx][:, fixed] @ values
    k_ff = k[idx][:, idx].tocsc()
    try:
        # K is symmetric positive definite: symmetric ordering and no pivoting keep the fill low (a penalty-coupled
        # contact matrix factorised with partial pivoting took 147 s against 4 s this way)
        lu = spla.splu(k_ff, permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=0.0, options={"SymmetricMode": True})
    except RuntimeError as exc:
        raise SolveError(
            f"stiffness matrix is singular: {exc}",
            command="strength",
            remediation=[
                "The model is under-constrained: add a fixed support (loads: kind: fixed) or tie the bodies."
            ],
        ) from exc
    sol = lu.solve(rhs)
    for _ in range(2):  # iterative refinement: cheap, and it tames nearly-singular element patches
        sol = sol + lu.solve(rhs - k_ff @ sol)
    residual = float(np.linalg.norm(k_ff @ sol - rhs))
    # backward error: small even for a singular matrix, so a rigid-body mode is caught by the size of u below
    backward = residual / (float(spla.norm(k_ff, 1)) * float(np.linalg.norm(sol, np.inf)) + float(np.linalg.norm(rhs)) + 1e-30)
    if not np.all(np.isfinite(sol)) or backward > 1e-8:
        raise SolveError(
            f"the linear solve did not converge (backward error {backward:.1e})",
            command="strength",
            remediation=["Check for slivers in the mesh (--mesh-size) and for a fully constrained model."],
        )
    if size_mm > 0.0 and float(np.abs(sol).max()) > 0.5 * size_mm:
        raise SolveError(
            f"displacements reach {float(np.abs(sol).max()):.3g} mm on a {size_mm:.3g} mm model: it is under-constrained "
            "(rigid-body motion) or the load is far beyond the linear range",
            command="strength",
            remediation=["Add a fixed support to every body that is not tied to a supported one, or lower the load."],
        )
    u[idx] = sol
    return u


def _require_supported(problem: Problem, fixed: np.ndarray) -> None:
    """Every body must be held: a support on it, or a contact chain to a body that has one."""
    reach = {
        b.name for b in problem.bodies if np.any((fixed >= b.offset) & (fixed < b.offset + b.ndof))
    }
    grew = True
    while grew:
        grew = False
        for data in problem.contacts:
            if (data.slave in reach) != (data.master in reach):
                reach.update((data.slave, data.master))
                grew = True
    floating = [b.name for b in problem.bodies if b.name not in reach]
    if floating:
        raise SolveError(
            f"body {', '.join(floating)} is under-constrained: it has no support and no contact to a supported body",
            command="strength",
            remediation=[
                "Add a `kind: fixed` load on it, declare a mate to a supported body (`mates:`), or pass --contact tied.",
            ],
        )


def solve_case(
    problem: Problem,
    case: LoadCase,
    *,
    repeated_use: bool = False,
    max_iterations: int = 12,
) -> CaseSolution:
    start = time.perf_counter()
    fixed, values, support_notes, support_points = _fixed_dofs(problem, case)
    if len(fixed) == 0 and not any(load.kind == "gravity" for load in case.loads):
        raise SolveError(
            f"load case {case.name!r} has no support",
            command="strength",
            remediation=["Add a `kind: fixed` entry under loads in 3d.yaml, or use --fixture."],
        )
    _require_supported(problem, fixed)
    f, load_notes, load_points = _load_vector(problem, case)
    tied = [c for c in problem.contacts if c.pair.kind == "tied"]
    sliding = [c for c in problem.contacts if c.pair.kind == "frictionless"]

    k_base = problem.k.copy()
    for data in tied:
        g, pen, _ = _contact_rows(data, None, problem.ndof)
        k_base = k_base + g.T @ sp.diags(pen) @ g
    for data in sliding:
        g, pen = _tangent_rows(data, problem.ndof)
        k_base = k_base + g.T @ sp.diags(pen) @ g

    gap_enter = 1e-6 * min(b.tet.edge_length_mm for b in problem.bodies)
    active = [np.array([bool(gap <= 1e-9) for gap in data.gaps]) for data in sliding]
    iterations = 0
    u = np.zeros(problem.ndof)
    settled = not sliding
    for iterations in range(1, max_iterations + 1):  # noqa: B007 - read after the loop
        k = k_base
        rhs = f.copy()
        for data, act in zip(sliding, active, strict=True):
            if not np.any(act):
                continue
            g, pen, gap = _contact_rows(data, act, problem.ndof)
            k = k + g.T @ sp.diags(pen) @ g
            rhs -= g.T @ (pen * gap)
        u = _solve_reduced(k.tocsr(), rhs, fixed, values, problem.size_mm)
        if not sliding:
            break
        changed = False
        for i, data in enumerate(sliding):
            g_all, _, _ = _contact_rows(data, None, problem.ndof)
            gap_now = data.gaps + g_all @ u
            # penalty force per node (> 0 = compression); a node leaves the contact when it pulls, enters when it penetrates
            # by a real amount, so nodes with ~zero force cannot flip-flop between iterations
            force = -data.penalty * gap_now
            f_ref = float(np.mean(np.abs(force[active[i]]))) if np.any(active[i]) else 0.0
            leave = active[i] & (force < -1e-3 * f_ref)
            enter = ~active[i] & (gap_now < -gap_enter)
            now = (active[i] & ~leave) | enter
            if not np.array_equal(now, active[i]):
                changed = True
                active[i] = now
        if not changed:
            settled = True
            break
    if not settled:
        raise SolveError(
            f"frictionless contact did not settle in {max_iterations} iterations: the bodies separate "
            "(tension across a frictionless pair) or the contact chatters",
            command="strength",
            remediation=[
                "Frictionless contact cannot hold a tensile load: bond the mate (`fit_type: glue`, `contact: tied`) "
                "or support the loose body.",
            ],
        )

    residual = problem.k @ u - f
    components = np.concatenate([b.dof_component for b in problem.bodies])
    reaction = np.array([float(residual[fixed][components[fixed] == c].sum()) for c in range(3)]) if len(fixed) else np.zeros(3)
    fields = [_recover(body, u[body.offset : body.offset + body.ndof], repeated_use) for body in problem.bodies]
    active_counts = {f"{d.pair.a}<->{d.pair.b}": int(a.sum()) for d, a in zip(sliding, active, strict=True)}
    for d in tied:
        active_counts[f"{d.pair.a}<->{d.pair.b}"] = d.count
    points = [*support_points, *load_points]
    return CaseSolution(
        name=case.name,
        fields=fields,
        reaction_n=reaction,
        applied_n=_applied_total(problem, case),
        contact_iterations=iterations,
        active_contacts=active_counts,
        solve_seconds=time.perf_counter() - start,
        force_controlled=any(load.kind != "displacement" for load in case.loads),
        constrained_dofs=len(fixed),
        notes=[*support_notes, *load_notes],
        constraint_points=np.concatenate(points) if points else np.zeros((0, 3)),
        contact_points=np.vstack([d.positions for d in problem.contacts]) if problem.contacts else np.zeros((0, 3)),
    )


def _applied_total(problem: Problem, case: LoadCase) -> np.ndarray:
    total = np.zeros(3)
    for load in case.loads:
        if load.kind == "force":
            total += np.asarray(load.vector)
    return total


def _recover(body: BodyModel, u_body: np.ndarray, repeated_use: bool) -> BodyField:
    skfem = _require_skfem()
    vertices = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    ).T
    basis_v = skfem.Basis(
        body.mesh, body.basis.elem, quadrature=(vertices, np.full(4, 1.0 / 24.0))
    )
    b = _strain_operator(basis_v)  # (nel, 4, 6, nloc)
    ue = u_body[basis_v.element_dofs.T]  # (nel, nloc)
    strain_v = np.einsum("eqaj,ej->eqa", b, ue)
    stress_v = (
        np.einsum("ab,eqb->eqa", body.cmat, strain_v) * body.stiff_scale[:, None, None]
    )
    stress = _voigt_to_tensor(stress_v, shear_factor=1.0)
    strain = _voigt_to_tensor(strain_v, shear_factor=0.5)
    failure = fdm.failure_fields(
        stress, body.build_axis, body.card, body.strength_scale[:, None]
    )
    eigvals, eigvecs = np.linalg.eigh(strain)
    principal = eigvals[..., 2]
    direction = eigvecs[..., :, 2]
    c2 = np.clip((direction @ body.build_axis) ** 2, 0.0, 1.0)
    allow = 1.0 / np.sqrt(
        c2 / body.card.strain_limit_z**2 + (1.0 - c2) / body.card.strain_limit_p**2
    )
    allow = allow * (0.6 if repeated_use else 1.0) * body.strength_scale[:, None]
    strain_sf = np.where(
        principal > 1e-12, allow / np.maximum(principal, 1e-12), np.inf
    )
    nodes = np.stack([u_body[body.basis.nodal_dofs[c]] for c in range(3)], axis=1)
    return BodyField(
        body=body,
        u=nodes,
        vertex_stress=stress,
        vertex_strain=strain,
        failure=failure,
        principal_strain=principal,
        strain_sf=strain_sf,
    )


def _voigt_to_tensor(v: np.ndarray, *, shear_factor: float) -> np.ndarray:
    t = np.zeros(v.shape[:-1] + (3, 3))
    t[..., 0, 0], t[..., 1, 1], t[..., 2, 2] = v[..., 0], v[..., 1], v[..., 2]
    t[..., 1, 2] = t[..., 2, 1] = v[..., 3] * shear_factor
    t[..., 0, 2] = t[..., 2, 0] = v[..., 4] * shear_factor
    t[..., 0, 1] = t[..., 1, 0] = v[..., 5] * shear_factor
    return t
