"""devices.py — the physical-printer registry: short name -> adapter, host, port, model.

WHY separate from `3d printers` (lib/registries/printers.py): that registry describes printer
MODELS ("Prusa MK4": bed, nozzle, firmware) and every entry needs a bed. A device is one
machine on the LAN ("u1" at 192.168.0.26 speaking Moonraker). `model:` links a device to its
model entry. Issue https://github.com/alex-mextner/3d-cli/issues/91.

FILES (later wins per name, whole-entry replace):
  1. ~/.config/3d-cli/printer-devices.yaml   (XDG_CONFIG_HOME honoured)
  2. ./printer-devices.yaml                  (project-local override)

ENTRY:
  u1:   {adapter: moonraker, host: 192.168.0.26, port: 7125, model: Snapmaker U1}
  k1se: {adapter: creality-stock, host: 192.168.0.27, model: Creality K1 SE}
  `port` defaults to 7125 (moonraker) / 80 (creality-stock HTTP); creality-stock also takes
  `ws_port` (default 9999). Optional `axis_limits: {x: [min, max], y: [min, max]}` is the
  real X/Y travel used by `3d print-preflight` (Moonraker printers report it themselves).
  Optional `trusted_macros: [PAUSE, START_PRINT]` names macros (commands whose bodies the
  bounds check cannot see) known to keep the head on the bed; any other macro in a G-code
  FAILs print-preflight, so `3d printer start` needs --force for it.

Only devices listed here are ever contacted — no LAN discovery (issue #62 network modes).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from errors import InvalidArgument, MissingDependency, ThreeDError, UsageError
from printer_ops.move_bounds import Limits

FILENAME = "printer-devices.yaml"
ADAPTERS = ("moonraker", "creality-stock")
DEFAULT_PORT = {"moonraker": 7125, "creality-stock": 80}
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")

EXAMPLE = f"""# {FILENAME}
u1:
  adapter: moonraker        # Klipper + Moonraker (Snapmaker U1, Voron, ...)
  host: 192.168.0.26
  port: 7125
  model: Snapmaker U1
k1se:
  adapter: creality-stock   # Creality K1 family on stock firmware (HTTP + ws:9999)
  host: 192.168.0.27
  model: Creality K1 SE
  axis_limits: {{x: [-5, 229], y: [-0.5, 227]}}   # stepper_x/y position_min/max, printer.cfg
  trusted_macros: [START_PRINT, END_PRINT, PAUSE]  # vendor macros known to stay on the bed"""


class DeviceConfigError(ThreeDError):
    exit_code = 2


@dataclass(frozen=True)
class Device:
    name: str
    adapter: str
    host: str
    port: int
    model: str | None
    source: Path
    ws_port: int | None = None  # creality-stock only: WebSocket port (default 9999)
    axis_limits: Limits | None = None  # real X/Y travel ((xmin, xmax), (ymin, ymax)), for print-preflight
    trusted_macros: frozenset[str] = frozenset()  # macros print-preflight may treat as staying on the bed


def user_path() -> Path:
    from cli.paths import config_dir

    return config_dir() / FILENAME


def search_paths(cwd: Path | None = None) -> list[Path]:
    return [user_path(), (cwd or Path.cwd()) / FILENAME]


def _port(value: Any, key: str, path: Path) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 65536:
        raise InvalidArgument(key, str(value), ["an integer 1-65535"], command="printer", extra=f"Fix {path}.")
    return value


def _axis_limits(value: Any, name: str, path: Path) -> Limits:
    """`{x: [-5, 229], y: [-0.5, 227]}` -> ((-5.0, 229.0), (-0.5, 227.0))."""
    bad = InvalidArgument(f"{name}.axis_limits", str(value), ["{x: [min, max], y: [min, max]} with min < max"],
                          command="printer", extra=f"Fix {path}.")
    if not isinstance(value, dict):
        raise bad
    out = []
    for axis in ("x", "y"):
        pair = value.get(axis)
        if (not isinstance(pair, list) or len(pair) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in pair)
                or not pair[0] < pair[1]):
            raise bad
        out.append((float(pair[0]), float(pair[1])))
    return (out[0], out[1])


def _trusted_macros(value: Any, name: str, path: Path) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise InvalidArgument(f"{name}.trusted_macros", str(value), ["a list of macro names, e.g. [PAUSE]"],
                              command="printer", extra=f"Fix {path}.")
    return frozenset(v.strip().upper() for v in value)


def _parse(path: Path, data: Any) -> dict[str, Device]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise DeviceConfigError(f"{path}: top level must be a mapping of device name -> settings",
                                command="printer", remediation=["Example:\n" + EXAMPLE])
    out: dict[str, Device] = {}
    for name, spec in data.items():
        name = str(name)
        if not _NAME.fullmatch(name):
            raise DeviceConfigError(f"{path}: device name {name!r} must be letters/digits/_ . -",
                                    command="printer")
        if not isinstance(spec, dict):
            raise DeviceConfigError(f"{path}: device {name!r} must be a mapping", command="printer",
                                    remediation=["Example:\n" + EXAMPLE])
        adapter = str(spec.get("adapter", ""))
        if adapter not in ADAPTERS:
            raise InvalidArgument(f"{name}.adapter", adapter, ADAPTERS, command="printer",
                                  extra=f"Fix {path}.")
        host = str(spec.get("host", "")).strip()
        if not host:
            raise DeviceConfigError(f"{path}: device {name!r} has no host", command="printer",
                                    remediation=[f"Add `host: <ip or hostname>` under {name}: in {path}"])
        port = _port(spec.get("port", DEFAULT_PORT[adapter]), f"{name}.port", path)
        ws_port = _port(spec["ws_port"], f"{name}.ws_port", path) if "ws_port" in spec else None
        model = spec.get("model")
        limits = _axis_limits(spec["axis_limits"], name, path) if "axis_limits" in spec else None
        trusted = _trusted_macros(spec["trusted_macros"], name, path) if "trusted_macros" in spec else frozenset()
        out[name] = Device(name, adapter, host, port, str(model) if model else None, path, ws_port, limits,
                           trusted)
    return out


def load_devices(cwd: Path | None = None) -> dict[str, Device]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - pyyaml is a core dependency
        raise MissingDependency("pyyaml", install="uv sync", degrades="printer-devices.yaml cannot be read",
                                command="printer") from exc
    devices: dict[str, Device] = {}
    for path in search_paths(cwd):
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise DeviceConfigError(f"{path}: invalid YAML: {exc}", command="printer") from exc
        devices.update(_parse(path, data))
    return devices


def get_device(name: str, cwd: Path | None = None) -> Device:
    devices = load_devices(cwd)
    if not devices:
        raise UsageError(
            "no printers configured",
            command="printer",
            remediation=[f"Create {user_path()} (or ./{FILENAME}) like:\n{EXAMPLE}"],
        )
    if name not in devices:
        raise InvalidArgument("printer", name, sorted(devices), command="printer",
                              extra="Run `3d printer list` to see configured printers.")
    return devices[name]
