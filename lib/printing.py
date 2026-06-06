"""printing.py — dry-run print workflow planner.

This module is the headless core for the first bounded `3d print` slice. It validates
printer/profile/job fields and returns a deterministic plan, but it does not discover
LAN printers, upload files, start jobs, or call slicers yet.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import asdict, dataclass
from typing import Any

from errors import InputNotFound, InvalidArgument, UsageError

COMMAND_NAME = "print"

ACCEPTED_INPUT_EXTENSIONS = ("3mf", "gcode", "scad", "stl")
ACCEPTED_PROFILE_EXTENSIONS = ("ini", "json")


@dataclass(frozen=True, slots=True)
class ProfileFields:
    """Slicer profile files selected for a planned print."""

    machine: str | pathlib.Path | None = None
    process: str | pathlib.Path | None = None
    filament: str | pathlib.Path | None = None


@dataclass(frozen=True, slots=True)
class JobFields:
    """User-controlled job metadata for the dry-run plan."""

    name: str
    material: str | None
    copies: int
    start: bool


@dataclass(frozen=True, slots=True)
class DryRunPlan:
    """A deterministic print plan suitable for JSON rendering and future execution."""

    plan_id: str
    mode: str
    input_path: str
    input_format: str
    printer: dict[str, Any]
    profiles: dict[str, str]
    job: dict[str, Any]
    steps: list[str]


def _validate_input(path: str | pathlib.Path) -> tuple[pathlib.Path, str]:
    p = pathlib.Path(path).expanduser()
    if not p.is_file():
        raise InputNotFound(str(path), command=COMMAND_NAME)
    ext = p.suffix.lower().lstrip(".")
    if ext not in ACCEPTED_INPUT_EXTENSIONS:
        raise InvalidArgument(
            "input extension",
            p.suffix or "(none)",
            ["." + ext for ext in ACCEPTED_INPUT_EXTENSIONS],
            command=COMMAND_NAME,
        )
    return p.resolve(), ext


def _validate_profiles(profiles: ProfileFields) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for field in ("machine", "process", "filament"):
        value = getattr(profiles, field)
        if value is None:
            continue
        p = pathlib.Path(value).expanduser()
        if not p.is_file():
            raise InputNotFound(str(value), command=COMMAND_NAME)
        ext = p.suffix.lower().lstrip(".")
        if ext not in ACCEPTED_PROFILE_EXTENSIONS:
            raise InvalidArgument(
                f"{field} profile extension",
                p.suffix or "(none)",
                ["." + ext for ext in ACCEPTED_PROFILE_EXTENSIONS],
                command=COMMAND_NAME,
                extra="Export slicer profiles as .json or .ini files, then pass the matching profile flag.",
            )
        normalized[field] = str(p.resolve())
    return normalized


def _normalize_job(input_path: pathlib.Path, job: JobFields) -> dict[str, Any]:
    name = job.name.strip() or input_path.stem
    if not name:
        raise UsageError(
            "job name cannot be empty",
            command=COMMAND_NAME,
            remediation=["Pass --job-name NAME or use an input file with a non-empty basename."],
        )
    if job.copies < 1:
        raise UsageError(
            f"copies must be >= 1, got {job.copies}",
            command=COMMAND_NAME,
            remediation=["Pass --copies N with N as a positive integer."],
        )
    material = _normalize_material(job.material, input_path)
    return {
        "name": name,
        "material": material,
        "copies": job.copies,
        "start": job.start,
    }


def _normalize_material(material_name: str | None, input_path: pathlib.Path) -> str | None:
    if material_name is None:
        return None
    material_name = material_name.strip()
    if not material_name:
        return None
    from materials import load_materials  # lazy: registry reads yaml and project layers

    materials = load_materials(start=input_path)
    if material_name not in materials:
        raise InvalidArgument(
            "material",
            material_name,
            sorted(materials),
            command=COMMAND_NAME,
            extra="Run `3d materials list`, or define it in ./materials.yaml or ~/.config/3d-cli/materials.yaml.",
        )
    return materials[material_name].name


def _printer_summary(printer_name: str, input_path: pathlib.Path) -> dict[str, Any]:
    from printers import get_printer  # lazy: registry reads yaml and project layers

    printer = get_printer(printer_name, command=COMMAND_NAME, start=input_path)
    return {
        "name": printer.name,
        "bed_mm": printer.bed,
        "nozzle_mm": printer.nozzle_mm,
        "firmware": printer.firmware,
        "default_material": printer.material,
    }


def _plan_steps(input_format: str, *, start: bool) -> list[str]:
    steps = ["validate input"]
    if input_format != "gcode":
        steps.append("slice model")
    steps.append("upload job")
    if start:
        steps.append("start print")
    return steps


def _plan_id(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]


def build_dry_run_plan(
    input_path: str | pathlib.Path,
    *,
    printer_name: str,
    profiles: ProfileFields,
    job: JobFields,
) -> DryRunPlan:
    """Validate fields and build a deterministic dry-run print plan."""
    if not printer_name.strip():
        raise UsageError(
            "`3d print` needs --printer NAME for this dry-run skeleton",
            command=COMMAND_NAME,
            remediation=["Run `3d printers list`, then pass one of those names with --printer."],
        )
    normalized_input, input_format = _validate_input(input_path)
    normalized_profiles = _validate_profiles(profiles)
    normalized_job = _normalize_job(normalized_input, job)
    printer = _printer_summary(printer_name, normalized_input)
    steps = _plan_steps(input_format, start=bool(normalized_job["start"]))

    payload: dict[str, Any] = {
        "mode": "dry-run",
        "input_path": str(normalized_input),
        "input_format": input_format,
        "printer": printer,
        "profiles": normalized_profiles,
        "job": normalized_job,
        "steps": steps,
    }
    return DryRunPlan(plan_id=_plan_id(payload), **payload)


def plan_to_json(plan: DryRunPlan) -> str:
    """Render a plan as stable, human-readable JSON."""
    return json.dumps(asdict(plan), indent=2, sort_keys=True, ensure_ascii=True) + "\n"
