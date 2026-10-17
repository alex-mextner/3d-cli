"""topcam.photo_config — the per-printer top-camera photo settings (fleet `photo:` block).

WHAT: `validate(value)` → list of schema problems for a fleet entry's `photo:` mapping;
  `merge(fleet_photo, cli)` → the effective settings (CLI over fleet over DEFAULTS) with the
  origin of every value, so the photo JSON records where each setting came from.
WHY: a calibration photo is only comparable to the last one if it is taken the same way —
  same camera, pose, light, lock and averaging. Those live per printer in
  fleet/printers.yaml and every one can be overridden for one run from the CLI (#342).
INVARIANTS: stdlib only (fleet.py validates through this module at load time).

Schema (every key optional):
  photo:
    camera: topcam            # usb | usb:/dev/videoN | http://host:port | host:port | node name
    res: 2304x1536
    focus: sweep              # auto | N (manual) | sweep[:LO-HI[:STEP]] (software focus)
    focus_roi: 0.2,0.2,0.8,0.8   # sweep region as frame fractions
    exposure: 300             # 100 µs units (auto exposure off)
    gain: 0
    wb: 4500                  # kelvin (auto WB off)
    ctrl: {sharpness: 0}      # any other control by v4l2-ctl name or alias
    frames: 8                 # frames averaged (a node serves up to 8 per request; more = several requests)
    skip: 3
    average: mean             # mean | median | none
    led: on                   # node LED ring during the photo: on | off
    settle_ms: 300
    printer_light: {on: "SET_PIN PIN=LED VALUE=1", off: "SET_PIN PIN=LED VALUE=0"}
    pose: {x: 110, y: 220, z: 120, speed: 6000}   # where the head/bed go for the photo (mm, mm/min)
    camera_height_mm: 300     # lens above the bed surface at the pose (parallax corrections)
    px_per_mm: 10             # rectified image resolution
    scale_ref: scale-refs/steel-card.json   # external scale reference (relative to the fleet file)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

DEFAULTS: dict[str, Any] = {
    "camera": "topcam",
    "res": "2304x1536",
    "frames": 4,
    "skip": 3,
    "average": "mean",
    "px_per_mm": 10.0,
}
INT_KEYS = ("exposure", "gain", "wb", "frames", "skip", "settle_ms")
NUM_KEYS = ("camera_height_mm", "px_per_mm")
STR_KEYS = ("camera", "res", "scale_ref", "focus_roi")
KEYS = frozenset(
    {*INT_KEYS, *NUM_KEYS, *STR_KEYS, "focus", "ctrl", "average", "led", "printer_light", "pose"}
)
POSE_KEYS = frozenset({"x", "y", "z", "speed"})
AVERAGES = ("mean", "median", "none")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def validate(value: Any) -> list[str]:
    """Problems as "photo.<key>: what is wrong" (empty list = valid)."""
    if not isinstance(value, Mapping):
        return ["photo: must be a mapping (see lib/topcam/photo_config.py)"]
    bad: list[str] = [
        f"photo.{k}: unknown key; accepted: {', '.join(sorted(KEYS))}"
        for k in value
        if k not in KEYS
    ]
    bad.extend(
        f"photo.{key}: {value[key]!r} is not an integer"
        for key in INT_KEYS
        if key in value and not _is_int(value[key])
    )
    bad.extend(
        f"photo.{key}: {value[key]!r} is not a positive number"
        for key in NUM_KEYS
        if key in value and not (_is_num(value[key]) and value[key] > 0)
    )
    bad.extend(
        f"photo.{key}: must be text"
        for key in STR_KEYS
        if key in value and not (isinstance(value[key], str) and value[key].strip())
    )
    if "focus" in value:
        from topcam.focus import FocusError, parse_focus

        try:
            parse_focus(value["focus"])
        except FocusError as exc:
            bad.append(f"photo.{exc}")
    res = value.get("res")
    if isinstance(res, str):
        w, sep, h = res.lower().partition("x")
        if not (sep and w.isdigit() and h.isdigit()):
            bad.append(f"photo.res: {res!r} is not WIDTHxHEIGHT")
    if _is_int(value.get("frames")) and not 1 <= value["frames"] <= 32:
        bad.append("photo.frames: must be 1..32 (every frame is held in memory)")
    if "average" in value and value["average"] not in AVERAGES:
        bad.append(
            f"photo.average: {value['average']!r} is not one of {', '.join(AVERAGES)}"
        )
    if "led" in value and value["led"] not in (True, False, "on", "off"):
        bad.append(f"photo.led: {value['led']!r} is not on/off")
    ctrl = value.get("ctrl")
    if ctrl is not None and not (
        isinstance(ctrl, Mapping)
        and all(isinstance(k, str) and _is_int(v) for k, v in ctrl.items())
    ):
        bad.append(
            "photo.ctrl: must map control names to integers, e.g. {sharpness: 0}"
        )
    light = light_gcode(value.get("printer_light"))
    if light is not None and not (
        isinstance(light, Mapping)
        and set(light) == {"on", "off"}
        and all(isinstance(v, str) and v.strip() for v in light.values())
    ):
        bad.append("photo.printer_light: must be {on: <G-code>, off: <G-code>}")
    pose = value.get("pose")
    if pose is not None:
        if not isinstance(pose, Mapping) or not pose:
            bad.append("photo.pose: must be a mapping of x/y/z/speed")
        else:
            bad.extend(
                f"photo.pose.{k}: unknown key; accepted: x, y, z, speed"
                for k in pose
                if k not in POSE_KEYS
            )
            bad.extend(
                f"photo.pose.{k}: {v!r} is not a number"
                for k, v in pose.items()
                if k in POSE_KEYS and not _is_num(v)
            )
            if _is_num(pose.get("speed")) and pose["speed"] <= 0:
                bad.append("photo.pose.speed: must be > 0 (mm/min)")
            if not {"x", "y", "z"} & set(pose):
                bad.append("photo.pose: needs at least one of x, y, z")
    return bad


def merge(
    fleet_photo: Mapping[str, Any] | None, cli: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Effective settings and the origin of each (`cli`, `fleet`, `default`). `cli` holds only
    the options given on the command line (None values are ignored)."""
    settings: dict[str, Any] = {}
    origin: dict[str, str] = {}
    for layer, name in (
        (DEFAULTS, "default"),
        (fleet_photo or {}, "fleet"),
        (cli, "cli"),
    ):
        for key, value in layer.items():
            if value is None:
                continue
            settings[key] = dict(value) if isinstance(value, Mapping) else value
            origin[key] = name
    if "led" in settings and isinstance(settings["led"], str):
        settings["led"] = settings["led"] == "on"
    if "printer_light" in settings:
        settings["printer_light"] = light_gcode(settings["printer_light"])
    return settings, origin


def light_gcode(value: Any) -> Any:
    """`printer_light` with on/off keys. YAML 1.1 (PyYAML) reads an unquoted `on:`/`off:` key
    as true/false, so `{on: G1, off: G2}` arrives as {True: G1, False: G2}; map them back."""
    if isinstance(value, Mapping):
        names = {True: "on", False: "off"}
        return {
            names.get(k, k) if isinstance(k, bool) else k: v for k, v in value.items()
        }
    return value
