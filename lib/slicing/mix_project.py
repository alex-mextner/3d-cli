"""mix_project.py — lint a project 3MF's mixed parts against the layer rules, before slicing.

WHAT: for an Orca-family project 3MF, three groups of findings:
  - recipe   every mixed filament's pattern (tokens decoded to heads) passes the pattern rules of
             slicing/mix_pattern.py: the additional colour never on adjacent layers, no colour band
             thicker than the run limit; judged at the thickest layer of the object that uses it;
  - layers   for every part in a mixed filament, the layers that will be sliced over its height are
             no thicker than the cap (default 0.12 mm). The stack is simulated exactly as Orca builds
             it: the object's `Metadata/layer_heights_profile.txt` entry (variable layer height) when
             there is one, else `layer_height` under the first layer's `initial_layer_print_height`;
  - plate    an object's profile that Orca would silently drop (last z not the object height) is a
             FAIL; so is a prime tower over several objects whose layer heights differ (Orca refuses
             to slice: Print.cpp compares the objects' profiles).
  Raft layers shift the layer stack; with a raft the layer findings are SKIP, not guessed.
WHY: a mixed part sliced at the process layer height (0.2-0.32 mm) prints stripes that read as bands;
  `3d mix profile` writes the capped profile, this lint proves a project has it (2026-10-20,
  Chekhov on the U1: mixed regions need thin layers, variable layers + prime tower = one object).
USED BY: `3d mix check FILE.3mf`. Stdlib only.
"""

from __future__ import annotations

import builtins
import json
import zipfile
from dataclasses import dataclass, field
from typing import Any

from slicing.head_sequence import ProjectError, read_recipes
from slicing.layer_heights import (
    FIRST_LAYER_MM,
    PROFILE_ENTRY,
    cap_profile,
    check_profile,
    layers_in_zone,
    parse_profiles,
    simulate_layers,
    thicknesses,
    uniform_layers,
)
from slicing.mix_faces import PartFaces
from slicing.mix_faces import check as check_faces
from slicing.mix_pattern import TOLERANCE_MM, MixRules, analyse_pattern

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


@dataclass
class Finding:
    scope: str  # recipe | layers | plate
    subject: str  # "filament 5 (GWG)", "PTA / PTA", "plate"
    status: str
    detail: str

    def to_dict(self) -> dict[str, builtins.object]:
        return {
            "scope": self.scope,
            "subject": self.subject,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class MixProjectReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def status(self) -> str:
        states = {f.status for f in self.findings}
        if FAIL in states:
            return FAIL
        return PASS if PASS in states else SKIP

    @property
    def failures(self) -> int:
        return sum(f.status == FAIL for f in self.findings)

    def to_dict(self) -> dict[str, builtins.object]:
        return {
            "check": "mix-layers-project",
            "status": self.status,
            "failures": self.failures,
            "findings": [f.to_dict() for f in self.findings],
        }


def _number(value: Any, default: float) -> float:
    if isinstance(value, list):
        value = value[0] if value else None
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, list):
        value = value[0] if value else None
    return str(value).strip().lower() in ("1", "true", "yes")


def object_layers(
    height: float, profile: list[float] | None, layer_height: float, first: float
) -> list[float]:
    """Layer tops Orca slices an object of `height` mm into."""
    if profile is not None:
        return simulate_layers(profile, height, first)
    return uniform_layers(height, layer_height, first)


def check(project_3mf: str, rules: MixRules = MixRules()) -> MixProjectReport:  # noqa: B008
    """Raises ProjectError for a non-project input or a project without geometry."""
    faces = check_faces(
        project_3mf
    )  # reads recipes, the mixed parts and their z ranges
    report = MixProjectReport()
    try:
        with zipfile.ZipFile(project_3mf) as zf:
            cfg = json.loads(zf.read("Metadata/project_settings.config"))
            patterns, raft = read_recipes(zf)
            names = zf.namelist()
            profiles = (
                parse_profiles(zf.read(PROFILE_ENTRY).decode("utf-8", "replace"))
                if PROFILE_ENTRY in names
                else {}
            )
    except (zipfile.BadZipFile, KeyError, ValueError, TypeError, OSError) as exc:
        raise ProjectError(
            f"{project_3mf}: not a readable Orca-family project 3MF: {exc}"
        ) from exc
    layer_h = _number(cfg.get("layer_height"), 0.2)
    first = _number(cfg.get("initial_layer_print_height"), FIRST_LAYER_MM)
    by_object: dict[int, list[PartFaces]] = {}
    for p in faces.parts:
        by_object.setdefault(p.object_id, []).append(p)
    # layers first: the recipe findings are judged at the thickest layer of the zone
    layer_findings: list[Finding] = []
    thickest: dict[int, float] = {}  # filament id -> thickest layer in its zones
    for oid, parts in by_object.items():
        profile = profiles.get(oid)
        height = parts[0].object_height
        if raft > 0:
            layer_findings.extend(
                Finding(
                    "layers",
                    f"{p.object} / {p.part}",
                    SKIP,
                    f"raft_layers {raft}: the raft shifts the layer stack, not measured",
                )
                for p in parts
            )
            continue
        if profile is not None and (why := check_profile(profile, height)):
            layer_findings.append(
                Finding(
                    "layers",
                    parts[0].object,
                    FAIL,
                    f"layer height profile of object {oid} is dropped by Orca: {why}",
                )
            )
            profile = None
        tops = object_layers(height, profile, layer_h, first)
        th = thicknesses(tops)
        source = (
            "its layer height profile"
            if profile is not None
            else f"layer_height {layer_h:g} mm"
        )
        for p in parts:
            idx = [i for i in layers_in_zone(tops, p.z_lo, p.z_hi) if i > 0]
            worst = max((th[i] for i in idx), default=0.0)
            thickest[p.extruder] = max(thickest.get(p.extruder, 0.0), worst)
            subject = f"{p.object} / {p.part}"
            if not idx:
                layer_findings.append(
                    Finding(
                        "layers",
                        subject,
                        SKIP,
                        f"no layer is sliced over z {p.z_lo:.2f}-{p.z_hi:.2f} mm",
                    )
                )
            elif worst > rules.layer_cap_mm + TOLERANCE_MM:
                n = sum(th[i] > rules.layer_cap_mm + TOLERANCE_MM for i in idx)
                layer_findings.append(
                    Finding(
                        "layers",
                        subject,
                        FAIL,
                        f"{n} of {len(idx)} layers over z {p.z_lo:.2f}-{p.z_hi:.2f} mm are thicker than the "
                        f"{rules.layer_cap_mm:g} mm cap (thickest {worst:.3f} mm, from {source}): "
                        "stripes that thick read as bands; `3d mix profile` caps the layers over mixed parts",
                    )
                )
            else:
                layer_findings.append(
                    Finding(
                        "layers",
                        subject,
                        PASS,
                        f"{len(idx)} layers over z {p.z_lo:.2f}-{p.z_hi:.2f} mm, thickest {worst:.3f} mm (cap {rules.layer_cap_mm:g} mm, from {source})",
                    )
                )
    for fid, pattern in sorted(patterns.items()):
        if not pattern:
            continue  # a ratio (Local-Z) row has no layer pattern
        at = min(thickest.get(fid) or rules.layer_cap_mm, rules.layer_cap_mm)
        result = analyse_pattern(pattern, at, rules)
        subject = f"filament {fid} ({pattern})"
        if result.ok:
            report.findings.append(
                Finding(
                    "recipe",
                    subject,
                    PASS,
                    f"the additional colour is one layer between the others (bands up to {result.max_run_mm:.3f} mm)",
                )
            )
        else:
            report.findings.append(
                Finding(
                    "recipe",
                    subject,
                    FAIL,
                    "; ".join(f"{v.rule}: {v.detail}" for v in result.violations),
                )
            )
    report.findings.extend(layer_findings)
    report.findings.extend(_plate(cfg, profiles, faces.objects))
    return report


def _plate(
    cfg: dict[str, Any], profiles: dict[int, list[float]], objects: int
) -> list[Finding]:
    if objects < 2 or not profiles or not _truthy(cfg.get("enable_prime_tower")):
        return []
    mine = {i: profiles.get(i) for i in range(1, objects + 1)}
    first = next(iter(mine.values()))
    if all(p is not None and p == first for p in mine.values()):
        return []
    return [
        Finding(
            "plate",
            "plate",
            FAIL,
            f"prime tower on with {objects} objects whose layer heights differ "
            f"(profiles for objects {sorted(i for i, p in mine.items() if p is not None)}): "
            "Orca refuses to slice (the prime tower needs the same variable layer height on every object); "
            "make the mixed parts ONE multi-part object, or switch the prime tower off",
        )
    ]


@dataclass
class ObjectPlan:
    object: str
    object_id: int
    height: float
    zones: list[tuple[float, float]]
    profile: list[float]


def plan_profiles(
    project_3mf: str,
    *,
    cap: float,
    hmin: float,
    hmax: float,
) -> list[ObjectPlan]:
    """The capped layer height profile of every object that has a mixed part: the project's own
    layer height (or its existing profile) everywhere, at most `cap` over the mixed parts.
    Raises ProjectError when Orca could not slice the result (a raft, a prime tower over
    several objects)."""
    faces = check_faces(project_3mf)
    try:
        with zipfile.ZipFile(project_3mf) as zf:
            cfg = json.loads(zf.read("Metadata/project_settings.config"))
            _patterns, raft = read_recipes(zf)
            profiles = (
                parse_profiles(zf.read(PROFILE_ENTRY).decode("utf-8", "replace"))
                if PROFILE_ENTRY in zf.namelist()
                else {}
            )
    except (zipfile.BadZipFile, KeyError, ValueError, TypeError, OSError) as exc:
        raise ProjectError(
            f"{project_3mf}: not a readable Orca-family project 3MF: {exc}"
        ) from exc
    if not faces.parts:
        raise ProjectError(
            f"{project_3mf}: no part prints in a mixed filament, nothing to cap"
        )
    if raft > 0:
        raise ProjectError(
            f"{project_3mf}: raft_layers {raft}: the raft shifts the layer stack; switch the raft off"
        )
    if faces.objects > 1 and _truthy(cfg.get("enable_prime_tower")):
        raise ProjectError(
            f"{project_3mf}: prime tower on with {faces.objects} objects: Orca needs the same variable "
            "layer height on every object, so make the parts ONE multi-part object or switch the tower off"
        )
    layer_h = _number(cfg.get("layer_height"), 0.2)
    by_object: dict[int, list[PartFaces]] = {}
    for p in faces.parts:
        by_object.setdefault(p.object_id, []).append(p)
    out = []
    for oid, parts in by_object.items():
        height = parts[0].object_height
        base = profiles.get(oid) or [0.0, layer_h, height, layer_h]
        zones = sorted({(round(p.z_lo, 4), round(p.z_hi, 4)) for p in parts})
        out.append(
            ObjectPlan(
                parts[0].object,
                oid,
                height,
                zones,
                cap_profile(height, zones, cap=cap, base=base, hmin=hmin, hmax=hmax),
            )
        )
    return out


def format_lines(report: MixProjectReport) -> list[str]:
    return [f"{f.status:<4} {f.scope} {f.subject}: {f.detail}" for f in report.findings]
