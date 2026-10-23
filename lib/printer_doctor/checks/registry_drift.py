"""registry_drift.py — is the printer's effective address the one the fleet registry says?

CHECK `device-override-stale`: `printer_ops.devices.load_devices` layers fleet/printers.yaml (base)
  under ~/.config/3d-cli/printer-devices.yaml and ./printer-devices.yaml; an entry with the same
  name REPLACES the whole device part of the fleet entry. An override whose adapter/host/port differ
  from the fleet entry is the classic leftover of a printer that was re-IP'd or re-flashed (the
  fleet file was fixed, the override still wins and points at the old address). Reports both and
  says which one wins. Applies only when both exist; reads config only; manual-only fix.
"""

from __future__ import annotations

from printer_doctor.context import Context
from printer_doctor.model import Check, CheckResult, Fix, JsonValue, Verdict


def _applies(ctx: Context) -> str | None:
    t = ctx.target
    if t.fleet is None:
        return "no fleet entry: nothing to compare against"
    if t.device is None or t.fleet_device is None:
        return "no effective device (offline printer) or no fleet device settings"
    if t.device.source == t.fleet_device.source:
        return "no override: the fleet entry is the effective device"
    return None


def _describe(
    adapter: str, host: str, port: int, source: object
) -> dict[str, JsonValue]:
    return {"adapter": adapter, "host": host, "port": port, "source": str(source)}


def _run(ctx: Context) -> Verdict:
    t = ctx.target
    assert t.device is not None and t.fleet_device is not None
    fleet, eff = t.fleet_device, t.device
    evidence: dict[str, JsonValue] = {
        "fleet": _describe(fleet.adapter, fleet.host, fleet.port, fleet.source),
        "effective": _describe(eff.adapter, eff.host, eff.port, eff.source),
        "winner": "override",
    }
    diff = [
        f"{k} {a!r} -> {b!r}"
        for k, a, b in (
            ("adapter", fleet.adapter, eff.adapter),
            ("host", fleet.host, eff.host),
            ("port", fleet.port, eff.port),
        )
        if a != b
    ]
    if not diff:
        return Verdict.ok(
            "the override repeats the fleet entry's adapter/host/port", **evidence
        )
    return Verdict.fail(
        f"{eff.source} overrides the fleet entry ({'; '.join(diff)}); the override wins and "
        f"replaces the fleet entry's device settings",
        "warn",
        **evidence,
    )


def _steps(ctx: Context, result: CheckResult) -> list[str]:
    eff = ctx.device
    return [
        f"If the fleet entry is right: delete the `{ctx.name}:` entry from {eff.source} (the fleet entry then applies again).",
        f"If the override is right: update the `{ctx.name}` device settings in {ctx.target.fleet_device.source if ctx.target.fleet_device else 'fleet/printers.yaml'} to match, then delete the override.",
        "`3d printer list` shows the effective entry and where it comes from.",
    ]


CHECKS = (
    Check(
        id="device-override-stale",
        title="a printer-devices.yaml override does not silently replace the fleet entry",
        severity="warn",
        applies_to="fleet printers that also have a printer-devices.yaml entry",
        contacts_printer=False,
        applies=_applies,
        run=_run,
        fix=Fix("manual-only", "edit or delete the override entry", _steps),
    ),
)
