"""ha_vision.py — `3d ha vision`: the Home Assistant side of camera vision, generated per fleet printer.

WHAT: for EVERY printer of fleet/printers.yaml that has a camera (printer_ops.camera) it emits the HA
  artifacts that used to exist for the U1 only, from the printer's own settings (name, host, port,
  model, camera kind / URLs, bundled camera calibration) instead of copy-pasted U1 constants:

    packages/printer_vision.yaml     whole file: per camera printer the vision sensors (verdict p/q,
                                     events, printing / paused / alive, from 3d-api
                                     GET /v1/printers/NAME/vision) and, for a printer WITH a bundled
                                     camera calibration, the ghost switch (input_boolean, rest_command,
                                     sync automation)
    go2rtc/go2rtc.yaml               region `go2rtc-streams`: one live stream per camera printer
    compose/3d.docker-compose.yaml   region `compose-proxies`: the camera proxy service of every printer
                                     that needs one (a Snapmaker-style monitor camera that must be kept
                                     awake, or a calibrated printer: the ghost is drawn into its frames)
    dashboards/ui-3d-printers.yaml   region `dashboard:NAME` inside each printer's column: the live
                                     camera card, the ghost tile (calibrated printers) and the vision card

  THE GHOST (what the user noticed on the U1 only): a semi-transparent 3D drawing of the walls still
  to be printed (lib/vision_ghost.py), projected through the printer's camera calibration and baked
  into the camera proxy's frames; the print drifting off its ghost, or a part vanishing from it, is
  the visual tell of a detached part. It needs `lib/vision_geometry` CameraModel = a calibration, so a
  printer without a bundled one (today every printer but the U1, the K1 SE included) gets everything
  else and NO ghost: `plan()` and the generated package header say so, naming the missing file.

INVARIANTS: pure text in, text out (stdlib + lazy pyyaml); `generate` never writes, `write` only
  rewrites the files it owns (a region between its markers, or the whole package); deterministic
  (no machine-specific input: only repo data, so CI can check the committed files for drift);
  nothing here touches Home Assistant or a printer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from errors import GateFailure, UsageError

CMD = "ha"
INTEGRATION = Path("integrations") / "homeassistant"
MANIFEST = INTEGRATION / "manifest.yaml"
PACKAGE = INTEGRATION / "packages" / "printer_vision.yaml"
GO2RTC = INTEGRATION / "go2rtc" / "go2rtc.yaml"
COMPOSE = INTEGRATION / "compose" / "3d.docker-compose.yaml"
DASHBOARD = INTEGRATION / "dashboards" / "ui-3d-printers.yaml"
PROXY_SCRIPT = "scripts/u1_mjpeg_proxy.py"
KEEPALIVE_INTERVAL_S = 240  # u1_mjpeg_proxy.py U1_CAMERA_START_MONITOR_INTERVAL
PROXY_FPS = "1.0"


@dataclass(frozen=True)
class Settings:
    """`generate:` of integrations/homeassistant/manifest.yaml."""

    printops_api: str  # origin of 3d-api as seen from HA, e.g. http://192.168.0.39:8765
    proxy_port_base: (
        int  # the first camera proxy listens here, the next one on +1 (fleet order)
    )
    scan_interval_s: int  # how often HA polls the vision status
    alive_max_age_s: int  # a status older than this = the vision loop is not running


@dataclass(frozen=True)
class VisionPrinter:
    name: str
    title: str  # "U1", "K1 SE": the model without its vendor
    slug: str  # HA entity slug of the title ("u1", "k1_se"), as the existing printer_<slug>_* sensors
    model: str
    host: str
    port: int
    kind: str  # printer_ops.camera kind
    snapshot_url: str | None
    streams: tuple[str, ...]  # direct live-stream URLs (when no proxy)
    keepalive: bool
    ghost: bool  # a bundled camera calibration exists for the model and the printer speaks Moonraker
    proxy_port: (
        int | None
    )  # the camera proxy service of this printer, None = direct stream

    @property
    def proxied(self) -> bool:
        return self.proxy_port is not None

    @property
    def proxy_service(self) -> str:
        return f"{self.name}-camera-proxy"

    @property
    def stream(self) -> str:
        return f"{self.name}_camera"


# Longest first: a model reads "<vendor> <name>"; the HA sensors of the existing printers carry the
# name only (`printer_u1_*`, `printer_k1_se_*`).
VENDORS = ("Prusa Research", "Bambu Lab", "Snapmaker", "Creality", "Prusa", "Anycubic", "Elegoo", "Flashforge",
           "Qidi", "Sovol", "Artillery", "Voron")


def title_of(model: str) -> str:
    """'Snapmaker U1' -> 'U1', 'Creality K1 SE' -> 'K1 SE': the model without its vendor."""
    for vendor in VENDORS:
        rest = model[len(vendor):].strip()
        if model.lower().startswith(vendor.lower() + " ") and rest:
            return rest
    return model


def slug_of(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")


def load_settings(root: Path) -> Settings:
    import yaml

    path = root / MANIFEST
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    block = data.get("generate")
    keys = ("printops_api", "proxy_port_base", "scan_interval_s", "alive_max_age_s")
    if not isinstance(block, dict) or any(k not in block for k in keys):
        raise GateFailure(
            f"{path}: `generate:` must hold {', '.join(keys)}",
            command=CMD,
            remediation=[
                "See integrations/homeassistant/manifest.yaml and docs/commands/ha.md."
            ],
        )
    api = str(block["printops_api"]).rstrip("/")
    if not re.fullmatch(r"https?://[^/\s]+", api):
        raise GateFailure(
            f"{path}: generate.printops_api must be an http(s) origin, got {api!r}",
            command=CMD,
        )
    nums = {k: block[k] for k in keys[1:]}
    if any(
        isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in nums.values()
    ):
        raise GateFailure(
            f"{path}: generate.{', '.join(keys[1:])} must be positive integers",
            command=CMD,
        )
    return Settings(
        api, nums["proxy_port_base"], nums["scan_interval_s"], nums["alive_max_age_s"]
    )


def camera_printers(fleet: Any, settings: Settings) -> list[VisionPrinter]:
    """Every contactable fleet printer with a camera, in fleet-file order (stable proxy ports)."""
    from printer_ops.camera import MONITOR, camera_spec
    from printer_ops.devices import fleet_devices
    from vision_geometry import has_bundled_calibration

    devices = fleet_devices(fleet)
    out: list[VisionPrinter] = []
    next_port = settings.proxy_port_base
    for name, printer in fleet.printers.items():
        device = devices.get(name)
        if device is None:
            continue
        spec = camera_spec(device)
        if not spec.present:
            continue
        ghost = device.adapter == "moonraker" and has_bundled_calibration(printer.model)
        proxy_port = None
        if spec.kind == MONITOR or ghost:
            proxy_port, next_port = next_port, next_port + 1
        title = title_of(printer.model)
        clash = [o.name for o in out if o.slug == slug_of(title)]
        if clash:
            raise GateFailure(
                f"fleet printers {clash[0]} and {name} share the Home Assistant name {title!r} (model {printer.model})",
                command=CMD,
                remediation=["Give the two models distinct names in fleet/printers.yaml / lib/data/printers.yaml."],
            )
        out.append(
            VisionPrinter(
                name,
                title,
                slug_of(title),
                printer.model,
                device.host,
                device.port,
                spec.kind,
                spec.snapshot_url(device.host, device.port),
                spec.streams,
                spec.keepalive,
                ghost,
                proxy_port,
            )
        )
    return out


def plan(fleet: Any, printers: list[VisionPrinter]) -> list[str]:
    """One line per fleet printer: what it gets and why not the rest."""
    from printer_ops.camera import camera_spec
    from printer_ops.devices import fleet_devices

    devices = fleet_devices(fleet)
    by_name = {p.name: p for p in printers}
    lines = []
    for name, printer in fleet.printers.items():
        p = by_name.get(name)
        if p is None:
            why = (
                camera_spec(devices[name]).source
                if name in devices
                else f"{printer.access}: never contacted"
            )
            lines.append(f"{name}: no camera ({why}) -> nothing generated")
            continue
        parts = [
            f"camera {p.kind}",
            "proxy :" + str(p.proxy_port) if p.proxied else "direct stream",
            "vision sensors",
        ]
        parts.append(
            "ghost"
            if p.ghost
            else f"NO ghost: no bundled camera calibration for {p.model} "
            "(add lib/data/vision/<model>-camera.yaml and its _BUNDLED entry in lib/vision_geometry.py)"
        )
        lines.append(f"{name}: " + ", ".join(parts))
    return lines


# ---- the package -----------------------------------------------------------------------------


def _vision_rest(p: VisionPrinter, s: Settings) -> str:
    t, slug = p.title, p.slug
    return f"""\
  - resource: "{s.printops_api}/v1/printers/{p.name}/vision"
    headers:
      Authorization: !secret printops_api_auth
    scan_interval: {s.scan_interval_s}
    timeout: 10
    sensor:
      - name: "Printer {t} Vision Verdict"
        unique_id: printer_{slug}_vision_verdict
        icon: mdi:eye-check-outline
        state_class: measurement
        value_template: "{{{{ value_json.q if value_json.q is not none else value_json.p }}}}"
        json_attributes: [state, file, p, q, trend, luma, findings, frames, age_s, camera_error]
      - name: "Printer {t} Vision Events"
        unique_id: printer_{slug}_vision_events
        icon: mdi:alert-circle-outline
        state_class: measurement
        value_template: "{{{{ value_json.events.total }}}}"
        json_attributes: [events, last_event]
    binary_sensor:
      - name: "Printer {t} Vision Printing"
        unique_id: printer_{slug}_vision_printing
        value_template: "{{{{ value_json.state == 'printing' }}}}"
      - name: "Printer {t} Vision Paused"
        unique_id: printer_{slug}_vision_paused
        value_template: "{{{{ value_json.state == 'paused' }}}}"
      - name: "Printer {t} Vision Alive"
        unique_id: printer_{slug}_vision_alive
        device_class: connectivity
        value_template: "{{{{ value_json.age_s < {s.alive_max_age_s} }}}}"
"""


def _ghost_command(p: VisionPrinter) -> str:
    return f"""\
  {p.name}_camera_ghost_set:
    url: "http://{p.proxy_service}:{p.proxy_port}/ghost"
    method: post
    content_type: "application/json"
    payload: '{{"enabled": {{{{ enabled | bool | tojson }}}}}}'
    timeout: 10
"""


def _ghost_boolean(p: VisionPrinter) -> str:
    return f"""\
  {p.name}_camera_ghost:
    name: "{p.title}: призрак остатка на камере"
    icon: mdi:cube-outline
    # No `initial:` — it would override the restored state on every HA start. A new helper
    # starts off; turn it on once after the first deploy (input_boolean.turn_on).
"""


def _ghost_automation(p: VisionPrinter) -> str:
    return f"""\
  - id: {p.name}_camera_ghost_sync
    alias: "{p.title}: призрак остатка → прокси камеры"
    mode: restart
    triggers:
      - trigger: state
        entity_id: input_boolean.{p.name}_camera_ghost
      - trigger: homeassistant
        event: start
      - trigger: time_pattern
        minutes: "/5"
    actions:
      - action: rest_command.{p.name}_camera_ghost_set
        data:
          enabled: "{{{{ is_state('input_boolean.{p.name}_camera_ghost', 'on') }}}}"
"""


def frame_url(p: VisionPrinter) -> str | None:
    """Where a JPEG frame of this printer's camera is fetched from inside the HA network."""
    if p.proxied:
        return f"http://{p.proxy_service}:{p.proxy_port}/{p.name}.jpg"
    if p.streams:
        return f"http://go2rtc:1984/api/frame.jpeg?src={p.stream}"
    return None


def _grabber(p: VisionPrinter, url: str) -> str:
    """The command_line sensor script.printer_send_notification refreshes for a phone-alert frame."""
    return f"""\
  - sensor:
      name: "Printer {p.title} Alert Snapshot"
      unique_id: printer_{p.slug}_alert_snapshot
      icon: mdi:camera
      scan_interval: 315360000
      command_timeout: 10
      command: >-
        d=/media/printer-alerts; f={p.slug}-$(date +%Y%m%d-%H%M%S)-$$.jpg; mkdir -p "$d" &&
        if curl -fsS --connect-timeout 2 --max-time 5 -o "$d/$f" '{url}'
        && [ "$(head -c 2 "$d/$f" | od -An -tx1 | tr -d ' \\n')" = ffd8 ];
        then echo "printer-alerts/$f"; else rm -f "$d/$f"; echo failed; fi;
        ls -1t "$d"/{p.slug}-*.jpg 2>/dev/null | tail -n +31 | xargs -r rm -f
"""


def render_package(printers: list[VisionPrinter], s: Settings) -> str:
    ghosts = [p for p in printers if p.ghost]
    head = """\
# GENERATED by `3d ha vision` from fleet/printers.yaml (and lib/data/vision calibrations): do not
# edit; change the fleet entry, run `3d ha vision --write` and commit. Needs in the HA repo's
# secrets.yaml:  printops_api_auth: "Bearer <3d-api token, scope read>"  (packages/printops_tasks.yaml).
#
# Per printer with a camera (watch.vision.camera):
#   sensor.printer_<slug>_vision_verdict   q of the last frame (else p); attributes: state, file, p, q,
#                                          trend, luma, findings, frames, age_s, camera_error
#   sensor.printer_<slug>_vision_events    events raised by the running `3d printer vision`;
#                                          attributes: events {total, by_kind}, last_event
#   binary_sensor.printer_<slug>_vision_{printing,paused,alive}   alive = the status is fresher than
#                                          generate.alive_max_age_s, i.e. the vision loop is running
#   sensor.printer_<slug>_alert_snapshot   the frame script.printer_send_notification attaches to a phone alert
# Per printer with a bundled camera calibration (the ghost of the rest of the print on its camera):
#   input_boolean.<printer>_camera_ghost, rest_command.<printer>_camera_ghost_set, a sync automation.
#
"""
    for p in printers:
        if not p.ghost:
            head += (
                f"# {p.name}: NO ghost — no bundled camera calibration for {p.model}\n"
                "#   (lib/data/vision, lib/vision_geometry.py _BUNDLED); the sensors and the live camera need none.\n"
            )
    body = "\nrest:\n" + "".join(_vision_rest(p, s) for p in printers)
    # Camera frames for the phone alerts (script.printer_send_notification in packages/3d_printers.yaml,
    # docs/commands/ha.md "Printer alerts carry a camera frame"): refreshed only by homeassistant.update_entity.
    grabbers = [(p, url) for p in printers if (url := frame_url(p))]
    if grabbers:
        body += "\ncommand_line:\n" + "".join(_grabber(p, url) for p, url in grabbers)
    if ghosts:
        body += "\nrest_command:\n" + "".join(_ghost_command(p) for p in ghosts)
        body += "\ninput_boolean:\n" + "".join(_ghost_boolean(p) for p in ghosts)
        body += (
            "\nautomation:\n  # A proxy forgets its switch when its container restarts; re-sending every 5 minutes and at\n"
            "  # HA start brings it back to the helper's position.\n"
            + "".join(_ghost_automation(p) for p in ghosts)
        )
    return head + body


# ---- regions of hand-written files -------------------------------------------------------------


def markers(region: str) -> tuple[str, str]:
    return (
        f"# >>> 3d ha vision: {region} (generated from fleet/printers.yaml; do not edit, run `3d ha vision --write`)",
        f"# <<< 3d ha vision: {region}",
    )


def render_go2rtc(printers: list[VisionPrinter]) -> str:
    out = "streams:\n"
    for p in printers:
        out += f"  {p.stream}:\n"
        if p.proxied:
            url = f"http://{p.proxy_service}:{p.proxy_port}/{p.name}.mjpeg"
            out += f"    - ffmpeg:{url}#video=h264\n    - {url}\n"
        elif p.streams:
            out += "".join(f"    - {u}\n" for u in p.streams)
        else:
            out += "    []  # no live stream: set watch.vision.camera.streams in fleet/printers.yaml\n"
    return out if printers else "streams: {}\n"


def render_compose(printers: list[VisionPrinter]) -> str:
    out = ""
    for p in (p for p in printers if p.proxied):
        env = [
            f"U1_CAMERA_URL={p.snapshot_url}",
            f"U1_CAMERA_FPS={PROXY_FPS}",
            f"U1_CAMERA_PORT={p.proxy_port}",
            f"U1_CAMERA_START_MONITOR={'true' if p.keepalive else 'false'}",
        ]
        if p.keepalive:
            env += [
                f"U1_CAMERA_START_MONITOR_URL=http://{p.host}:{p.port}/server/mqtt/publish",
                f"U1_CAMERA_START_MONITOR_INTERVAL={KEEPALIVE_INTERVAL_S}",
            ]
        env += [
            f"U1_CAMERA_NAME={p.name}",
            f"U1_GHOST_MODEL={p.model}",
            f"U1_PRINTER_URL=http://{p.host}:{p.port}",
        ]
        out += (
            f"""\
  {p.proxy_service}:
    # Preserve callbacks to HA after HA leaves the bridge network.
    extra_hosts:
      - "homeassistant:host-gateway"
      - "homeassistant.local:host-gateway"
    image: ghcr.io/home-assistant/home-assistant:stable
    user: "1000:1000"
    command: ["python", "/config/{PROXY_SCRIPT}"]
    environment:
"""
            + "".join(f"      - {e}\n" for e in env)
            + f"""\
    volumes:
      - /usr/share/fonts:/usr/share/fonts:ro
      - /home/ultra/homeassistant:/config:ro
    expose:
      - "{p.proxy_port}"
    restart: unless-stopped
    networks:
      - ha-net
"""
        )
    return out


_STYLE = (
    "video {aspect-ratio: 16/9; object-fit: contain; background: #111;}\n"
    ".controls {top: 32px; right: 8px; bottom: auto; left: auto; gap: 6px; padding: 2px 6px; "
    "border-radius: 8px; background: rgba(0,0,0,.45);}\n"
)


def render_dashboard(p: VisionPrinter) -> str:
    """The cards of one printer's column below its status cards (indent 10 = inside `cards:`)."""
    note = (
        "# The camera proxy bakes the inspector status into the bottom band of every frame, so the",
        "# player controls move top-right; the stream is switched via the mode label (top-right).",
    ) if p.proxied else (
        f"# {p.title}: go2rtc stream {p.stream}, served by the printer's own streamer. Its JPEG frames are",
        "# rejected by WebRTC/MSE (\"codecs not matched\"); MJPEG mode relays them over the HA websocket",
        "# unchanged, so phones play it without a transcode. Controls sit top-right.",
    )
    lines = [
        *note,
        "- type: custom:webrtc-camera",
        f"  title: {p.title} Camera",
        "  server: http://go2rtc:1984/",
        "  muted: true",
        "  ui: true",
        "  background: true",
        "  streams:",
    ]
    modes = (
        (("WebRTC", "webrtc"), ("MSE", "mse"), ("MJPEG", "mjpeg"))
        if p.proxied
        else (("MJPEG", "mjpeg"),)
    )
    for label, mode in modes:
        lines += [
            f"    - url: {p.stream}",
            f"      name: {label}",
            f"      mode: {mode}",
            "      media: video",
        ]
    hide = (
        ".space, .stream {display: none !important;}"
        if p.proxied
        else ".space, .stream, .play {display: none !important;}"
    )
    lines += [
        "  style: >-",
        "    " + _STYLE.splitlines()[0],
        "    " + _STYLE.splitlines()[1],
        "    " + hide,
    ]
    if not p.proxied and not p.streams:
        lines = [
            f"# {p.name}: camera {p.kind} has no live stream (set watch.vision.camera.streams): no camera card"
        ]
    if p.ghost:
        lines += [
            "# Semi-transparent 3D ghost of the layers still to print, baked into the stream by the proxy.",
            "- type: tile",
            f"  entity: input_boolean.{p.name}_camera_ghost",
            "  name: Призрак печати",
            "  icon: mdi:cube-outline",
            "  tap_action:",
            "    action: toggle",
        ]
    else:
        lines.append(
            f"# No ghost overlay for {p.title}: no bundled camera calibration for {p.model} "
            "(lib/data/vision, lib/vision_geometry.py)."
        )
    prefix = f"printer_{p.slug}_vision"
    lines += [
        "- type: entities",
        f"  title: {p.title} vision",
        "  show_header_toggle: false",
        "  entities:",
        f"    - entity: binary_sensor.{prefix}_alive",
        "      name: Vision running",
        f"    - entity: sensor.{prefix}_verdict",
        "      name: Verdict (q, else p)",
        f"    - entity: sensor.{prefix}_events",
        "      name: Events",
        f"    - entity: binary_sensor.{prefix}_printing",
        "      name: Printing",
        f"    - entity: binary_sensor.{prefix}_paused",
        "      name: Paused",
    ]
    return "".join(f"          {line}\n" if line else "\n" for line in lines)


def find_region(text: str, region: str) -> tuple[int, int] | None:
    """(start, end) character offsets of the region INCLUDING both marker lines, or None."""
    begin, end = markers(region)
    i = text.find(begin)
    j = text.find(end)
    if i == -1 or j == -1 or j < i:
        return None
    return i, j + len(end)


def replace_region(text: str, region: str, body: str, where: Path) -> str:
    found = find_region(text, region)
    if found is None:
        begin, end = markers(region)
        raise UsageError(
            f"{where}: no region `{region}` to fill",
            command=CMD,
            remediation=[
                f"Add these two lines where the generated block belongs, then run `3d ha vision --write`:\n"
                f"{begin}\n{end}"
            ],
        )
    start, stop = found
    indent = " " * (start - (text.rfind("\n", 0, start) + 1))
    begin, end = markers(region)
    return text[:start] + f"{begin}\n{body}{indent}{end}" + text[stop:]


# ---- the whole run -----------------------------------------------------------------------------


def generate(
    root: Path, fleet: Any
) -> tuple[dict[Path, str], list[VisionPrinter], Settings]:
    """{path relative to root: new full text} for every file this command owns."""
    settings = load_settings(root)
    printers = camera_printers(fleet, settings)
    files: dict[Path, str] = {PACKAGE: render_package(printers, settings)}
    for path, region, body in (
        (GO2RTC, "go2rtc-streams", render_go2rtc(printers)),
        (COMPOSE, "compose-proxies", render_compose(printers)),
    ):
        files[path] = replace_region(
            (root / path).read_text(encoding="utf-8"), region, body, root / path
        )
    dashboard = (root / DASHBOARD).read_text(encoding="utf-8")
    for p in printers:
        dashboard = replace_region(
            dashboard, f"dashboard:{p.name}", render_dashboard(p), root / DASHBOARD
        )
    files[DASHBOARD] = dashboard
    return files, printers, settings


def drift(root: Path, files: dict[Path, str]) -> list[Path]:
    """The generated files whose committed text differs from what the fleet generates now."""
    stale = []
    for path, text in files.items():
        target = root / path
        if not target.is_file() or target.read_text(encoding="utf-8") != text:
            stale.append(path)
    return stale


# ---- what must exist -----------------------------------------------------------------------------

def expected_entities(p: VisionPrinter) -> list[str]:
    """Entity ids HA must offer for this printer (sensors, helpers, the dashboard's entity cards)."""
    prefix = f"printer_{p.slug}_vision"
    out = [f"sensor.{prefix}_verdict", f"sensor.{prefix}_events", f"binary_sensor.{prefix}_alive",
           f"binary_sensor.{prefix}_printing", f"binary_sensor.{prefix}_paused"]
    if p.ghost:
        out.append(f"input_boolean.{p.name}_camera_ghost")
    return out


def expected_grabber(p: VisionPrinter) -> str | None:
    """The unique_id of the phone-alert frame grabber of this printer, None without a frame source."""
    return f"printer_{p.slug}_alert_snapshot" if frame_url(p) else None


def _walk(node: Any, key: str) -> list[Any]:
    """Every value stored under `key` anywhere in a parsed YAML tree."""
    found: list[Any] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                found.append(v)
            found += _walk(v, key)
    elif isinstance(node, list):
        for item in node:
            found += _walk(item, key)
    return found


def _load(path: Path) -> Any:
    from ha_deploy import load_ha_yaml

    return load_ha_yaml(path.read_text(encoding="utf-8")) if path.is_file() else None


def missing(root: Path, printers: list[VisionPrinter]) -> list[str]:
    """Everything the camera printers need in the COMMITTED files and that is not there: package
    sensors / ghost helpers / ghost service+automation, the go2rtc stream, the proxy service, the
    dashboard region and its entities, the manifest entry of the package. Empty = complete."""
    problems: list[str] = []
    package = _load(root / PACKAGE)
    unique = {str(u) for u in _walk(package, "unique_id")}
    sensors = {f"{d}.{u}" for u in unique for d in ("sensor", "binary_sensor")}
    helpers = {f"input_boolean.{k}" for b in _walk(package, "input_boolean") if isinstance(b, dict) for k in b}
    commands = {str(k) for b in _walk(package, "rest_command") if isinstance(b, dict) for k in b}
    automations = {str(a.get("id")) for b in _walk(package, "automation") if isinstance(b, list) for a in b}
    resources = {str(r) for r in _walk(package, "resource")}
    streams = (_load(root / GO2RTC) or {}).get("streams") or {}
    services = (_load(root / COMPOSE) or {}).get("services") or {}
    dashboard = _load(root / DASHBOARD)
    dash_entities = {str(e) for e in _walk(dashboard, "entity")}
    dash_streams = {str(s) for s in _walk(dashboard, "url")}
    dash_text = (root / DASHBOARD).read_text(encoding="utf-8") if (root / DASHBOARD).is_file() else ""
    for p in printers:
        where = p.name
        for entity in expected_entities(p):
            if not entity.startswith("input_boolean.") and entity not in sensors:
                problems.append(f"{where}: {entity} is not defined in {PACKAGE}")
            if entity.startswith("input_boolean.") and entity not in helpers:
                problems.append(f"{where}: {entity} is not defined in {PACKAGE}")
            if entity not in dash_entities:
                problems.append(f"{where}: {entity} is not on the dashboard ({DASHBOARD})")
        if not any(r.endswith(f"/v1/printers/{p.name}/vision") for r in resources):
            problems.append(f"{where}: no REST resource of /v1/printers/{p.name}/vision in {PACKAGE}")
        grabber = expected_grabber(p)
        if grabber is not None and grabber not in unique:
            problems.append(f"{where}: command_line sensor {grabber} (alert frame grabber) is not defined in {PACKAGE}")
        if p.ghost:
            if f"{p.name}_camera_ghost_set" not in commands:
                problems.append(f"{where}: rest_command.{p.name}_camera_ghost_set is not defined in {PACKAGE}")
            if f"{p.name}_camera_ghost_sync" not in automations:
                problems.append(f"{where}: automation {p.name}_camera_ghost_sync is not defined in {PACKAGE}")
        if p.stream not in streams:
            problems.append(f"{where}: go2rtc stream {p.stream} is not in {GO2RTC}")
        if (p.stream in dash_streams) is not bool(p.proxied or p.streams):
            problems.append(f"{where}: the dashboard camera card of stream {p.stream} is "
                            f"{'missing' if p.proxied or p.streams else 'present without a live stream'}")
        if p.proxied and p.proxy_service not in services:
            problems.append(f"{where}: compose service {p.proxy_service} is not in {COMPOSE}")
        if not p.proxied and p.proxy_service in services:
            problems.append(f"{where}: compose service {p.proxy_service} exists but the printer needs no proxy")
        if find_region(dash_text, f"dashboard:{p.name}") is None:
            problems.append(f"{where}: no `dashboard:{p.name}` region in {DASHBOARD}")
    manifest = (root / MANIFEST).read_text(encoding="utf-8") if (root / MANIFEST).is_file() else ""
    if printers and f"src: {PACKAGE.relative_to(INTEGRATION)}" not in manifest:
        problems.append(f"{PACKAGE.relative_to(INTEGRATION)} is not a `files` entry of {MANIFEST}")
    return problems
