"""FEA_LINEAR benchmarks against analytic solutions, plus assembly / anisotropy behaviour.

Tolerances are stated per case: they are mesh tolerances of the quadratic-tet solver at the mesh
size each test uses, not slack. Skipped when scikit-fem / pytetwild are not installed.
"""

from __future__ import annotations

import numpy as np
import pytest
from structural import fdm, fea
from structural_fixtures import (
    bar,
    card,
    end_slab,
    pipe,
    solve,
    tapered_bar,
    tip_deflection,
)

pytest.importorskip("skfem")
pytest.importorskip("pytetwild")

X = (1.0, 0.0, 0.0)
Z = (0.0, 0.0, 1.0)


def _cantilever_case(
    length: float,
    force: float,
    direction: tuple[float, float, float] = (0.0, 0.0, -1.0),
) -> fea.LoadCase:
    vector = (force * direction[0], force * direction[1], force * direction[2])
    load = fea.Load("tip", "force", end_slab(X, length), vector=vector)
    return fea.LoadCase("tip", (load,), (fea.Support("root", end_slab(X, 0.0)),))


def test_cantilever_tip_deflection_within_two_percent() -> None:
    length, b, h, p = 100.0, 10.0, 10.0, 10.0
    sol, _ = solve([("beam", bar(length, b, h))], _cantilever_case(length, p), edge=3.2)
    e_x = card().modulus_along(np.array(X), np.array(Z))
    analytic = p * length**3 / (3.0 * e_x * (b * h**3 / 12.0))
    assert abs(tip_deflection(sol, "beam", length, 2)) == pytest.approx(
        analytic, rel=0.02
    )
    assert sol.reaction_n[2] == pytest.approx(p, rel=1e-6)


def test_cantilever_root_stress_within_ten_percent_and_reports_the_singularity_flag() -> (
    None
):
    length, b, h, p = 100.0, 10.0, 10.0, 10.0
    sol, _ = solve([("beam", bar(length, b, h))], _cantilever_case(length, p), edge=3.2)
    field = sol.fields[0]
    analytic = p * length * (h / 2.0) / (b * h**3 / 12.0)
    # sample 10 mm from the clamp, away from the fixed-edge singularity
    centroids = field.body.mesh.p[:, field.body.mesh.t].mean(axis=1)  # (3, nel)
    band = (centroids[0] > 8.0) & (centroids[0] < 12.0)
    sigma_xx = field.vertex_stress[band][:, :, 0, 0].max()
    expected_at_10mm = p * (length - 10.0) * (h / 2.0) / (b * h**3 / 12.0)
    assert sigma_xx == pytest.approx(expected_at_10mm, rel=0.10)
    assert (
        field.failure["von_mises"].max() >= analytic * 0.95
    )  # the clamp corner never reads low


def test_tapered_cantilever_deflection_within_three_percent() -> None:
    from structural import beams

    length, w, h0, tip, p = 60.0, 10.0, 8.0, 4.0, 6.0
    sol, _ = solve(
        [("arm", tapered_bar(length, w, h0, tip))],
        _cantilever_case(length, p),
        edge=2.6,
    )
    e_x = card().modulus_along(np.array(X), np.array(Z))
    analytic = beams.tapered_thickness_tip_deflection(p, length, e_x, w, h0, tip / h0)
    assert abs(tip_deflection(sol, "arm", length, 2)) == pytest.approx(
        analytic, rel=0.03
    )


def test_three_point_bend_matches_timoshenko_and_euler_bernoulli() -> None:
    span, b, h, f = 80.0, 10.0, 8.0, 20.0
    strip = 0.03 * span
    d = (0.0, 0.0, -1.0)
    top_strip = fea.Region(
        "slab", axis=X, lo=span / 2 - strip / 2, hi=span / 2 + strip / 2, normal_axis=d, normal_sign=-1.0, min_alignment=0.5
    )
    under = -h / 2

    def roller(x: float) -> fea.Region:
        return fea.Region("line", center=(x, 0.0, under), axis=(0.0, 1.0, 0.0))

    case = fea.LoadCase(
        "bend",
        (fea.Load("centre", "force", top_strip, vector=(0.0, 0.0, -f)),),
        (
            fea.Support("left", roller(0.0), components="z"),
            fea.Support("right", roller(span), components="z"),
            fea.Support("pin-xy", fea.Region("point", center=(0.0, 0.0, under)), components="xy"),
            fea.Support("pin-y", fea.Region("point", center=(span, 0.0, under)), components="y"),
        ),
    )
    sol, _ = solve([("beam", bar(span, b, h))], case, edge=2.6)
    e_x = card().modulus_along(np.array(X), np.array(Z))
    analytic = f * span**3 / (48.0 * e_x * (b * h**3 / 12.0))
    field = sol.fields[0]
    nodes = field.body.mesh.p
    bottom_mid = (np.abs(nodes[0] - span / 2) < 1.0) & (nodes[2] < under + 1e-3)
    deflection = abs(float(field.u[bottom_mid][:, 2].mean()))
    # span/depth = 10: shear deformation adds ~3 % that Euler-Bernoulli leaves out, so check both
    g_xz = card().g_p
    timoshenko = analytic + f * span / (4.0 * (5.0 / 6.0) * g_xz * b * h)
    assert deflection == pytest.approx(analytic, rel=0.05)
    assert deflection == pytest.approx(timoshenko, rel=0.015)
    assert sol.reaction_n[2] == pytest.approx(f, rel=1e-6)


def test_pipe_hoop_stress_matches_lame_within_five_percent() -> None:
    r_in, r_out, height, pressure = 8.0, 12.0, 24.0, 1.0
    bore = fea.Region("annulus", center=(0.0, 0.0, 0.0), axis=Z, lo=r_in - 0.05, hi=r_in + 0.05)
    case = fea.LoadCase(
        "pressure",
        (fea.Load("bore", "pressure", bore, pressure_mpa=pressure),),
        (
            fea.Support("axial", end_slab(Z, 0.0), components="z"),
            # three pins fix Tx, Ty and Rz without touching the radial expansion (symmetry planes)
            fea.Support("pin-x+", fea.Region("point", center=(0.0, r_out, height / 2)), components="x"),
            fea.Support("pin-x-", fea.Region("point", center=(0.0, -r_out, height / 2)), components="x"),
            fea.Support("pin-y", fea.Region("point", center=(r_out, 0.0, height / 2)), components="y"),
        ),
    )
    sol, _ = solve([("pipe", pipe(r_in, r_out, height))], case, edge=2.2)
    field = sol.fields[0]
    mesh = field.body.mesh
    nodes = mesh.p[:, mesh.t]  # (3, 4, nel)
    hoop_values = []
    for e in range(mesh.t.shape[1]):
        for v in range(4):
            x, y, z = nodes[:, v, e]
            r = float(np.hypot(x, y))
            if abs(r - r_in) < 0.3 and abs(z - height / 2) < 5.0:
                tangent = np.array([-y, x, 0.0]) / r
                hoop_values.append(float(tangent @ field.vertex_stress[e, v] @ tangent))
    lame = pressure * (r_out**2 + r_in**2) / (r_out**2 - r_in**2)
    assert len(hoop_values) > 20
    assert float(np.mean(hoop_values)) == pytest.approx(lame, rel=0.05)


def test_snap_hook_root_strain_matches_closed_form_within_ten_percent() -> None:
    t, length, w, y = 1.0, 7.0, 6.0, 0.2
    arm = bar(length, w, t)  # length along x, thickness along z
    tip = fea.Region("slab", axis=X, lo=length - 1e-6, hi=length + 1e-6, min_alignment=0.9)
    case = fea.LoadCase(
        "assembly",
        (fea.Load("deflect", "displacement", tip, vector=(0.0, 0.0, -y), components="z"),),
        (fea.Support("root", end_slab(X, 0.0)),),
    )
    sol, _ = solve([("hook", arm)], case, edge=0.55)
    field = sol.fields[0]
    root_strain = 1.5 * t * y / length**2
    # the clamp corner is a singularity: read the bending strain 1 mm from the root, where the beam strain
    # falls linearly to (L - 1) / L of its root value
    mesh = field.body.mesh
    xs = mesh.p[0, mesh.t]  # (4, nel)
    band = (xs > 0.7) & (xs < 1.3)
    eps_xx = np.abs(field.vertex_strain[:, :, 0, 0]).T  # (4, nel)
    measured = float(eps_xx[band].max())
    assert measured == pytest.approx(root_strain * (length - 1.0) / length, rel=0.10)
    assert float(field.principal_strain.max()) >= root_strain * 0.95  # the root itself never reads low
    assert not sol.force_controlled


def test_two_body_tied_beam_reproduces_the_monolithic_beam() -> None:
    length, b, h, p = 100.0, 10.0, 10.0, 10.0
    half = length / 2.0
    parts = [("left", bar(half, b, h)), ("right", bar(half, b, h, x0=half))]
    pair = fea.ContactPair("left", "right", "tied")
    sol, problem = solve(parts, _cantilever_case(length, p), edge=3.2, pairs=[pair])
    assert len(problem.contacts) == 1
    e_x = card().modulus_along(np.array(X), np.array(Z))
    analytic = p * length**3 / (3.0 * e_x * (b * h**3 / 12.0))
    assert abs(tip_deflection(sol, "right", length, 2)) == pytest.approx(
        analytic, rel=0.04
    )


def test_assembled_pair_differs_from_the_parts_analysed_separately() -> None:
    length, b, h, p = 80.0, 10.0, 6.0, 4.0
    base = bar(length, b, h)
    plate = bar(length, b, 3.0)
    plate.apply_translation([0.0, 0.0, h / 2 + 1.5])  # stacked on top of the base
    case = _cantilever_case(length, p)
    alone, _ = solve([("base", base)], case, edge=2.4)
    tied, problem = solve(
        [("base", base), ("plate", plate)],
        _two_body_case(length, p),
        edge=2.4,
        pairs=[fea.ContactPair("base", "plate", "tied")],
    )
    assert len(problem.contacts) == 1
    stress_alone = max(
        float(f.failure["von_mises"].max())
        for f in alone.fields
        if f.body.name == "base"
    )
    stress_tied = float(
        next(f for f in tied.fields if f.body.name == "base").failure["von_mises"].max()
    )
    assert abs(tip_deflection(tied, "base", length, 2)) < 0.7 * abs(
        tip_deflection(alone, "base", length, 2)
    )
    assert stress_tied < 0.8 * stress_alone


def _two_body_case(length: float, force: float) -> fea.LoadCase:
    root = end_slab(X, 0.0)
    tip = end_slab(X, length)
    return fea.LoadCase(
        "tip",
        (fea.Load("tip", "force", tip, vector=(0.0, 0.0, -force)),),
        (fea.Support("root", root),),
    )


def test_frictionless_stack_transmits_the_compressive_load() -> None:
    a, h1, h2, p = 12.0, 10.0, 10.0, 60.0
    lower = bar(h1, a, a)  # x is the stack axis here
    upper = bar(h2, a, a, x0=h1)
    case = fea.LoadCase(
        "squeeze",
        (fea.Load("push", "force", end_slab(X, h1 + h2), vector=(-p, 0.0, 0.0)),),
        (
            fea.Support("base", end_slab(X, 0.0), components="x"),
            fea.Support("pin-yz", fea.Region("point", center=(0.0, 0.0, 0.0)), components="yz"),
            fea.Support("pin-z", fea.Region("point", center=(0.0, a / 2, 0.0)), components="z"),
        ),
    )
    sol, problem = solve(
        [("lower", lower), ("upper", upper)], case, edge=4.5, pairs=[fea.ContactPair("lower", "upper", "frictionless")]
    )
    assert len(problem.contacts) == 1
    assert sol.active_contacts["lower<->upper"] > 10  # the interface is in compression
    assert sol.contact_iterations <= 12 and not problem.warnings
    e_x = card().modulus_along(np.array(X), np.array(Z))
    shortening = p * (h1 + h2) / (e_x * a * a)
    assert abs(tip_deflection(sol, "upper", h1 + h2, 0)) == pytest.approx(shortening, rel=0.05)
    # the bulk stress is p/A; the peak on the contact face is mesh-dependent node-to-surface noise, so check the median
    for f in sol.fields:
        assert float(np.median(f.failure["von_mises"])) == pytest.approx(p / (a * a), rel=0.10)


def test_frictionless_pair_cannot_hold_a_tensile_load_but_a_tied_pair_does() -> None:
    a, h1, h2 = 12.0, 10.0, 6.0
    lower, upper = bar(h1, a, a), bar(h2, a, a, x0=h1)
    base = (fea.Support("base", end_slab(X, 0.0), components="xyz"),)
    pull = fea.LoadCase("pull", (fea.Load("pull", "force", end_slab(X, h1 + h2), vector=(5.0, 0.0, 0.0)),), base)
    tied, _ = solve(
        [("lower", lower), ("upper", upper)], pull, edge=4.5, pairs=[fea.ContactPair("lower", "upper", "tied")]
    )
    e_x = card().modulus_along(np.array(X), np.array(Z))
    assert abs(tip_deflection(tied, "upper", h1 + h2, 0)) == pytest.approx(5.0 * (h1 + h2) / (e_x * a * a), rel=0.05)
    with pytest.raises(fea.SolveError):
        solve([("lower", lower), ("upper", upper)], pull, edge=4.5, pairs=[fea.ContactPair("lower", "upper", "frictionless")])


def test_a_body_with_no_support_and_no_contact_is_rejected() -> None:
    from structural import mesh as meshing

    length, b, h = 40.0, 10.0, 6.0
    far = bar(length, b, h)
    far.apply_translation([0.0, 0.0, 30.0])
    bodies = [
        fea.build_body(name, meshing.tetrahedralize(surface, 3.0), card(), np.array(Z), order=2)
        for name, surface in (("base", bar(length, b, h)), ("floating", far))
    ]
    problem = fea.assemble_problem(bodies, [fea.ContactPair("base", "floating", "tied")])
    assert any("NOT coupled" in w for w in problem.warnings)
    case = fea.LoadCase(
        "tip",
        (fea.Load("tip", "force", end_slab(X, length), vector=(0.0, 0.0, -2.0), part="base"),),
        (fea.Support("root", end_slab(X, 0.0), part="base"),),
    )
    with pytest.raises(fea.SolveError, match="under-constrained"):
        fea.solve_case(problem, case)


def test_anisotropy_changes_stiffness_and_allowables_with_print_orientation() -> None:
    pla = card("PLA")
    x_dir = np.array(X)
    flat = np.array(Z)  # layers stacked along Z: X is in-plane
    standing = np.array(X)  # layers stacked along X: X is the across-layer direction
    assert pla.modulus_along(x_dir, flat) == pytest.approx(pla.e_p)
    assert pla.modulus_along(x_dir, standing) == pytest.approx(pla.e_z)
    assert pla.strength_along(x_dir, flat) == pytest.approx(pla.yield_p)
    assert pla.strength_along(x_dir, standing) == pytest.approx(
        pla.yield_p * pla.layer_adhesion
    )
    # a uniaxial stress along X: in-plane when printed flat, across-layer when standing
    sigma = np.zeros((3, 3))
    sigma[0, 0] = 10.0
    sf_flat = float(fdm.failure_fields(sigma, flat, pla)["sf"])
    sf_standing = float(fdm.failure_fields(sigma, standing, pla)["sf"])
    assert sf_flat == pytest.approx(pla.yield_p / 10.0)
    assert sf_standing == pytest.approx(pla.yield_p * pla.layer_adhesion / 10.0)
    assert sf_standing < 0.5 * sf_flat


def test_solved_deflection_follows_the_across_layer_modulus() -> None:
    length, b, h, p = 100.0, 10.0, 10.0, 10.0
    flat, _ = solve(
        [("beam", bar(length, b, h))],
        _cantilever_case(length, p),
        edge=3.2,
        build_axis=Z,
    )
    standing, _ = solve(
        [("beam", bar(length, b, h))],
        _cantilever_case(length, p),
        edge=3.2,
        build_axis=X,
    )
    ratio = abs(tip_deflection(standing, "beam", length, 2)) / abs(
        tip_deflection(flat, "beam", length, 2)
    )
    pla = card()
    assert ratio == pytest.approx(pla.e_p / pla.e_z, rel=0.06)
    assert standing.fields[0].failure["sf"].min() < flat.fields[0].failure["sf"].min()


def test_unsupported_model_is_reported_as_under_constrained() -> None:
    length, b, h = 40.0, 10.0, 6.0
    case = fea.LoadCase(
        "free",
        (fea.Load("tip", "force", end_slab(X, length), vector=(0.0, 0.0, -1.0)),),
        (),
    )
    with pytest.raises(fea.SolveError, match="no support"):
        solve([("beam", bar(length, b, h))], case, edge=3.0)
    sliding = fea.LoadCase(
        "slide",
        (fea.Load("tip", "force", end_slab(X, length), vector=(0.0, 0.0, -1.0)),),
        (fea.Support("x-only", end_slab(X, 0.0), components="x"),),
    )
    with pytest.raises(fea.SolveError):
        solve([("beam", bar(length, b, h))], sliding, edge=3.0)
