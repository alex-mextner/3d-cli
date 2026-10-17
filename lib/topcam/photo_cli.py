"""`3d calibrate photo` and `3d calibrate rectify` — the top-camera photo mode (#342).

WHAT: photo = settings (CLI > fleet `photo:` > defaults) → optional printer pose + light via
  Moonraker → locked capture of N frames → averaged 16-bit PNG → ArUco rectification + external
  scale (lib/calib_rectify.py) → `calib-photo/1` JSON; optional --save into fleet and --session
  upload. rectify = the rectification step alone, for any photo (phone, Telegram, old stills).
INVARIANTS: stdlib at import (lib/commands/calibrate.py dispatches here); the printer is only
  touched with --move/--printer-light, and a move needs --yes.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from errors import (
    GateFailure,
    InvalidArgument,
    MissingDependency,
    ThreeDError,
    UsageError,
)

PREFIX = "calibrate"

USAGE = """3d calibrate photo --printer P [--camera C] [--res WxH] [--focus auto|N|sweep] [--focus-roi R] [--exposure E] [--gain G] [--wb K]
                   [--ctrl N=V,…] [--frames N] [--skip K] [--average mean|median|none] [--led on|off]
                   [--settle-ms MS] [--printer-light on|off] [--pose X,Y,Z[,F]] [--move --yes] [--home]
                   [--manifest M | --fiducials ID:X,Y;… [--fiducial-size MM]] [--px-per-mm N]
                   [--area X0,Y0,X1,Y1] [--scale-ref FILE] [--camera-height-mm H] [--no-rectify]
                   [--out DIR] [--save] [--session ID] [--json]
3d calibrate rectify PHOTO (--manifest M | --fiducials ID:X,Y;…) [--fiducial-size MM] [--px-per-mm N]
                   [--area X0,Y0,X1,Y1] [--scale-ref FILE] [--camera-height-mm H] [--out DIR] [--json]"""

HELP = """  photo     top-camera photo of the plate: settings from fleet/printers.yaml `photo:` (CLI wins),
            optional pose + light, N locked frames averaged, ArUco rectification to bed mm, external
            scale; writes DIR/photo.png (16-bit), DIR/rectified.png, DIR/detections.png,
            DIR/calib-photo.json. Exit 1 when fewer than 3 fiducials (or the scale card) are found.
  rectify   rectification + scale only, for an existing photo"""

OPTIONS = """  photo/rectify:
  --camera C         usb | usb:/dev/videoN | http://host:port | host:port | node name (default: fleet, else topcam)
  --res WxH          still resolution (default 2304x1536)
  --focus auto|N|sweep[:LO-HI[:STEP]]   autofocus, manual position, or software focus sweep
  --focus-roi R      sweep region X0,Y0,X1,Y1 as frame fractions (default the centre half)
  --exposure/--gain/--wb N   manual lock (exposure in 100 µs, wb in K); autos off
  --ctrl N=V,…       other controls by v4l2-ctl name or alias
  --frames N         frames averaged (default 4; > 8 = several node requests)
  --skip K           frames dropped after streaming starts (default 3)
  --average M        mean | median | none
  --led on|off       camera-node LED ring during the photo
  --settle-ms MS     wait after the LED switches
  --printer-light on|off   run the fleet `printer_light.on` G-code before (and `.off` after) the photo
  --pose X,Y,Z[,F]   photo pose in mm (F mm/min); default fleet `pose`; used only with --move
  --move             move to the pose first (needs --yes; refused while printing or not homed)
  --home             home (G28) first when the printer is not homed
  --yes              confirm the move
  --manifest M       plate manifest (calib-plate/1) with the fiducials
  --fiducials L      fiducials as ID:X,Y;… in bed mm (ArUco DICT_4X4_50), e.g. 0:12,12;1:208,12;2:208,208;3:12,208
  --fiducial-size MM marker size (default 10)
  --px-per-mm N      rectified resolution (default fleet, else 10)
  --area X0,Y0,X1,Y1 rectified bed area in mm (default the fiducials' bounding box + 10 mm)
  --scale-ref FILE   external scale reference (scale-ref/1 JSON: markers at measured true positions)
  --camera-height-mm H   lens height above the bed (corrects the card's thickness parallax)
  --no-rectify       only capture
  --out DIR          output directory (default ./calib-photo-<printer>-<time>)
  --save             store the effective settings as the printer's `photo:` in fleet/printers.yaml
  --session ID       upload the photo to calibration session ID (3d-api, #277)"""

EXAMPLES = """  3d calibrate photo --printer k1se --camera usb --fiducials '0:12,12;1:208,12;2:208,208;3:12,208' --out shot/
  3d calibrate photo --printer k1se --camera usb --focus sweep --focus-roi 0.1,0.1,0.9,0.9 --no-rectify
  3d calibrate photo --printer u1 --camera topcam --exposure 250 --wb 4500 --frames 8 --average median --led on
  3d calibrate photo --printer k1se --move --yes --pose 110,220,150,6000 --printer-light on --manifest plate1.json
  3d calibrate photo --printer k1se --manifest plate1.json --scale-ref fleet/scale-refs/steel-card.json --camera-height-mm 300
  3d calibrate photo --printer u1 --focus 30 --exposure 300 --gain 0 --wb 4500 --settle-ms 500 --save --no-rectify
  3d calibrate photo --printer u1 --manifest plate1.json --session 17 --json
  3d calibrate rectify phone.jpg --manifest plate1.json --px-per-mm 8 --area 0,0,220,220 --out rect/"""

PHOTO_VALUES = {
    "--printer",
    "--camera",
    "--res",
    "--focus",
    "--focus-roi",
    "--exposure",
    "--gain",
    "--wb",
    "--ctrl",
    "--frames",
    "--skip",
    "--average",
    "--led",
    "--settle-ms",
    "--printer-light",
    "--pose",
    "--manifest",
    "--fiducials",
    "--fiducial-size",
    "--px-per-mm",
    "--area",
    "--scale-ref",
    "--camera-height-mm",
    "--out",
    "--session",
    "--api",
    "--token",
}
PHOTO_FLAGS = {"--move", "--yes", "--home", "--no-rectify", "--save", "--json"}
RECTIFY_VALUES = {
    "--manifest",
    "--fiducials",
    "--fiducial-size",
    "--px-per-mm",
    "--area",
    "--scale-ref",
    "--camera-height-mm",
    "--out",
}


def _cmd(sub: str) -> str:
    return f"{PREFIX} {sub}"


def _float(raw: str, opt: str, sub: str, positive: bool = False) -> float:
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or (positive and value <= 0):
        raise UsageError(
            f"{opt} must be a {'positive ' if positive else ''}number, got {raw!r}", command=_cmd(sub)
        )
    return value


def _pose(raw: str, sub: str) -> dict[str, float]:
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) not in (3, 4):
        raise InvalidArgument(
            "--pose", raw, accepted=["X,Y,Z or X,Y,Z,F (mm, mm/min)"], command=_cmd(sub)
        )
    keys = ("x", "y", "z", "speed")
    return {k: _float(v, "--pose", sub) for k, v in zip(keys, parts, strict=False)}


def _rectify_tool(
    image: Path, out_dir: Path, opts: dict[str, Any], sub: str
) -> tuple[int, dict[str, Any]]:
    from cli.pyrun import tool_argv

    args = [str(image), "--out-dir", str(out_dir)]
    for key in (
        "manifest",
        "fiducials",
        "fiducial_size",
        "px_per_mm",
        "area",
        "scale_ref",
        "camera_height_mm",
    ):
        value = opts.get(key)
        if value is not None:
            args += ["--" + key.replace("_", "-"), str(value)]
    argv = tool_argv("opencv-python-headless,numpy", "calib_rectify.py", args)
    proc = subprocess.run(argv, capture_output=True, text=True)
    try:
        result: dict[str, Any] = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        if "No module named 'cv2'" in proc.stderr or "aruco" in proc.stderr:
            raise MissingDependency(
                "opencv-python-headless (>= 4.7, with aruco)",
                install="uv sync --extra vision",
                degrades="calibrate photo/rectify cannot rectify",
                command=_cmd(sub),
            ) from exc
        raise ThreeDError(
            f"rectification failed: {proc.stderr.strip()[-600:]}", command=_cmd(sub)
        ) from exc
    if proc.returncode == 2:
        raise UsageError(f"rectify: {result.get('reason')}", command=_cmd(sub))
    return proc.returncode, result


def _fiducial_opts(opts: dict[str, str], sub: str) -> dict[str, Any]:
    if "--manifest" in opts and "--fiducials" in opts:
        raise UsageError("give --manifest or --fiducials, not both", command=_cmd(sub))
    out: dict[str, Any] = {
        "manifest": opts.get("--manifest"),
        "fiducials": opts.get("--fiducials"),
        "fiducial_size": opts.get("--fiducial-size"),
        "area": opts.get("--area"),
        "scale_ref": opts.get("--scale-ref"),
        "camera_height_mm": opts.get("--camera-height-mm"),
        "px_per_mm": opts.get("--px-per-mm"),
    }
    for key in ("fiducial_size", "px_per_mm", "camera_height_mm"):
        if out[key] is not None:
            _float(str(out[key]), "--" + key.replace("_", "-"), sub, positive=True)
    # checked here, before any printer move or capture, so a typo never costs a photo run
    if out["area"] is not None:
        parts = str(out["area"]).split(",")
        vals = [_float(v, "--area", sub) for v in parts] if len(parts) == 4 else []
        if not vals or vals[2] <= vals[0] or vals[3] <= vals[1]:
            raise UsageError(f"--area {out['area']!r}: want X0,Y0,X1,Y1 in mm with X1>X0, Y1>Y0",
                             command=_cmd(sub))
    num = r"-?(?:\d+(?:\.\d*)?|\.\d+)"
    one = rf"\s*\d+\s*:\s*{num}\s*,\s*{num}\s*"
    if out["fiducials"] is not None and not re.fullmatch(rf"{one}(?:;{one})*;?\s*", str(out["fiducials"])):
        raise UsageError(f"--fiducials {out['fiducials']!r}: want ID:X,Y;ID:X,Y;... in mm", command=_cmd(sub))
    if out["manifest"] and not Path(out["manifest"]).is_file():
        raise UsageError(
            f"--manifest {out['manifest']}: no such file", command=_cmd(sub)
        )
    if out["manifest"]:
        try:
            man = json.loads(Path(out["manifest"]).read_text(encoding="utf-8"))
            fids = man.get("fiducials") if isinstance(man, dict) else None
            if not isinstance(fids, list) or not fids or not all(
                    isinstance(f, dict) and {"id", "center_mm", "size_mm"} <= set(f) for f in fids):
                raise ValueError("fiducials must be a non-empty list of {id, center_mm, size_mm}")
        except ValueError as exc:
            raise UsageError(f"--manifest {out['manifest']}: {exc}", command=_cmd(sub)) from exc
    if out["scale_ref"] and not Path(out["scale_ref"]).is_file():
        raise UsageError(
            f"--scale-ref {out['scale_ref']}: no such file", command=_cmd(sub)
        )
    return out


def _print_rectify(result: dict[str, Any]) -> None:
    if result.get("status") != "ok":
        print(f"rectify: FAILED — {result.get('reason')}")
        return
    r = result["residual_mm"]
    print(
        f"rectify: {result['fiducials_found']} fiducials {result['ids']}, residual rms {r['rms']:.3f} mm "
        f"max {r['max']:.3f} mm → {result['rectified']} ({result['px_per_mm']:g} px/mm)"
    )
    scale = result.get("scale")
    if scale and scale.get("status") == "ok":
        t = scale["true_mm_per_bed_mm"]
        print(
            f"scale: true mm per bed mm x {t['x']:.5f}, y {t['y']:.5f} "
            f"({scale['printer_scale_error_pct']['x']:+.3f}% / {scale['printer_scale_error_pct']['y']:+.3f}%), "
            f"card fit rms {scale['fit_residual_mm']['rms']:.3f} mm"
            + ("" if scale["parallax_corrected"] else "  WARNING: " + scale["warning"])
        )


def rectify(argv: list[str]) -> int:
    from printops.cli._common import parse

    p = parse(
        argv, "rectify", values=RECTIFY_VALUES, flags={"--json"}, n_pos=1, prefix=PREFIX
    )
    image = Path(p.pos[0])
    if not image.is_file():
        raise UsageError(f"{image}: no such file", command=_cmd("rectify"))
    fid = _fiducial_opts(p.opts, "rectify")
    if not fid["manifest"] and not fid["fiducials"]:
        raise UsageError(
            "rectify needs --manifest or --fiducials", command=_cmd("rectify")
        )
    out_dir = Path(p.opts.get("--out") or f"{image.stem}-rectified")
    code, result = _rectify_tool(image, out_dir, fid, "rectify")
    if "--json" in p.flags:
        print(json.dumps(result, indent=2))
    else:
        _print_rectify(result)
    if code:
        raise GateFailure(
            f"rectify: {result.get('reason')}", command=_cmd("rectify"), silent=True
        )
    return 0


def photo(argv: list[str]) -> int:  # noqa: C901 — one linear workflow, each step guarded
    from printops.cli._common import parse

    from commands.camera import still_spec
    from topcam import photo_config
    from topcam.client import CameraError, open_camera
    from topcam.photo import (
        PhotoError,
        PrinterLink,
        capture_frames,
        now_iso,
        save_photo_settings,
        write_average,
    )

    sub = "photo"
    p = parse(argv, sub, values=PHOTO_VALUES, flags=PHOTO_FLAGS, prefix=PREFIX)
    name = p.opts.get("--printer")
    if not name:
        raise UsageError(
            "--printer is required (its fleet `photo:` block holds the defaults)",
            command=_cmd(sub),
        )
    from printops.fleet import load_fleet

    fleet = load_fleet()
    entry = fleet.printers.get(name) if fleet is not None else None
    if fleet is not None and entry is None:
        raise InvalidArgument(
            "--printer", name, accepted=sorted(fleet.printers), command=_cmd(sub)
        )
    fleet_photo = dict(entry.photo or {}) if entry is not None else {}
    if fleet_photo.get("scale_ref") and entry is not None:
        fleet_photo["scale_ref"] = str(
            (entry.source.parent / fleet_photo["scale_ref"]).resolve()
        )

    cli: dict[str, Any] = {
        "camera": p.opts.get("--camera"),
        "res": p.opts.get("--res"),
        "average": p.opts.get("--average"),
        "led": p.opts.get("--led"),
        "focus": p.opts.get("--focus"),
        "focus_roi": p.opts.get("--focus-roi"),
        "scale_ref": p.opts.get("--scale-ref"),
    }
    for key in ("exposure", "gain", "wb", "frames", "skip", "settle_ms"):
        raw = p.opts.get("--" + key.replace("_", "-"))
        if raw is not None:
            try:
                cli[key] = int(raw)
            except ValueError as exc:
                raise UsageError(
                    f"--{key.replace('_', '-')} must be an integer, got {raw!r}",
                    command=_cmd(sub),
                ) from exc
    for key in ("px_per_mm", "camera_height_mm"):
        raw = p.opts.get("--" + key.replace("_", "-"))
        if raw is not None:
            cli[key] = _float(raw, "--" + key.replace("_", "-"), sub, positive=True)
    if "--ctrl" in p.opts:
        from commands.camera import _pairs

        cli["ctrl"] = dict(_pairs(p.opts["--ctrl"], "--ctrl", sub))
    if "--pose" in p.opts:
        cli["pose"] = _pose(p.opts["--pose"], sub)
    if cli["led"] not in (None, "on", "off"):
        raise InvalidArgument(
            "--led", str(cli["led"]), accepted=["on", "off"], command=_cmd(sub)
        )
    settings, origin = photo_config.merge(fleet_photo, cli)
    if settings.get("scale_ref") and not Path(str(settings["scale_ref"])).is_file():
        raise UsageError(f"scale_ref {settings['scale_ref']} ({origin.get('scale_ref')}): no such file",
                         command=_cmd(sub))
    problems = photo_config.validate(settings)
    if problems:
        raise UsageError(
            "bad photo settings: " + "; ".join(problems), command=_cmd(sub)
        )
    if str(settings.get("camera") or "").startswith("usb") and origin.get("led") == "fleet":
        settings["led"] = None  # the fleet's node LED does not apply to a USB camera (--save keeps it)
    if settings["average"] not in photo_config.AVERAGES:
        raise InvalidArgument(
            "--average",
            settings["average"],
            accepted=list(photo_config.AVERAGES),
            command=_cmd(sub),
        )
    light = p.opts.get("--printer-light")
    if light not in (None, "on", "off"):
        raise InvalidArgument(
            "--printer-light", str(light), accepted=["on", "off"], command=_cmd(sub)
        )
    move = "--move" in p.flags
    if move and "--yes" not in p.flags:
        raise UsageError(
            "--move drives the printer to the photo pose: add --yes to confirm",
            command=_cmd(sub),
            remediation=["Check the bed is clear, then rerun with --move --yes."],
        )
    if move and "pose" not in settings:
        raise UsageError(
            f"--move needs a pose: --pose X,Y,Z[,F] or `photo.pose` for {name} in the fleet file",
            command=_cmd(sub),
        )
    if light and "printer_light" not in settings:
        raise UsageError(
            f"--printer-light needs `photo.printer_light: {{on: …, off: …}}` for {name} in the "
            "fleet file (the light G-code differs per printer)",
            command=_cmd(sub),
        )
    fid = _fiducial_opts(p.opts, sub)
    fid["scale_ref"] = settings.get("scale_ref")
    fid["camera_height_mm"] = settings.get("camera_height_mm")
    fid["px_per_mm"] = settings.get("px_per_mm")
    rectify_wanted = "--no-rectify" not in p.flags
    if rectify_wanted and not fid["manifest"] and not fid["fiducials"]:
        raise UsageError(
            "photo needs --manifest or --fiducials to rectify (or --no-rectify)",
            command=_cmd(sub),
        )

    stamp = now_iso()
    out_dir = Path(
        p.opts.get("--out") or f"calib-photo-{name}-{stamp[:19].replace(':', '')}"
    )
    spec = still_spec(
        {},
        sub,
        {
            k: settings.get(k)
            for k in (
                "res",
                "focus",
                "focus_roi",
                "exposure",
                "gain",
                "wb",
                "ctrl",
                "frames",
                "skip",
                "led",
            )
        },
    )
    spec = replace(spec, settle_ms=settings.get("settle_ms"))  # keeps focus + focus_roi

    link = None
    if move or light:
        from printer_ops import make_adapter
        from printer_ops.devices import get_device
        from printer_ops.moonraker import MoonrakerAdapter

        device = get_device(name)
        if device.access != "full":
            raise ThreeDError(
                f"{name} has access {device.access}: 3d may not move it or switch its light",
                command=_cmd(sub),
            )
        adapter = make_adapter(device)
        if not isinstance(adapter, MoonrakerAdapter):
            raise ThreeDError(
                f"{name} uses the {device.adapter} adapter: pose and light need Moonraker",
                command=_cmd(sub),
            )
        link = PrinterLink(name, adapter, device.axis_limits)

    record: dict[str, Any] = {
        "schema": "calib-photo/1",
        "printer": name,
        "taken_at": stamp,
        "settings": settings,
        "origin": origin,
    }
    try:
        from topcam.registry import lookup_url

        api, token = p.opts.get("--api"), p.opts.get("--token")
        cam = open_camera(str(settings["camera"]), lambda n: lookup_url(n, api=api, token=token))
    except CameraError as exc:
        raise ThreeDError(str(exc), command=_cmd(sub)) from exc
    light_on = False
    try:
        if link is not None:
            if move:
                record["pose"] = {
                    "moved": True,
                    **link.move(settings["pose"], "--home" in p.flags),
                }
            if light == "on":
                link.light(settings["printer_light"]["on"])
                light_on = True
            elif light == "off":
                link.light(settings["printer_light"]["off"])
            record["printer_light"] = light
        if not move:
            record["pose"] = {"moved": False, "configured": settings.get("pose")}
        shot = capture_frames(cam, spec, spec.frames)
    except PhotoError as exc:
        raise ThreeDError(str(exc), command=_cmd(sub)) from exc
    except CameraError as exc:
        raise ThreeDError(
            f"camera {settings['camera']}: {exc}", command=_cmd(sub)
        ) from exc
    finally:
        if light_on and link is not None:
            try:
                link.light_off(settings["printer_light"]["off"])
            except Exception as exc:  # never mask the capture's own error; say what is left on
                print(f"warning: printer light may still be on: {exc}", file=sys.stderr)
    record["camera"] = {
        "source": shot.source,
        "controls": shot.controls,
        "dropped": shot.dropped,
        "seconds": round(shot.seconds, 3),
        "requested": {
            "focus": spec.lock.focus,
            "exposure": spec.lock.exposure,
            "gain": spec.lock.gain,
            "wb": spec.lock.wb,
            "ctrl": dict(spec.lock.extra),
            "led": spec.led,
        },
    }
    if link is not None:
        record["printer_gcode"] = link.log
    record["photo"] = write_average(shot, settings["average"], out_dir / "photo.png")

    code = 0
    (out_dir / "calib-photo.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    if rectify_wanted:
        code, record["rectify"] = _rectify_tool(
            out_dir / "photo.png", out_dir, fid, sub
        )
    record["status"] = "ok" if code == 0 else "failed"
    record_path = out_dir / "calib-photo.json"
    # written now and again below: a failing --save or --session never loses the photo's record
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    if "--save" in p.flags:
        if entry is None:
            raise UsageError(
                "--save needs the fleet file (THREED_FLEET is off)", command=_cmd(sub)
            )
        keep = {k: v for k, v in settings.items() if origin.get(k) != "default"}
        if origin.get("led") == "fleet" and entry.photo and "led" in entry.photo:
            keep["led"] = entry.photo["led"]  # a USB run cleared it for this shot only
        if "scale_ref" in keep and p.opts.get("--scale-ref") is None and entry.photo:
            keep["scale_ref"] = entry.photo.get("scale_ref")
        elif keep.get("scale_ref"):
            # the fleet resolves scale_ref against its own directory: store it relative to that
            ref = Path(str(keep["scale_ref"])).resolve()
            keep["scale_ref"] = os.path.relpath(ref, entry.source.parent.resolve())
        try:
            save_photo_settings(entry.source, name, keep)
        except PhotoError as exc:
            raise ThreeDError(str(exc), command=_cmd(sub)) from exc
        record["saved_to"] = str(entry.source)
    if "--session" in p.opts:
        from printops.cli._common import api_errors, client

        from topcam.photo import upload_to_session

        with api_errors(sub, prefix=PREFIX):
            record["session"] = upload_to_session(
                client(p, sub, prefix=PREFIX),
                p.opts["--session"],
                out_dir / "photo.png",
                record,
            )
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    if "--json" in p.flags:
        print(json.dumps(record, indent=2))
    else:
        ph = record["photo"]
        noise = ph["temporal_noise"]
        print(
            f"photo: {ph['path']} {ph['width']}x{ph['height']}, {ph['frames']} frame(s) {ph['average']}"
            + (f", temporal noise {noise:.2f}" if noise is not None else "")
            + (
                f", {ph['clipped_fraction'] * 100:.1f}% clipped"
                if ph["clipped_fraction"]
                else ""
            )
        )
        if "rectify" in record:
            _print_rectify(record["rectify"])
        if "saved_to" in record:
            print(f"settings saved to {record['saved_to']} ({name}.photo)")
        print(f"record: {out_dir / 'calib-photo.json'}")
    if code:
        raise GateFailure(
            f"photo: {record['rectify'].get('reason')}", command=_cmd(sub), silent=True
        )
    return 0
