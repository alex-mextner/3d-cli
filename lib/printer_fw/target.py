"""target.py — turn a fleet-registry printer into what the firmware engine needs to reach it.

WHAT: `Target` is the engine's view of one printer of the registry (fleet/printers.yaml plus the
  printer-devices.yaml layers): its name, model, access level and — for a contactable one — host
  and registry adapter. `endpoint()` maps a profile's named services (`moonraker`, `web`, `ssh`,
  ...) to ports: the profile's vendor defaults, then the registry's own `port` for the service
  its adapter speaks (the profile's `adapter_services`), then `THREED_FW_SERVICE_PORTS`.

WHY: the registry is the single source of truth for WHICH printers exist and WHERE they are; the
  profile is the single source of truth for what a firmware family looks like. Neither may be
  copied into generic code, so this module only joins them.

ACCESSED VIA: `printer_fw.engine`, `printers_cli/fw.py`.

INVARIANTS: an offline fleet printer has no address and is never contacted (`contactable` is
  False); a read-only printer may be read (status, backup) but never changed. The port override
  env var is for port-forwards and loopback test fakes; a malformed value is a usage error.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from errors import InvalidArgument
from printer_fw.probes import Endpoint
from printer_fw.profiles import Profile

if TYPE_CHECKING:
    from printers_cli._common import PrinterRef

ENV_PORTS = "THREED_FW_SERVICE_PORTS"


@dataclass(frozen=True)
class Target:
    name: str
    model: str | None
    access: str  # full | read-only | offline | unknown
    host: str
    adapter: str
    adapter_port: int
    owner: str | None = None

    @property
    def contactable(self) -> bool:
        return bool(self.host) and self.access in ("full", "read-only")

    @property
    def writable(self) -> bool:
        return self.access == "full" and bool(self.host)


def target_from_ref(ref: PrinterRef) -> Target:
    """A registry entry -> Target. A device-only entry (printer-devices.yaml without a fleet
    row) has no fleet model: its own `model:` is used."""
    fleet, device = ref.fleet, ref.device
    model = (fleet.model if fleet is not None else None) or (
        device.model if device is not None else None
    )
    owner = fleet.owner if fleet is not None else None
    if device is None or ref.access == "offline":
        return Target(ref.name, model, ref.access, "", "", 0, owner)
    return Target(
        ref.name, model, ref.access, device.host, device.adapter, device.port, owner
    )


def all_targets() -> list[Target]:
    from printers_cli._common import known_printers

    return [target_from_ref(r) for r in known_printers().values()]


def resolve_target(name: str, command: str) -> Target:
    from printers_cli._common import require_printer

    return target_from_ref(require_printer(name, command))


def parse_port_overrides(text: str) -> dict[str, int]:
    """`web=8080,ssh=2222` -> {"web": 8080, "ssh": 2222}."""
    out: dict[str, int] = {}
    for part in filter(None, (p.strip() for p in text.split(","))):
        name, _, port = part.partition("=")
        # isascii first: str.isdigit() is true for "²" and int() then raises.
        if (
            not name
            or not (port.isascii() and port.isdigit())
            or not 0 < int(port) < 65536
        ):
            raise InvalidArgument(
                ENV_PORTS,
                text,
                ["name=PORT[,name=PORT...] with PORT 1-65535, e.g. web=8080,ssh=2222"],
                command="printers fw",
            )
        out[name] = int(port)
    return out


def validate_port_overrides(
    profiles: Iterable[Profile], environ: Mapping[str, str] | None = None
) -> None:
    """The override env var is shared by every printer, so a name must be a service of SOME
    profile; a typo (`ssh2=22`) would otherwise be silently ignored."""
    env = os.environ if environ is None else environ
    known = {name for p in profiles for name in p.services}
    for name in parse_port_overrides(env.get(ENV_PORTS, "")):
        if name not in known:
            raise InvalidArgument(
                ENV_PORTS,
                env.get(ENV_PORTS, ""),
                sorted(known),
                command="printers fw",
                extra=f"{name!r} is not a service of any firmware profile.",
            )


def service_ports(
    profile: Profile, target: Target, environ: Mapping[str, str] | None = None
) -> dict[str, int]:
    ports = dict(profile.services)
    own = profile.adapter_services.get(target.adapter)
    if own is not None and target.adapter_port:
        ports[own] = target.adapter_port
    env = os.environ if environ is None else environ
    for name, port in parse_port_overrides(env.get(ENV_PORTS, "")).items():
        if name in ports:
            ports[name] = port
    return ports


def endpoint(
    profile: Profile, target: Target, environ: Mapping[str, str] | None = None
) -> Endpoint:
    return Endpoint(
        target.host,
        service_ports(profile, target, environ),
        target.adapter,
        profile.ssh,
    )
