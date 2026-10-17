"""3d camera — the top camera: controls, locked full-resolution stills, a node service.

WHAT: `controls` lists/sets a camera's V4L2 controls, `still` takes N uncompressed frames with
  focus/exposure/gain/white balance locked and writes an averaged PNG + JSON read-back,
  `serve` runs the camera-node HTTP service (`topcam.node`) on this machine, `ls|show|rm`
  read/forget the nodes registered with 3d-api (`topcam.registry`).
  --camera: `usb` / `usb:/dev/videoN` (UVC camera on this machine), `http://host:port`,
  `host:port`, or a node NAME (3d-api record, else NAME.local).
WHY: calibration plates are read by machine (#273); the photo must be repeatable and lossless
  (issue #340).
INVARIANTS: stdlib-only at import time; numpy only inside `still` (lazy).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cli.registry import Command
from errors import InvalidArgument, ThreeDError, UsageError

USAGE = """3d camera controls [--camera C] [--set NAME=V[,NAME=V…]] [--json]
3d camera still [--camera C] [--res WxH] [--focus auto|N|sweep[:LO-HI[:STEP]]] [--focus-roi R] [--exposure E] [--gain G] [--wb K]
                [--ctrl NAME=V[,NAME=V…]] [--frames N] [--skip K] [--average mean|median|none]
                [--depth 8|16] [--led on|off] [--out FILE.png] [--json]
3d camera serve [--device DEV|auto] [--port 8080] [--name N] [--config FILE] [--no-stream] [--bind ADDR]
3d camera ls [--json] [--api URL] [--token T]
3d camera show NAME [--json] [--api URL] [--token T]
3d camera rm NAME [--api URL] [--token T]

  The top camera above the bed: a Raspberry Pi Zero W camera node (topcam) or the C920 on
  this PC. Stills are uncompressed YUYV (C920: up to 2304x1536) with the camera in manual
  focus/exposure/white balance, so two stills of the same plate differ only by noise.

Subcommands:
  controls  list the camera's controls (v4l2-ctl names, short alias, range, value); --set writes
  still     lock, grab N frames, average, write FILE.png + FILE.json (controls read back after capture)
  serve     run the camera-node HTTP service here (the same code the Pi runs; see docs)
  ls        camera nodes registered with 3d-api: name, online + age of last check-in, url, card, version
  show      one node's 3d-api record (url, preview stream, formats, LED, last address, token)
  rm        forget a node's 3d-api record (a running node re-registers within a minute)

Options:
  --camera C       usb | usb:/dev/videoN | http://host:port | host:port | NAME (default usb)
  --set N=V,…      controls: values to write, e.g. focus_auto=0,focus=40
  --res WxH        still resolution (default 2304x1536; the C920 also has 2304x1296, 1920x1080, …)
  --focus F        auto | N (manual, autofocus off; C920 0..250 step 5) | sweep[:LO-HI[:STEP]]
                   (software focus: max sharpness in --focus-roi; kept for the whole series)
  --focus-roi R    sweep region X0,Y0,X1,Y1 as frame fractions (default 0.25,0.25,0.75,0.75)
  --exposure E     manual exposure in 100 µs units (turns auto exposure off; C920 3..2047)
  --gain G         gain (C920 0..255)
  --wb K           white balance in kelvin (turns auto WB off; C920 2000..6500)
  --ctrl N=V,…     any other control by v4l2-ctl name or alias, applied last
  --frames N       frames to grab, 1..32 (default 1; a node serves up to 8 per request)
  --skip K         frames dropped after streaming starts (default 3)
  --average M      mean (default) | median | none (last frame)
  --depth 8|16     PNG bit depth (default 8; 16 keeps the averaged sub-LSB detail)
  --led on|off     node LED ring during the still (restored afterwards)
  --out FILE.png   output (default ./still-<camera>-<time>.png); FILE.json is written next to it
  --json           print JSON instead of text
  --device DEV     serve: V4L2 device (default auto = first UVC camera with YUYV)
  --port P         serve: HTTP port (default 8080); the ustreamer preview uses stream.port (8081)
  --name N         serve: node name (registers as this name with 3d-api when configured)
  --config FILE    serve: node JSON config (keys in docs/commands/camera.md)
  --no-stream      serve: no ustreamer preview
  --bind ADDR      serve: listen address (default 0.0.0.0)
  --api URL        ls/show/rm: 3d-api base URL (default $PRINTOPS_API or ~/.config/3d-cli/printops.yaml)
  --token T        ls/show/rm: 3d-api token, scope read (rm: write) (default $PRINTOPS_TOKEN)

Examples:
  3d camera controls --camera usb
  3d camera controls --camera usb --set focus_auto=0,focus=40
  3d camera still --camera usb --focus 40 --exposure 300 --gain 0 --wb 4500 --frames 4 --out plate.png
  3d camera still --camera usb --focus sweep:0-250:25 --focus-roi 0.2,0.2,0.8,0.8 --exposure 300
  3d camera still --camera topcam --res 2304x1296 --exposure 200 --ctrl sharpness=0 --led on
  3d camera still --camera http://192.168.0.50:8080 --frames 8 --average median --depth 16 --json
  3d camera serve --device /dev/video2 --port 8080 --name pc-c920 --no-stream
  3d camera ls
  3d camera ls --json --api http://192.168.0.39:8765 --token po_…
  3d camera show topcam --json
  3d camera rm old-topcam"""

PREFIX = "camera"
MAX_FRAMES = 32  # frames averaged per still/photo; all are held in memory


def _cmd(sub: str) -> str:
    return f"{PREFIX} {sub}"


def _int(raw: str, opt: str, sub: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise UsageError(
            f"{opt} must be an integer, got {raw!r}", command=_cmd(sub)
        ) from exc


def _pairs(raw: str, opt: str, sub: str) -> tuple[tuple[str, int], ...]:
    out: list[tuple[str, int]] = []
    for part in raw.split(","):
        name, sep, value = part.partition("=")
        if not sep or not name.strip():
            raise UsageError(
                f"{opt} wants NAME=VALUE pairs, got {part!r}", command=_cmd(sub)
            )
        out.append((name.strip(), _int(value.strip(), opt, sub)))
    return tuple(out)


def _res(raw: str, sub: str) -> tuple[int, int]:
    w, sep, h = raw.lower().partition("x")
    if not sep or not w.isdigit() or not h.isdigit():
        raise InvalidArgument(
            "--res", raw, accepted=["WIDTHxHEIGHT, e.g. 2304x1536"], command=_cmd(sub)
        )
    return int(w), int(h)


def _open(spec: str, sub: str) -> Any:
    from topcam.client import CameraError, open_camera

    try:
        return open_camera(spec)
    except CameraError as exc:
        raise ThreeDError(str(exc), command=_cmd(sub)) from exc


def _camera_errors(sub: str, fn: Callable[[], int]) -> int:
    from topcam.client import CameraError

    try:
        return fn()
    except CameraError as exc:
        raise ThreeDError(
            str(exc),
            command=_cmd(sub),
            remediation=[
                "Check the camera: `3d camera controls --camera C`; a node answers on /health.",
                "usb: is the camera plugged in and readable (`ls -l /dev/v4l/by-id/`, group video)?",
            ],
        ) from exc


def _parse(
    argv: list[str], sub: str, values: set[str], flags: set[str]
) -> tuple[dict[str, str], set[str]]:
    from printops.cli._common import parse

    p = parse(argv, sub, values=values, flags=flags, prefix=PREFIX)
    return p.opts, p.flags


def _controls(argv: list[str]) -> int:
    opts, flags = _parse(argv, "controls", {"--camera", "--set"}, {"--json"})
    cam = _open(opts.get("--camera", "usb"), "controls")

    def go() -> int:
        if "--set" in opts:
            result = cam.set_controls(dict(_pairs(opts["--set"], "--set", "controls")))
            if "--json" in flags:
                print(json.dumps({"set": result}, indent=2))
            else:
                for name, value in result.items():
                    print(f"  {name} = {value}")
            return 0
        data = cam.controls()
        if "--json" in flags:
            print(json.dumps(data, indent=2))
            return 0
        print(f"camera {cam.describe()}")
        for c in data["controls"]:
            alias = (
                f" ({c['alias']})" if c.get("alias") and c["alias"] != c["name"] else ""
            )
            state = " inactive" if c.get("inactive") else ""
            menu = ""
            if c.get("menu"):
                menu = "  " + ", ".join(f"{k}={v}" for k, v in c["menu"].items())
            print(
                f"  {c['name'] + alias:<44} {c['type']:<5} {c['min']}..{c['max']} "
                f"step {c['step']} default {c['default']}  value {c['value']}{state}{menu}"
            )
        for fourcc, sizes in data.get("formats", {}).items():
            print(f"  format {fourcc}: " + " ".join(f"{w}x{h}" for w, h in sizes))
        return 0

    return _camera_errors("controls", go)


def still_spec(
    opts: dict[str, str], sub: str, defaults: dict[str, Any] | None = None
) -> Any:
    """StillSpec from CLI options over `defaults` (keys res/focus/exposure/gain/wb/ctrl/frames/
    skip/led); shared with `3d calibrate photo`."""
    from topcam.client import StillSpec
    from topcam.node import lock_for

    d = dict(defaults or {})

    def pick(opt: str, key: str) -> Any:
        if opt in opts:
            return opts[opt]
        return d.get(key)

    width, height = _res(str(pick("--res", "res") or "2304x1536"), sub)
    nums: dict[str, int | None] = {}
    for key in ("exposure", "gain", "wb"):
        raw = pick(f"--{key}", key)
        nums[key] = None if raw is None else _int(str(raw), f"--{key}", sub)
    from topcam.focus import FocusError, parse_focus, parse_roi

    try:
        focus = parse_focus(pick("--focus", "focus"))
        roi_raw = pick("--focus-roi", "focus_roi")
        roi = parse_roi(str(roi_raw)) if roi_raw else None
    except FocusError as exc:
        raise UsageError(str(exc), command=_cmd(sub)) from exc
    extra_raw = pick("--ctrl", "ctrl")
    if isinstance(extra_raw, dict):
        extra = tuple((str(k), int(v)) for k, v in extra_raw.items())
    else:
        extra = _pairs(str(extra_raw), "--ctrl", sub) if extra_raw else ()
    frames = _int(str(pick("--frames", "frames") or 1), "--frames", sub)
    skip = _int(
        str(pick("--skip", "skip") if pick("--skip", "skip") is not None else 3),
        "--skip",
        sub,
    )
    if not 1 <= frames <= MAX_FRAMES or skip < 0:
        # every frame is held in memory (a 2304x1536 YUYV frame is ~7 MB, more as RGB)
        raise UsageError(f"--frames must be 1..{MAX_FRAMES} and --skip >= 0", command=_cmd(sub))
    led_raw = pick("--led", "led")
    if isinstance(led_raw, bool):
        led: bool | None = led_raw
    elif led_raw in (None, "keep"):
        led = None
    elif led_raw in ("on", "off"):
        led = led_raw == "on"
    else:
        raise InvalidArgument(
            "--led", str(led_raw), accepted=["on", "off"], command=_cmd(sub)
        )
    return StillSpec(
        width,
        height,
        frames,
        skip,
        lock_for(focus, nums["exposure"], nums["gain"], nums["wb"], extra),
        led,
        None,
        focus,
        roi,
    )


STILL_VALUES = {
    "--camera",
    "--focus-roi",
    "--res",
    "--focus",
    "--exposure",
    "--gain",
    "--wb",
    "--ctrl",
    "--frames",
    "--skip",
    "--average",
    "--depth",
    "--led",
    "--out",
}


def _still(argv: list[str]) -> int:
    opts, flags = _parse(argv, "still", STILL_VALUES, {"--json"})
    camera = opts.get("--camera", "usb")
    spec = still_spec(opts, "still")
    mode = opts.get("--average", "mean")
    from topcam.frames import AVERAGE_MODES

    if mode not in AVERAGE_MODES:
        raise InvalidArgument(
            "--average", mode, accepted=list(AVERAGE_MODES), command=_cmd("still")
        )
    depth = _int(opts.get("--depth", "8"), "--depth", "still")
    if depth not in (8, 16):
        raise InvalidArgument(
            "--depth", str(depth), accepted=["8", "16"], command=_cmd("still")
        )
    safe = "".join(ch if ch.isalnum() else "-" for ch in camera).strip("-")
    out = Path(
        opts.get("--out") or f"still-{safe}-{time.strftime('%Y%m%d-%H%M%S')}.png"
    )
    cam = _open(camera, "still")
    from topcam.client import CameraError

    from topcam.photo import capture_frames

    try:
        shot = capture_frames(cam, spec, spec.frames)  # a node serves 8 per request: chunked
    except CameraError as exc:
        raise ThreeDError(str(exc), command=_cmd("still")) from exc
    from topcam import frames as fr

    images = [fr.to_rgb(f, shot.width, shot.height) for f in shot.frames]
    image = fr.average(images, mode)
    out.parent.mkdir(parents=True, exist_ok=True)
    fr.write_png(out, image, depth)
    meta = {
        "schema": "topcam-still/1",
        "image": str(out),
        "width": shot.width,
        "height": shot.height,
        "format": "YUYV",
        "frames": len(shot.frames),
        "dropped": shot.dropped,
        "skip": spec.skip,
        "average": mode,
        "depth": depth,
        "temporal_noise": fr.noise(
            [fr.luma(f, shot.width, shot.height) for f in shot.frames]
        ),
        "requested": {
            "focus": spec.lock.focus,
            "exposure": spec.lock.exposure,
            "gain": spec.lock.gain,
            "wb": spec.lock.wb,
            "ctrl": dict(spec.lock.extra),
            "led": spec.led,
        },
        "controls": shot.controls,
        "focus": shot.focus,
        "source": shot.source,
        "seconds": round(shot.seconds, 3),
        "taken_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    out.with_suffix(".json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    if "--json" in flags:
        print(json.dumps(meta, indent=2))
    else:
        locked = ", ".join(
            f"{k}={shot.controls[k]}"
            for k in sorted(shot.controls)
            if any(w in k for w in ("focus", "exposure", "gain", "white_balance"))
        )
        print(
            f"{out}: {shot.width}x{shot.height} YUYV, {len(shot.frames)} frame(s) {mode}, "
            f"{depth}-bit; {locked}"
        )
    return 0


def _serve(argv: list[str]) -> int:
    opts, flags = _parse(
        argv,
        "serve",
        {"--device", "--port", "--name", "--config", "--bind"},
        {"--no-stream"},
    )
    from topcam import node

    args: list[str] = []
    for opt in ("--device", "--port", "--name", "--config", "--bind"):
        if opt in opts:
            args += [opt, opts[opt]]
    if "--no-stream" in flags:
        args.append("--no-stream")
    return node.main(args)


def _ls(argv: list[str]) -> int:
    from topcam import registry

    return registry.ls(argv)


def _show(argv: list[str]) -> int:
    from topcam import registry

    return registry.show(argv)


def _rm(argv: list[str]) -> int:
    from topcam import registry

    return registry.rm(argv)


SUBCOMMANDS: dict[str, Callable[[list[str]], int]] = {
    "controls": _controls,
    "still": _still,
    "serve": _serve,
    "ls": _ls,
    "show": _show,
    "rm": _rm,
}


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    sub, rest = argv[0], argv[1:]
    if sub in ("-h", "--help", "help") or any(a in ("-h", "--help") for a in rest):
        print(USAGE)
        return 0
    fn = SUBCOMMANDS.get(sub)
    if fn is None:
        raise UsageError(
            f"unknown subcommand {sub!r}",
            command=PREFIX,
            remediation=[f"One of: {', '.join(SUBCOMMANDS)} — see `3d camera --help`."],
        )
    return fn(rest)


COMMAND = Command(
    name="camera",
    group="PRINTING",
    summary="top camera: controls, locked full-res stills, camera-node service and registry",
    usage="camera <controls|still|serve|ls|show|rm> [options]",
    run=run,
)
