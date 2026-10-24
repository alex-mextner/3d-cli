"""rules.py — the rules file: label name -> colour group, filament assign, depth of the colour volume.

WHAT: `load_rules(path)` reads a JSON (or YAML) rules file into `Rules`; `Rules.rule_of(name)` is the first rule whose
  `label` regex fully matches a label name. The rules file is the ONLY place that maps a label name to a colour; a label
  without a rule stops the build.

RULES FILE:
  {
    "island_mm2": 3.0,            # optional: label islands / holes smaller than this merge into the surround
    "transfer_tol_mm": 0.25,      # optional: a transferred label counts only this close to the target skin
    "rules": [                    # order = priority (the first group is cut out first)
      {"label": "eye_[LR]_iris",  # regex, full match on the label name
       "group": "iris",           # colour group: rules with the same group form ONE colour volume
       "assign": {"mix": "GKGKWK"},   # {"key": "W"} | {"slot": 2} | {"mix": "WR"} | null = the default colour
       "depth_mm": 1.6,           # depth of the colour volume under the skin (required with an assign)
       "pitch_mm": 0.08,          # voxel pitch of the volume (required with an assign)
       "min_speck_mm3": 0.2,      # optional: smallest piece the slicer step keeps
       "owner": "white",          # optional: only the track of this name may deliver this label
       "passive": true}           # optional: the label never overrides a lower-priority track
    ]}

INVARIANTS:
  * `assign` is the `assign` of a `3d prep` colour spec region, so `regions.json` entries are usable there as written.
  * A group name carries ONE (assign, depth, pitch): two rules of one group that disagree are a spec error.
  * `assign: null` marks the default colour: no volume is made for it, but its faces still END the neighbouring
    volumes exactly on the label border (a volume competes against every non-member face of the skin).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from colour_regions import CMD
from errors import InputNotFound, InvalidArgument, UsageError

DEFAULT_ISLAND_MM2 = 3.0
DEFAULT_TRANSFER_TOL_MM = 0.25
_RULE_KEYS = ("label", "group", "assign", "depth_mm", "pitch_mm", "min_speck_mm3", "owner", "passive")
_TOP_KEYS = ("rules", "island_mm2", "transfer_tol_mm")
_ASSIGN_KEYS = ("key", "slot", "mix")


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern[str]
    group: str
    assign: Mapping[str, Any] | None
    depth_mm: float
    pitch_mm: float
    min_speck_mm3: float | None
    owner: str | None
    passive: bool

    @property
    def coloured(self) -> bool:
        return self.assign is not None


@dataclass(frozen=True)
class Rules:
    rules: tuple[Rule, ...]
    island_mm2: float
    transfer_tol_mm: float

    def rule_of(self, name: str) -> Rule:
        for r in self.rules:
            if r.pattern.fullmatch(name):
                return r
        raise UsageError(
            f"label {name!r} has no colour rule",
            command=CMD,
            remediation=[
                "Add a rule for it to the rules file; the patterns are: " + ", ".join(r.pattern.pattern for r in self.rules)
            ],
        )

    def group_order(self) -> list[str]:
        """Group names in priority order (first appearance in the rules)."""
        seen: list[str] = []
        for r in self.rules:
            if r.group not in seen:
                seen.append(r.group)
        return seen

    def group_rule(self, group: str) -> Rule:
        """The first rule of the group: it carries the group's assign, depth and pitch."""
        return next(r for r in self.rules if r.group == group)


def _bad(flag: str, got: object, accepted: Sequence[str]) -> InvalidArgument:
    return InvalidArgument(flag, json.dumps(got) if not isinstance(got, str) else got, accepted, command=CMD)


def _number(raw: Mapping[str, Any], key: str, where: str, *, required: bool) -> float | None:
    v = raw.get(key)
    if v is None:
        if required:
            raise _bad(f"{where}.{key}", v, ["a positive number (mm)"])
        return None
    if isinstance(v, bool) or not isinstance(v, int | float) or v <= 0:
        raise _bad(f"{where}.{key}", v, ["a positive number"])
    return float(v)


def _parse_assign(raw: object, where: str) -> Mapping[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or len(raw) != 1 or next(iter(raw)) not in _ASSIGN_KEYS:
        raise _bad(f"{where}.assign", raw, ['null (default colour)', '{"key": "W"}', '{"slot": 2}', '{"mix": "WR"}'])
    k, v = next(iter(raw.items()))
    if k == "slot" and (isinstance(v, bool) or not isinstance(v, int)):
        raise _bad(f"{where}.assign.slot", v, ["an integer filament slot"])
    if k in ("key", "mix") and (not isinstance(v, str) or not v.strip()):
        raise _bad(f"{where}.assign.{k}", v, ["a non-empty string"])
    return {k: v.strip() if isinstance(v, str) else v}


def _parse_rule(raw: object, i: int) -> Rule:
    where = f"rules[{i}]"
    if not isinstance(raw, dict):
        raise _bad(where, raw, ["an object"])
    extra = sorted(set(raw) - set(_RULE_KEYS))
    if extra:
        raise _bad(where, ", ".join(extra), list(_RULE_KEYS))
    label, group = raw.get("label"), raw.get("group")
    if not isinstance(label, str) or not label:
        raise _bad(f"{where}.label", label, ["a regex matched against the whole label name"])
    if not isinstance(group, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", group):
        raise _bad(f"{where}.group", group, ["letters, digits, _ . - (it names the patch file)"])
    try:
        pattern = re.compile(label)
    except re.error as exc:
        raise _bad(f"{where}.label", f"{label} ({exc})", ["a valid regex"]) from exc
    assign = _parse_assign(raw.get("assign"), where)
    depth = _number(raw, "depth_mm", where, required=assign is not None)
    pitch = _number(raw, "pitch_mm", where, required=assign is not None)
    speck = _number(raw, "min_speck_mm3", where, required=False)
    owner = raw.get("owner")
    if owner is not None and not isinstance(owner, str):
        raise _bad(f"{where}.owner", owner, ["a track name"])
    passive = raw.get("passive", False)
    if not isinstance(passive, bool):
        raise _bad(f"{where}.passive", passive, ["true or false"])
    if passive and assign is not None:
        raise _bad(
            f"{where}.passive", "true with an assign", ["passive labels mean 'nothing here' and carry `assign: null`"]
        )
    return Rule(pattern, group, assign, depth or 0.0, pitch or 0.0, speck, owner, passive)


def _read(path: Path) -> object:
    if not path.is_file():
        raise InputNotFound(str(path), command=CMD)
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        import yaml

        try:
            return yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise UsageError(f"{path}: not valid YAML ({exc})", command=CMD) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsageError(f"{path}: not valid JSON ({exc})", command=CMD) from exc


def parse_rules(raw: object) -> Rules:
    """Validate a parsed rules document."""
    if not isinstance(raw, dict):
        raise _bad("rules file", raw, ["an object with a `rules` list"])
    extra = sorted(set(raw) - set(_TOP_KEYS))
    if extra:
        raise _bad("rules file", ", ".join(extra), list(_TOP_KEYS))
    items = raw.get("rules")
    if not isinstance(items, list) or not items:
        raise _bad("rules", items, ["a non-empty list of rules"])
    rules = tuple(_parse_rule(r, i) for i, r in enumerate(items))
    first: dict[str, Rule] = {}
    for r in rules:
        f = first.setdefault(r.group, r)
        if (f.assign, f.depth_mm, f.pitch_mm) != (r.assign, r.depth_mm, r.pitch_mm):
            raise _bad(
                f"rules: group {r.group!r}",
                "two rules disagree on assign / depth_mm / pitch_mm",
                ["one (assign, depth_mm, pitch_mm) per group name"],
            )
    island = _number(raw, "island_mm2", "rules file", required=False)
    tol = _number(raw, "transfer_tol_mm", "rules file", required=False)
    return Rules(
        rules,
        DEFAULT_ISLAND_MM2 if island is None else island,
        DEFAULT_TRANSFER_TOL_MM if tol is None else tol,
    )


def load_rules(path: str | Path) -> Rules:
    """Read and validate a rules file (.json, .yaml, .yml)."""
    return parse_rules(_read(Path(path)))
