"""topcam.client — one interface over the two top-camera backends: a node over HTTP and a
UVC camera on this machine (USB).

WHAT: `open_camera(spec)` → a `Camera` with `health()`, `controls()`, `set_controls()`,
  `led()` and `still(StillSpec)`; a still always comes back as raw YUYV frames plus the
  control read-back taken right after the capture.
  spec: `usb` | `usb:/dev/videoN` (local, the C920 on the PC) | `http://host:port` |
        `host:port` | NAME (3d-api camera-node record, else http://NAME.local:8080).
WHY: `3d camera still` and `3d calibrate photo` must behave the same whether the camera
  hangs off the Pi above the bed or is plugged into the PC.
INVARIANTS: stdlib only at import time (command modules import this). The USB backend
  shares `topcam.node`'s request rules (the same Lock plan, the same skip semantics).
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from topcam import v4l2
from topcam.focus import DEFAULT_ROI, FocusError, FocusMode
import contextlib

NODE_PORT = 8080


class CameraError(Exception):
    """The camera or node refused or is unreachable; the message says which and why."""


@dataclass(frozen=True)
class StillSpec:
    width: int = 2304
    height: int = 1536
    frames: int = 1
    skip: int = 3
    lock: v4l2.Lock = field(default_factory=v4l2.Lock)
    led: bool | None = None  # node LED ring: True on, False off, None leave as is
    settle_ms: int | None = None
    focus: FocusMode = field(default_factory=lambda: FocusMode("default"))
    focus_roi: tuple[float, float, float, float] | None = None

    def query(self) -> dict[str, Any]:
        q: dict[str, Any] = {
            "res": f"{self.width}x{self.height}",
            "fmt": "yuyv",
            "frames": self.frames,
            "skip": self.skip,
        }
        if self.focus.mode != "default":
            q["focus"] = self.focus.text()
        elif self.lock.focus_auto:
            q["focus"] = "auto"
        elif self.lock.focus is not None:
            q["focus"] = str(self.lock.focus)
        if self.focus_roi is not None:
            q["focus_roi"] = ",".join(f"{v:g}" for v in self.focus_roi)
        for key in ("exposure", "gain", "wb"):
            value = getattr(self.lock, key)
            if value is not None:
                q[key] = value
        if self.lock.extra:
            q["ctrl"] = [f"{k}={v}" for k, v in self.lock.extra]
        if self.led is not None:
            q["led"] = "on" if self.led else "off"
        if self.settle_ms is not None:
            q["settle_ms"] = self.settle_ms
        return q


@dataclass
class Capture:
    width: int
    height: int
    frames: list[bytes]  # YUYV, W*H*2 bytes each
    controls: dict[str, int]  # read-back after the capture
    dropped: int
    seconds: float
    source: dict[str, Any]  # backend, device/url, card
    focus: dict[str, Any] = field(default_factory=dict)  # {"mode", "sweep": {best, scores}|None}


class Camera(Protocol):
    def describe(self) -> dict[str, Any]: ...
    def health(self) -> dict[str, Any]: ...
    def controls(self) -> dict[str, Any]: ...
    def set_controls(self, values: Mapping[str, int]) -> dict[str, int]: ...
    def led(self, on: bool) -> bool: ...
    def still(self, spec: StillSpec) -> Capture: ...


class UsbCamera:
    """A UVC camera on this machine, driven in-process through topcam.v4l2."""

    def __init__(self, device: str = "auto") -> None:
        if device == "auto":
            from topcam.node import NodeError, find_device

            try:
                device = find_device()
            except NodeError as exc:
                raise CameraError(str(exc)) from exc
        self.device = device

    def _open(self) -> v4l2.Device:
        try:
            return v4l2.Device(self.device)
        except v4l2.V4L2Error as exc:
            raise CameraError(str(exc)) from exc

    def describe(self) -> dict[str, Any]:
        return {"backend": "usb", "device": self.device}

    def health(self) -> dict[str, Any]:
        with self._open() as dev:
            return {
                "ok": True,
                "backend": "usb",
                "device": self.device,
                **dev.capability(),
            }

    def controls(self) -> dict[str, Any]:
        with self._open() as dev:
            try:
                return {
                    "controls": [c.as_dict() for c in dev.controls()],
                    "formats": {
                        k: [list(s) for s in v] for k, v in dev.formats().items()
                    },
                }
            except v4l2.V4L2Error as exc:
                raise CameraError(str(exc)) from exc

    def set_controls(self, values: Mapping[str, int]) -> dict[str, int]:
        with self._open() as dev:
            try:
                return dev.set_controls(values)
            except v4l2.V4L2Error as exc:
                raise CameraError(str(exc)) from exc

    def led(self, on: bool) -> bool:
        raise CameraError("the usb backend has no LED ring; use the camera node's LED")

    def still(self, spec: StillSpec) -> Capture:
        if spec.led is not None:
            raise CameraError(
                "--led needs a camera node (the usb backend has no LED ring)"
            )
        from topcam.node import capture_still

        with self._open() as dev:
            try:
                card = dev.capability()["card"]
                shot, after, swept = capture_still(
                    dev, spec.width, spec.height, spec.frames, spec.skip, spec.lock,
                    spec.focus, spec.focus_roi or DEFAULT_ROI,
                )
            except (v4l2.V4L2Error, FocusError) as exc:
                raise CameraError(str(exc)) from exc
        return Capture(
            spec.width,
            spec.height,
            shot.frames,
            after,
            shot.dropped,
            shot.seconds,
            {"backend": "usb", "device": self.device, "card": card},
            {"mode": spec.focus.text(), "sweep": swept},
        )


Opener = Callable[..., Any]


class NodeCamera:
    """A topcam node (`topcam.node`) over HTTP."""

    def __init__(
        self, url: str, opener: Opener | None = None, timeout: float = 120.0
    ) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen

    def describe(self) -> dict[str, Any]:
        return {"backend": "node", "url": self.url}

    def _call(
        self,
        method: str,
        path: str,
        query: Mapping[str, Any] | None = None,
        body: Any = None,
    ) -> tuple[bytes, Mapping[str, str]]:
        url = self.url + path
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with self._open(req, timeout=self.timeout) as resp:
                return resp.read(), dict(resp.headers.items())
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                detail = json.loads(raw).get("error", raw.decode("utf-8", "replace"))
            except (ValueError, AttributeError):
                detail = raw.decode("utf-8", "replace")
            raise CameraError(f"{self.url}{path}: HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise CameraError(f"{self.url}: unreachable: {reason}") from exc

    def _json(self, method: str, path: str, body: Any = None) -> Any:
        raw, _ = self._call(method, path, body=body)
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise CameraError(f"{self.url}{path}: not JSON: {raw[:120]!r}") from exc

    def health(self) -> dict[str, Any]:
        data: dict[str, Any] = self._json("GET", "/health")
        return data

    def controls(self) -> dict[str, Any]:
        data: dict[str, Any] = self._json("GET", "/controls")
        return data

    def set_controls(self, values: Mapping[str, int]) -> dict[str, int]:
        data = self._json("POST", "/controls", dict(values))
        return {str(k): int(v) for k, v in data["controls"].items()}

    def led(self, on: bool) -> bool:
        return bool(self._json("POST", "/led", {"on": on})["led"])

    def still(self, spec: StillSpec) -> Capture:
        raw, headers = self._call("GET", "/still", spec.query())
        h = {k.lower(): v for k, v in headers.items()}
        try:
            width, height = int(h["x-width"]), int(h["x-height"])
            count = int(h["x-frames"])
            controls = {
                str(k): int(v) for k, v in json.loads(h.get("x-controls", "{}")).items()
            }
        except (KeyError, ValueError) as exc:
            raise CameraError(
                f"{self.url}/still: malformed answer headers: {exc}"
            ) from exc
        size = width * height * 2
        if h.get("x-format") != "yuyv" or len(raw) != size * count:
            raise CameraError(
                f"{self.url}/still: expected {count} YUYV frames of {width}x{height} "
                f"({size * count} bytes), got {len(raw)} bytes as {h.get('x-format')}"
            )
        frames = [raw[i * size : (i + 1) * size] for i in range(count)]
        source: dict[str, Any] = {"backend": "node", "url": self.url}
        with contextlib.suppress(CameraError):
            source["card"] = self.health().get("card")
        try:
            dropped, seconds = int(h.get("x-dropped", 0)), float(h.get("x-seconds", 0))
            focus = json.loads(h.get("x-focus", "{}"))
        except ValueError as exc:
            raise CameraError(f"{self.url}: malformed still headers: {exc}") from exc
        if not isinstance(focus, dict):
            raise CameraError(f"{self.url}: malformed X-Focus header")
        return Capture(width, height, frames, controls, dropped, seconds, source, focus)


def node_url(name: str, lookup: Callable[[str], str | None] | None = None) -> str:
    """URL of a node by name: the 3d-api record when one exists, else NAME.local (mDNS)."""
    if lookup is None:
        from topcam.registry import lookup_url

        lookup = lookup_url
    url = lookup(name)
    if url:
        return url
    print(
        f"camera {name}: no 3d-api record; trying http://{name}.local:{NODE_PORT}",
        file=sys.stderr,
    )
    return f"http://{name}.local:{NODE_PORT}"


def open_camera(spec: str, lookup: Callable[[str], str | None] | None = None) -> Camera:
    if spec == "usb" or spec.startswith("usb:"):
        return UsbCamera(spec.partition(":")[2] or "auto")
    if "://" in spec:
        return NodeCamera(spec)
    if "." in spec or ":" in spec:
        host = spec if ":" in spec else f"{spec}:{NODE_PORT}"
        return NodeCamera(f"http://{host}")
    return NodeCamera(node_url(spec, lookup))
