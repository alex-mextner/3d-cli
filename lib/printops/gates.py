"""gates.py — the PrintOps state a print gate needs about one printer, and the offline snapshot.

ACCESSED VIA: 3d-api `GET /v1/gates/state` / `GET /v1/gates/snapshot` (routes_gates.py),
`3d printops gates snapshot|state` (cli/gates.py), and lib/printer_ops/printops_checks.py, which
evaluates the material gates of `3d print-preflight` against a state (issue #164, plan §9.1).

STATE (JSON, version 1) — one printer:
  printer, model, owner, access, enclosure, chamber_heater, plates   fleet/printers.yaml facts
                                     (None = not known)
  heads: [{label, index, usable, reason, nozzle: {diameter_mm, material, type} | None}]
  tools: [{tool, slot, head, spool}] G-code tool index → slot ("T3", or "ams-lite/2" on a
                                     single-head printer with an AMS) and the spool bound there
                                     (Spoolman `printops_slot` = "<printer>/<slot>"), or None
  bindings: "ledger" | "unknown"     "unknown" = no 3d-api behind this state (fleet only): a None
                                     spool then means "not known", not "nothing loaded"
  source, taken_at                   "3d-api" | "fleet"; ISO-8601 UTC
  A bound spool: {id, label, vendor, material, name, color_hex, measured_hex, lot_nr,
  filament_id, nozzle_temp_min/max, bed_temp_min/max, abrasive, hygroscopic_class
  (Spoolman filament + its PrintOps extra fields, None = not entered), remaining (the ledger
  interval, ledger.Interval.to_dict), drying (drying.assess: state/reasons/class or {error}),
  conflict (ids of other spools bound to the same slot)}.
  Later slices ADD keys (maintenance #166, calibrations #167); a gate treats an absent key as
  "not known" (UNVERIFIED), never as "fine".

SNAPSHOT (`3d printops gates snapshot`): {version, taken_at, api, printers: {name: STATE}} — what
`3d print-preflight --offline` (the slicer hook, plan §9.2 tier 1) reads without a network.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from printops import drying, spools
from printops.db import utc_now
from printops.drying_policy import DryingPolicy
from printops.fleet import Fleet, FleetPrinter, Head
from printops.homeassistant import HaStates
from printops.spoolman import Spoolman

STATE_VERSION = 1
SNAPSHOT_ENV = "PRINTOPS_SNAPSHOT"
_FILAMENT_EXTRAS = (
    "nozzle_temp_min",
    "nozzle_temp_max",
    "bed_temp_min",
    "bed_temp_max",
    "abrasive",
    "hygroscopic_class",
    "measured_hex",
)


def snapshot_path(env: Mapping[str, str] | None = None) -> Path:
    """$PRINTOPS_SNAPSHOT, else ~/.cache/3d-cli/printops-snapshot.json."""
    env = os.environ if env is None else env
    raw = env.get(SNAPSHOT_ENV)
    if raw:
        return Path(raw).expanduser()
    from cli import paths

    return paths.cache_dir() / "printops-snapshot.json"


def tool_slots(fp: FleetPrinter) -> list[tuple[int, str, Head | None]]:
    """(G-code tool index, slot, head) for every tool the printer can be sliced for.

    A single-head printer with AMS-like units feeds tool n from the n-th unit slot overall
    (Bambu `T0..T3` = AMS slots 1..4); otherwise tool n is the head with index n."""
    if fp.ams and len(fp.heads) == 1:
        slots = [f"{unit.id}/{n}" for unit in fp.ams for n in range(1, unit.slots + 1)]
        return [(i, slot, fp.heads[0]) for i, slot in enumerate(slots)]
    return [(h.index, h.label, h) for h in sorted(fp.heads, key=lambda h: h.index)]


def _head(h: Head) -> dict[str, Any]:
    nozzle = None
    if h.nozzle is not None:
        nozzle = {
            "diameter_mm": h.nozzle.diameter_mm,
            "material": h.nozzle.material,
            "type": h.nozzle.type,
        }
    return {
        "label": h.label,
        "index": h.index,
        "usable": h.usable,
        "reason": h.reason,
        "nozzle": nozzle,
    }


def fleet_state(fp: FleetPrinter, *, now: str | None = None) -> dict[str, Any]:
    """The state from fleet/printers.yaml alone: topology known, spool bindings unknown."""
    return {
        "version": STATE_VERSION,
        "printer": fp.name,
        "model": fp.model,
        "owner": fp.owner,
        "access": fp.access,
        "enclosure": fp.enclosure,
        "chamber_heater": fp.chamber_heater,
        "plates": list(fp.plates) if fp.plates is not None else None,
        "heads": [_head(h) for h in sorted(fp.heads, key=lambda h: h.index)],
        "tools": [
            {"tool": t, "slot": slot, "head": h.label if h else None, "spool": None}
            for t, slot, h in tool_slots(fp)
        ],
        "bindings": "unknown",
        "source": "fleet",
        "taken_at": now or utc_now(),
    }


def _spool(conn: sqlite3.Connection, sp: Mapping[str, Any]) -> dict[str, Any]:
    fil = sp.get("filament") or {}
    fx = drying.decoded_extra(fil)
    v = spools.view(conn, sp)
    out = {
        "id": v["id"],
        "label": " ".join(
            str(p)
            for p in (f"#{v['id']}", v["vendor"], v["name"] or v["material"])
            if p
        ),
        "vendor": v["vendor"],
        "material": v["material"],
        "name": v["name"],
        "color_hex": v["color_hex"],
        "lot_nr": v["lot_nr"],
        "filament_id": v["filament_id"],
        "remaining": v["remaining"],
        "spoolman_remaining_g": v["spoolman_remaining_g"],
        "conflict": [],
    }
    out.update({k: fx.get(k) for k in _FILAMENT_EXTRAS})
    return out


def printer_state(
    conn: sqlite3.Connection,
    spoolman: Spoolman,
    fp: FleetPrinter,
    *,
    drying_policy: DryingPolicy | None,
    drying_error: str | None = None,
    humidity: HaStates | None = None,
    now: datetime.datetime | None = None,
    all_spools: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """The full state: fleet facts + the spool bound to each tool's slot, with its remaining
    interval and drying assessment. Read-only (writes nothing to Spoolman or the DB).

    `drying_policy` None (policy unreadable) puts `drying: {error: drying_error}` on each spool."""
    moment = now or drying.parse_ts(utc_now())
    state = fleet_state(fp, now=drying.format_ts(moment))
    state["bindings"], state["source"] = "ledger", "3d-api"
    pool = all_spools if all_spools is not None else spools.all_spools(spoolman)
    by_slot: dict[str, list[Mapping[str, Any]]] = {}
    for sp in pool:
        key = spools.slot_of(sp)
        if key and not sp.get("archived"):
            by_slot.setdefault(key, []).append(sp)
    bound: dict[int, Mapping[str, Any]] = {}
    for tool in state["tools"]:
        here = sorted(
            by_slot.get(f"{fp.name}/{tool['slot']}", []), key=lambda s: int(s["id"])
        )
        if not here:
            continue
        tool["spool"] = _spool(conn, here[0])
        tool["spool"]["conflict"] = [int(s["id"]) for s in here[1:]]
        bound[int(here[0]["id"])] = here[0]
    if bound:
        assessed: dict[int, dict[str, Any]] = {}
        if drying_policy is not None:
            for a in drying.assess_all(
                conn,
                spoolman,
                drying_policy,
                humidity=humidity,
                now=moment,
                spool_ids=sorted(bound),
            ):
                assessed[a.spool_id] = {
                    "state": a.state,
                    "class": a.hygroscopic_class,
                    "reasons": a.reasons,
                    "notes": a.notes,
                }
        for tool in state["tools"]:
            sp = tool["spool"]
            if sp is not None:
                sp["drying"] = assessed.get(sp["id"]) or {
                    "error": drying_error or "not assessed"
                }
    return state


def snapshot(
    conn: sqlite3.Connection,
    spoolman: Spoolman,
    fleet: Fleet,
    *,
    drying_policy: DryingPolicy | None,
    drying_error: str | None = None,
    humidity: HaStates | None = None,
    now: datetime.datetime | None = None,
    api: str | None = None,
) -> dict[str, Any]:
    """Every fleet printer's state, reading Spoolman's spool list once."""
    moment = now or drying.parse_ts(utc_now())
    pool = spools.all_spools(spoolman)
    return {
        "version": STATE_VERSION,
        "taken_at": drying.format_ts(moment),
        "api": api,
        "printers": {
            name: printer_state(
                conn,
                spoolman,
                fp,
                drying_policy=drying_policy,
                drying_error=drying_error,
                humidity=humidity,
                now=moment,
                all_spools=pool,
            )
            for name, fp in sorted(fleet.printers.items())
        },
    }


class SnapshotError(ValueError):
    """The snapshot file is missing, not JSON, or not a version-1 snapshot."""


def read_snapshot(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise SnapshotError(
            f"no PrintOps snapshot at {path} (make one: 3d printops gates snapshot)"
        ) from e
    except (OSError, ValueError) as e:
        raise SnapshotError(f"PrintOps snapshot {path} is unreadable: {e}") from e
    if (
        not isinstance(data, dict)
        or data.get("version") != STATE_VERSION
        or not isinstance(data.get("printers"), dict)
    ):
        raise SnapshotError(
            f"{path} is not a version-{STATE_VERSION} PrintOps snapshot"
        )
    return data


def write_snapshot(path: Path, data: Mapping[str, Any]) -> None:
    """Atomic write (the slicer hook may read it at any moment). Each writer gets its own temp
    file, so concurrent refreshes never share one: the last replace wins whole. A writer killed
    mid-write can leave a `<name>.*.tmp` behind; readers never see it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            # mkstemp's 0600 would hide the cache file from other local readers
            os.fchmod(f.fileno(), 0o644)
            f.write(json.dumps(data, indent=1, sort_keys=True) + "\n")
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
