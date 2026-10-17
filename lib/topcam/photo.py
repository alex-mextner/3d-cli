"""topcam.photo — one calibration photo from the top camera, taken the same way every time.

WHAT: `printer pose` (optional, explicit): check the printer is idle and homed, move to the
  photo pose through Moonraker (`G90`, Z first when the bed goes down, `M400`); printer light
  on; camera lock; N frames (several node requests when N > 8) averaged into a 16-bit PNG;
  printer light restored; `save_photo_settings` writes the effective settings back into the
  printer's `photo:` block in fleet/printers.yaml; `upload_to_session` sends the photo to a
  calibration session (`POST /v1/calibration/sessions/{id}/photos`, plan §8.3.6, #277).
WHY: issue #342 — rectification and the vision readout (#273) need a photo whose pose, light
  and exposure do not change between runs.
INVARIANTS: nothing moves without `move=True` (the CLI requires --move --yes); a printing or
  paused printer is refused before any G-code is sent; numpy and pyyaml are imported lazily.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from topcam.client import Camera, CameraError, Capture, StillSpec
from topcam.focus import FocusMode

NODE_MAX_FRAMES = 8
IDLE_STATES = ("standby", "complete", "cancelled")  # Klipper print_stats.state


class PhotoError(Exception):
    """The photo was refused or failed; nothing further was sent to the printer."""


def capture_frames(
    cam: Camera, spec: StillSpec, total: int, per_request: int | None = None
) -> Capture:
    """`total` frames in requests of at most `per_request` (default: the node's `max_frames`
    from /health, else 8); the lock stays the same, so the frames are interchangeable.
    Read-back is from the last request."""
    if per_request is None and total > 1:
        try:
            limit = cam.health().get("max_frames")
        except (CameraError, AttributeError):  # unreachable /health or a camera without one
            limit = None
        per_request = limit if isinstance(limit, int) and limit >= 1 else NODE_MAX_FRAMES
    per_request = per_request or NODE_MAX_FRAMES
    frames: list[bytes] = []
    dropped = 0
    seconds = 0.0
    last: Capture | None = None
    first_focus: dict[str, Any] = {}
    chunk = spec
    while len(frames) < total:
        n = min(per_request, total - len(frames))
        last = cam.still(replace(chunk, frames=n))
        if not first_focus:
            first_focus = last.focus
            best = (last.focus.get("sweep") or {}).get("best")
            if spec.focus.mode == "sweep" and isinstance(best, int):
                # later chunks keep the focus the sweep chose: the frames stay interchangeable
                chunk = replace(spec, focus=FocusMode("manual", best), lock=replace(spec.lock, focus=best))
        frames.extend(last.frames)
        dropped += last.dropped
        seconds += last.seconds
    assert last is not None
    return Capture(
        last.width, last.height, frames, last.controls, dropped, seconds, last.source,
        first_focus,
    )


def pose_script(pose: Mapping[str, float], current: list[float] | None) -> str:
    """G-code for the photo pose. Z moves first when it goes up (bed away from the nozzle on
    a bed-in-Z printer), last when it comes down, so XY travel never happens closer to the
    bed than either end point."""
    speed = float(pose.get("speed", 3000))
    z_speed = min(speed, 600.0)
    xy = " ".join(f"{k.upper()}{float(pose[k]):.3f}" for k in ("x", "y") if k in pose)
    lines = ["G90"]
    z_line = f"G1 Z{float(pose['z']):.3f} F{z_speed:.0f}" if "z" in pose else None
    z_up = z_line is not None and (current is None or float(pose["z"]) >= current[2])
    if z_line and z_up:
        lines.append(z_line)
    if xy:
        lines.append(f"G1 {xy} F{speed:.0f}")
    if z_line and not z_up:
        lines.append(z_line)
    lines.append("M400")
    return "\n".join(lines)


@dataclass
class PrinterLink:
    """The printer side of a photo: state check, pose move, light. Moonraker only."""

    name: str
    adapter: Any  # printer_ops.moonraker.MoonrakerAdapter
    axis_limits: Any = None  # ((xmin, xmax), (ymin, ymax)) or None
    log: list[str] = field(default_factory=list)

    def state(self) -> dict[str, Any]:
        status = self.adapter.query(
            "print_stats=state&toolhead=homed_axes,position,axis_minimum,axis_maximum"
        )
        return {
            "state": status.get("print_stats", {}).get("state"),
            "homed_axes": status.get("toolhead", {}).get("homed_axes", ""),
            "position": status.get("toolhead", {}).get("position"),
            "axis_minimum": status.get("toolhead", {}).get("axis_minimum"),
            "axis_maximum": status.get("toolhead", {}).get("axis_maximum"),
        }

    def check_idle(self) -> dict[str, Any]:
        st = self.state()
        if st["state"] not in IDLE_STATES:  # fail closed: printing, paused, error or unreadable
            raise PhotoError(
                f"{self.name} is {st['state']}: pose moves and light changes need an idle printer "
                f"({', '.join(IDLE_STATES)})"
            )
        return st

    def move(self, pose: Mapping[str, float], home: bool) -> dict[str, Any]:
        st = self.check_idle()
        homed = str(st["homed_axes"] or "").lower()
        if not all(axis in homed for axis in "xyz"):
            if not home:
                raise PhotoError(
                    f"{self.name} is not homed (homed_axes={homed or 'none'}); home it first or pass --home"
                )
            self._gcode("G28")
            st = self.state()
            homed = str(st["homed_axes"] or "").lower()
            if not all(axis in homed for axis in "xyz"):
                raise PhotoError(f"{self.name} is still not homed after G28 (homed_axes={homed or 'none'})")
        self._check_bounds(pose, st)
        if "G28" in self.log:
            st = self.check_idle()  # homing took a while: re-read right before the move
        if "z" in pose and not st.get("position"):
            raise PhotoError(f"{self.name}: toolhead position unknown, so the safe Z order cannot be chosen")
        script = pose_script(pose, st.get("position"))
        self._gcode(script)
        return {
            "script": script,
            "from": st.get("position"),
            "homed_first": bool(home and "G28" in self.log),
        }

    def _check_bounds(self, pose: Mapping[str, float], st: Mapping[str, Any]) -> None:
        lo, hi = st.get("axis_minimum"), st.get("axis_maximum")
        limits = (
            dict(zip("xyz", zip(lo, hi, strict=False), strict=False))
            if lo and hi
            else {}
        )
        if self.axis_limits is not None:
            limits["x"], limits["y"] = (
                tuple(self.axis_limits[0]),
                tuple(self.axis_limits[1]),
            )
        unknown = [axis for axis in ("x", "y", "z") if axis in pose and axis not in limits]
        if unknown:
            raise PhotoError(f"{self.name}: travel range of {', '.join(unknown)} unknown, so the pose cannot be checked")
        for axis in ("x", "y", "z"):
            if axis in pose:
                a, b = float(limits[axis][0]), float(limits[axis][1])
                if not a <= float(pose[axis]) <= b:
                    raise PhotoError(
                        f"pose {axis}={pose[axis]} is outside the printer's {axis} range {a}..{b}"
                    )

    def light(self, gcode: str) -> None:
        self.check_idle()
        self._gcode(gcode)

    def light_off(self, gcode: str) -> None:
        """No idle check: switching the light off must work even if a job started meanwhile."""
        self._gcode(gcode)

    def _gcode(self, script: str) -> None:
        self.log.append(script)
        self.adapter.run_gcode(script)


def write_average(capture: Capture, mode: str, path: Path) -> dict[str, Any]:
    """Average the frames to a 16-bit RGB PNG; returns the image statistics for the JSON."""
    from topcam import frames as fr

    images = [fr.to_rgb(f, capture.width, capture.height) for f in capture.frames]
    image = fr.average(images, mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    fr.write_png(path, image, depth=16)
    lumas = [fr.luma(f, capture.width, capture.height) for f in capture.frames]
    clipped = float((image >= 254.5).any(axis=-1).mean())
    return {
        "path": str(path),
        "width": capture.width,
        "height": capture.height,
        "frames": len(capture.frames),
        "average": mode,
        "temporal_noise": fr.noise(lumas),
        "mean_level": float(image.mean()),
        "clipped_fraction": clipped,
    }


def save_photo_settings(
    fleet_file: Path, printer: str, settings: Mapping[str, Any]
) -> None:
    """Replace (or add) `photo:` under `printer:` in fleet/printers.yaml, keeping every other
    line and comment as it is, then re-validate the whole file."""
    import fcntl

    with open(fleet_file.with_name(f".{fleet_file.name}.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # read-modify-write: concurrent --save must not lose edits
        candidate = _photo_block_text(fleet_file, printer, settings)
        tmp = fleet_file.with_name(f".{fleet_file.name}.{os.getpid()}.tmp")
        tmp.write_text(candidate, encoding="utf-8")
        tmp.replace(fleet_file)


def _photo_block_text(fleet_file: Path, printer: str, settings: Mapping[str, Any]) -> str:
    import yaml

    from printops.fleet import validate

    text = fleet_file.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        text += "\n"  # the block splice below works on whole lines
    lines = text.splitlines(keepends=True)
    start = next(
        (
            i
            for i, ln in enumerate(lines)
            if re.match(rf"^{re.escape(printer)}:\s*(#.*)?$", ln)
        ),
        None,
    )
    if start is None:
        raise PhotoError(f"{fleet_file}: no printer {printer!r}")
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"^\S", lines[i])),
        len(lines),
    )
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    body = yaml.safe_dump(
        {"photo": dict(settings)}, sort_keys=False, default_flow_style=None, width=100
    )
    block = ["  " + ln + "\n" for ln in body.rstrip("\n").split("\n")]
    p0 = next(
        (i for i in range(start + 1, end) if re.match(r"^  photo:", lines[i])), None
    )
    if p0 is None:
        new = lines[:end] + block + lines[end:]
    else:
        p1 = next((i for i in range(p0 + 1, end) if re.match(r"^  \S", lines[i])), end)
        new = lines[:p0] + block + lines[p1:]
    candidate = "".join(new)
    _, before = validate(yaml.safe_load(text), fleet_file)
    _, after = validate(yaml.safe_load(candidate), fleet_file)
    old = {p.message for p in before}
    added = [p.message for p in after if p.message not in old]
    if added:
        raise PhotoError(
            "the saved settings would break the fleet file: " + "; ".join(added)
        )
    return candidate


def upload_to_session(
    client: Any, session: str, image: Path, meta: Mapping[str, Any]
) -> Any:
    """POST /v1/calibration/sessions/{id}/photos, multipart: file + source=camera + meta JSON."""
    boundary = uuid.uuid4().hex
    parts = [
        ("source", None, b"camera", "text/plain"),
        ("meta", None, json.dumps(meta).encode(), "application/json"),
        ("file", image.name, image.read_bytes(), "image/png"),
    ]
    chunks: list[bytes] = []
    for name, filename, data, ctype in parts:
        disp = f'form-data; name="{name}"' + (
            f'; filename="{filename}"' if filename else ""
        )
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: {disp}\r\nContent-Type: {ctype}\r\n\r\n".encode()
            + data
            + b"\r\n"
        )
    body = b"".join(chunks) + f"--{boundary}--\r\n".encode()
    headers = client._headers(
        {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    url = client._url(f"/v1/calibration/sessions/{session}/photos")
    resp = client.transport.request(
        "POST", url, headers, body, max(client.timeout, 120.0)
    )
    if not 200 <= resp.status < 300:
        from printops.client import ApiError, _error_message

        raise ApiError(_error_message(resp.status, resp.body), status=resp.status)
    return resp.json()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
