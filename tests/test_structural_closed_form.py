"""Closed-form strength-of-materials fixtures (ANALYTICAL_SCREEN): beams, plate, pipe, snap hooks.

Every expected value is computed by an independent route (numerical quadrature of the curvature
integral, or a published textbook coefficient), never by re-calling the function under test.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy.integrate import quad
from structural import beams, hooks

E = 3300.0


def test_constant_cantilever_matches_curvature_integral() -> None:
    p, length, b, h = 10.0, 100.0, 10.0, 10.0
    i = beams.rect_inertia(b, h)
    reference, _ = quad(lambda x: p * (length - x) ** 2 / (E * i), 0.0, length)
    assert beams.cantilever_tip_deflection(p, length, E, i) == pytest.approx(
        reference, rel=1e-9
    )
    assert beams.cantilever_tip_deflection(p, length, E, i) == pytest.approx(
        1.2121, rel=1e-3
    )
    assert beams.cantilever_max_stress(p, length, i, h / 2) == pytest.approx(
        6.0, rel=1e-12
    )


@pytest.mark.parametrize("ratio", [0.25, 0.5, 0.75, 0.9])
def test_thickness_tapered_cantilever_within_one_percent(ratio: float) -> None:
    p, length, b, h0 = 5.0, 40.0, 8.0, 4.0

    def inertia(x: float) -> float:
        return b * (h0 * (1.0 - (1.0 - ratio) * x / length)) ** 3 / 12.0

    reference, _ = quad(lambda x: p * (length - x) ** 2 / (E * inertia(x)), 0.0, length)
    got = beams.tapered_thickness_tip_deflection(p, length, E, b, h0, ratio)
    assert got == pytest.approx(reference, rel=0.01)


def test_taper_ratio_one_recovers_the_constant_section() -> None:
    constant = beams.cantilever_tip_deflection(
        5.0, 40.0, E, beams.rect_inertia(8.0, 4.0)
    )
    assert beams.tapered_thickness_tip_deflection(
        5.0, 40.0, E, 8.0, 4.0, 1.0
    ) == pytest.approx(constant, rel=1e-9)
    assert beams.tapered_thickness_tip_deflection(
        5.0, 40.0, E, 8.0, 4.0, 0.9995
    ) == pytest.approx(constant, rel=2e-3)


def test_width_tapered_cantilever_within_one_percent() -> None:
    p, length, b0, t = 5.0, 40.0, 12.0, 3.0

    def inertia(x: float) -> float:
        return b0 * (1.0 - x / length) * t**3 / 12.0

    # the integrand (L - x)^2 / (1 - x/L) is finite at the tip; integrate to just short of it
    reference, _ = quad(
        lambda x: p * (length - x) ** 2 / (E * inertia(x)), 0.0, length * (1.0 - 1e-9)
    )
    assert beams.tapered_width_tip_deflection(p, length, E, b0, t) == pytest.approx(
        reference, rel=0.01
    )


def test_three_point_bend_matches_curvature_integral() -> None:
    f, span, b, h = 40.0, 80.0, 10.0, 6.0
    i = beams.rect_inertia(b, h)

    def moment_unit(x: float) -> float:
        return min(x, span - x) / 2.0

    reference, _ = quad(
        lambda x: f * moment_unit(x) * moment_unit(x) / (E * i),
        0.0,
        span,
        points=[span / 2.0],
    )
    assert beams.three_point_deflection(f, span, E, i) == pytest.approx(
        reference, rel=1e-6
    )
    assert beams.three_point_max_stress(f, span, i, h / 2) == pytest.approx(
        f * span / 4.0 * (h / 2) / i, rel=1e-12
    )


def test_simple_stress_formulas() -> None:
    assert beams.bearing_stress(100.0, 5.0, 4.0) == pytest.approx(5.0)
    assert beams.shear_stress(100.0, 20.0) == pytest.approx(5.0)
    assert beams.axial_stress(100.0, 25.0) == pytest.approx(4.0)
    assert beams.euler_buckling_load(E, 100.0, 50.0, 1.0) == pytest.approx(
        math.pi**2 * E * 100.0 / 2500.0
    )


def test_pipe_hoop_stress_thin_and_thick() -> None:
    assert beams.hoop_stress_thin(0.5, 20.0, 1.0) == pytest.approx(10.0)
    # Lame: ri 10, ro 20, p 1 -> (400 + 100) / (400 - 100) = 5/3 at the bore
    assert beams.hoop_stress_thick_inner(1.0, 10.0, 20.0) == pytest.approx(5.0 / 3.0)
    # the thin-wall formula approaches Lame as t/r -> 0
    assert beams.hoop_stress_thick_inner(1.0, 100.0, 101.0) == pytest.approx(
        beams.hoop_stress_thin(1.0, 100.5, 1.0), rel=0.01
    )


def test_simply_supported_square_plate_matches_timoshenko() -> None:
    q, a, t, nu = 0.01, 100.0, 2.0, 0.3
    w, sigma = beams.plate_simply_supported_uniform(q, a, a, t, E, nu)
    flexural = E * t**3 / (12.0 * (1.0 - nu**2))
    assert w == pytest.approx(0.00406 * q * a**4 / flexural, rel=2e-3)
    assert sigma == pytest.approx(0.2874 * q * a**2 / t**2, rel=2e-3)


@pytest.mark.parametrize(
    "call",
    [
        lambda: beams.cantilever_tip_deflection(-1.0, 10.0, E, 5.0),
        lambda: beams.cantilever_tip_deflection(1.0, math.nan, E, 5.0),
        lambda: beams.hoop_stress_thin(1.0, math.inf, 1.0),
        lambda: beams.tapered_thickness_tip_deflection(1.0, 10.0, E, 5.0, 2.0, 1.5),
        lambda: beams.hoop_stress_thick_inner(1.0, 10.0, 5.0),
    ],
)
def test_invalid_geometry_is_rejected(call) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        call()


# ---- snap hooks -------------------------------------------------------------------------------
def test_hook_root_strain_formula() -> None:
    g = hooks.HookGeometry(
        thickness_mm=1.0, length_mm=7.0, width_mm=6.0, deflection_mm=0.2
    )
    assert hooks.root_strain(g) == pytest.approx(1.5 * 1.0 * 0.2 / 49.0, rel=1e-12)


def test_tapered_hook_strain_matches_bayer_coefficient() -> None:
    # Bayer: a linear taper to h/2 raises the permissible deflection by "more than 60 %" (y = 1.09 eps L^2 / h)
    constant = hooks.HookGeometry(1.0, 10.0, 5.0, 1.0)
    tapered = hooks.HookGeometry(1.0, 10.0, 5.0, 1.0, tip_ratio=0.5)
    gain = hooks.max_deflection(tapered, 0.02) / hooks.max_deflection(constant, 0.02)
    assert gain == pytest.approx(1.09 / 0.667, rel=0.01)
    assert 1.6 < gain < 1.7


def test_hook_flex_force_and_insertion_force() -> None:
    g = hooks.HookGeometry(1.0, 7.0, 6.0, 0.1)
    # P = 3 E I y / L^3 with I = w t^3 / 12
    expected = 3.0 * E * (6.0 / 12.0) * 0.1 / 343.0
    assert hooks.deflection_force(g, E) == pytest.approx(expected, rel=1e-9)
    force = hooks.insertion_force(expected, 45.0, 0.4)
    assert force == pytest.approx(expected * (1.0 + 0.4) / (1.0 - 0.4), rel=1e-9)
    assert math.isinf(hooks.insertion_force(expected, 60.0, 0.7))  # self-locking face


def test_permissible_strain_interpolates_between_in_plane_and_across_layers() -> None:
    in_plane = hooks.permissible_strain(2.0, 0.45, 0.0, repeated_use=False)
    across = hooks.permissible_strain(2.0, 0.45, 1.0, repeated_use=False)
    assert in_plane == pytest.approx(0.02)
    assert across == pytest.approx(0.02 * 0.45)
    mid = hooks.permissible_strain(2.0, 0.45, math.sqrt(0.5), repeated_use=False)
    assert across < mid < in_plane
    assert hooks.permissible_strain(2.0, 0.45, 0.0, repeated_use=True) == pytest.approx(
        0.012
    )


@pytest.mark.parametrize(
    ("material", "cos_to_build", "deflection", "expect_pass"),
    [
        ("PLA", 0.0, 0.2, True),  # arm in the layer plane: 0.61 % vs 2 %
        (
            "PLA",
            1.0,
            0.2,
            False,
        ),  # arm along Z: 0.61 % vs 2 % x 0.45 = 0.9 % -> SF 1.5 < 2
        ("PETG", 1.0, 0.2, True),  # 0.61 % vs 4.5 % x 0.7 = 3.15 %
        ("PLA", 0.0, 0.6, False),  # 1.8 % vs 2 %
        ("PETG", 0.0, 0.6, True),  # 1.8 % vs 4.5 %
    ],
)
def test_reference_hook_against_card_strain_limits(
    material: str, cos_to_build: float, deflection: float, expect_pass: bool
) -> None:
    from materials import get_material

    m = get_material(material)
    geometry = hooks.HookGeometry(1.0, 7.0, 6.0, deflection)
    limit = hooks.permissible_strain(
        m.strain_limit_pct, m.layer_adhesion, cos_to_build, repeated_use=False
    )
    check = hooks.check_hook(
        geometry, modulus_along_arm=m.e_modulus_mpa, permissible=limit
    )
    assert (check.safety_factor >= 2.0) is expect_pass
    assert check.root_strain == pytest.approx(np.float64(1.5 * deflection / 49.0))
