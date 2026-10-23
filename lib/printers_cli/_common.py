"""_common.py — plumbing shared by the `3d printers <sub>` modules: argument parsing, the fleet's
printer names, `--json` output.

`parse` is the PrintOps parser (positionals, `--opt VALUE`, `--flag`; unknown options are a
UsageError, exit 2) with `3d printers` as the command prefix in messages.
"""

from __future__ import annotations

import json
from collections.abc import Set
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from errors import InvalidArgument, UsageError
from printops.cli._common import Parsed, api_errors as _api_errors
from printops.cli._common import client as _client
from printops.cli._common import parse as _parse

if TYPE_CHECKING:
    from printer_ops.devices import Device
    from printops.client import ApiClient
    from printops.fleet import FleetPrinter

__all__ = [
    "Parsed",
    "api_errors",
    "client",
    "PrinterRef",
    "emit_json",
    "known_printers",
    "parse",
    "require_printer",
    "wants_help",
]


def parse(
    argv: list[str],
    sub: str,
    *,
    values: Set[str] = frozenset(),
    flags: Set[str] = frozenset(),
    n_pos: int | tuple[int, int] = 0,
) -> Parsed:
    return _parse(argv, sub, values=values, flags=flags, n_pos=n_pos, prefix="printers")


def client(p: Parsed, sub: str) -> ApiClient:
    """The 3d-api client from `--api`/`--token`/env/printops.yaml (UsageError when none is configured)."""
    return _client(p, sub, prefix="printers")


def api_errors(sub: str) -> AbstractContextManager[None]:
    """Render a 3d-api ApiError as a structured CLI error (exit 1) with the likely fix."""
    return _api_errors(sub, prefix="printers")


def wants_help(argv: list[str]) -> bool:
    return any(a in ("-h", "--help") for a in argv)


def emit_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


@dataclass(frozen=True)
class PrinterRef:
    """One printer of the fleet registry: its fleet entry (None for a printer that exists only in a
    printer-devices.yaml) and the device 3d may contact (None for an `access: offline` printer)."""

    name: str
    fleet: FleetPrinter | None
    device: Device | None

    @property
    def access(self) -> str:
        if self.device is not None:
            return self.device.access
        return self.fleet.access if self.fleet is not None else "unknown"

    @property
    def contactable(self) -> bool:
        return self.device is not None and self.device.access != "offline"


def known_printers() -> dict[str, PrinterRef]:
    """Every printer in fleet/printers.yaml and the printer-devices.yaml layers, by name. A broken
    fleet file surfaces as its own structured error (the caller should not guess a registry)."""
    from printer_ops.devices import load_devices
    from printops.fleet import load_fleet

    fleet = load_fleet()
    printers = fleet.printers if fleet is not None else {}
    devices = load_devices()
    return {
        n: PrinterRef(n, printers.get(n), devices.get(n))
        for n in sorted(set(printers) | set(devices))
    }


def require_printer(name: str, command: str) -> PrinterRef:
    """The registry entry for `name`; an unknown name is an InvalidArgument listing the valid ones."""
    refs = known_printers()
    if name in refs:
        return refs[name]
    folded = [n for n in refs if n.casefold() == name.casefold()]
    if len(folded) == 1:
        return refs[folded[0]]
    if not refs:
        raise UsageError(
            "no printers configured: fleet/printers.yaml and printer-devices.yaml are empty",
            command=f"printers {command}",
            remediation=[
                "Run `3d printer list` to see where the registry is looked up."
            ],
        )
    raise InvalidArgument(
        "printer",
        name,
        sorted(refs),
        command=f"printers {command}",
        extra="`3d printer list` shows every printer of the fleet.",
    )
