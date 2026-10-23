"""_spool_view.py — what is loaded where, as one report: the data and the text of
`3d printers spools`, and the slot addressing the load/unload/swap/filament subs share.

WHAT: joins three read-only sources into one report per printer —
  3d-api  `GET /v1/printers`         slots: live reading, bound spool (Spoolman), open anomalies;
  3d-api  `GET /v1/spools?loaded=true`  the remaining-filament INTERVAL of each loaded spool;
  printer `adapter.active_spool()`   Moonraker's ONE active spool (the one it books usage to).
  The report states whether Moonraker's active spool AGREES with the spool bound in PrintOps, the
  drift that books usage to the wrong spool.
WHY: the three live in three places (Fluidd's Spoolman dialog, `printops slots`, `printops spool
  ls`); a person loading filament wants one screen, and the doctor wants the same agreement rule.
INVARIANTS: stdlib only, no write anywhere; a printer that cannot be read degrades to a status
  and a reason (`unreachable`, `skipped`), never an exception. The single-active-spool rule is
  `printops.head_sync.single_head_reason` (one source of truth with load/unload).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from errors import InvalidArgument, ThreeDError, UsageError

from printers_cli._common import PrinterRef, parallel_map, require_printer

if TYPE_CHECKING:
    from printer_ops.base import ActiveSpool

ActiveStatus = Literal["agrees", "mismatch", "not-applicable", "unreachable", "skipped"]
AGREES: ActiveStatus = "agrees"
MISMATCH: ActiveStatus = "mismatch"
NOT_APPLICABLE: ActiveStatus = "not-applicable"
UNREACHABLE: ActiveStatus = "unreachable"
SKIPPED: ActiveStatus = "skipped"


@dataclass(frozen=True)
class SlotTarget:
    """A printer slot a command acts on: `printer` (registry name), `slot` the part the 3d-api
    path takes (`T3`, `ams1/2`), `head_index` for a toolhead (None for an AMS slot)."""

    printer: str
    slot: str
    head_index: int | None
    ref: PrinterRef

    @property
    def key(self) -> str:
        return f"{self.printer}/{self.slot}"


def valid_slots(ref: PrinterRef) -> list[str]:
    """Every slot the fleet entry of `ref` declares: head labels, then `<ams id>/<n>`."""
    if ref.fleet is None:
        return []
    out = [h.label for h in ref.fleet.heads]
    for unit in ref.fleet.ams or ():
        out.extend(f"{unit.id}/{n}" for n in range(1, unit.slots + 1))
    return out


def resolve_slot(arg: str, command: str, refs: dict[str, PrinterRef] | None = None) -> SlotTarget:
    """`PRINTER` or `PRINTER/HEAD` (`u1/T3`, `p1s/ams1/2`) -> SlotTarget. A bare printer name
    works only when the fleet entry has exactly one slot; the head is matched case-insensitively
    and written the way the fleet spells it."""
    name, _, rest = arg.removeprefix("slot:").partition("/")
    ref = require_printer(name, command, refs)
    slots = valid_slots(ref)
    cmd = f"printers {command}"
    if rest:
        label = next((s for s in slots if s.casefold() == rest.casefold()), None)
        if label is None:
            # no fleet entry to check against: the 3d-api validates the slot
            if not slots:
                return SlotTarget(ref.name, rest, None, ref)
            raise InvalidArgument("slot", rest, slots, command=cmd,
                                  extra=f"{ref.name} declares these slots in fleet/printers.yaml.")
    elif len(slots) == 1:
        label = slots[0]
    else:
        raise UsageError(
            f"{ref.name} has {len(slots)} slots; say which: {ref.name}/<slot>",
            command=cmd,
            remediation=[f"slots of {ref.name}: {', '.join(slots) or '(none declared)'}"],
        )
    head = next((h for h in (ref.fleet.heads if ref.fleet else ()) if h.label == label), None)
    return SlotTarget(ref.name, label, head.index if head is not None else None, ref)


# ---- the report --------------------------------------------------------------------------------


@dataclass
class ActiveSpoolReport:
    status: ActiveStatus
    note: str
    spool_id: int | None = None  # Moonraker's active spool
    bound_spool_id: int | None = (
        None  # the spool PrintOps has bound to the printer's head
    )
    connected: bool | None = None  # Moonraker reaches its Spoolman
    pending_reports: int | None = None


@dataclass
class Report:
    printers: list[dict[str, Any]] = field(default_factory=list)
    spools_error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"printers": self.printers, "spools_error": self.spools_error}

    @property
    def mismatches(self) -> list[str]:
        return [
            p["name"] for p in self.printers if p["active_spool"]["status"] == MISMATCH
        ]


ActiveReader = Callable[[PrinterRef], "ActiveSpool"]


def read_active_spool(ref: PrinterRef, secs: float) -> ActiveSpool:
    """Moonraker's `[spoolman]` state of the printer behind `ref` (the live read, GET only)."""
    from printer_ops import make_adapter

    if ref.device is None:  # callers check `contactable` first
        raise ThreeDError(f"{ref.name} has no device to read", command="printers spools")
    return make_adapter(ref.device, timeout=secs).active_spool()


def _bound_ids(slots: list[dict[str, Any]]) -> list[int]:
    return [int(sp["spool_id"]) for s in slots for sp in (s.get("spools") or [])]


def active_report(
    view: dict[str, Any], ref: PrinterRef | None, *, live: bool, read: ActiveReader
) -> ActiveSpoolReport:
    """Does Moonraker's active spool agree with the spool PrintOps bound to the printer?"""
    from printops.head_sync import single_head_reason

    slots: list[dict[str, Any]] = view["slots"]
    bound = (
        _bound_ids(slots) if all(s.get("spools") is not None for s in slots) else None
    )
    why = single_head_reason(view["name"], len(slots))
    if why is not None:
        return ActiveSpoolReport(NOT_APPLICABLE, why)
    if ref is None or not ref.contactable:
        return ActiveSpoolReport(
            SKIPPED, f"{view['name']} is never contacted (access {view['access']})"
        )
    if not live:
        return ActiveSpoolReport(SKIPPED, "--no-live: the printer was not asked")
    bound_id = bound[0] if bound else None
    try:
        got = read(ref)
    except ThreeDError as e:
        # an adapter without [spoolman] says so itself (UnsupportedOperation); an unreachable one
        # raises PrinterCommError: only the first is "not applicable"
        from printer_ops.base import UnsupportedOperation

        status = NOT_APPLICABLE if isinstance(e, UnsupportedOperation) else UNREACHABLE
        return ActiveSpoolReport(status, e.message, bound_spool_id=bound_id)

    def seen(status: ActiveStatus, note: str) -> ActiveSpoolReport:
        return ActiveSpoolReport(status, note, got.spool_id, bound_id, got.connected, got.pending_reports)

    if bound is None:
        return seen(SKIPPED, "the spools bound in PrintOps are unknown (Spoolman unreadable)")
    if got.spool_id == bound_id:
        what = "none set, none bound" if bound_id is None else f"#{bound_id}"
        return seen(AGREES, f"Moonraker's active spool {what} is the bound one")
    mine = "no spool" if bound_id is None else f"#{bound_id}"
    theirs = "no active spool" if got.spool_id is None else f"active spool #{got.spool_id}"
    return seen(MISMATCH, f"Moonraker has {theirs} but PrintOps bound {mine}: usage is booked to the wrong spool")


def _enrich(spool: dict[str, Any], loaded: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """A slot's bound spool (3d-api printer view) plus the remaining interval and vendor of its loaded-spool view."""
    extra = loaded.get(spool["spool_id"], {})
    return {**spool, **{k: extra[k] for k in ("remaining_text", "vendor") if k in extra}}


def build_report(
    printer_views: list[dict[str, Any]],
    loaded: list[dict[str, Any]],
    refs: dict[str, PrinterRef],
    *,
    live: bool,
    read: ActiveReader,
    spools_error: str | None = None,
) -> Report:
    """Merge the 3d-api printer views with the loaded-spool views and the live active spools (the
    printers are asked concurrently: one dead host must not queue the others behind its timeout)."""
    by_id = {int(sp["id"]): sp for sp in loaded}
    merged = [
        {**view, "slots": [
            {**s, "spools": None if s.get("spools") is None else [_enrich(sp, by_id) for sp in s["spools"]]}
            for s in view["slots"]
        ]}
        for view in printer_views
    ]
    actives = parallel_map(lambda v: active_report(v, refs.get(v["name"]), live=live, read=read), merged)
    report = Report(spools_error=spools_error)
    for view, active in zip(merged, actives, strict=True):
        entry = {k: view[k] for k in ("name", "model", "access", "adapter")}
        entry["state"] = _state(view.get("live"))
        entry["slots"] = view["slots"]
        entry["active_spool"] = asdict(active)
        report.printers.append(entry)
    return report


def _state(live: dict[str, Any] | None) -> str:
    if live is None:
        return "not read"
    if not live.get("ok"):
        return f"unreadable: {live.get('error')}"
    return str(live.get("state"))


# ---- text --------------------------------------------------------------------------------------

def _live_slot(p: dict[str, Any] | None) -> str:
    if p is None:
        return "?"
    present = p.get("present")
    if present is False:
        return "empty"
    parts = [x for x in (p.get("material"), p.get("color"), p.get("vendor")) if x]
    if not parts:
        return "loaded (type unknown)" if present else "presence unknown"
    return " ".join(parts)


def _bound(spools: list[dict[str, Any]] | None) -> str:
    if spools is None:
        return "?"
    if not spools:
        return "no spool"
    out = []
    for sp in spools:
        left = sp.get("remaining_text") or (f"{sp['remaining_g']:.0f} g" if sp.get("remaining_g") is not None else "")
        color = sp["colors"][0] if sp.get("colors") else ""
        parts = (f"#{sp['spool_id']}", sp["name"], sp.get("material") or "", color, left)
        out.append(" ".join(x for x in parts if x))
    return "; ".join(out)


def render(report: Report) -> str:
    lines: list[str] = []
    if report.spools_error:
        lines.append(f"spools unknown: {report.spools_error}")
    for p in report.printers:
        lines.append(f"{p['name']}  {p['model']}  [{p['access']}]  {p['state']}")
        for s in p["slots"]:
            head = s["head"] + ("" if s["usable"] else f" (unusable: {s['reason']})")
            marks = "  ".join(f"!{a['kind']} #{a['id']}" for a in s["anomalies"])
            lines.append(
                f"  {head:<6} printer: {_live_slot(s['printer'])}  |  {_bound(s['spools'])}"
                + (f"  {marks}" if marks else "")
            )
        a = p["active_spool"]
        flag = {AGREES: "ok", MISMATCH: "MISMATCH"}.get(a["status"], a["status"])
        extra = ""
        if a.get("connected") is False:
            extra = "  (Moonraker cannot reach Spoolman)"
        elif a.get("pending_reports"):
            extra = (
                f"  ({a['pending_reports']} spool(s) with usage waiting for Spoolman)"
            )
        lines.append(f"  active spool: {flag} — {a['note']}{extra}")
    if not report.printers:
        lines.append("no printers in the fleet registry")
    return "\n".join(lines)
