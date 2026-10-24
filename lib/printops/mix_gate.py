"""mix_gate.py — is a plate's colour mix measured on this printer, and is the measurement still fresh?

WHAT: for one printer and a list of mix keys (printops.mix_key), find the `mix_color` record
  that speaks for each mix and judge it:
    current   a recorded, not superseded, not expired record, no newer than the `filament_color`
              records of its filaments (a spool re-measured afterwards moves the prediction the
              mix was judged against)
    never     no current record for this printer and key
    expired   the newest record is past its `valid_until`
    stale     a filament of the mix was re-measured after the mix: one of its `filament_color`
              records is newer, unless it comes from the same readout (equal `params.readout_sha256`
              on both: `3d mix record` writes the filament records and the mix record of one
              readout together, so the former does not outdate the latter)
  `accept_mixes` logs the owner's decision to print a mix as it is (`calibration.accepted`).
WHY: plan §20.7, issue #418 — the tile of 2026-10-17 was printed on predictions alone and came out
  ΔE 16.6 from the mockup. The client side (printer_ops.printops_checks.check_mix_calibration)
  maps the plate's mixed filaments to keys and turns these facts into a preflight line.
INVARIANTS: read-only except `accept_mixes`, which appends events; no Spoolman access (the
  client names the filament ids of each mix).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from printops import db
from printops import events as ev
from printops.calibrations import Calibration, CalibrationError, list_calibrations
from printops.mix_key import MixKeyError, canonical

KIND = "mix_color"
FILAMENT_KIND = "filament_color"
READOUT_KEY = "readout_sha256"  # params key shared by the records of one readout
_LIMIT = 1000


@dataclass(frozen=True)
class MixAsk:
    key: str  # canonical mix key
    filament_ids: tuple[int, ...]


@dataclass(frozen=True)
class MixFact:
    key: str
    status: str  # current | never | expired | stale
    detail: str
    record: Calibration | None = None
    newer_filament: Calibration | None = None

    def to_dict(self) -> dict[str, Any]:
        vals = (self.record.values if self.record else None) or {}
        return {
            "key": self.key,
            "status": self.status,
            "detail": self.detail,
            "record": None
            if self.record is None
            else {
                "id": self.record.id,
                "hex": vals.get("hex"),
                "predicted_hex": vals.get("predicted_hex"),
                "delta_e_predicted": vals.get("delta_e_predicted"),
                "recorded_at": self.record.recorded_at,
                "valid_until": self.record.valid_until,
            },
            "newer_filament_record": self.newer_filament.id
            if self.newer_filament
            else None,
        }


def parse_asks(raw: Any) -> list[MixAsk]:
    """`mixes` of a request body: [{key, filament_ids}] → asks with canonical keys (422 otherwise)."""
    if not isinstance(raw, list) or not raw:
        raise CalibrationError(
            422, "mixes must be a non-empty list of {key, filament_ids}"
        )
    out: dict[str, MixAsk] = {}
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            raise CalibrationError(422, f"mixes[{i}] needs a string `key`")
        ids = item.get("filament_ids") or []
        if not isinstance(ids, list) or not all(
            isinstance(f, int) and not isinstance(f, bool) and f > 0 for f in ids
        ):
            raise CalibrationError(
                422, f"mixes[{i}].filament_ids must be positive integers"
            )
        try:
            key = canonical(item["key"])
        except MixKeyError as e:
            raise CalibrationError(422, str(e)) from e
        out.setdefault(key, MixAsk(key, tuple(sorted(set(ids)))))
    return list(out.values())


def _readout(c: Calibration) -> str | None:
    sha = c.params.get(READOUT_KEY)
    return sha if isinstance(sha, str) and sha else None


def _day(at: str | None) -> str:
    return (at or "?")[:10]


def lookup(
    conn: sqlite3.Connection,
    printer: str,
    asks: Sequence[MixAsk],
    *,
    now: str | None = None,
) -> list[MixFact]:
    stamp = now or db.utc_now()
    filament_records = list_calibrations(
        conn, kind=FILAMENT_KIND, current=True, limit=_LIMIT
    )
    facts: list[MixFact] = []
    for ask in asks:
        records = list_calibrations(
            conn, printer=printer, kind=KIND, mix=ask.key, current=True, limit=1
        )
        if not records:
            facts.append(
                MixFact(ask.key, "never", f"no mix_color record for {printer}")
            )
            continue
        rec = records[0]
        if rec.valid_until and rec.valid_until < stamp:
            facts.append(
                MixFact(
                    ask.key,
                    "expired",
                    f"#{rec.id} expired {_day(rec.valid_until)}",
                    rec,
                )
            )
            continue
        newer = [
            f
            for f in filament_records
            if f.filament is not None
            and f.filament.filament_id in ask.filament_ids
            and (f.recorded_at or "") > (rec.recorded_at or "")
            and not (_readout(f) and _readout(f) == _readout(rec))
        ]
        if newer:
            n = newer[0]  # newest first
            fid = n.filament.filament_id if n.filament else "?"
            facts.append(
                MixFact(
                    ask.key,
                    "stale",
                    f"#{rec.id} ({_day(rec.recorded_at)}) predates filament_color #{n.id} of "
                    f"filament {fid} ({_day(n.recorded_at)})",
                    rec,
                    n,
                )
            )
            continue
        facts.append(
            MixFact(
                ask.key, "current", f"#{rec.id} measured {_day(rec.recorded_at)}", rec
            )
        )
    return facts


def accept_mixes(
    conn: sqlite3.Connection,
    printer: str,
    facts: Sequence[MixFact],
    keys: Sequence[str],
    *,
    reason: str | None,
    actor: str,
    gcode_sha: str | None,
    file: str | None,
    notes: Mapping[str, str] | None = None,
) -> list[str]:
    """Log one `calibration.accepted` event per accepted mix key; returns the keys logged.
    `notes` (mix key -> the finding's text, from the client) replaces the stored lookup detail."""
    if not reason or not reason.strip():
        raise CalibrationError(422, "accepting a mix needs a reason")
    wanted = [canonical(k) for k in keys]
    unknown = [k for k in wanted if k not in {f.key for f in facts}]
    if unknown:
        raise CalibrationError(
            422,
            f"accept names mixes the request does not ask about: {', '.join(unknown)}",
        )
    by_key = {f.key: f for f in facts}
    with db.transaction(conn):
        for key in wanted:
            fact = by_key[key]
            ev.emit(
                conn,
                "calibration.accepted",
                f"{printer}/{key}",
                {
                    "printer": printer,
                    "head": None,
                    "tool": None,
                    "kind": KIND,
                    "mix": key,
                    "status": fact.status,
                    "detail": (notes or {}).get(key) or fact.detail,
                    "reason": reason.strip(),
                    "gcode_sha": gcode_sha,
                    "file": file,
                    "record_id": fact.record.id if fact.record else None,
                },
                actor,
            )
    return wanted


def facts_dict(
    facts: Sequence[MixFact], printer: str, accepted: Sequence[str]
) -> Mapping[str, Any]:
    return {
        "printer": printer,
        "mixes": [f.to_dict() for f in facts],
        "accepted": list(accepted),
    }
