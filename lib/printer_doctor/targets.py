"""targets.py — the printers the doctor can look at: one `Target` per fleet registry entry.

WHAT: joins fleet/printers.yaml (`FleetPrinter`: heads, access, model) with the effective device
  (`printer_ops.devices.load_devices`: fleet base layer + printer-devices.yaml overrides) and
  keeps the device the FLEET entry alone would give (`fleet_device`), which the stale-override
  check compares against.
WHY: every check decides from this data (adapter kind, access, heads, camera), never from a name,
  so a printer added to the registry is diagnosed with no code change.
INVARIANTS: reads config files only; an `access: offline` printer has `device is None` and is
  never contacted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from printer_ops.devices import Device
    from printops.fleet import FleetPrinter


@dataclass(frozen=True)
class Target:
    name: str
    fleet: FleetPrinter | None
    device: Device | None  # effective device; None for an offline fleet printer
    fleet_device: Device | None  # what fleet/printers.yaml alone would give

    @property
    def access(self) -> str:
        if self.device is not None:
            return self.device.access
        return self.fleet.access if self.fleet is not None else "unknown"

    @property
    def contactable(self) -> bool:
        return self.device is not None and self.device.access != "offline"

    @property
    def adapter(self) -> str | None:
        """The adapter kind: the device's, else the one the fleet entry names (None = none)."""
        if self.device is not None:
            return self.device.adapter
        if self.fleet is not None:
            value = self.fleet.device.get("adapter")
            return value if isinstance(value, str) else None
        return None


def load_targets() -> dict[str, Target]:
    """Every printer of the fleet registry and the printer-devices.yaml layers, by name."""
    from printer_ops.devices import fleet_devices, load_devices
    from printops.fleet import load_fleet

    fleet = load_fleet()
    printers = fleet.printers if fleet is not None else {}
    base = fleet_devices(fleet)
    devices = load_devices()
    return {
        n: Target(n, printers.get(n), devices.get(n), base.get(n))
        for n in sorted(set(printers) | set(devices))
    }
