"""fleet_lint.py — the fleet rules of `3d lint --printops` (plan §9.3, issue #157).

RULES:
  fleet/schema         the file parses and every entry follows the schema (printops.fleet.validate)
  fleet/device         device settings of contactable entries are valid printer-devices.yaml
                       settings (printer_ops.devices does the parsing, one rule set for both files)
  fleet/model-known    `model` is a printer MODEL from `3d printers list` (lib/data/printers.yaml + layers)
  fleet/live-topology  only with --live: heads and nozzle diameters match what the printer reports

LIVE IS READ-ONLY: one request per contactable printer — Moonraker `GET /printer/objects/query
  ?configfile=settings` (extruder sections), Creality stock: the status the adapter reads from
  ws:9999. Never a write; offline printers are never probed. An unreachable printer is a WARN
  (the check could not run), a mismatch an ERROR.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from errors import ThreeDError
from printops.fleet import (
    Fleet,
    FleetError,
    FleetPrinter,
    fleet_path,
    read_yaml,
    validate,
)

if TYPE_CHECKING:
    from printer_ops.devices import Device

ERROR, WARN = "error", "warn"
_EXTRUDER = re.compile(r"extruder(\d*)")


@dataclass(frozen=True)
class Finding:
    rule: str
    level: str  # error | warn
    subject: str  # printer name, or the file for file-level problems
    message: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class LiveTopology:
    extruders: tuple[str, ...]  # Klipper extruder object names in tool order
    nozzle_mm: dict[
        str, float
    ]  # extruder -> nozzle diameter, where the firmware reports it


def lint_fleet(
    path: Path | None = None,
    *,
    live: bool = False,
    probe: Callable[[Device], LiveTopology] | None = None,
) -> tuple[Path | None, list[Finding]]:
    """(file linted, findings). `probe` replaces the live read (tests)."""
    path = path or fleet_path()
    if path is None:
        return None, [
            Finding(
                "fleet/schema",
                ERROR,
                "fleet",
                "THREED_FLEET is empty: no fleet file to lint",
            )
        ]
    if not path.is_file():
        return path, [Finding("fleet/schema", ERROR, str(path), "fleet file not found")]
    try:
        data = read_yaml(path)
    except FleetError as exc:
        return path, [Finding("fleet/schema", ERROR, str(path), exc.message)]
    printers, problems = validate(data, path)
    findings = [
        Finding("fleet/schema", ERROR, p.subject or str(path), p.message)
        for p in problems
    ]
    findings += _model_known(printers.values())
    fleet = Fleet(path, printers)
    devices = _devices(fleet, findings)
    if live:
        findings += _live(fleet, devices, probe or probe_live)
    return path, findings


def _model_known(printers: Iterable[FleetPrinter]) -> list[Finding]:
    from registries.printers import load_printers

    known = load_printers()
    return [
        Finding(
            "fleet/model-known",
            ERROR,
            p.name,
            f"{p.name}.model: {p.model!r} is not a printer model (`3d printers list`); add it to "
            "lib/data/printers.yaml with its vendor specs",
        )
        for p in printers
        if p.model not in known
    ]


def _devices(fleet: Fleet, findings: list[Finding]) -> dict[str, Device]:
    """Contactable entries as Devices, parsed one at a time so every bad entry is reported;
    offline entries' static profiles are checked too but never returned (never probed)."""
    out: dict[str, Device] = {}
    for name, printer in fleet.printers.items():
        device, finding = _device(fleet, name)
        if finding is not None:
            findings.append(finding)
        elif device is not None and printer.contactable:
            out[name] = device
    return out


def _device(fleet: Fleet, name: str) -> tuple[Device | None, Finding | None]:
    from printer_ops.devices import fleet_devices, offline_device

    printer = fleet.printers[name]
    try:
        if not printer.contactable:
            return offline_device(printer), None
        return fleet_devices(Fleet(fleet.path, {name: printer}))[name], None
    except ThreeDError as exc:
        return None, Finding("fleet/device", ERROR, name, f"{name}: {exc.message}")


def probe_live(device: Device) -> LiveTopology:
    """Read-only topology of a printer: one GET (Moonraker) or one ws status read (Creality)."""
    if device.adapter == "moonraker":
        from printer_ops.base import http_json

        body = http_json(
            "GET",
            f"http://{device.host}:{device.port}/printer/objects/query?configfile=settings",
            timeout=10.0,
        )
        settings = body["result"]["status"]["configfile"]["settings"]
        names = sorted(
            (k for k in settings if _EXTRUDER.fullmatch(k)),
            key=lambda k: int(k[8:] or 0),
        )
        return LiveTopology(
            tuple(names),
            {
                k: float(settings[k]["nozzle_diameter"])
                for k in names
                if "nozzle_diameter" in settings[k]
            },
        )
    from printer_ops import make_adapter

    temps = make_adapter(device).status().temps
    count = sum(1 for k in temps if k.startswith("nozzle"))
    return LiveTopology(
        tuple("extruder" if i == 0 else f"extruder{i}" for i in range(count)), {}
    )


def _live(
    fleet: Fleet, devices: dict[str, Device], probe: Callable[[Device], LiveTopology]
) -> list[Finding]:
    out: list[Finding] = []
    for name, device in sorted(devices.items()):
        heads = fleet.printers[name].heads
        try:
            topo = probe(device)
        except (ThreeDError, OSError, ValueError, KeyError, TypeError) as exc:
            msg = (
                exc.message
                if isinstance(exc, ThreeDError)
                else f"{type(exc).__name__}: {exc}"
            )
            out.append(
                Finding(
                    "fleet/live-topology",
                    WARN,
                    name,
                    f"{name}: live read failed, not checked: {msg}",
                )
            )
            continue
        if len(heads) != len(topo.extruders):
            out.append(
                Finding(
                    "fleet/live-topology",
                    ERROR,
                    name,
                    f"{name}: fleet lists {len(heads)} head(s), the printer reports "
                    f"{len(topo.extruders)} extruder(s) ({', '.join(topo.extruders) or 'none'})",
                )
            )
        for h in heads:
            if h.extruder and h.extruder not in topo.extruders:
                out.append(
                    Finding(
                        "fleet/live-topology",
                        ERROR,
                        name,
                        f"{name}.{h.label}: extruder {h.extruder!r} is not on the printer",
                    )
                )
            live_mm = topo.nozzle_mm.get(h.extruder or "")
            if (
                h.nozzle is not None
                and live_mm is not None
                and abs(live_mm - h.nozzle.diameter_mm) > 1e-6
            ):
                out.append(
                    Finding(
                        "fleet/live-topology",
                        ERROR,
                        name,
                        f"{name}.{h.label}: nozzle {h.nozzle.diameter_mm} mm in the fleet, "
                        f"{live_mm} mm in the printer config ({h.extruder})",
                    )
                )
    return out
