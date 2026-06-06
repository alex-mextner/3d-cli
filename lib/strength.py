"""Pure helpers for the `3d strength` structural-check skeleton.

This module intentionally does no mesh loading or OpenSCAD execution. It validates and
formats the dry-run/report contract that the command can expose before the solver exists.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any


SUPPORTED_EXTENSIONS = (".scad", ".stl", ".3mf")
AXES = ("X", "Y", "Z")
FIXTURES = ("cantilever", "simple", "compression")

_PLANNED_CHECKS = (
    "validate input part path and units",
    "resolve material properties and anisotropy",
    "export/load a watertight analysis mesh",
    "estimate section geometry and stress from the requested load case",
    "compare stress against material yield with the requested safety factor",
)


@dataclass(slots=True, frozen=True)
class StrengthRequest:
    """Validated structural-check inputs."""

    path: pathlib.Path
    material: str
    load_n: float
    axis: str
    fixture: str
    safety_factor: float
    dry_run: bool


@dataclass(slots=True, frozen=True)
class PlannedCheck:
    """One planned structural check in the dry-run report."""

    name: str
    status: str = "planned"

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status}


@dataclass(slots=True, frozen=True)
class StrengthReport:
    """Structured dry-run output for the strength command."""

    request: StrengthRequest
    status: str
    verdict: str
    material_name: str
    yield_mpa: float
    layer_adhesion: float
    controlling_strength_mpa: float
    steps: tuple[str, ...]
    checks: tuple[PlannedCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "verdict": self.verdict,
            "request": {
                "file": str(self.request.path),
                "material": self.request.material,
                "load_n": self.request.load_n,
                "axis": self.request.axis,
                "fixture": self.request.fixture,
                "safety_factor": self.request.safety_factor,
                "dry_run": self.request.dry_run,
            },
            "material": {
                "name": self.material_name,
                "yield_mpa": self.yield_mpa,
                "layer_adhesion": self.layer_adhesion,
                "controlling_strength_mpa": self.controlling_strength_mpa,
            },
            "steps": list(self.steps),
            "checks": [check.to_dict() for check in self.checks],
            "notes": [
                "dry-run only: no mesh solver has been executed",
                "controlling strength applies the material layer-adhesion knockdown",
            ],
        }


def controlling_strength_mpa(*, yield_mpa: float, layer_adhesion: float) -> float:
    """Return the conservative yield estimate used by the dry-run report."""
    return yield_mpa * layer_adhesion


def build_report(
    request: StrengthRequest,
    *,
    material_name: str,
    yield_mpa: float,
    layer_adhesion: float,
) -> StrengthReport:
    """Create the deterministic structural-check dry-run report."""
    checks = tuple(PlannedCheck(step) for step in _PLANNED_CHECKS)
    return StrengthReport(
        request=request,
        status="DRY-RUN",
        verdict="NOT_EVALUATED",
        material_name=material_name,
        yield_mpa=yield_mpa,
        layer_adhesion=layer_adhesion,
        controlling_strength_mpa=controlling_strength_mpa(
            yield_mpa=yield_mpa,
            layer_adhesion=layer_adhesion,
        ),
        steps=_PLANNED_CHECKS,
        checks=checks,
    )
