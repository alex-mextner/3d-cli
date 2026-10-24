"""mix_uses.py — which mixed filaments a project 3MF's plate prints, on which heads, in what colour.

WHAT: `read_uses(project)` lists every mixed filament (a `mixed_filament_definitions` row, manual
  layer pattern or Local-Z ratio) that at least one printed part of the plate uses:
    virtual_id   Orca's id of the mixed filament (physical count + 1 + row index)
    heads        1-based physical heads, one per layer of the pattern cycle (a ratio row: its two)
    ratio        (percent of the row's first filament, percent of the second) for a Local-Z row
    layer_height the layer height the mix prints at (the part's own override, else the project's)
    objects      the names of the objects printing it
    target_hex   the model colour of the first of them that carries one, when the project has any
  `mix_key(use, filament_ids)` turns a use into the canonical key of printops.mix_key once the
  Spoolman filament id behind every head is known.
WHY: issue #418 / plan §20.7 — the `mix_calibration` preflight line asks 3d-api whether each of
  these mixes was measured on the printer; it needs the key (filaments by id, not by head) and the
  colour the part is meant to have (the measured mix is judged against it).
MODEL COLOUR: 3MF core colour of an object — `<object pid pindex>` into a `<basematerials>`
  (`displaycolor`) or a materials-extension `<m:colorgroup>` (`color`). An object without one has
  no target; the preflight then judges only freshness.
INVARIANTS: stdlib only; reads the main model with iterparse and drops every mesh as it ends, so a
  project with millions of triangles costs no memory here.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from printops.mix_key import MixKeyError, cycle_key, ratio_key
from slicing.head_sequence import ProjectError
from slicing.mix_faces import read_settings
from slicing.mix_pattern import parse_row

_NOT_PRINTED = ("negative_part", "support_blocker", "support_enforcer")
_MAIN_MODEL = "3D/3dmodel.model"
_HEX = re.compile(r"^#?([0-9A-Fa-f]{6})(?:[0-9A-Fa-f]{2})?$")


@dataclass(frozen=True)
class MixUse:
    virtual_id: int
    heads: tuple[int, ...]
    ratio: tuple[int, int] | None
    layer_height: float
    objects: tuple[str, ...]
    target_hex: str | None = None
    target_from: str | None = None

    @property
    def name(self) -> str:
        """`431` for a manual pattern, `local-z 1:75 4:25` for a ratio row."""
        if self.ratio is None:
            return "".join(str(h) for h in self.heads)
        return "local-z " + " ".join(
            f"{h}:{p}" for h, p in zip(self.heads, self.ratio, strict=True)
        )

    def distinct_heads(self) -> tuple[int, ...]:
        return tuple(dict.fromkeys(self.heads))


def mix_key(use: MixUse, filament_ids: Mapping[int, int]) -> str:
    """Canonical mix key; `filament_ids` maps a 1-based head to its Spoolman filament id.
    Raises MixKeyError when a head has no id or the mix cannot be keyed."""
    missing = [h for h in use.distinct_heads() if h not in filament_ids]
    if missing:
        raise MixKeyError(
            f"no filament id for head(s) {', '.join(f'T{h}' for h in missing)}"
        )
    if use.ratio is None:
        return cycle_key(use.layer_height, [filament_ids[h] for h in use.heads])
    shares: dict[int, int] = {}
    for head, pct in zip(use.heads, use.ratio, strict=True):
        fid = filament_ids[head]
        shares[fid] = shares.get(fid, 0) + pct
    return ratio_key(use.layer_height, shares)


def _positive(raw: object, default: float) -> float:
    value = raw[0] if isinstance(raw, list) and raw else raw
    try:
        v = float(str(value).strip())
    except ValueError:
        return default
    return v if v > 0 else default


def _recipe(
    row: str, n_phys: int, index: int
) -> tuple[int, tuple[int, ...], tuple[int, int] | None]:
    """(virtual id, heads, ratio) of one row; ValueError when it has no two filament numbers."""
    parsed = parse_row(row)
    vid = n_phys + 1 + index
    if parsed.pattern:
        return vid, tuple(int(c) for c in parsed.slots), None
    fields = [f.strip() for f in row.split(",")]
    pct = int(fields[4]) if len(fields) > 4 and fields[4].isdigit() else 50
    pct = min(max(pct, 0), 100)
    return vid, (parsed.component_a, parsed.component_b), (100 - pct, pct)


def _hex(raw: str | None) -> str | None:
    m = _HEX.match((raw or "").strip())
    return f"#{m.group(1).upper()}" if m else None


def _object_colours(zf: zipfile.ZipFile) -> dict[str, str]:
    """Object id -> model colour from the main model's colour resources."""
    groups: dict[str, list[str | None]] = {}
    objects: list[tuple[str, str, str]] = []  # (object id, pid, pindex)
    with zf.open(_MAIN_MODEL) as fh:
        for _event, el in ET.iterparse(fh, events=("end",)):
            tag = el.tag.rsplit("}", 1)[-1]
            if tag == "mesh":
                el.clear()  # the geometry is not read here
            elif tag == "basematerials":
                groups[el.get("id", "")] = [
                    _hex(b.get("displaycolor")) for b in el if b.tag.endswith("base")
                ]
            elif tag == "colorgroup":
                groups[el.get("id", "")] = [
                    _hex(c.get("color")) for c in el if c.tag.endswith("color")
                ]
            elif tag == "object" and el.get("pid") is not None:
                objects.append(
                    (el.get("id", ""), el.get("pid", ""), el.get("pindex", "0"))
                )
    out: dict[str, str] = {}
    for oid, pid, pindex in objects:
        colours = groups.get(pid)
        if colours and pindex.isdigit() and int(pindex) < len(colours):
            colour = colours[int(pindex)]
            if colour:
                out[oid] = colour
    return out


def read_uses(project_3mf: str) -> list[MixUse]:
    """The mixed filaments the plate's printed parts use, in virtual-id order. [] when none.
    Raises ProjectError for a file that is not a readable Orca-family project."""
    try:
        with zipfile.ZipFile(project_3mf) as zf:
            cfg = json.loads(zf.read("Metadata/project_settings.config"))
            n_phys = len(cfg.get("filament_colour") or [])
            rows = [
                r
                for r in str(cfg.get("mixed_filament_definitions") or "").split(";")
                if r
            ]
            if not rows:
                return []
            project_layer = _positive(cfg.get("layer_height"), 0.2)
            recipes: dict[int, tuple[tuple[int, ...], tuple[int, int] | None]] = {}
            for i, row in enumerate(rows):
                try:
                    vid, heads, ratio = _recipe(row, n_phys, i)
                except ValueError:
                    continue
                recipes[vid] = (heads, ratio)
            settings = read_settings(zf)
            colours = _object_colours(zf)
    except (
        zipfile.BadZipFile,
        KeyError,
        ValueError,
        ET.ParseError,
        TypeError,
        OSError,
    ) as exc:
        raise ProjectError(
            f"{project_3mf}: not a readable Orca-family project 3MF: {exc}"
        ) from exc

    users: dict[int, list[tuple[str, float, str | None]]] = defaultdict(list)
    for oid, st in settings.items():
        printed = {
            pex or st.extruder
            for part_id, (_name, pex) in st.parts.items()
            if st.subtypes.get(part_id, "normal_part") not in _NOT_PRINTED
        }
        for ex in sorted((printed or {st.extruder}) & recipes.keys()):
            users[ex].append(
                (st.name, st.layer_height or project_layer, colours.get(oid))
            )
    out: list[MixUse] = []
    for vid in sorted(users):
        heads, ratio = recipes[vid]
        coloured = next(((n, c) for n, _h, c in users[vid] if c), None)
        out.append(
            MixUse(
                vid,
                heads,
                ratio,
                users[vid][0][1],
                tuple(dict.fromkeys(n for n, _h, _c in users[vid])),
                coloured[1] if coloured else None,
                coloured[0] if coloured else None,
            )
        )
    return out
