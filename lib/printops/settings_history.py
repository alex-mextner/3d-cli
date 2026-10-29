"""settings_history.py — the settings-change journal and the import of the offline buffers (#264, plan §7.4).

ACCESSED VIA: 3d-api routes_settings.py (`POST /v1/settings/import`, `GET /v1/settings/history`)
→ `3d settings import|history` (commands/settings.py).

TABLE `settings_change` (migration 0100): one row per change of a printer setting; UPDATE and
DELETE abort. `uid` is the idempotency key, `entry` keeps the buffer line verbatim (method,
measured readings, notes and refs that have no column).

IMPORT of one buffer file (lines parsed by printops.history_buffer; a torn line stops it all):
  skipped   the uid is already in the table with the identical entry (a second import adds 0);
            the same uid with a different entry is refused — one uid, two changes
  imported  a settings_change row, plus a calibration record when the line is one (below)
  refused   nothing of the line is written: a line that should become a calibration record but
            cannot (no spool to bind it to, values outside the catalog, unusable head), an unknown
            printer/head/status, a `reverts` uid not imported yet. A later import with
            `--spool UID=ID` can still bring it in.

CALIBRATION MAPPING (the decision of #264; the catalog is fleet/calibrations.yaml):
  which    `kind` is a catalog kind, or `kind: slicer_profile` with a `measured` block whose key
           ends in `/ <orca key>` that a catalog kind applies (`apply: orca:<key>`):
           `… / filament_max_volumetric_speed` → max_volumetric_speed.
  status   applied, measured → a record. measured_not_applied → a record only when the measured
           values equal the ones in force (`new` == `old` on the catalog values): the run then
           confirms what the printer runs. Otherwise the line is journal-only — the printer still
           runs `old`, and a record of `new` would make the calibration_current gate demand a
           value no profile carries. restored, observed, rolled_back → journal-only.
  values   per catalog value name: the name itself in `new`, else its G-code footer key, else
           (single-value kinds) its Orca apply key — `filament_flow_ratio` → flow_ratio.
  filament (kinds scoped to a filament), the first that applies: the `--spool UID=ID` override;
           the line's spool_id / filament_id; the spool_binding of the same printer/head in force at
           the line's time (the last change before it, else the first one recorded — bindings are
           often written down after loading), and its colour must equal the line's `spool.color_hex`. Spoolman colours alone are not used: two spools may share one
           (the K1's orange #13 and Boris' orange #9).
  method   the `--method UID=M` override (must be a catalog method of the kind), else the line's
           method when the catalog lists it, else `manual` (the original kept in
           params.buffer_method); a kind without `manual` (input_shaper) is refused instead,
           naming the methods to pass — a method is never guessed.
  time     `at` (a bare date → 00:00Z that day, the conservative side for invalidation events;
           an offset → UTC), else `recorded_at`. Superseding follows this time
           (calibrations.insert_past).
  applied  status applied → applied_to [{target: the kind's first apply target, before, after, at,
           confirmed_by, note: the line's applied_to text}].
DRY RUN: the whole import runs in one transaction that is then rolled back.
"""

from __future__ import annotations

import datetime
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from printops import calibrations as cal
from printops import db
from printops import events as ev
from printops import history_buffer as hb
from printops.calibration_catalog import Catalog, Kind

STATUSES = (
    "measured",
    "measured_not_applied",
    "applied",
    "restored",
    "observed",
    "rolled_back",
)
RECORD_STATUSES = ("measured", "applied")
_CHUNK = 500  # uids per `IN (…)` lookup: below SQLite's oldest bind-parameter cap (999)
COLUMNS = (
    "id",
    "uid",
    "at",
    "recorded_at",
    "printer",
    "head",
    "plate",
    "nozzle_install_id",
    "spool_id",
    "filament_id",
    "lot_nr",
    "kind",
    "key",
    "old",
    "new",
    "status",
    "source",
    "actor",
    "command",
    "evidence",
    "calibration_id",
    "maintenance_id",
    "reverts",
    "imported_at",
    "imported_by",
)
_JSON_COLUMNS = ("old", "new", "evidence")


class HistoryError(Exception):
    """A request that cannot be done; `status` is the HTTP status 3d-api answers."""

    def __init__(
        self, status: int, message: str, *, detail: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = dict(detail or {})


class _Rollback(Exception):
    """Raised inside the transaction of a dry run to undo it."""


class _Refused(Exception):
    pass


def instant(raw: Any) -> str:
    """An ISO date or datetime as db.utc_now() text; a bare date is 00:00Z, naive is UTC."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"not an ISO date/time: {raw!r}")
    text = raw.strip()
    if len(text) == 10:
        dt = datetime.datetime.combine(
            datetime.date.fromisoformat(text),
            datetime.time(),
            tzinfo=datetime.timezone.utc,
        )
    else:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        dt = dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)
    try:
        dt = dt.astimezone(datetime.timezone.utc)
    except OverflowError as e:  # e.g. 9999-12-31T23:00-05:00 is past datetime.max in UTC
        raise ValueError(f"{text} is out of the date range") from e
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _canonical(data: Mapping[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _color(raw: Any) -> str | None:
    text = str(raw).strip().lstrip("#").upper() if isinstance(raw, str) else ""
    return text or None


def _int_or_none(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


# -- calibration mapping ------------------------------------------------------------------------


def calibration_kind(catalog: Catalog, d: Mapping[str, Any]) -> Kind | None:
    """The catalog kind a buffer line records, or None (module doc, CALIBRATION MAPPING)."""
    kind = d.get("kind")
    if isinstance(kind, str) and kind in catalog.kinds:
        return catalog.kinds[kind]
    if (
        kind == "slicer_profile"
        and isinstance(d.get("measured"), dict)
        and isinstance(d.get("key"), str)
    ):
        _, sep, tail = d["key"].rpartition("/")
        if sep:
            target = f"orca:{tail.strip()}"
            return next((k for k in catalog.kinds.values() if target in k.apply), None)
    return None


def _aliases(kind: Kind, name: str) -> list[str]:
    aliases = [name] + [g.key for g in kind.gcode if g.value == name]
    if len(kind.values) == 1:
        aliases += [a.split(":", 1)[1] for a in kind.apply if a.startswith("orca:")]
    return aliases


def catalog_values(kind: Kind, obj: Any) -> dict[str, Any]:
    """The kind's values found in a buffer `old`/`new` object, by value name or its keys."""
    if not isinstance(obj, dict):
        return {}
    out: dict[str, Any] = {}
    for name in kind.values:
        hit = next((a for a in _aliases(kind, name) if a in obj), None)
        if hit is not None:
            out[name] = obj[hit]
    return out


# -- import -------------------------------------------------------------------------------------


@dataclass
class LineResult:
    line: int
    uid: str
    kind: str
    key: str
    outcome: str  # imported | skipped | refused
    detail: str
    id: int | None = None
    calibration_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "uid": self.uid,
            "kind": self.kind,
            "key": self.key,
            "outcome": self.outcome,
            "detail": self.detail,
            "id": self.id,
            "calibration_id": self.calibration_id,
        }


@dataclass
class ImportReport:
    file: str | None
    dry_run: bool
    results: list[LineResult] = field(default_factory=list)
    unused_uids: list[str] = field(
        default_factory=list
    )  # override uids not in this file

    def to_dict(self) -> dict[str, Any]:
        def count(outcome: str) -> int:
            return sum(1 for r in self.results if r.outcome == outcome)

        return {
            "file": self.file,
            "dry_run": self.dry_run,
            "lines": len(self.results),
            "counts": {
                "imported": count("imported"),
                "calibrations": sum(
                    1 for r in self.results if r.calibration_id is not None
                ),
                "skipped": count("skipped"),
                "refused": count("refused"),
            },
            "results": [r.to_dict() for r in self.results],
            "unused_uids": self.unused_uids,
        }


@dataclass(frozen=True)
class Overrides:
    """What the importer supplies for calibration lines the buffer leaves open, by uid."""

    spools: dict[str, int] = field(default_factory=dict)
    methods: dict[str, str] = field(default_factory=dict)


@dataclass
class _Plan:
    line: hb.Line
    result: LineResult
    row: dict[str, Any] | None = None  # settings_change columns (imported lines)
    record: cal.PreparedRecord | None = None
    reverts_uid: str | None = None
    binding_uid: str | None = (
        None  # the spool_binding line a colour-bound calibration relies on
    )


@dataclass(frozen=True)
class Binding:
    """One spool_binding change of a head: from `at` on, `spool` (None = unloaded) is on it."""

    at: str
    spool: int | None
    color: str | None
    uid: str


Bindings = dict[
    tuple[str, str], list[Binding]
]  # (printer, head casefolded) → changes, oldest first


def _binding_of(d: Mapping[str, Any]) -> tuple[tuple[str, str], Binding] | None:
    new, printer, head = d.get("new"), d.get("printer"), d.get("head")
    if (
        d.get("kind") != "spool_binding"
        or str(d.get("status") or "").strip() != "applied"
        or not isinstance(new, dict)
        or not isinstance(printer, str)
        or not isinstance(head, str)
    ):
        return None
    try:
        at = instant(d.get("at") or d.get("recorded_at"))
    except ValueError:
        return None
    return (printer, head.casefold()), Binding(
        at,
        _int_or_none(new.get("spool_id")),
        _color(new.get("color_hex")),
        str(d.get("uid")),
    )


def _bindings(
    conn: sqlite3.Connection, printers: Sequence[str], lines: Sequence[hb.Line]
) -> Bindings:
    """Spool binding changes of `printers`: the imported rows plus `lines` (the caller passes only
    lines that import, so a refused binding never binds a calibration)."""
    entries = (
        [
            json.loads(r["entry"])
            for r in conn.execute(
                "SELECT entry FROM settings_change WHERE kind = 'spool_binding' "
                f"AND printer IN ({', '.join('?' * len(printers))})",
                printers,
            )
        ]
        if printers
        else []
    )
    out: Bindings = {}
    for d in entries + [ln.data for ln in lines]:
        b = _binding_of(d)
        if b is not None:
            out.setdefault(b[0], []).append(b[1])
    for changes in out.values():
        changes.sort(key=lambda b: (b.at, b.uid))
    return out


def bound_at(changes: Sequence[Binding], at: str) -> Binding | None:
    """The binding in force at `at`: the last change at or before it. A measurement older than
    every recorded change takes the first one — bindings are often written down after the spool
    was loaded (the K1's orange #13 was bound in 3d-api hours after its calibration prints)."""
    before = [b for b in changes if b.at <= at]
    return before[-1] if before else (changes[0] if changes else None)


def _gcode(evidence: Any) -> tuple[str | None, str | None]:
    """The calibration G-code (path, sha256) named in the evidence list, if any."""
    for item in evidence if isinstance(evidence, list) else []:
        if not isinstance(item, dict):
            continue
        gcode = item.get("gcode")
        g: dict[str, Any] = gcode if isinstance(gcode, dict) else item
        path, sha = g.get("path"), g.get("sha256")
        if isinstance(path, str) and path.endswith(".gcode"):
            return path, sha if isinstance(sha, str) and re.fullmatch(
                r"[0-9a-f]{64}", sha
            ) else None
    return None, None


def _obj(d: Mapping[str, Any], key: str) -> dict[str, Any]:
    v = d.get(key)
    return v if isinstance(v, dict) else {}


def _named_filament(desc: Mapping[str, Any], color: str | None) -> str:
    return (
        " ".join(
            str(x) for x in (desc.get("filament"), f"#{color}" if color else None) if x
        )
        or "no filament"
    )


def _unbound_why(b: Binding | None, where: str) -> str:
    if b is None:
        return f"no spool_binding of {where} is imported"
    if b.spool is None:
        return f"{where} had no spool bound then ({b.at[:16]})"
    return f"the spool bound to {where} then (#{b.spool}, {b.at[:16]}) is #{b.color or '?'}"


def _bound_spool(
    d: Mapping[str, Any],
    uid: str,
    kind: Kind,
    at: str,
    bindings: Bindings,
) -> tuple[int | None, int | None, str, str | None]:
    """The spool bound to the line's head at `at`, when its colour matches; _Refused otherwise."""
    desc = _obj(d, "spool")
    color = _color(desc.get("color_hex"))
    raw_head = d.get("head")
    where = f"{d.get('printer')}/{raw_head if isinstance(raw_head, str) else '?'}"
    key = (
        str(d.get("printer")),
        raw_head.casefold() if isinstance(raw_head, str) else "",
    )
    b = bound_at(bindings.get(key, []), at)
    if b is not None and b.spool is not None and color is not None and b.color == color:
        return (
            b.spool,
            None,
            f"spool #{b.spool} (bound to {where}, colour #{color})",
            b.uid,
        )
    raise _Refused(
        f"{kind.id} is per filament and the line names {_named_filament(desc, color)}: "
        f"{_unbound_why(b, where)}; pass --spool {uid}=<spool id>"
    )


def _spool_for(
    d: Mapping[str, Any],
    uid: str,
    kind: Kind,
    at: str,
    bindings: Bindings,
    spools: Mapping[str, int],
) -> tuple[int | None, int | None, str, str | None]:
    """(spool_id, filament_id, how, binding uid) for a filament-scoped record; _Refused when unbound."""
    if uid in spools:
        return spools[uid], None, f"spool #{spools[uid]} (--spool)", None
    spool, fil = _int_or_none(d.get("spool_id")), _int_or_none(d.get("filament_id"))
    if spool is not None:
        return spool, None, f"spool #{spool}", None
    if fil is not None:
        return None, fil, f"filament #{fil}", None
    return _bound_spool(d, uid, kind, at, bindings)


def _journal_only_reason(
    kind: Kind, status: str, d: Mapping[str, Any], values: Mapping[str, Any]
) -> str | None:
    """Why a line is journal-only (no calibration record), or None when it maps to a record."""
    if status == "measured_not_applied":
        if not values or catalog_values(kind, d.get("old")) != values:
            return f"{kind.id} measured, not applied (the printer runs `old`): journal only"
        return None
    if status not in RECORD_STATUSES:
        return f"{kind.id} {status}: not a measurement, journal only"
    return None


def _method_for(
    kind: Kind,
    uid: str,
    overrides: Overrides,
    line_method: str | None,
    params: dict[str, Any],
) -> str:
    """The calibration method of a line; a method outside the catalog falls back to `manual`."""
    method = overrides.methods.get(uid) or line_method
    if method in kind.methods:
        return str(method)
    if uid in overrides.methods:
        raise _Refused(
            f"{kind.id}: --method {method!r} is not one of {', '.join(kind.methods)}"
        )
    if "manual" not in kind.methods:
        raise _Refused(
            f"{kind.id}: the line's method {line_method or '(none)'} is not in the catalog and "
            f"the kind has no `manual`; pass --method {uid}=<{'|'.join(kind.methods)}>"
        )
    if line_method:
        params["buffer_method"] = line_method
    return "manual"


def _applied_entries(
    kind: Kind,
    d: Mapping[str, Any],
    values: Mapping[str, Any],
    at: str,
    actor: str,
) -> list[dict[str, Any]]:
    return [
        {
            "target": kind.apply[0] if kind.apply else "journal",
            "before": catalog_values(kind, d.get("old")) or None,
            "after": values,
            "at": at,
            "confirmed_by": d.get("decided_by") or actor,
            "note": d.get("applied_to") if isinstance(d.get("applied_to"), str) else None,
        }
    ]


def _run_request(
    d: Mapping[str, Any],
    uid: str,
    kind: Kind,
    method: str,
    scope: tuple[int | None, int | None],
    params: dict[str, Any],
) -> cal.RunRequest:
    gcode_path, gcode_sha = _gcode(d.get("evidence"))
    note = d.get("note") if isinstance(d.get("note"), str) else None
    return cal.RunRequest(
        kind=kind.id,
        method=method,
        printer=str(d["printer"]),
        head=d.get("head")
        if kind.head_scoped and isinstance(d.get("head"), str)
        else None,
        spool_id=scope[0],
        filament_id=scope[1],
        lot_nr=d.get("lot_nr") if isinstance(d.get("lot_nr"), str) else None,
        plate=d.get("plate")
        if "plate" in kind.scope and isinstance(d.get("plate"), str)
        else None,
        mix=d.get("mix") if kind.mix_scoped and isinstance(d.get("mix"), str) else None,
        params=params,
        gcode_path=gcode_path,
        gcode_sha=gcode_sha,
        note=f"imported from settings history {uid}" + (f": {note}" if note else ""),
    )


def _record_for(
    ctx: cal.Ctx,
    d: Mapping[str, Any],
    uid: str,
    kind: Kind,
    at: str,
    bindings: Bindings,
    overrides: Overrides,
) -> tuple[cal.PreparedRecord | None, str, str | None]:
    """The calibration record a line maps to (None + why when it is journal-only), and the uid of
    the spool_binding line the record's spool comes from (colour binding only)."""
    status = str(d["status"]).strip()  # _plan validated the stripped value
    values = catalog_values(kind, d.get("new"))
    journal_only = _journal_only_reason(kind, status, d, values)
    if journal_only is not None:
        return None, journal_only, None
    if not values:
        raise _Refused(f"{kind.id}: `new` carries none of {', '.join(kind.values)}")
    spool_id = filament_id = binding_uid = None
    how = ""
    if "filament" in kind.scope:
        spool_id, filament_id, how, binding_uid = _spool_for(
            d, uid, kind, at, bindings, overrides.spools
        )
    line_method = d.get("method") if isinstance(d.get("method"), str) else None
    params: dict[str, Any] = (
        dict(d["params"]) if isinstance(d.get("params"), dict) else {}
    )
    params["settings_change_uid"] = uid
    method = _method_for(kind, uid, overrides, line_method, params)
    diameter = _obj(d, "nozzle").get("diameter_mm")
    actor = _text(d, "actor")
    past = cal.PastRecord(
        req=_run_request(d, uid, kind, method, (spool_id, filament_id), params),
        values=values,
        at=at,
        by=actor,
        evidence=d["evidence"] if isinstance(d.get("evidence"), list) else [],
        applied_to=_applied_entries(kind, d, values, at, actor)
        if status == "applied"
        else [],
        nozzle_diameter_mm=float(diameter)
        if isinstance(diameter, (int, float))
        else None,
    )
    try:
        prep = cal.prepare_past(ctx, past)
    except cal.CalibrationError as e:
        raise _Refused(f"{kind.id}: {e.message}") from e
    return (
        prep,
        f"{kind.id} {_values_text(prep.values)}" + (f", {how}" if how else ""),
        binding_uid,
    )


def _values_text(values: Mapping[str, Any]) -> str:
    return ", ".join(
        f"{k}={v:g}" if isinstance(v, float) else f"{k}={v}" for k, v in values.items()
    )


def _text(d: Mapping[str, Any], key: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v.strip():
        raise _Refused(f"{key} must be a non-empty string, got {v!r}")
    return v.strip()


def _head_of(printer: str, p: Any, raw_head: Any) -> str | None:
    """The printer's head label a line names (case-insensitive); _Refused when it has no such head."""
    if raw_head is None:
        return None
    label = raw_head.casefold() if isinstance(raw_head, str) else None
    head = next((h.label for h in p.heads if h.label.casefold() == label), None)
    if head is None:
        raise _Refused(f"{printer} has no head {raw_head!r}")
    return head


@dataclass(frozen=True)
class _Header:
    """The validated common fields of a buffer line."""

    printer: str
    kind: str
    key: str
    status: str
    actor: str
    source: str
    head: str | None
    at: str
    recorded: str
    reverts: str | None


def _header(ctx: cal.Ctx, d: Mapping[str, Any]) -> _Header:
    printer, kind_s, key, status = (
        _text(d, "printer"),
        _text(d, "kind"),
        _text(d, "key"),
        _text(d, "status"),
    )
    actor, source = _text(d, "actor"), _text(d, "source")
    if status not in STATUSES:
        raise _Refused(f"status must be one of {', '.join(STATUSES)}, got {status!r}")
    p = ctx.fleet.printers.get(printer)
    if p is None:
        raise _Refused(
            f"unknown printer {printer!r}; fleet has: {', '.join(sorted(ctx.fleet.printers))}"
        )
    head = _head_of(printer, p, d.get("head"))
    try:
        at = instant(d.get("at") or d.get("recorded_at"))
        recorded = instant(d.get("recorded_at") or d.get("at"))
    except ValueError as e:
        raise _Refused(f"at/recorded_at: {e}") from e
    reverts = d.get("reverts")
    if reverts is not None and not isinstance(reverts, str):
        raise _Refused(
            f"reverts must be the uid of the reverted change, got {reverts!r}"
        )
    return _Header(printer, kind_s, key, status, actor, source, head, at, recorded, reverts)


def _row_spool_id(d: Mapping[str, Any], prep: cal.PreparedRecord | None) -> int | None:
    snap_fil = prep.snap.filament if prep is not None else None
    if snap_fil and snap_fil.spool_id is not None:
        return snap_fil.spool_id
    if prep:
        return prep.past.req.spool_id
    return _int_or_none(d.get("spool_id"))


def _row_of(
    line: hb.Line, h: _Header, prep: cal.PreparedRecord | None
) -> dict[str, Any]:
    """The settings_change columns of an imported line."""
    d = line.data
    snap_fil = prep.snap.filament if prep is not None else None
    spool_id = _row_spool_id(d, prep)
    return {
        "uid": line.uid,
        "at": h.at,
        "recorded_at": h.recorded,
        "printer": h.printer,
        "head": h.head,
        "plate": d.get("plate") if isinstance(d.get("plate"), str) else None,
        "nozzle_install_id": _int_or_none((d.get("nozzle") or {}).get("install_id"))
        if isinstance(d.get("nozzle"), dict)
        else None,
        "spool_id": spool_id,
        "filament_id": snap_fil.filament_id
        if snap_fil
        else _int_or_none(d.get("filament_id")),
        "lot_nr": snap_fil.lot_nr
        if snap_fil
        else (d.get("lot_nr") if isinstance(d.get("lot_nr"), str) else None),
        "kind": h.kind,
        "key": h.key,
        "old": json.dumps(d.get("old"), ensure_ascii=False) if "old" in d else None,
        "new": json.dumps(d.get("new"), ensure_ascii=False) if "new" in d else None,
        "status": h.status,
        "source": h.source,
        "actor": h.actor,
        "command": d.get("command") if isinstance(d.get("command"), str) else None,
        "evidence": json.dumps(
            d.get("evidence") if isinstance(d.get("evidence"), list) else [],
            ensure_ascii=False,
        ),
        "maintenance_id": _int_or_none(d.get("maintenance_id")),
        "entry": _canonical(d),
    }


def _plan(
    ctx: cal.Ctx,
    line: hb.Line,
    stored: Mapping[str, str],
    bindings: Bindings,
    overrides: Overrides,
) -> _Plan:
    d = line.data
    res = LineResult(
        line.n,
        line.uid,
        str(d.get("kind") or "?"),
        str(d.get("key") or "?"),
        "refused",
        "",
    )
    plan = _Plan(line, res)
    if line.uid in stored:
        same = stored[line.uid] == _canonical(d)
        res.outcome = "skipped" if same else "refused"
        res.detail = (
            "already imported"
            if same
            else "uid already imported with a different entry: one uid, two changes"
        )
        return plan
    try:
        h = _header(ctx, d)
        kind = calibration_kind(ctx.catalog, d)
        prep, detail, binding_uid = (
            (None, "journal only", None)
            if kind is None
            else _record_for(ctx, d, line.uid, kind, h.at, bindings, overrides)
        )
    except _Refused as e:
        res.detail = str(e)
        return plan
    plan.row = _row_of(line, h, prep)
    plan.record = prep
    plan.reverts_uid = h.reverts
    plan.binding_uid = binding_uid
    res.outcome, res.detail = "imported", detail
    return plan


def _execute(ctx: cal.Ctx, plan: _Plan, now: str) -> None:
    res, row = plan.result, plan.row
    assert row is not None
    conn = ctx.conn
    hit = conn.execute(
        "SELECT entry FROM settings_change WHERE uid = ?", (row["uid"],)
    ).fetchone()
    if hit is not None:  # imported since the plan was made, by a concurrent import
        if hit["entry"] == row["entry"]:
            res.outcome, res.detail = (
                "skipped",
                "already imported (by a concurrent import)",
            )
        else:
            res.outcome, res.detail = (
                "refused",
                (
                    "uid imported by a concurrent import with a different "
                    "entry: one uid, two changes"
                ),
            )
        return
    reverts = None
    if plan.reverts_uid is not None:
        hit = conn.execute(
            "SELECT id FROM settings_change WHERE uid = ?", (plan.reverts_uid,)
        ).fetchone()
        if hit is None:
            res.outcome, res.detail = (
                "refused",
                f"reverts {plan.reverts_uid}, which is not imported",
            )
            return
        reverts = int(hit["id"])
    cal_id = None
    if plan.record is not None:
        rec = cal.insert_past(ctx, plan.record)
        cal_id = rec.id
        res.detail += f" → calibration #{rec.id}" + (
            f" (superseded by #{rec.superseded_by})" if rec.superseded_by else ""
        )
    cols = {
        **row,
        "calibration_id": cal_id,
        "reverts": reverts,
        "imported_at": now,
        "imported_by": ctx.actor,
    }
    cur = conn.execute(
        f"INSERT INTO settings_change ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        tuple(cols.values()),
    )
    res.id, res.calibration_id = int(cur.lastrowid or 0), cal_id
    ev.emit(
        conn,
        "settings.changed",
        f"settings_change:{res.id}",
        {
            "id": res.id,
            "uid": row["uid"],
            "printer": row["printer"],
            "head": row["head"],
            "kind": row["kind"],
            "key": row["key"],
            "status": row["status"],
            "calibration_id": cal_id,
            "imported": True,
        },
        ctx.actor,
    )


def _stored_entries(ctx: cal.Ctx, uids: Sequence[str]) -> dict[str, str]:
    """uid -> canonical entry of the lines already imported."""
    stored: dict[str, str] = {}
    # one bind parameter per uid; SQLite caps them per statement
    for i in range(0, len(uids), _CHUNK):
        chunk = uids[i : i + _CHUNK]
        stored.update(
            (str(r["uid"]), str(r["entry"]))
            for r in ctx.conn.execute(
                f"SELECT uid, entry FROM settings_change WHERE uid IN ({', '.join('?' * len(chunk))})",
                chunk,
            )
        )
    return stored


def _repeat_plan(line: hb.Line, same: bool) -> _Plan:
    """The plan of a line whose uid an earlier line of the file already used."""
    return _Plan(
        line,
        LineResult(
            line.n,
            line.uid,
            str(line.data.get("kind") or "?"),
            str(line.data.get("key") or "?"),
            "skipped" if same else "refused",
            "repeats an earlier line"
            if same
            else "uid of an earlier line with a different entry",
        ),
    )


def _plan_lines(
    ctx: cal.Ctx,
    lines: Sequence[hb.Line],
    stored: Mapping[str, str],
    overrides: Overrides,
) -> tuple[list[_Plan], set[str]]:
    """The plan of every line, in file order, and the uids the file holds. Calibration lines are
    planned once the bindings that do import are known."""
    plans: dict[int, _Plan] = {}
    seen: dict[str, str] = {}
    calibration_lines: list[hb.Line] = []
    for line in lines:
        canon = _canonical(line.data)
        if line.uid in seen:
            plans[line.n] = _repeat_plan(line, seen[line.uid] == canon)
            continue
        seen[line.uid] = canon
        if calibration_kind(ctx.catalog, line.data) is not None:
            calibration_lines.append(line)
        else:
            plans[line.n] = _plan(ctx, line, stored, {}, overrides)
    printers = sorted(
        {p for ln in lines if isinstance(p := ln.data.get("printer"), str)}
    )
    importing = [p.line for p in plans.values() if p.result.outcome == "imported"]
    bindings = _bindings(ctx.conn, printers, importing)
    for line in calibration_lines:
        plans[line.n] = _plan(ctx, line, stored, bindings, overrides)
    return [plans[ln.n] for ln in lines], set(seen)


def _binding_lost(
    ctx: cal.Ctx, p: _Plan, todo: Sequence[_Plan], lost: set[str]
) -> bool:
    """Whether the spool binding `p` relies on was refused (or taken by a concurrent import with
    other content, which is then added to `lost`)."""
    uid = p.binding_uid
    if uid is None:
        return False
    if uid not in lost:
        hit = ctx.conn.execute(
            "SELECT entry FROM settings_change WHERE uid = ?", (uid,)
        ).fetchone()
        mine = next((q for q in todo if q.line.uid == uid), None)
        if (
            hit is None
            or mine is None
            or hit["entry"] == _canonical(mine.line.data)
        ):
            return False
        lost.add(uid)
    p.result.outcome = "refused"
    p.result.detail = f"its spool binding {uid} was refused: import again"
    return True


def _run_plans(
    ctx: cal.Ctx, todo: Sequence[_Plan], now: str, dry_run: bool
) -> None:
    try:
        with db.transaction(ctx.conn):
            lost: set[str] = (
                set()
            )  # binding uids a concurrent import took with other content
            for p in todo:
                if _binding_lost(ctx, p, todo, lost):
                    continue
                _execute(ctx, p, now)
                if (
                    p.result.outcome == "refused"
                    and p.row is not None
                    and p.row["kind"] == "spool_binding"
                ):
                    lost.add(p.line.uid)
            if dry_run:
                raise _Rollback
    except _Rollback:
        pass


def import_lines(
    ctx: cal.Ctx,
    raw_lines: Sequence[str],
    *,
    file: str | None = None,
    spools: Mapping[str, int] | None = None,
    methods: Mapping[str, str] | None = None,
    dry_run: bool = False,
) -> ImportReport:
    """Import one buffer file's lines (module doc). Spoolman is read before the write
    transaction opens; the transaction holds every row of the file, or none on a dry run.
    `spools` / `methods` ({uid: …}) supply what a calibration line lacks."""
    overrides = Overrides(dict(spools or {}), dict(methods or {}))
    try:
        lines = hb.parse_lines(raw_lines)
    except hb.HistoryBufferError as e:
        raise HistoryError(
            422, f"{file or 'buffer'}: {e}", detail={"line": e.line}
        ) from e
    stored = _stored_entries(ctx, [ln.uid for ln in lines])
    ordered, seen = _plan_lines(ctx, lines, stored, overrides)
    report = ImportReport(
        file,
        dry_run,
        [p.result for p in ordered],
        sorted(u for u in {*overrides.spools, *overrides.methods} if u not in seen),
    )
    # journal-only lines (spool bindings among them) first: a binding refused inside the
    # transaction (reverts target missing, concurrent import) is then known before any
    # calibration line that relies on it
    todo = sorted(
        (p for p in ordered if p.result.outcome == "imported"),
        key=lambda p: p.record is not None,
    )
    if todo:
        _run_plans(ctx, todo, db.utc_now(), dry_run)
    return report


# -- history ------------------------------------------------------------------------------------


def _row(r: sqlite3.Row) -> dict[str, Any]:
    out = {c: r[c] for c in COLUMNS}
    for c in _JSON_COLUMNS:
        out[c] = json.loads(r[c]) if r[c] is not None else None
    return out


def history(
    conn: sqlite3.Connection,
    *,
    printer: str | None = None,
    kind: str | None = None,
    since: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Journal rows, newest change first (by `at`, then id)."""
    where: list[str] = ["1 = 1"]
    args: list[Any] = []
    for col, val in (("printer", printer), ("kind", kind)):
        if val is not None:
            where.append(f"{col} = ?")
            args.append(val)
    if since is not None:
        try:
            where.append("at >= ?")
            args.append(instant(since))
        except ValueError as e:
            raise HistoryError(
                422, f"since must be an ISO date/time, got {since!r}"
            ) from e
    args.append(max(1, min(limit, 1000)))
    rows = conn.execute(
        f"SELECT * FROM settings_change WHERE {' AND '.join(where)} "
        "ORDER BY at DESC, id DESC LIMIT ?",
        args,
    ).fetchall()
    return [_row(r) for r in rows]
