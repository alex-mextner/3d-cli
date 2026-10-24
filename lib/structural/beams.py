"""Closed-form strength-of-materials formulas (the ANALYTICAL_SCREEN method).

Pure `math`, SI-free: callers pick one consistent unit set, normally N, mm, MPa (N/mm^2). Every
function returns plain floats so the formulas can be unit-tested against textbook values
(Roark's Formulas for Stress and Strain, Timoshenko & Woinowsky-Krieger "Theory of Plates and
Shells", Shigley) and reused by the snap-hook module. Invalid geometry (non-positive or
non-finite) raises ValueError; the command layer turns that into exit 2.
"""

from __future__ import annotations

import math


def _positive(**values: float) -> None:
    for name, value in values.items():
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"{name} must be a positive finite number, got {value!r}")


def rect_inertia(width: float, height: float) -> float:
    """Second moment of area of a width x height rectangle about its centroidal axis, bending in the height direction."""
    _positive(width=width, height=height)
    return width * height**3 / 12.0


def cantilever_tip_deflection(
    force: float, length: float, modulus: float, inertia: float
) -> float:
    """End-loaded cantilever: delta = P L^3 / (3 E I)."""
    _positive(force=force, length=length, modulus=modulus, inertia=inertia)
    return force * length**3 / (3.0 * modulus * inertia)


def cantilever_max_stress(
    force: float, length: float, inertia: float, c: float
) -> float:
    """Root bending stress of an end-loaded cantilever: sigma = P L c / I (c = distance to the extreme fibre)."""
    _positive(force=force, length=length, inertia=inertia, c=c)
    return force * length * c / inertia


def three_point_deflection(
    force: float, span: float, modulus: float, inertia: float
) -> float:
    """Simply supported beam, centre point load: delta = F L^3 / (48 E I)."""
    _positive(force=force, span=span, modulus=modulus, inertia=inertia)
    return force * span**3 / (48.0 * modulus * inertia)


def three_point_max_stress(
    force: float, span: float, inertia: float, c: float
) -> float:
    """Mid-span bending stress of a centre-loaded simple beam: sigma = F L c / (4 I)."""
    _positive(force=force, span=span, inertia=inertia, c=c)
    return force * span * c / (4.0 * inertia)


def _taper_g(ratio: float) -> float:
    """g(r) = 2 r - r^2/2 - 3/2 - ln r, with r = tip/root thickness; ~ k^3/3 as k = 1 - r -> 0."""
    k = 1.0 - ratio
    if k < 1e-3:
        return k**3 / 3.0 + k**4 / 4.0 + k**5 / 5.0
    return 2.0 * ratio - 0.5 * ratio**2 - 1.5 - math.log(ratio)


def tapered_thickness_tip_deflection(
    force: float,
    length: float,
    modulus: float,
    width: float,
    root_thickness: float,
    tip_ratio: float,
) -> float:
    """Cantilever whose thickness tapers linearly from h0 (root) to r*h0 (tip), constant width.

    delta = 12 P L^3 g(r) / (E b h0^3 (1 - r)^3), g(r) = 2r - r^2/2 - 3/2 - ln r  (derived by
    integrating M m / (E I) with I = b h(x)^3 / 12). r = 1 recovers P L^3 / (3 E I).
    """
    _positive(
        force=force,
        length=length,
        modulus=modulus,
        width=width,
        root_thickness=root_thickness,
    )
    if not 0.0 < tip_ratio <= 1.0:
        raise ValueError(f"tip_ratio must be in (0, 1], got {tip_ratio!r}")
    k = 1.0 - tip_ratio
    if k < 1e-3:
        return cantilever_tip_deflection(
            force, length, modulus, rect_inertia(width, root_thickness)
        )
    return (
        12.0
        * force
        * length**3
        * _taper_g(tip_ratio)
        / (modulus * width * root_thickness**3 * k**3)
    )


def tapered_width_tip_deflection(
    force: float, length: float, modulus: float, root_width: float, thickness: float
) -> float:
    """Cantilever of constant thickness whose width tapers linearly to zero at the tip (triangular plan).

    delta = 6 P L^3 / (E b0 t^3): exactly twice the constant-section deflection of the same root
    section, and the root fibre stress is the same 6 P L / (b0 t^2) all along the beam (uniform strength).
    """
    _positive(
        force=force,
        length=length,
        modulus=modulus,
        root_width=root_width,
        thickness=thickness,
    )
    return 6.0 * force * length**3 / (modulus * root_width * thickness**3)


def euler_buckling_load(
    modulus: float, inertia: float, length: float, k_factor: float = 1.0
) -> float:
    """Euler critical load P_cr = pi^2 E I / (K L)^2 (K = 2 fixed-free, 1 pinned-pinned, 0.5 fixed-fixed)."""
    _positive(modulus=modulus, inertia=inertia, length=length, k_factor=k_factor)
    return math.pi**2 * modulus * inertia / (k_factor * length) ** 2


def axial_stress(force: float, area: float) -> float:
    """Uniform axial / compressive stress F / A."""
    _positive(force=force, area=area)
    return force / area


def shear_stress(force: float, area: float) -> float:
    """Average (direct) shear stress tau = F / A."""
    _positive(force=force, area=area)
    return force / area


def bearing_stress(force: float, diameter: float, thickness: float) -> float:
    """Pin / boss bearing stress on the projected area: sigma_b = F / (d t)."""
    _positive(force=force, diameter=diameter, thickness=thickness)
    return force / (diameter * thickness)


def hoop_stress_thin(pressure: float, radius: float, thickness: float) -> float:
    """Thin-wall cylinder hoop stress sigma = p r / t (valid for t < r / 10)."""
    _positive(pressure=pressure, radius=radius, thickness=thickness)
    return pressure * radius / thickness


def hoop_stress_thick_inner(
    pressure: float, inner_radius: float, outer_radius: float
) -> float:
    """Lame thick-wall cylinder, internal pressure, hoop stress at the bore: p (ro^2 + ri^2) / (ro^2 - ri^2)."""
    _positive(pressure=pressure, inner_radius=inner_radius, outer_radius=outer_radius)
    if outer_radius <= inner_radius:
        raise ValueError("outer_radius must exceed inner_radius")
    return (
        pressure
        * (outer_radius**2 + inner_radius**2)
        / (outer_radius**2 - inner_radius**2)
    )


def plate_simply_supported_uniform(
    pressure: float,
    a: float,
    b: float,
    thickness: float,
    modulus: float,
    poisson: float,
    terms: int = 101,
) -> tuple[float, float]:
    """Rectangular plate, simply supported on all edges, uniform pressure: (centre deflection, max bending stress).

    Navier double sine series, odd m, n up to `terms`. Square plate, nu = 0.3 reproduces
    Timoshenko: w = 0.00406 q a^4 / D and sigma = 0.2874 q a^2 / t^2.
    """
    _positive(pressure=pressure, a=a, b=b, thickness=thickness, modulus=modulus)
    if not 0.0 <= poisson < 0.5:
        raise ValueError(f"poisson must be in [0, 0.5), got {poisson!r}")
    flexural = modulus * thickness**3 / (12.0 * (1.0 - poisson**2))
    w = 0.0
    moment = 0.0
    for m in range(1, terms + 1, 2):
        for n in range(1, terms + 1, 2):
            sign = (-1.0) ** ((m + n) // 2 - 1)
            denom = (m**2 / a**2 + n**2 / b**2) ** 2
            coeff = 16.0 * pressure * sign / (math.pi**6 * flexural * m * n * denom)
            w += coeff
            kx = (m * math.pi / a) ** 2
            ky = (n * math.pi / b) ** 2
            moment += flexural * (kx + poisson * ky) * coeff
    return w, 6.0 * moment / thickness**2
