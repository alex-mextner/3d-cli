"""printer_ops — drive real printers from `3d printer` (epic https://github.com/alex-mextner/3d-cli/issues/90).

Layout: base.py (status model, adapter contract, HTTP helpers), ws.py (stdlib WebSocket
client), moonraker.py and creality.py (adapters), devices.py (printer-devices.yaml),
preflight.py (`3d print-preflight` checks). All stdlib (+ lazy pyyaml for the config).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from printer_ops.base import Adapter
    from printer_ops.devices import Device


def make_adapter(device: Device, **options: Any) -> Adapter:
    """Adapter instance for a configured device. `options` pass adapter-specific knobs
    (e.g. listen_s for the Creality WebSocket window)."""
    if device.adapter == "moonraker":
        from printer_ops.moonraker import MoonrakerAdapter

        return MoonrakerAdapter(device.name, device.host, device.port,
                                **{k: v for k, v in options.items() if k in ("timeout", "upload_timeout")})
    from printer_ops.creality import CrealityStockAdapter

    if device.ws_port is not None:
        options.setdefault("ws_port", device.ws_port)
    return CrealityStockAdapter(device.name, device.host, device.port, **options)


def adapter_by_name(name: str, *, secs: str | None = None, command: str = "printer") -> Adapter:
    """Configured device `name` -> adapter; `secs` is the user's --secs (Creality listen window)."""
    from printer_ops.devices import get_device

    options: dict[str, float] = {}
    if secs is not None:
        try:
            options["listen_s"] = float(secs)
        except ValueError:
            from errors import InvalidArgument

            raise InvalidArgument("--secs", secs, ["a number of seconds, e.g. 3"], command=command) from None
    return make_adapter(get_device(name), **options)
