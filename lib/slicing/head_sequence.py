"""Head-sequence check: does each object print, layer by layer, on the heads its filaments call for?

WHAT: for an Orca-family plate project 3MF and its sliced G-code, every object must print
  each layer only on heads its parts' filaments call for on that layer (a subset, not
  equality: a part of a multi-part object may end below the others, so a planned head can
  legitimately be absent from a layer; a head that is NOT planned is the failure). A physical filament prints
  on its own head; a mixed filament (Full Spectrum / Snapmaker Orca
  `mixed_filament_definitions`) with a manual pattern prints one pattern character per
  layer, at any starting phase. Raft layers (`raft_layers`) print in the support filament
  and are skipped. Ratio rows (Local-Z: both components every layer) have no per-layer
  pattern; objects using them are reported `local-z`, not checked.

WHY: a plate-level "set of heads used == set planned" check stays green when a mix is
  printed with another row's recipe. Snapmaker Orca 2.4.0 stored `LayerTools::layer_index`
  once per print_z, so on plates mixing layer heights a 1:1 mix printed a whole cube in
  filament 1. Only a per-object, per-layer head analysis of the G-code catches that.

Ported from babushkin-dom gen/head_sequence.py. Stdlib only.
"""
from __future__ import annotations

import collections
import itertools
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field

# Bambu emits T255 / T1000 as pseudo tool changes; real heads are below this.
_MAX_HEAD_INDEX = 255
_TOOL_RE = re.compile(r"^T(\d+)")
_PLATER_ID_RE = re.compile(rb'key="plater_id"\s+value="(\d+)"', re.IGNORECASE)
# Bounds the exhaustive phase search; real projects use 1-2 mixed rows of <=12 digits per
# object (~144 combinations), so this only rejects adversarial/garbled patterns.
_MAX_PHASE_COMBINATIONS = 100_000
_EXTRUDE_RE = re.compile(r"E[\d.]")


@dataclass
class ObjectResult:
    name: str
    status: str  # "ok" | "FAIL" | "local-z"
    heads: list[int] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    layers: int = 0
    detail: str = ""


@dataclass
class HeadSequenceReport:
    objects: list[ObjectResult]
    mixed_rows: int
    raft_layers: int

    @property
    def failures(self) -> int:
        return sum(1 for o in self.objects if o.status == "FAIL")

    @property
    def applicable(self) -> bool:
        """False when nothing can go wrong per layer: no mixed rows and every object single-head."""
        return self.mixed_rows > 0 or any(len(o.heads) > 1 for o in self.objects)

    def to_dict(self) -> dict[str, object]:
        return {
            "check": "head-sequence",
            "status": "FAIL" if self.failures else ("PASS" if self.applicable else "SKIP"),
            "failures": self.failures,
            "mixed_rows": self.mixed_rows,
            "raft_layers": self.raft_layers,
            "objects": [asdict(o) for o in self.objects],
        }


def read_recipes(zf: zipfile.ZipFile) -> tuple[dict[int, str], int]:
    """Virtual filament id -> manual pattern ('' = ratio/Local-Z row), and raft layer count.

    Virtual ids follow the physical filaments: id = n_physical + 1 + row index. A row's
    pattern is its last comma field when all digits (one 1-based head per layer).
    """
    cfg = json.loads(zf.read("Metadata/project_settings.config"))
    n_phys = len(cfg.get("filament_colour") or [])
    rows = [r for r in str(cfg.get("mixed_filament_definitions") or "").split(";") if r]
    patterns: dict[int, str] = {}
    for i, row in enumerate(rows):
        last = row.split(",")[-1].strip()
        patterns[n_phys + 1 + i] = last if last.isdigit() else ""
    raft = cfg.get("raft_layers", "0")
    if isinstance(raft, list):
        raft = raft[0] if raft else "0"
    return patterns, int(str(raft or "0"))


def read_object_extruders(zf: zipfile.ZipFile) -> dict[str, list[int]]:
    """Object name -> sorted part extruders (object-level value when parts carry none)."""
    root = ET.fromstring(zf.read("Metadata/model_settings.config"))
    objs: dict[str, list[int]] = {}
    for obj in root.findall("object"):
        name_el = obj.find("metadata[@key='name']")
        if name_el is None:
            continue
        ex = {
            int(e.get("value", "1"))
            for part in obj.findall("part")
            for e in part.findall("metadata[@key='extruder']")
        }
        if not ex:
            obj_ex = obj.find("metadata[@key='extruder']")
            ex = {int(obj_ex.get("value", "1")) if obj_ex is not None else 1}
        objs[name_el.get("value", "")] = sorted(ex)
    return objs


def heads_by_layer(lines: Iterable[str]) -> dict[str, dict[int, set[int]]]:
    """Object name -> layer index -> 1-based heads that extruded with XY motion on that layer."""
    tool, layer, cur = 1, -1, None  # single-head printers never emit T
    seen: dict[str, dict[int, set[int]]] = collections.defaultdict(lambda: collections.defaultdict(set))
    for line in lines:
        if line.startswith(";LAYER_CHANGE"):
            layer += 1
        elif (m := _TOOL_RE.match(line)) and int(m.group(1)) < _MAX_HEAD_INDEX:
            tool = int(m.group(1)) + 1
        elif line.startswith("EXCLUDE_OBJECT_START"):
            cur = line.split("NAME=", 1)[1].strip() if "NAME=" in line else None
        elif line.startswith("EXCLUDE_OBJECT_END"):
            cur = None
        elif cur and line.startswith("G1"):
            code = line.split(";", 1)[0]
            if _EXTRUDE_RE.search(code) and ("X" in code or "Y" in code):
                seen[cur][layer].add(tool)
    return seen


def _norm(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s)


def _match_object(gname: str, objs: dict[str, list[int]]) -> str | None:
    """G-code labels decorate project names (`<name>_id_0_copy_0`); match the LONGEST project name
    that is an alphanumeric prefix, so `AB_id_0` is never attributed to a sibling object `A`."""
    g = _norm(gname)
    best: str | None = None
    best_len = 0
    for k in objs:
        n = _norm(k.split(" — ")[0])
        if n and g.startswith(n) and len(n) > best_len:
            best, best_len = k, len(n)
    return best


def _phase_count(mixed: list[int], pat: dict[int, str]) -> int:
    n = 1
    for e in mixed:
        n *= len(pat[e])
    return n


# Subset (<=), not equality: the 3MF gives no per-part height, so a shorter part's head may
# legitimately vanish from upper layers. A wrong head on a layer is what this catches.
def _follows(layers: dict[int, set[int]], keys: list[int], fixed: set[int], mixed: list[int], pat: dict[int, str]) -> bool:
    for phases in itertools.product(*[range(len(pat[e])) for e in mixed]):
        if all(
            layers[k] <= fixed | {int(pat[e][(i + p) % len(pat[e])]) for e, p in zip(mixed, phases)}
            for i, k in enumerate(keys)
        ):
            return True
    return False


class ProjectError(ValueError):
    """The input is not a readable Orca-family project 3MF (missing/malformed metadata)."""


def _read_project(project_3mf: str) -> tuple[dict[int, str], int, dict[str, list[int]], int]:
    try:
        with zipfile.ZipFile(project_3mf) as zf:
            pat, raft = read_recipes(zf)
            objs = read_object_extruders(zf)
            plates = len(set(_PLATER_ID_RE.findall(zf.read("Metadata/model_settings.config")))) or 1
    except (zipfile.BadZipFile, KeyError, ValueError, ET.ParseError, TypeError) as exc:
        raise ProjectError(
            f"{project_3mf}: not a readable Orca-family project 3MF "
            f"(needs Metadata/project_settings.config + model_settings.config): {exc}"
        ) from exc
    return pat, raft, objs, plates


def check(project_3mf: str, gcode: str) -> HeadSequenceReport:
    """Raises ProjectError for a non-project input."""
    pat, raft, objs, plates = _read_project(project_3mf)
    with open(gcode, encoding="utf-8", errors="replace") as fh:
        seen = heads_by_layer(fh)
    results: list[ObjectResult] = []
    if not seen:
        results.append(ObjectResult("(G-code)", "FAIL",
                                    detail="no extrusion inside EXCLUDE_OBJECT labels; cannot attribute heads"))
    matched: set[str] = set()
    for gname, layers in seen.items():
        key = _match_object(gname, objs)
        if key is None:
            results.append(ObjectResult(gname, "FAIL", detail="not in the project"))
            continue
        matched.add(key)
        ex = objs[key]
        if any(e in pat and not pat[e] for e in ex):
            results.append(ObjectResult(key, "local-z", ex, detail="ratio row, not checked"))
            continue
        mixed = [e for e in ex if e in pat]
        fixed = {e for e in ex if e not in pat}
        keys = sorted(layers)[raft:]
        if _phase_count(mixed, pat) > _MAX_PHASE_COMBINATIONS:
            results.append(ObjectResult(key, "FAIL", ex, [pat[e] for e in mixed], len(keys),
                                        detail=f"more than {_MAX_PHASE_COMBINATIONS} pattern phase combinations"))
            continue
        ok = _follows(layers, keys, fixed, mixed, pat)
        results.append(ObjectResult(key, "ok" if ok else "FAIL", ex, [pat[e] for e in mixed], len(keys)))
    # A single-plate project's G-code must print every object; on a multi-plate project the
    # other plates' objects are legitimately absent, and model_settings does not say which.
    if plates == 1 and seen:
        for key in objs:
            if key not in matched:
                results.append(ObjectResult(key, "FAIL", objs[key], detail="not printed in the G-code"))
    return HeadSequenceReport(results, len(pat), raft)



def format_lines(report: HeadSequenceReport) -> list[str]:
    out = []
    for o in report.objects:
        if o.status == "local-z" or o.detail:
            out.append(f"{o.status:<4} {o.name}: {o.detail}")
        else:
            out.append(f"{o.status:<4} {o.name}: heads {o.heads}, patterns {o.patterns}, {o.layers} layers")
    out.append(f"objects off their recipes: {report.failures}")
    return out


def preflight_check(gcode: str, source_3mf: str) -> dict[str, object]:
    """print-preflight entry: check `head_sequence` -> {status PASS/FAIL/SKIP, detail, objects}."""
    report = check(source_3mf, gcode)
    result = report.to_dict()
    result["check"] = "head_sequence"
    result["detail"] = "; ".join(format_lines(report))
    return result
