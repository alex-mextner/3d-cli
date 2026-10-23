"""head_sync.py — after `spool load`, make the printer's head show the spool now bound to it.

ACCESSED VIA: `printops.spools.load` (3d-api `POST /v1/printers/{p}/slots/{s}/load`, so
`3d printops spool load`, the bot and HA) once the Spoolman binding is committed. Issue #400.

WHY: preflight's slot check and the printer screen read the head's filament from the printer
(U1 `print_task_config`). Without this write a freshly loaded U1 head still says `#FFFFFF NONE`
and every print fails the slot check.

WHAT IS WRITTEN: colour (filament `color_hex`, else the first of `multi_color_hexes`), material
(filament `material`), vendor (Spoolman vendor, "Generic" when the filament has none) and the
sub-type "Basic" — through `Adapter.set_head_filament`, read back by the adapter.

PRESSURE ADVANCE: the U1 resets the head's K whenever its filament is written (FLOW_RESET_K,
see printer_ops/moonraker.py). The K is read after the write; when the calibration engine has a
CURRENT pressure_advance record for this head and spool (printops.calibrations.requirements),
its value is re-applied with `Adapter.set_pressure_advance` and read back; otherwise the
firmware's factory K stays and the outcome carries a warning.

ACTIVE SPOOL (Moonraker `[spoolman]`, e.g. the rooted K1 SE): Moonraker keeps ONE active Spoolman
spool and books the printer's usage to it itself (it is the usage writer for such a printer;
PrintOps never books those jobs, docs/printops/oss.md section 9.6). So after the head step, for a
SINGLE-head printer, `Adapter.set_active_spool(<loaded spool>)` makes Moonraker book to the spool
now on the printer, read back (`Outcome.active_spool`). A multi-head printer (U1) is skipped without
contact: one active spool cannot follow tool changes, so any value would misbook a head. `unload`
clears it (`release_active_spool`) only when it IS the unloaded spool and that spool is not
bound to the slot again; the answer reports it, no event is written (nothing is bound for the
reconciler's anomaly to name). A printer without `[spoolman]` (404) or an adapter without the concept is skipped. The
step runs whatever the head step did (a K1 has no head step): head and active spool are separate
event channels for the reconciler, so a failure of one never hides the other's.

OUTCOMES (`Outcome.status`, the head filament write; `Outcome.active_spool` has its own status):
  written  head written and verified (K restored, or factory K kept with a warning)
  skipped  nothing sent: opted out, an AMS slot, an offline printer, an adapter/firmware
           without per-head filament (K1, Moonraker without print_task_config), or the spool
           left the slot (another request) before the write
  failed   refused (job running, official RFID head without force, a value G-code cannot carry,
           a spool without colour) or the printer failed/answered something else on read-back
EVENTS: `slot.printer_written` / `slot.printer_write_failed` (subject `slot:<p>/<head>`, payload
printer/head/spool_id/wanted + after/pressure_advance or error; the active-spool step of a load adds
`write` = "active_spool" and `active_spool` to a written event or `stage` = "active_spool" to a
failed one). The reconciler
turns the latest failure of a still-bound spool into a `printer_write_failed` anomaly
(printops.reconcile).

CONCURRENCY: the write runs after the binding's transaction, so another load may commit in
between. Writes of one slot are serialised (a per-slot lock; 3d-api is one process) and each
re-reads the binding first, so the head ends up showing the last committed load. An `unload`
never writes the head's filament and is not serialised against it: one racing a write leaves the
old filament on the head, which the next reconcile pass reports (`unbound_filament`). Its
active-spool clear takes the same per-slot lock.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from errors import ThreeDError
from printops import db
from printops import events as ev

if TYPE_CHECKING:
    from printer_ops.base import ActiveSpoolWrite, HeadFilament, Slot
    from printer_ops.devices import Device
    from printops.calibrations import Calibration
    from printops.fleet import Fleet, Head
    from printops.spoolman import JSON
    from printops.spools import Ctx, SlotRef

WRITTEN, SKIPPED, FAILED = "written", "skipped", "failed"
EVENT_WRITTEN = "slot.printer_written"
EVENT_FAILED = "slot.printer_write_failed"
DEFAULT_VENDOR = "Generic"
DEFAULT_SUB_TYPE = "Basic"
PA_KIND = "pressure_advance"
ACTIVE_STAGE = "active_spool"  # failure stage and `write` channel of Moonraker's active spool
TIMEOUT_S = 10.0  # per printer request; a load makes at most five head requests and four for the active spool
_SLOT_LOCKS: dict[str, threading.Lock] = {}  # one 3d-api process writes the printers
_SLOT_LOCKS_GUARD = threading.Lock()


def _slot_lock(key: str) -> threading.Lock:
    with _SLOT_LOCKS_GUARD:
        return _SLOT_LOCKS.setdefault(key.casefold(), threading.Lock())


@dataclass
class Outcome:
    status: str
    reason: str | None = None
    wanted: dict[str, str] | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    pressure_advance: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    active_spool: dict[str, Any] | None = None  # Moonraker's active spool, see `_active`

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "wanted": self.wanted,
            "before": self.before,
            "after": self.after,
            "pressure_advance": self.pressure_advance,
            "warnings": self.warnings,
            "active_spool": self.active_spool,
        }


def _active(
    status: str,
    reason: str | None,
    spool_id: int,
    before: int | None = None,
    after: int | None = None,
) -> dict[str, Any]:
    """The `active_spool` part of an outcome: what happened to Moonraker's active spool, which
    was `before` and (read back) is `after` the write; `spool_id` is the spool it concerns."""
    return {
        "status": status,
        "reason": reason,
        "spool_id": spool_id,
        "before": before,
        "after": after,
    }


def wanted_filament(spool: JSON) -> HeadFilament:
    """The head filament a Spoolman spool stands for. ValueError when it has no colour or material."""
    from printer_ops.base import HeadFilament

    fil = spool.get("filament") or {}
    colors = [fil.get("color_hex"), *str(fil.get("multi_color_hexes") or "").split(",")]
    color = next(
        (str(c).strip().lstrip("#") for c in colors if c and str(c).strip()), None
    )
    if color is None:
        raise ValueError(f"spool #{spool['id']}'s filament has no colour in Spoolman")
    material = str(fil.get("material") or "").strip().upper()
    if not material:
        raise ValueError(f"spool #{spool['id']}'s filament has no material in Spoolman")
    vendor = str((fil.get("vendor") or {}).get("name") or "").strip() or DEFAULT_VENDOR
    return HeadFilament(
        color="#" + color.upper(),
        material=material,
        vendor=vendor,
        sub_type=DEFAULT_SUB_TYPE,
    )


def _slot_dict(s: Slot) -> dict[str, Any]:
    return {
        "color": s.color,
        "material": s.material,
        "vendor": s.vendor,
        "sub_type": s.sub_type,
    }


def current_pressure_advance(
    ctx: Ctx, slot: SlotRef, spool_id: int
) -> tuple[Calibration | None, str]:
    """The CURRENT pressure_advance record for this head and spool, or None and why there is none."""
    from printops import calibrations as cal
    from printops.calibration_catalog import CatalogError, load_catalog
    from printops.fleet import FleetError, load_fleet
    from printops.policy import PolicyError, section

    try:
        fleet = load_fleet()
        if fleet is None:
            return None, "no fleet registry"
        cctx = cal.Ctx(
            conn=ctx.conn,
            spoolman=ctx.spoolman,
            actor=ctx.actor,
            catalog=load_catalog(),
            policy=section("calibrations"),
            fleet=fleet,
        )
        rs = cal.requirements(cctx, slot.printer, head=slot.head, spool_id=spool_id)
    except cal.CalibrationError as e:
        return None, f"calibrations unreadable: {e.message}"
    except (CatalogError, PolicyError, FleetError) as e:
        return None, f"calibrations unreadable: {e}"
    req = next(
        (r for r in rs.items if r.kind is not None and r.kind.id == PA_KIND), None
    )
    if req is None:
        return None, f"pressure advance is not tracked for {slot.printer}"
    k = (req.record.values or {}).get("pressure_advance") if req.record else None
    if req.status == "current" and isinstance(k, (int, float)):
        return req.record, ""
    return None, f"pressure advance is {req.status}: {req.detail}"


def _emit(
    ctx: Ctx,
    slot: SlotRef,
    spool_id: int,
    out: Outcome,
    *,
    error_stage: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "printer": slot.printer,
        "head": slot.head,
        "spool_id": spool_id,
        "wanted": out.wanted,
    }
    if out.status == WRITTEN:
        kind = EVENT_WRITTEN
        payload |= {"after": out.after, "pressure_advance": out.pressure_advance}
    else:
        kind = EVENT_FAILED
        payload |= {"error": out.reason, "stage": error_stage}
    with db.transaction(ctx.conn):
        ev.emit(ctx.conn, kind, slot.path, payload, ctx.actor)


def _emit_active(ctx: Ctx, slot: SlotRef, active: dict[str, Any]) -> None:
    """The event of an active-spool write: `slot.printer_written` (with the `active_spool` part) or
    `slot.printer_write_failed` with stage `active_spool` (the reconciler's anomaly)."""
    payload: dict[str, Any] = {
        "printer": slot.printer,
        "head": slot.head,
        "spool_id": active["spool_id"],
        "wanted": None,
        "write": ACTIVE_STAGE,  # which of a slot's two writes this is (reconcile.write_failures)
    }
    if active["status"] == WRITTEN:
        kind, payload = EVENT_WRITTEN, payload | {"active_spool": active}
    else:
        kind = EVENT_FAILED
        payload |= {"error": active["reason"], "stage": ACTIVE_STAGE}
    with db.transaction(ctx.conn):
        ev.emit(ctx.conn, kind, slot.path, payload, ctx.actor)


def _contact(slot: SlotRef) -> tuple[Fleet, Device] | str:
    """The fleet and device of `slot`'s printer, or why 3d does not contact it."""
    from printer_ops.devices import fleet_devices
    from printops.fleet import FleetError, load_fleet

    try:
        fleet = load_fleet()
        device = fleet_devices(fleet).get(slot.printer)
    except (FleetError, ThreeDError) as e:
        return f"printer address unknown: {getattr(e, 'message', e)}"
    if fleet is None or device is None:
        return f"{slot.printer} is offline in the fleet: 3d never contacts it"
    return fleet, device


def single_head_reason(printer: str, n_heads: int) -> str | None:
    """Why Moonraker's one active spool cannot stand for a head of `printer`, None when it can
    (also what `3d printers spools` and the doctor use to decide where the check applies)."""
    if n_heads == 1:
        return None
    return (
        f"{printer} has {n_heads} heads and Moonraker keeps ONE active spool for all of them: "
        "it cannot follow tool changes, so none is set"
    )


def _single_head_only(slot: SlotRef, n_heads: int) -> str | None:
    """Why Moonraker's one active spool cannot stand for `slot`, None when it can."""
    return single_head_reason(slot.printer, n_heads)


def sync_head(
    ctx: Ctx, slot: SlotRef, spool: JSON, *, force_official: bool = False
) -> Outcome:
    """Write the head `slot` to show `spool` and set the printer's active spool (see the module
    doc); never raises for printer trouble."""
    from printops.spools import SpoolError, get_spool, slot_of

    spool_id = int(spool["id"])
    if not slot.is_head:
        return Outcome(SKIPPED, f"{slot.key} is an AMS slot: only heads are written")
    contact = _contact(slot)
    if isinstance(contact, str):
        return Outcome(SKIPPED, contact)
    fleet, device = contact
    printer = fleet.printers[slot.printer]
    head = next(h for h in printer.heads if h.label == slot.head)
    # The binding committed before this write; a concurrent load/replace into the same slot may
    # have committed since. Writes of one slot run one at a time, each after re-reading the
    # binding, so the last committed load is the one the head shows.
    with _slot_lock(slot.key):
        try:
            now = get_spool(ctx.spoolman, spool_id)
        except SpoolError as e:
            out = Outcome(FAILED, f"binding not re-readable before the printer write: {e.message}")
            _emit(ctx, slot, spool_id, out, error_stage="filament")
            return out
        if now.get("archived") or slot_of(now).casefold() != slot.key.casefold():
            return Outcome(SKIPPED, f"spool #{spool_id} left {slot.key} before the printer write (another request)")
        out = _write(ctx, slot, now, device, head, force_official=force_official)
        # Moonraker's active spool does not depend on the head write (a K1 has none): its own
        # event channel keeps a failure of either from hiding the other (reconcile.write_failures)
        _set_active_spool(ctx, slot, spool_id, device, len(printer.heads), out)
        return out


def _queued_usage_warning(slot: SlotRef, wrote: ActiveSpoolWrite, *, cleared: bool = False) -> str:
    """Moonraker took the write without a Spoolman link: its usage waits per spool id until it is back."""
    waiting = f" ({wrote.pending_reports} spool(s) waiting)" if wrote.pending_reports else ""
    what = "the active spool is cleared" if cleared else "the active spool is set"
    return (
        f"{slot.printer}'s Moonraker cannot reach Spoolman right now: {what}, and "
        f"usage is queued per spool{waiting} and booked once the link is back"
    )


def _set_active_spool(
    ctx: Ctx, slot: SlotRef, spool_id: int, device: Device, n_heads: int, out: Outcome
) -> None:
    """Make Moonraker's active spool `spool_id` (read back) and record it in `out.active_spool`;
    never raises for printer trouble."""
    from printer_ops import make_adapter
    from printer_ops.base import UnsupportedOperation

    why = _single_head_only(slot, n_heads)
    if why is not None:
        out.active_spool = _active(SKIPPED, why, spool_id)
        return
    try:
        adapter = make_adapter(device, timeout=TIMEOUT_S)
        wrote = adapter.set_active_spool(spool_id)
    except UnsupportedOperation as e:
        out.active_spool = _active(SKIPPED, e.message, spool_id)
        return
    except ThreeDError as e:
        out.active_spool = _active(FAILED, e.message, spool_id)
        _emit_active(ctx, slot, out.active_spool)
        return
    out.active_spool = _active(WRITTEN, None, spool_id, wrote.before, wrote.after)
    if not wrote.connected:
        out.warnings.append(_queued_usage_warning(slot, wrote))
    _emit_active(ctx, slot, out.active_spool)


def release_active_spool(ctx: Ctx, slot: SlotRef, spool_ids: list[int]) -> Outcome:
    """After an `unload`: clear Moonraker's active spool when it is one of the unloaded `spool_ids`
    (and none of them is bound to the slot again), so the printer stops booking to a spool that left
    it; any other value stays. Never raises for printer trouble; the answer's
    `printer_write.active_spool` says what happened. No event: a failed clear has no binding for the
    reconciler's anomaly to hang on (the CLI warns)."""
    from printer_ops import make_adapter
    from printer_ops.base import UnsupportedOperation

    from printops.spools import SpoolError, spool_in_slot

    unloaded = spool_ids[0]
    out = Outcome(SKIPPED, "unload leaves the head's filament as it is")

    def done(status: str, reason: str | None, before: int | None = None, after: int | None = None,
             *, spool_id: int = unloaded) -> Outcome:
        out.active_spool = _active(status, reason, spool_id, before, after)
        return out

    if not slot.is_head:
        return done(SKIPPED, f"{slot.key} is an AMS slot: only heads have an active spool")
    contact = _contact(slot)
    if isinstance(contact, str):
        return done(SKIPPED, contact)
    fleet, device = contact
    why = _single_head_only(slot, len(fleet.printers[slot.printer].heads))
    if why is not None:
        return done(SKIPPED, why)
    with _slot_lock(slot.key):
        try:
            # a load of the same spool that committed after this unload has set the active spool
            # already and must keep it
            rebound = [int(sp["id"]) for sp in spool_in_slot(ctx.spoolman, slot) if int(sp["id"]) in spool_ids]
        except SpoolError as e:
            return done(FAILED, f"slot not re-readable before the clear: {e.message}")
        if rebound:
            return done(SKIPPED, f"spool #{rebound[0]} is bound to {slot.key} again (another request): "
                                 "active spool left as it is")
        try:
            adapter = make_adapter(device, timeout=TIMEOUT_S)
            now = adapter.active_spool().spool_id
            if now is None or now not in spool_ids:
                held = "no spool" if now is None else f"spool #{now}"
                return done(SKIPPED, f"{slot.printer}'s active spool is {held}, not the unloaded "
                                     f"#{unloaded}: left as it is", now, now)
            wrote = adapter.set_active_spool(None)
        except UnsupportedOperation as e:
            return done(SKIPPED, e.message)
        except ThreeDError as e:
            return done(FAILED, e.message)
        if wrote.before is not None and wrote.before not in spool_ids:  # picked in Fluidd since our read
            out.warnings.append(
                f"{slot.printer}'s active spool changed to #{wrote.before} while unloading and was cleared too: "
                "select the spool that is on the printer in Fluidd"
            )
        if not wrote.connected:
            out.warnings.append(_queued_usage_warning(slot, wrote, cleared=True))
        if wrote.before is None:  # cleared in Fluidd since our read: nothing for us to clear
            return done(SKIPPED, f"{slot.printer}'s active spool was cleared meanwhile", now, None)
        return done(WRITTEN, None, wrote.before, wrote.after, spool_id=now)


def _write(ctx: Ctx, slot: SlotRef, spool: JSON, device: Device, head: Head, *, force_official: bool) -> Outcome:
    from printer_ops import make_adapter
    from printer_ops.base import UnsupportedOperation

    spool_id = int(spool["id"])
    try:
        want = wanted_filament(spool)
    except ValueError as e:
        out = Outcome(FAILED, f"{e}: the head was not written")
        _emit(ctx, slot, spool_id, out, error_stage="filament")
        return out
    out = Outcome(
        WRITTEN,
        wanted={
            "color": want.color,
            "material": want.material,
            "vendor": want.vendor,
            "sub_type": want.sub_type,
        },
    )
    try:
        adapter = make_adapter(device, timeout=TIMEOUT_S)
        wrote = adapter.set_head_filament(
            head.index, want, force_official=force_official
        )
    except UnsupportedOperation as e:
        return Outcome(SKIPPED, e.message)
    except ThreeDError as e:
        out.status, out.reason = FAILED, e.message
        _emit(ctx, slot, spool_id, out, error_stage="filament")
        return out
    out.before, out.after = _slot_dict(wrote.before), _slot_dict(wrote.after)
    factory_k = wrote.pressure_advance
    record, why_not = current_pressure_advance(ctx, slot, spool_id)
    if record is None:
        out.pressure_advance = {
            "action": "factory",
            "k": factory_k,
            "calibration_id": None,
        }
        out.warnings.append(
            f"{slot.key} keeps the firmware's factory pressure advance K={factory_k} "
            f"(no current calibration for spool #{spool_id}: {why_not})"
        )
    else:
        values = record.values or {}
        smooth = values.get("smooth_time")
        try:
            k = adapter.set_pressure_advance(
                head.index,
                float(values["pressure_advance"]),
                smooth_time=float(smooth) if isinstance(smooth, (int, float)) else None,
            )
        except ThreeDError as e:
            out.status = FAILED
            out.reason = (
                f"filament written, but restoring pressure advance #{record.id} failed: {e.message}; "
                f"the head runs with factory K={factory_k}"
            )
            out.pressure_advance = {
                "action": "restore_failed",
                "k": factory_k,
                "calibration_id": record.id,
            }
            _emit(ctx, slot, spool_id, out, error_stage="pressure_advance")
            return out
        out.pressure_advance = {
            "action": "restored",
            "k": k,
            "factory_k": factory_k,
            "calibration_id": record.id,
        }
    _emit(ctx, slot, spool_id, out)
    return out
