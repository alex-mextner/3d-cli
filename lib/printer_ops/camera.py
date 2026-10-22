"""camera.py — which camera a printer has: the ONE place `watch.vision` camera settings are read.

WHAT: `camera_spec(device)` turns a device's `watch.vision` block (fleet/printers.yaml or a
  printer-devices.yaml override) into a `CameraSpec`: the kind of camera, the snapshot URL,
  whether it needs a keepalive, and the live-stream URLs for Home Assistant / go2rtc. Everything
  that needs a frame — `3d printer vision`, `guard`, the bed monitor (`3d printer bed`), the
  dossier and the Home Assistant generator (lib/ha_vision.py) — asks this module, so no caller
  carries a printer's camera path in a literal.

CONFIG (`<printer>.watch.vision`):
    camera:
      kind: moonraker-monitor   # Snapmaker U1: Moonraker serves /server/files/camera/monitor.jpg
                                #   only while `camera.start_monitor` is renewed (keepalive on)
      # kind: mjpg-streamer     # Creality Helper Script: http://HOST:PORT/?action=snapshot|stream
      #   port: 8080            #   (default 8080); `url` overrides the snapshot URL
      #   streams: [URL, ...]   #   live-stream URLs for go2rtc (default: ?action=stream)
      # kind: snapshot-url      # any camera that answers a JPEG GET; `url` = absolute URL or a
      #   url: http://...       #   path on the printer's Moonraker origin; optional `streams`
      # kind: none              # the printer has no camera: vision/bed report "no camera"
      keepalive: false          # optional bool; default true only for moonraker-monitor
    snapshot: URL               # shorthand for camera: {kind: snapshot-url, url: URL}

A device with no camera setting gets its MODEL's default (lib/data/vision/cameras.yaml, e.g.
`Snapmaker U1` -> moonraker-monitor); without one the printer has `kind: none` ("not
configured"), never a guessed U1 path.

INVARIANTS: stdlib at import time (pyyaml lazily for the model table); nothing here touches the
  network. `vision_problems` returns messages (fleet lint), `camera_spec` raises InvalidArgument.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from errors import InvalidArgument, UsageError

if TYPE_CHECKING:
    from printer_ops.devices import Device
    from printer_ops.vision import MoonrakerCamera

MONITOR = "moonraker-monitor"
MJPG = "mjpg-streamer"
SNAPSHOT_URL = "snapshot-url"
NONE = "none"
KINDS = (MONITOR, MJPG, SNAPSHOT_URL, NONE)
# Moonraker's own camera file on Snapmaker firmware: written by `unisrv` only while a client
# renews `camera.start_monitor`; printer_ops.vision.MoonrakerCamera does the renewal.
MONITOR_PATH = "/server/files/camera/monitor.jpg"
DEFAULT_MJPG_PORT = 8080
MODELS_FILE = Path(__file__).resolve().parents[1] / "data" / "vision" / "cameras.yaml"
_URL_KEYS = {
    MONITOR: ("kind", "keepalive"),
    MJPG: ("kind", "url", "port", "streams", "keepalive"),
    SNAPSHOT_URL: ("kind", "url", "streams", "keepalive"),
    NONE: ("kind",),
}


@dataclass(frozen=True)
class CameraSpec:
    kind: str  # one of KINDS
    snapshot: (
        str | None
    )  # absolute URL, or a path on the printer's Moonraker origin; None = no camera
    keepalive: bool  # the camera sleeps until `camera.start_monitor` is renewed
    streams: tuple[
        str, ...
    ]  # absolute live-stream URLs (go2rtc / HA); () = snapshots only
    source: str  # where the setting came from, for messages

    @property
    def present(self) -> bool:
        return self.kind != NONE

    def snapshot_url(self, host: str, port: int) -> str | None:
        """The absolute snapshot URL (a bare path is resolved against the printer's Moonraker)."""
        if self.snapshot is None:
            return None
        return (
            self.snapshot
            if "://" in self.snapshot
            else f"http://{host}:{port}/{self.snapshot.lstrip('/')}"
        )


def _model_key(model: str) -> str:
    return re.sub(r"[^a-z0-9]", "", model.lower())


def model_default(model: str | None) -> dict[str, Any] | None:
    """The camera block shipped for a printer model (lib/data/vision/cameras.yaml), or None."""
    if not model or not MODELS_FILE.is_file():
        return None
    import yaml

    data = yaml.safe_load(MODELS_FILE.read_text(encoding="utf-8")) or {}
    for name, block in data.items():
        if _model_key(str(name)) == _model_key(model):
            return dict(block)
    return None


def _check_camera(block: Any, where: str) -> list[str]:
    if not isinstance(block, dict):
        return [f"{where}: must be a mapping {{kind: {'|'.join(KINDS)}, ...}}"]
    kind = block.get("kind")
    if kind not in KINDS:
        return [f"{where}.kind: {kind!r} is not one of {', '.join(KINDS)}"]
    bad = [
        f"{where}.{k}: not a {kind} key (allowed: {', '.join(_URL_KEYS[kind])})"
        for k in sorted(set(block) - set(_URL_KEYS[kind]))
    ]
    if kind == SNAPSHOT_URL and not _text(block.get("url")):
        bad.append(
            f"{where}.url: required for {SNAPSHOT_URL} (an absolute URL or a path on the printer's Moonraker)"
        )
    if "url" in block and not _text(block["url"]):
        bad.append(f"{where}.url: must be a non-empty URL or path")
    if "port" in block and (
        isinstance(block["port"], bool)
        or not isinstance(block["port"], int)
        or not 0 < block["port"] < 65536
    ):
        bad.append(f"{where}.port: an integer 1-65535")
    if "streams" in block:
        streams = block["streams"]
        if (
            not isinstance(streams, list)
            or not streams
            or not all(_text(s) and "://" in s for s in streams)
        ):
            bad.append(f"{where}.streams: a non-empty list of absolute stream URLs")
    if "keepalive" in block and not isinstance(block["keepalive"], bool):
        bad.append(f"{where}.keepalive: true or false")
    return bad


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def vision_problems(vision: Any, where: str = "watch.vision") -> list[str]:
    """Every problem of the camera settings in a `watch.vision` block (empty = fine)."""
    if vision is None:
        return []
    if not isinstance(vision, dict):
        return [f"{where}: must be a mapping"]
    bad: list[str] = []
    if "camera" in vision and "snapshot" in vision:
        bad.append(f"{where}: give `camera:` or its shorthand `snapshot:`, not both")
    if "camera" in vision:
        bad.extend(_check_camera(vision["camera"], f"{where}.camera"))
    if "snapshot" in vision and not _text(vision["snapshot"]):
        bad.append(f"{where}.snapshot: must be a non-empty URL or path")
    if "keepalive" in vision and not isinstance(vision["keepalive"], bool):
        bad.append(f"{where}.keepalive: true or false")
    from printer_ops.vision_config import limits_problems

    bad.extend(limits_problems(vision, where))
    return bad


def _spec(
    block: dict[str, Any], vision: dict[str, Any], host: str, source: str
) -> CameraSpec:
    kind = block["kind"]
    keep = vision.get("keepalive")
    keep = block.get("keepalive") if not isinstance(keep, bool) else keep
    keepalive = keep if isinstance(keep, bool) else kind == MONITOR
    if kind == NONE:
        return CameraSpec(NONE, None, False, (), source)
    if kind == MONITOR:
        return CameraSpec(MONITOR, MONITOR_PATH, keepalive, (), source)
    port = int(block.get("port", DEFAULT_MJPG_PORT))
    origin = f"http://{host}:{port}"
    if kind == MJPG:
        snapshot = str(block.get("url") or f"{origin}/?action=snapshot").strip()
        streams = tuple(block.get("streams") or [f"{origin}/?action=stream"])
    else:
        snapshot = str(block["url"]).strip()
        streams = tuple(block.get("streams") or ())
    return CameraSpec(kind, snapshot, keepalive, streams, source)


def camera_spec(device: Device) -> CameraSpec:
    """The camera of `device` from its `watch.vision` settings, else its model's default, else
    `kind: none`. Raises InvalidArgument for a broken block."""
    vision = (device.watch or {}).get("vision") or {}
    problems = vision_problems(vision, f"{device.name}.watch.vision")
    if problems:
        raise InvalidArgument(
            f"{device.name}.watch.vision",
            "; ".join(problems),
            [f"camera: {{kind: {'|'.join(KINDS)}, ...}} or snapshot: URL"],
            command="printer",
            extra=f"Fix {device.source}.",
        )
    if "camera" in vision:
        return _spec(
            vision["camera"], vision, device.host, f"{device.name}.watch.vision.camera"
        )
    if "snapshot" in vision:
        return _spec(
            {"kind": SNAPSHOT_URL, "url": vision["snapshot"]},
            vision,
            device.host,
            f"{device.name}.watch.vision.snapshot",
        )
    default = model_default(device.model)
    if default is not None:
        return _spec(default, vision, device.host, f"model default of {device.model}")
    return CameraSpec(
        NONE,
        None,
        False,
        (),
        f"{device.name}: no watch.vision.camera and no model default",
    )


def require_camera(device: Device, command: str) -> CameraSpec:
    """The camera of `device`, or a UsageError that says it has none (never a guessed path)."""
    spec = camera_spec(device)
    if not spec.present:
        raise UsageError(
            f"{device.name}: no camera ({spec.source})",
            command=command,
            remediation=[
                f"Set `watch.vision.camera` for {device.name} in fleet/printers.yaml or printer-devices.yaml, e.g.\n"
                f"  watch:\n    vision:\n      camera: {{kind: {MJPG}, port: {DEFAULT_MJPG_PORT}}}\n"
                f'  kinds: {", ".join(KINDS)} (docs/commands/printer.md "Fleet registry")',
            ],
        )
    return spec


def open_camera(
    device: Device,
    spec: CameraSpec,
    *,
    snapshot: str | None = None,
    keepalive: bool | None = None,
) -> MoonrakerCamera:
    """A frame source for `spec`; `snapshot` / `keepalive` are the command-line overrides."""
    from printer_ops.vision import MoonrakerCamera

    url = snapshot or spec.snapshot
    if url is None:
        raise UsageError(f"{device.name}: no camera snapshot", command="printer vision")
    return MoonrakerCamera(
        device.host,
        device.port,
        snapshot=url,
        # a --snapshot override is a different file: the configured camera's keepalive (the
        # Snapmaker monitor wake-up) applies only when that file is the monitor itself
        keepalive=(
            spec.keepalive if snapshot is None else snapshot == MONITOR_PATH
        )
        if keepalive is None
        else keepalive,
    )
