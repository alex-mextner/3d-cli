"""forces.py — snap-hook flex limit and assembly/removal force: the seam to the S4 closed-form hook formulas.

WHAT: `HookModel.estimate(...)` answers two questions about one cantilever snap hook:
  - FLEX LIMIT: the largest tip deflection the arm survives (`strain_limit` x arm geometry);
  - FORCES: the force to flex the arm by the deflection a sweep measured, and the push-in / pull-off force over the
    barb faces (lead / retention angle, friction).
  Both come from `structural.hooks` (Bayer closed forms, umbrella #570 slice S4 #575). When that module is not part
  of this checkout the estimate is reported as `available: false` with the reason and the inputs it needs: the
  sweep, the clearance and the ergonomics checks never depend on it, and no number is invented.

ACCESSED VIA: `assembly.sweep` (snap verdict), `assembly.ergonomics` (force column), `3d assembly sweep|ergonomics`.
"""

from __future__ import annotations

import importlib
import math
from dataclasses import dataclass
from typing import Any

from assembly.spec import Hook, SnapSpec

S4_NOTE = (
    "the closed-form snap-hook formulas ship with the S4 solver (#575, lib/structural/hooks.py); "
    "this checkout has no `structural.hooks`, so no force or strain-derived flex limit is computed"
)


def _s4_hooks() -> Any | None:
    """The S4 hook module, or None when this checkout does not have it."""
    try:
        return importlib.import_module("structural.hooks")
    except ImportError:
        return None


def s4_available() -> bool:
    return _s4_hooks() is not None


@dataclass(frozen=True)
class HookModel:
    """Arm geometry + material of one hook, enough for the S4 closed forms."""

    thickness_mm: float | None
    length_mm: float | None
    width_mm: float | None
    modulus_mpa: float | None
    strain_limit: float | None
    lead_angle_deg: float
    retention_angle_deg: float
    friction: float

    @property
    def complete(self) -> bool:
        return None not in (self.thickness_mm, self.length_mm, self.width_mm)

    def missing(self) -> list[str]:
        names = {
            "arm.thickness_mm": self.thickness_mm,
            "arm.length_mm": self.length_mm,
            "arm.width_mm": self.width_mm,
        }
        return [k for k, v in names.items() if v is None]

    def flex_limit_mm(self) -> tuple[float | None, str]:
        """Tip deflection at the strain limit, with where the number came from."""
        mod = _s4_hooks()
        if mod is None:
            return None, S4_NOTE
        if not self.complete or self.strain_limit is None:
            need = self.missing() + (
                ["snap.strain_limit"] if self.strain_limit is None else []
            )
            return None, f"needs {', '.join(need)} to derive the flex limit"
        geo = mod.HookGeometry(self.thickness_mm, self.length_mm, self.width_mm, 1.0)
        return float(mod.max_deflection(geo, self.strain_limit)), (
            f"structural.hooks.max_deflection at strain {self.strain_limit:g}"
        )

    def estimate(self, deflection_mm: float) -> dict[str, Any]:
        """Strain, flex force and push-in / pull-off force for a measured tip deflection."""
        mod = _s4_hooks()
        out: dict[str, Any] = {
            "deflection_mm": round(deflection_mm, 4),
            "available": False,
        }
        if mod is None:
            out["reason"] = S4_NOTE
            return out
        if not self.complete or self.modulus_mpa is None:
            need = self.missing() + (
                ["snap.modulus_mpa (or a known context.material)"]
                if self.modulus_mpa is None
                else []
            )
            out["reason"] = f"needs {', '.join(need)}"
            return out
        if deflection_mm <= 0:
            out.update(
                available=True,
                strain=0.0,
                deflection_force_n=0.0,
                push_in_force_n=0.0,
                pull_off_force_n=0.0,
            )
            return out
        geo = mod.HookGeometry(
            self.thickness_mm, self.length_mm, self.width_mm, deflection_mm
        )
        strain = float(mod.root_strain(geo))
        flex = float(mod.deflection_force(geo, self.modulus_mpa))

        def over_barb(angle: float) -> float | None:
            value = float(mod.insertion_force(flex, angle, self.friction))
            return value if math.isfinite(value) else None

        out.update(
            available=True,
            source="structural.hooks (Bayer closed forms)",
            strain=round(strain, 5),
            deflection_force_n=round(flex, 3),
            push_in_force_n=_round(over_barb(self.lead_angle_deg)),
            pull_off_force_n=_round(over_barb(self.retention_angle_deg)),
        )
        return out


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def hook_model(
    snap: SnapSpec, hook: Hook, material_modulus_mpa: float | None
) -> HookModel:
    return HookModel(
        hook.arm_thickness_mm,
        hook.arm_length_mm,
        hook.arm_width_mm,
        snap.modulus_mpa if snap.modulus_mpa is not None else material_modulus_mpa,
        snap.strain_limit,
        snap.lead_angle_deg if snap.lead_angle_deg is not None else 30.0,
        snap.retention_angle_deg if snap.retention_angle_deg is not None else 45.0,
        snap.friction,
    )


def material_modulus(material: str | None) -> float | None:
    """Young's modulus (MPa, in-plane) of a registry material, matched by name or word; None when unknown."""
    if not material:
        return None
    from fits_registry import material_identity
    from registries.materials import get_material

    ident = material_identity(material)
    if not ident.known or ident.name is None:
        return None
    return float(get_material(ident.name).e_modulus_mpa)


def flex_limit(
    snap: SnapSpec, hook: Hook, material: str | None
) -> tuple[float | None, str]:
    """The hook's declared flex limit, else the S4-derived one, else None with the reason."""
    if snap.flex_limit_mm is not None:
        return snap.flex_limit_mm, "declared in the spec (snap.flex_limit_mm)"
    return hook_model(snap, hook, material_modulus(material)).flex_limit_mm()
