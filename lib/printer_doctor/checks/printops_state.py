"""printops_state.py — does what 3d-api (PrintOps) believes about a printer match the printer?

CHECKS (all need a configured 3d-api; without one they SKIP with that reason, offline-safe)
  active-spool-binding  Moonraker keeps ONE active spool and books the printer's usage to it
                        itself. For a printer whose adapter has an active spool and that has a
                        SINGLE head (`printops.head_sync.single_head_reason`: one active spool
                        cannot follow tool changes, so multi-head printers are skipped with that
                        reason), it must equal the spool PrintOps has bound to the head
                        (`GET /v1/printers/{p}` -> slots[].spools[].spool_id). Fix (safe):
                        `Adapter.set_active_spool(bound)` (read back by the adapter), idle printers
                        only, journalled; refused while a job is printing or paused.
  slot-binding-unknown  PrintOps shows a spool bound to a head while the printer's live slot
                        reading is missing/empty, or an open reconciliation anomaly exists for the
                        printer (`GET /v1/anomalies?printer=P&status=open`, routes_reconcile.py).
                        warn; manual.
  heads-vs-faults       the fleet registry's `usable` flag of each head against the open faults of
                        3d-api (`GET /v1/printers/{p}/faults`): a head unusable in the fleet without
                        an open fault is `info` (the fault is seeded by `3d printops maint sync-fleet`),
                        an open fault on a head the fleet still calls usable is `warn`. manual.
WHY: the usage ledger, the planner and the print gates trust these records; a drift between them
  and the machine books filament to the wrong spool or plans on a broken head.
"""

from __future__ import annotations

from collections.abc import Mapping

from printer_doctor.context import Context, as_list, as_obj
from printer_doctor.model import (
    CannotSee,
    Check,
    CheckResult,
    Fix,
    FixChange,
    FixRefused,
    JsonValue,
    Verdict,
)


def _view(ctx: Context) -> Mapping[str, JsonValue]:
    return as_obj(
        ctx.api_get(f"/v1/printers/{ctx.name}"), f"GET /v1/printers/{ctx.name}"
    )


def _bound_ids(view: Mapping[str, JsonValue]) -> list[int]:
    """Spoolman spool ids bound to the printer's slots; CannotSee when 3d-api cannot read Spoolman."""
    ids: list[int] = []
    for slot in as_list(view.get("slots"), "printer view slots"):
        spools = slot.get("spools") if isinstance(slot, Mapping) else None
        if spools is None:
            raise CannotSee(f"3d-api cannot read Spoolman: {view.get('spools_error')}")
        for spool in as_list(spools, "slot spools"):
            sid = spool.get("spool_id") if isinstance(spool, Mapping) else None
            if isinstance(sid, int) and not isinstance(sid, bool) and sid not in ids:
                ids.append(sid)
    return sorted(ids)


# ---- active-spool-binding -------------------------------------------------------------------


def _active_applies(ctx: Context) -> str | None:
    reason = ctx.moonraker_only()
    if reason:
        return reason
    fleet = ctx.target.fleet
    if fleet is None:
        return "no fleet entry: the head count is unknown"
    from printops.head_sync import single_head_reason

    reason = single_head_reason(ctx.name, len(fleet.heads))
    if reason:
        return reason
    if "spoolman" not in ctx.components():
        return "no [spoolman] component in this Moonraker: it has no active spool"
    return ctx.api_configured()


def _read_active(ctx: Context) -> tuple[int | None, bool, int | None]:
    from printer_ops.base import PrinterCommError, UnsupportedOperation

    try:
        active = ctx.adapter().active_spool()
    except UnsupportedOperation as exc:
        raise CannotSee(exc.message) from exc
    except PrinterCommError as exc:
        raise CannotSee(exc.message, "error") from exc
    return active.spool_id, active.connected, active.pending_reports


def _active(ctx: Context) -> Verdict:
    bound = _bound_ids(_view(ctx))
    active, connected, pending = _read_active(ctx)
    evidence: dict[str, JsonValue] = {
        "bound_spool_ids": bound,
        "active_spool_id": active,
        "spoolman_connected": connected,
        "pending_reports": pending,
    }
    if len(bound) > 1:
        return Verdict.fail(
            f"{len(bound)} spools {bound} are bound to a one-head printer: PrintOps itself is ambiguous",
            "error",
            **evidence,
        )
    want = bound[0] if bound else None
    if active == want:
        what = "no spool" if want is None else f"spool #{want}"
        return Verdict.ok(
            f"Moonraker's active spool is {what}, as PrintOps has it", **evidence
        )
    have = "none" if active is None else f"#{active}"
    wanted = "none bound" if want is None else f"#{want}"
    return Verdict.fail(
        f"Moonraker books usage to spool {have} but PrintOps has {wanted} on the head",
        "error",
        **evidence,
    )


def _active_apply(ctx: Context, result: CheckResult) -> FixChange:
    bound = result.evidence.get("bound_spool_ids")
    ids = [b for b in bound if isinstance(b, int)] if isinstance(bound, list) else []
    if len(ids) > 1:
        raise FixRefused(
            f"PrintOps binds {len(ids)} spools to the head: resolve that first (3d printops slots {ctx.name})"
        )
    job = ctx.job_state()
    if job in ("printing", "paused"):
        raise FixRefused(f"a job is {job}: the active spool is not changed mid-print")
    write = ctx.adapter().set_active_spool(ids[0] if ids else None)
    return FixChange(
        write.before,
        write.after,
        "Moonraker active spool set to the spool PrintOps has bound",
    )


def _active_steps(ctx: Context, result: CheckResult) -> list[str]:
    bound = result.evidence.get("bound_spool_ids")
    ids = bound if isinstance(bound, list) else []
    value = str(ids[0]) if len(ids) == 1 else "null"
    return [
        "When the printer is idle: curl -X POST -H 'Content-Type: application/json' "
        f"-d '{{\"spool_id\": {value}}}' {ctx.base_url}/server/spoolman/spool_id",
        f"Or reload the spool through PrintOps (it sets the active spool): `3d printops slots {ctx.name}`, then `3d printops spool load ID {ctx.name}/SLOT`.",
        f"Or use the web UI's Spoolman panel to select the spool. Check: `3d printers doctor {ctx.name} --check active-spool-binding`.",
    ]


# ---- slot-binding-unknown -------------------------------------------------------------------


def _slots_applies(ctx: Context) -> str | None:
    if ctx.target.fleet is None:
        return "no fleet entry: 3d-api knows no slots for it"
    if not ctx.target.contactable:
        return "offline printer: 3d-api never reads its slots"
    return ctx.api_configured()


def _slots(ctx: Context) -> Verdict:
    view = _view(ctx)
    if view.get("polled") is False:
        return Verdict.skip(
            "3d-api does not poll this printer: there is no live slot reading"
        )
    problems: list[JsonValue] = []
    for slot in as_list(view.get("slots"), "printer view slots"):
        if not isinstance(slot, Mapping):
            continue
        spools = slot.get("spools")
        if spools is None:
            raise CannotSee(f"3d-api cannot read Spoolman: {view.get('spools_error')}")
        if not isinstance(spools, list) or not spools:
            continue
        ids = [s.get("spool_id") for s in spools if isinstance(s, Mapping)]
        live = slot.get("printer")
        if not isinstance(live, Mapping):
            problems.append(
                {
                    "slot": slot.get("slot"),
                    "spools": ids,
                    "problem": "printer's slot reading is unknown",
                }
            )
        elif live.get("present") is False:
            problems.append(
                {
                    "slot": slot.get("slot"),
                    "spools": ids,
                    "problem": "printer reports the slot empty",
                }
            )
    raw = as_obj(
        ctx.api_get("/v1/anomalies", printer=ctx.name, status="open"),
        "GET /v1/anomalies",
    ).get("anomalies")
    anomalies: list[JsonValue] = [
        {"id": a.get("id"), "kind": a.get("kind"), "subject": a.get("subject")}
        for a in as_list(raw, "anomalies")
        if isinstance(a, Mapping)
    ]
    evidence: dict[str, JsonValue] = {"slots": problems, "anomalies": anomalies}
    if problems or anomalies:
        return Verdict.fail(
            f"{len(problems)} bound slot(s) without a matching live reading, {len(anomalies)} open anomaly(ies)",
            "warn",
            **evidence,
        )
    return Verdict.ok(
        "every bound slot has a live reading, no open anomalies", **evidence
    )


def _slots_steps(ctx: Context, result: CheckResult) -> list[str]:
    return [
        f"See both sides: `3d printops slots {ctx.name}` and `3d printops anomalies --printer {ctx.name}`.",
        "Empty or unknown slot with a bound spool: load the spool into the printer's head, or "
        "unload it in PrintOps (`3d printops spool unload " + ctx.name + "/SLOT`).",
        "An anomaly that is intended (a different spool on purpose): `3d printops anomalies ack ID --note TEXT`.",
    ]


# ---- heads-vs-faults ------------------------------------------------------------------------


def _faults_applies(ctx: Context) -> str | None:
    if ctx.target.fleet is None or not ctx.target.fleet.heads:
        return "no fleet entry with heads"
    return ctx.api_configured()


def _faults(ctx: Context) -> Verdict:
    fleet = ctx.target.fleet
    assert fleet is not None
    raw = as_obj(
        ctx.api_get(f"/v1/printers/{ctx.name}/faults", status="open"), "faults"
    ).get("faults")
    fault_heads: dict[str, list[JsonValue]] = {}
    for f in as_list(raw, "faults"):
        head = f.get("head") if isinstance(f, Mapping) else None
        if isinstance(head, str) and isinstance(f, Mapping):
            fault_heads.setdefault(head.casefold(), []).append(f.get("id"))
    unmarked: list[JsonValue] = []  # unusable in the fleet, no open fault
    contradicted: list[JsonValue] = []  # open fault, fleet says usable
    for fh in fleet.heads:
        ids = fault_heads.get(fh.label.casefold(), [])
        if not fh.usable and not ids:
            unmarked.append({"head": fh.label, "reason": fh.reason})
        if fh.usable and ids:
            contradicted.append({"head": fh.label, "faults": ids})
    evidence: dict[str, JsonValue] = {
        "unusable_without_fault": unmarked,
        "fault_on_usable_head": contradicted,
    }
    if contradicted:
        heads = ", ".join(
            str(c["head"]) for c in contradicted if isinstance(c, Mapping)
        )
        return Verdict.fail(
            f"open fault on head(s) the fleet still marks usable: {heads}",
            "warn",
            **evidence,
        )
    if unmarked:
        heads = ", ".join(str(c["head"]) for c in unmarked if isinstance(c, Mapping))
        return Verdict.fail(
            f"head(s) unusable in the fleet but with no open fault in 3d-api: {heads}",
            "info",
            **evidence,
        )
    return Verdict.ok("fleet head flags and open faults agree", **evidence)


def _faults_steps(ctx: Context, result: CheckResult) -> list[str]:
    return [
        "Fault on a head the fleet calls usable: either repair it and close the fault "
        "(`3d printops maint fault close ID --root-cause TEXT`), or mark the head `usable: false` "
        f"with a `reason:` under {ctx.name}.heads in fleet/printers.yaml.",
        f"Head unusable in the fleet without a fault: `3d printops maint sync-fleet` seeds the fault from "
        f"fleet/printers.yaml, or open one by hand: `3d printops maint fault open {ctx.name}/HEAD --symptom TEXT`.",
        "Or, when the head works again, set `usable: true` in fleet/printers.yaml.",
    ]


CHECKS = (
    Check(
        id="active-spool-binding",
        title="Moonraker's active spool is the spool PrintOps has bound to the head",
        severity="error",
        applies_to="single-head Moonraker printers with [spoolman] (multi-head printers are skipped); needs 3d-api",
        applies=_active_applies,
        run=_active,
        fix=Fix(
            "safe",
            "set Moonraker's active spool to the bound spool (idle printers only, read back, journalled)",
            _active_steps,
            _active_apply,
        ),
    ),
    Check(
        id="slot-binding-unknown",
        title="spools bound in PrintOps match what the printer reports; no open anomalies",
        severity="warn",
        applies_to="fleet printers 3d-api polls; needs 3d-api",
        contacts_printer=False,
        applies=_slots_applies,
        run=_slots,
        fix=Fix(
            "manual-only", "reload the spool / acknowledge the anomaly", _slots_steps
        ),
    ),
    Check(
        id="heads-vs-faults",
        title="the fleet's usable flag of each head agrees with the open faults",
        severity="warn",
        applies_to="fleet printers with heads; needs 3d-api",
        contacts_printer=False,
        applies=_faults_applies,
        run=_faults,
        fix=Fix(
            "manual-only",
            "close the fault or mark the head usable/unusable",
            _faults_steps,
        ),
    ),
)
