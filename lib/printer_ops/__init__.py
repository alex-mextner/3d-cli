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
    from printer_ops.move_bounds import Limits


def make_adapter(device: Device, **options: Any) -> Adapter:
    """Adapter instance for a configured device. `options` pass adapter-specific knobs
    (e.g. listen_s for the Creality WebSocket window)."""
    adapter: Adapter
    if device.access == "offline":  # a static profile (get_device offline_ok) has no address
        from printer_ops.base import PrinterRefused

        raise PrinterRefused(f"{device.name} is an offline fleet printer: 3d never contacts it",
                             command="printer")
    if device.adapter == "moonraker":
        from printer_ops.moonraker import MoonrakerAdapter

        adapter = MoonrakerAdapter(device.name, device.host, device.port,
                                   **{k: v for k, v in options.items() if k in ("timeout", "upload_timeout")})
    else:
        from printer_ops.creality import CrealityStockAdapter

        if device.ws_port is not None:
            options.setdefault("ws_port", device.ws_port)
        adapter = CrealityStockAdapter(device.name, device.host, device.port, **options)
    adapter.configured_limits = device.axis_limits
    adapter.trusted_macros = device.trusted_macros
    adapter.device = device
    return adapter


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


def limits_from_option(value: str, *, command: str) -> tuple[Limits | None, str]:
    """`--axis-limits` / `--printer` for offline bounds checks -> (limits, source or reason).

    Accepts `X0:X1,Y0:Y1` or a printer name, whose `axis_limits` come from
    printer-devices.yaml / fleet/printers.yaml (offline fleet printers too). Never contacts it."""
    from printer_ops.devices import get_device
    from printer_ops.move_bounds import parse_limits

    try:
        return parse_limits(value), "--axis-limits"
    except ValueError:
        if ":" in value or "," in value:
            from errors import InvalidArgument

            raise InvalidArgument("--axis-limits", value, ["X0:X1,Y0:Y1 with min < max, e.g. -5:229,-0.5:227",
                                                           "a printer name from `3d printer list`"],
                                  command=command) from None
    device = get_device(value, offline_ok=True)
    if device.axis_limits is None:
        return None, f"no axis_limits for {device.name} in {device.source}"
    return device.axis_limits, f"{device.name} in {device.source}"


def trusted_macros_for(spec: str | None) -> frozenset[str]:
    """The `trusted_macros` of the printer named by --printer / --axis-limits NAME; nothing
    when no printer is named (an X0:X1,Y0:Y1 spec or no option). Never contacts the printer."""
    from printer_ops.devices import get_device
    from printer_ops.move_bounds import parse_limits

    if spec is None:
        return frozenset()
    try:
        parse_limits(spec)
        return frozenset()
    except ValueError:
        return get_device(spec, offline_ok=True).trusted_macros


def bridge_max_for(flag: str | None, device: Device | None, *, command: str) -> tuple[float, str]:
    """The overhangs gate's bridge limit and where it came from: --bridge-max, else the
    printer's `bridge_max_mm` in printer-devices.yaml, else the default."""
    import math

    from errors import InvalidArgument
    from gcode_overhangs import BRIDGE_MAX_MM

    if flag is not None:
        try:
            value = float(flag)
        except ValueError:
            value = -1.0
        if not (value > 0 and math.isfinite(value)):
            raise InvalidArgument("--bridge-max", flag, ["a number of mm > 0, e.g. 25"], command=command)
        return value, "--bridge-max"
    if device is not None and device.bridge_max_mm is not None:
        return device.bridge_max_mm, f"{device.name} in {device.source}"
    return BRIDGE_MAX_MM, "default"


def device_for(spec: str | None) -> Device | None:
    """The device named by --axis-limits NAME (None for an X0:X1,Y0:Y1 spec or no option);
    an offline fleet printer gives its static profile (never contacted)."""
    from printer_ops.devices import get_device
    from printer_ops.move_bounds import parse_limits

    if spec is None or ":" in spec or "," in spec:
        return None  # X0:X1,Y0:Y1 (malformed ones are reported by limits_from_option)
    try:
        parse_limits(spec)
        return None
    except ValueError:
        return get_device(spec, offline_ok=True)
