"""Cantilever snap-hook closed forms and the permissible-strain check.

A snap hook is a short cantilever arm (thickness t, free length L, width w) whose barb is pushed
aside by `y` during assembly. The arm is strain-limited, not stress-limited, so the check is
`eps_root <= eps_permissible` (Bayer "Snap-Fit Joints for Plastics", Table 1 and Table 2).

  constant section      eps = 1.5 t y / L^2
  linear taper to r*t   eps = t y (1 - r)^3 / (2 L^2 g(r))    (g from beams._taper_g; r = 0.5 -> 0.918 t y / L^2)
  deflection force      P = 3 E I y / L^3 (constant), generally from the tapered tip deflection
  insertion / pull-off  F = P (tan(a) + mu) / (1 - mu tan(a)), a = barb face angle from the insertion axis

All lengths mm, forces N, moduli MPa, strains dimensionless.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from structural import beams


@dataclass(frozen=True)
class HookGeometry:
    """Rectangular snap-hook arm. `tip_ratio` = tip thickness / root thickness (1 = constant section)."""

    thickness_mm: float
    length_mm: float
    width_mm: float
    deflection_mm: float
    tip_ratio: float = 1.0

    def validate(self) -> None:
        beams._positive(
            thickness_mm=self.thickness_mm,
            length_mm=self.length_mm,
            width_mm=self.width_mm,
            deflection_mm=self.deflection_mm,
        )
        if not 0.0 < self.tip_ratio <= 1.0:
            raise ValueError(f"tip_ratio must be in (0, 1], got {self.tip_ratio!r}")


def root_strain(geometry: HookGeometry) -> float:
    """Root bending strain for the imposed tip deflection (dimensionless)."""
    geometry.validate()
    t, length, y, r = (
        geometry.thickness_mm,
        geometry.length_mm,
        geometry.deflection_mm,
        geometry.tip_ratio,
    )
    k = 1.0 - r
    if k < 1e-3:
        return 1.5 * t * y / length**2
    return t * y * k**3 / (2.0 * length**2 * beams._taper_g(r))


def max_deflection(geometry: HookGeometry, strain_limit: float) -> float:
    """Largest tip deflection that keeps the root strain at `strain_limit`."""
    beams._positive(strain_limit=strain_limit)
    unit = HookGeometry(
        geometry.thickness_mm,
        geometry.length_mm,
        geometry.width_mm,
        1.0,
        geometry.tip_ratio,
    )
    return strain_limit / root_strain(unit)


def deflection_force(geometry: HookGeometry, modulus: float) -> float:
    """Tip force that produces the imposed deflection (E in MPa -> N)."""
    geometry.validate()
    beams._positive(modulus=modulus)
    unit_deflection = beams.tapered_thickness_tip_deflection(
        1.0,
        geometry.length_mm,
        modulus,
        geometry.width_mm,
        geometry.thickness_mm,
        geometry.tip_ratio,
    )
    return geometry.deflection_mm / unit_deflection


def insertion_force(
    deflection_force_n: float, face_angle_deg: float, friction: float
) -> float:
    """Force along the insertion axis needed to push the barb over: P (tan a + mu) / (1 - mu tan a)."""
    beams._positive(deflection_force_n=deflection_force_n)
    if not 0.0 <= face_angle_deg < 90.0:
        raise ValueError(f"face_angle_deg must be in [0, 90), got {face_angle_deg!r}")
    if not 0.0 <= friction < 1.0:
        raise ValueError(f"friction must be in [0, 1), got {friction!r}")
    tan_a = math.tan(math.radians(face_angle_deg))
    denom = 1.0 - friction * tan_a
    if denom <= 0.0:
        return math.inf  # self-locking barb face: no finite push-through force
    return deflection_force_n * (tan_a + friction) / denom


def permissible_strain(
    strain_limit_inplane_pct: float,
    layer_adhesion: float,
    cos_to_build_axis: float,
    *,
    repeated_use: bool,
) -> float:
    """Permissible strain for a bending fibre whose direction makes angle theta with the build axis.

    In-plane (theta = 90 deg) the limit is the card's `strain_limit_pct`; along the build axis
    (theta = 0, the bending fibre crosses the layers) it is scaled by `layer_adhesion`. In between
    the same ellipse interpolation as the stress allowables is used. Repeated assembly applies the
    Bayer 0.6 derating.
    """
    beams._positive(
        strain_limit_inplane_pct=strain_limit_inplane_pct, layer_adhesion=layer_adhesion
    )
    if not 0.0 <= abs(cos_to_build_axis) <= 1.0 + 1e-9:
        raise ValueError(
            f"cos_to_build_axis must be in [-1, 1], got {cos_to_build_axis!r}"
        )
    c2 = min(1.0, cos_to_build_axis**2)
    inplane = strain_limit_inplane_pct / 100.0
    along_z = inplane * layer_adhesion
    value = 1.0 / math.sqrt(c2 / along_z**2 + (1.0 - c2) / inplane**2)
    return value * (0.6 if repeated_use else 1.0)


@dataclass(frozen=True)
class HookCheck:
    """Result of one snap-hook strain check. Forces are None when the barb face self-locks (no finite force)."""

    root_strain: float
    permissible_strain: float
    safety_factor: float
    max_deflection_mm: float
    deflection_force_n: float
    insertion_force_n: float | None
    pull_off_force_n: float | None


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


def check_hook(
    geometry: HookGeometry,
    *,
    modulus_along_arm: float,
    permissible: float,
    lead_angle_deg: float = 30.0,
    retention_angle_deg: float = 45.0,
    friction: float = 0.4,
) -> HookCheck:
    """Evaluate one hook: strain vs permissible strain, flex force, insertion and pull-off force."""
    eps = root_strain(geometry)
    force = deflection_force(geometry, modulus_along_arm)
    return HookCheck(
        root_strain=eps,
        permissible_strain=permissible,
        safety_factor=permissible / eps,
        max_deflection_mm=max_deflection(geometry, permissible),
        deflection_force_n=force,
        insertion_force_n=_finite(insertion_force(force, lead_angle_deg, friction)),
        pull_off_force_n=_finite(insertion_force(force, retention_angle_deg, friction)),
    )
