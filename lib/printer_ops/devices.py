"""devices.py — the physical-printer registry: short name -> adapter, host, port, model.

WHY separate from `3d printers` (lib/registries/printers.py): that registry describes printer
MODELS ("Prusa MK4": bed, nozzle, firmware) and every entry needs a bed. A device is one
machine on the LAN ("u1" at 192.168.0.26 speaking Moonraker). `model:` links a device to its
model entry. Issue https://github.com/alex-mextner/3d-cli/issues/91.

FILES (later wins per name, whole-entry replace):
  0. <repo>/fleet/printers.yaml              (base layer, printops.fleet; offline entries skipped)
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
  Optional `watch: {steer: <omp session>, tg_fallback: true, exec: CMD, min_severity: warning, ...}`
  configures `3d printer watch` for this printer (keys: printer_ops/watch_run.py WATCH_KEYS).
  Optional `resume:` holds the printer-specific values of `3d gcode-resume` (ResumeProfile:
  off-bed purge macro and lengths, travel clearance, start commands); every key is required.
  Optional `bridge_max_mm: 25` is the longest span this printer bridges cleanly (measure it
  with a bridge test); the print-preflight overhangs gate uses it unless --bridge-max is
  given (default 15).
  Optional `access: full | read-only` (default full): `3d printer upload/start/cancel`
  refuse a read-only printer. An offline fleet printer is never contacted: get_device refuses
  it, except get_device(name, offline_ok=True) for the G-code gates, which returns its static
  profile (axis_limits, trusted_macros, bridge_max_mm) as a Device with adapter/access `offline`.

Only devices listed here are ever contacted — no LAN discovery (issue #62 network modes).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from errors import InvalidArgument, MissingDependency, ThreeDError, UsageError
from printer_ops.base import PrinterRefused
from printer_ops.move_bounds import Limits

if TYPE_CHECKING:
    from printops.fleet import Fleet, FleetPrinter

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
  resume:                   # `3d gcode-resume` (Snapmaker/u1-klipper@10f2f69 macros)
    purge_macro: SM_PRINT_PREEXTRUDE_FILAMENT  # INDEX=n FORCE=1 LENGTH: off-bed purge (never RETRACT_LENGTH)
    purge_mm: 30            # header purge per head (cold since the pause, up to 10 mm retracted)
    purge_retract_mm: 0.5   # the macro's default RETRACT_LENGTH: mm a head is left retracted after a purge
    no_tower_prime_mm: 12   # --no-tower: off-bed prime per tool change on top of the un-retract
    clearance_mm: 5         # above everything printed before any X/Y move of the header
    tower_margin_mm: 3      # margin around the prime tower kept free of low travels
    filament_diameter_mm: 1.75
    min_print_temp: 170     # an M109 at or above this is a print (not standby) temperature
    default_accel: 10000    # M204 S when the G-code set none before the cut
    z_max_mm: 275           # stepper_z.position_max: every resume Z stays 1 mm below it (fail-closed)
    start_commands: [DEFECT_DETECTION_START]   # after the bed is hot, before the heads purge
k1se:
  adapter: creality-stock   # Creality K1 family on stock firmware (HTTP + ws:9999)
  host: 192.168.0.27
  model: Creality K1 SE
  axis_limits: {{x: [-5, 229], y: [-0.5, 227]}}   # stepper_x/y position_min/max, printer.cfg
  trusted_macros: [START_PRINT, END_PRINT, PAUSE]  # vendor macros known to stay on the bed
  bridge_max_mm: 15         # longest clean bridge (mm), from a bridge test print"""


class DeviceConfigError(ThreeDError):
    exit_code = 2


@dataclass(frozen=True)
class ResumeProfile:
    """The `resume:` block of a device: printer-specific values of `3d gcode-resume`."""
    purge_macro: str              # off-bed purge macro, called `<macro> INDEX=n FORCE=1 LENGTH=`
    purge_mm: float               # header purge per head
    purge_retract_mm: float       # mm the purge macro leaves the head retracted (its default; never passed)
    no_tower_prime_mm: float      # --no-tower: off-bed prime per tool change
    clearance_mm: float           # Z above everything printed before any X/Y move of the header
    tower_margin_mm: float        # margin around the (old) prime tower
    filament_diameter_mm: float
    min_print_temp: float         # an M109 at or above this is a print temperature
    default_accel: float          # M204 S when the G-code set none before the cut
    z_max_mm: float               # stepper_z.position_max: the output never goes above it - 1 mm
    start_commands: tuple[str, ...]  # emitted once the bed is hot, before the heads purge


_RESUME_NUMBERS = ("purge_mm", "purge_retract_mm", "no_tower_prime_mm", "clearance_mm", "tower_margin_mm",
                   "filament_diameter_mm", "min_print_temp", "default_accel", "z_max_mm")


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
    resume: ResumeProfile | None = None  # `3d gcode-resume` values
    watch: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)  # `3d printer watch` settings
    bridge_max_mm: float | None = None  # longest clean bridge, for the print-preflight overhangs gate
    access: str = "full"  # full | read-only | offline (static profile only, never contacted)


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


def _resume(value: Any, name: str, path: Path) -> ResumeProfile:
    keys = ("purge_macro", *_RESUME_NUMBERS, "start_commands")
    if not isinstance(value, dict) or set(value) != set(keys):
        raise InvalidArgument(f"{name}.resume", str(value), [f"a mapping with exactly the keys {', '.join(keys)}"],
                              command="printer", extra=f"Fix {path}; see `3d gcode-resume --help`.")
    macro, cmds = value["purge_macro"], value["start_commands"]
    if not isinstance(macro, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", macro):
        raise InvalidArgument(f"{name}.resume.purge_macro", str(macro), ["an upper-case macro name"],
                              command="printer", extra=f"Fix {path}.")
    for key in _RESUME_NUMBERS:
        v = value[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (math.isfinite(v) and v >= 0):
            raise InvalidArgument(f"{name}.resume.{key}", str(v), ["a finite number >= 0"], command="printer",
                                  extra=f"Fix {path}.")
    if not isinstance(cmds, list) or not all(isinstance(c, str) and c.strip() for c in cmds):
        raise InvalidArgument(f"{name}.resume.start_commands", str(cmds), ["a list of G-code lines, may be []"],
                              command="printer", extra=f"Fix {path}.")
    f = {k: float(value[k]) for k in _RESUME_NUMBERS}
    return ResumeProfile(macro, f["purge_mm"], f["purge_retract_mm"], f["no_tower_prime_mm"], f["clearance_mm"],
                         f["tower_margin_mm"], f["filament_diameter_mm"], f["min_print_temp"], f["default_accel"],
                         f["z_max_mm"], tuple(c.strip() for c in cmds))


def _bridge_max(value: Any, name: str, path: Path) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not (value > 0 and math.isfinite(value)):
        raise InvalidArgument(f"{name}.bridge_max_mm", str(value), ["a number of mm > 0, e.g. 25"],
                              command="printer", extra=f"Fix {path}.")
    return float(value)


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
        bridge = _bridge_max(spec["bridge_max_mm"], name, path) if "bridge_max_mm" in spec else None
        resume = _resume(spec["resume"], name, path) if "resume" in spec else None
        watch = spec.get("watch") or {}
        if not isinstance(watch, dict):
            raise InvalidArgument(f"{name}.watch", str(watch), ["a mapping, e.g. {tg_fallback: true, min_severity: warning}"],
                                  command="printer", extra=f"Fix {path}.")
        from printer_ops.camera import vision_problems

        bad = vision_problems(watch.get("vision"), f"{name}.watch.vision")
        if bad:
            raise DeviceConfigError(f"{path}: {'; '.join(bad)}", command="printer",
                                    remediation=["watch.vision.camera: see docs/commands/printer.md \"Fleet registry\""])
        access = spec.get("access", "full")
        if access not in ("full", "read-only"):
            raise InvalidArgument(f"{name}.access", str(access), ["full", "read-only"], command="printer",
                                  extra=f"Fix {path}.")
        out[name] = Device(name, adapter, host, port, str(model) if model else None, path, ws_port, limits,
                           trusted, resume, dict(watch), bridge, str(access))
    return out


def fleet_devices(fleet: Fleet | None) -> dict[str, Device]:
    """The printers of fleet/printers.yaml that 3d may contact, as Devices (offline ones are not)."""
    return _parse(fleet.path, fleet.device_specs()) if fleet is not None else {}


def offline_device(printer: FleetPrinter) -> Device:
    """The static profile of an offline fleet printer (no address: never contacted)."""
    d, name, path = printer.device, printer.name, printer.source
    limits = _axis_limits(d["axis_limits"], name, path) if "axis_limits" in d else None
    trusted = _trusted_macros(d["trusted_macros"], name, path) if "trusted_macros" in d else frozenset()
    bridge = _bridge_max(d["bridge_max_mm"], name, path) if "bridge_max_mm" in d else None
    return Device(name, "offline", "", 0, printer.model, path, None, limits, trusted, bridge_max_mm=bridge,
                  access="offline")


def load_devices(cwd: Path | None = None) -> dict[str, Device]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - pyyaml is a core dependency
        raise MissingDependency("pyyaml", install="uv sync", degrades="printer-devices.yaml cannot be read",
                                command="printer") from exc
    from printops.fleet import load_fleet

    fleet = load_fleet()
    devices = fleet_devices(fleet)
    for path in search_paths(cwd):
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise DeviceConfigError(f"{path}: invalid YAML: {exc}", command="printer") from exc
        devices.update(_parse(path, data))
    # An override replaces device settings, never the fleet's access (D7): an offline printer
    # stays out of the contactable set, a read-only one stays read-only. The model is the
    # printer's identity: an override without `model:` keeps the fleet's (vision calibration).
    for name, printer in (fleet.printers if fleet is not None else {}).items():
        if name not in devices:
            continue
        if printer.access == "offline":
            del devices[name]
            continue
        device = devices[name]
        devices[name] = replace(device, model=device.model or printer.model,
                                access=device.access if printer.access == "full" else printer.access)
    return devices


def get_device(name: str, cwd: Path | None = None, *, offline_ok: bool = False) -> Device:
    """Device `name`. An offline fleet printer is refused, unless `offline_ok` (callers that only
    read its static profile, never contact it)."""
    devices = load_devices(cwd)
    if name not in devices:
        from printops.fleet import load_fleet

        fleet = load_fleet()
        offline = fleet.printers.get(name) if fleet is not None else None
        if offline is not None:
            if offline_ok:
                return offline_device(offline)
            raise PrinterRefused(
                f"{name} ({offline.model}, owner {offline.owner}) is an offline fleet printer: 3d never contacts it",
                command="printer",
                remediation=["Run its jobs by hand; `3d printer list` shows which printers 3d can drive."])
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
