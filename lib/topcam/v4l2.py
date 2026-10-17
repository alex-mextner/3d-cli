"""topcam.v4l2 — V4L2 controls and raw-frame capture in pure Python (ctypes + ioctl).

WHAT: list/get/set camera controls (focus, exposure, gain, white balance, …) by the names
  `v4l2-ctl` prints (`focus_absolute`, `auto_exposure`, …) or by short aliases (`focus`,
  `exposure`, `wb`, …); lock a camera into fully manual mode; enumerate formats and frame
  sizes; grab N uncompressed frames (YUYV) through mmap streaming, skipping the first K.
WHY: the same code runs on the PC (C920 on USB, the fallback backend) and on the Raspberry
  Pi Zero W camera node (ARMv6, Python 3 from Raspberry Pi OS Lite). No v4l-utils, ffmpeg
  or OpenCV is needed to take a measurement still, and the node stays small enough for a
  512 MB board.
INVARIANTS: stdlib only and no imports from the rest of `lib/` — the SD image ships this
  file as-is (`topcam/` package next to `node.py`). Struct layouts follow
  <linux/videodev2.h>; `timeval` is two native longs and the `m` union is pointer-sized,
  so ctypes gives the right size on x86_64 (v4l2_buffer = 88 bytes) and on 32-bit ARM
  (68 bytes, the kernel's time32 variant), and the ioctl numbers are computed from those
  sizes.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import fcntl
import mmap
import os
import re
import select
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

# ---- ioctl encoding (asm-generic, identical on x86 and ARM) ------------------------------
_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord("V") << 8) | nr


def _iowr(nr: int, struct: type[ctypes.Structure]) -> int:
    return _ioc(_IOC_READ | _IOC_WRITE, nr, ctypes.sizeof(struct))


def _ior(nr: int, struct: type[ctypes.Structure]) -> int:
    return _ioc(_IOC_READ, nr, ctypes.sizeof(struct))


def _iow_int(nr: int) -> int:
    return _ioc(_IOC_WRITE, nr, ctypes.sizeof(ctypes.c_int))


u8 = ctypes.c_uint8
u32 = ctypes.c_uint32
s32 = ctypes.c_int32
s64 = ctypes.c_int64


class Capability(ctypes.Structure):
    _fields_ = [
        ("driver", u8 * 16),
        ("card", u8 * 32),
        ("bus_info", u8 * 32),
        ("version", u32),
        ("capabilities", u32),
        ("device_caps", u32),
        ("reserved", u32 * 3),
    ]


class QueryCtrl(ctypes.Structure):
    _fields_ = [
        ("id", u32),
        ("type", u32),
        ("name", u8 * 32),
        ("minimum", s32),
        ("maximum", s32),
        ("step", s32),
        ("default_value", s32),
        ("flags", u32),
        ("reserved", u32 * 2),
    ]


class Control(ctypes.Structure):
    _fields_ = [("id", u32), ("value", s32)]


class _MenuUnion(ctypes.Union):
    _pack_ = 1
    _fields_ = [("name", u8 * 32), ("value", s64)]


class QueryMenu(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("id", u32), ("index", u32), ("u", _MenuUnion), ("reserved", u32)]


class FmtDesc(ctypes.Structure):
    _fields_ = [
        ("index", u32),
        ("type", u32),
        ("flags", u32),
        ("description", u8 * 32),
        ("pixelformat", u32),
        ("mbus_code", u32),
        ("reserved", u32 * 3),
    ]


class _FrmSizeDiscrete(ctypes.Structure):
    _fields_ = [("width", u32), ("height", u32)]


class _FrmSizeStepwise(ctypes.Structure):
    _fields_ = [
        ("min_width", u32),
        ("max_width", u32),
        ("step_width", u32),
        ("min_height", u32),
        ("max_height", u32),
        ("step_height", u32),
    ]


class _FrmSizeUnion(ctypes.Union):
    _fields_ = [("discrete", _FrmSizeDiscrete), ("stepwise", _FrmSizeStepwise)]


class FrmSizeEnum(ctypes.Structure):
    _fields_ = [
        ("index", u32),
        ("pixel_format", u32),
        ("type", u32),
        ("u", _FrmSizeUnion),
        ("reserved", u32 * 2),
    ]


class PixFormat(ctypes.Structure):
    _fields_ = [
        ("width", u32),
        ("height", u32),
        ("pixelformat", u32),
        ("field", u32),
        ("bytesperline", u32),
        ("sizeimage", u32),
        ("colorspace", u32),
        ("priv", u32),
        ("flags", u32),
        ("ycbcr_enc", u32),
        ("quantization", u32),
        ("xfer_func", u32),
    ]


class _FormatUnion(ctypes.Union):
    # raw_data is the kernel's 200-byte union; the pointer member gives it the alignment of
    # struct v4l2_window (which holds pointers): 8 on x86_64, 4 on armhf.
    _fields_ = [("pix", PixFormat), ("raw_data", u8 * 200), ("_align", ctypes.c_void_p)]


class Format(ctypes.Structure):
    _fields_ = [("type", u32), ("fmt", _FormatUnion)]


class RequestBuffers(ctypes.Structure):
    _fields_ = [
        ("count", u32),
        ("type", u32),
        ("memory", u32),
        ("capabilities", u32),
        ("flags", u8),
        ("reserved", u8 * 3),
    ]


class _Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class _Timecode(ctypes.Structure):
    _fields_ = [
        ("type", u32),
        ("flags", u32),
        ("frames", u8),
        ("seconds", u8),
        ("minutes", u8),
        ("hours", u8),
        ("userbits", u8 * 4),
    ]


class _BufferM(ctypes.Union):
    _fields_ = [
        ("offset", u32),
        ("userptr", ctypes.c_ulong),
        ("planes", ctypes.c_void_p),
        ("fd", s32),
    ]


class Buffer(ctypes.Structure):
    _fields_ = [
        ("index", u32),
        ("type", u32),
        ("bytesused", u32),
        ("flags", u32),
        ("field", u32),
        ("timestamp", _Timeval),
        ("timecode", _Timecode),
        ("sequence", u32),
        ("memory", u32),
        ("m", _BufferM),
        ("length", u32),
        ("reserved2", u32),
        ("request_fd", s32),
    ]


VIDIOC_QUERYCAP = _ior(0, Capability)
VIDIOC_ENUM_FMT = _iowr(2, FmtDesc)
VIDIOC_G_FMT = _iowr(4, Format)
VIDIOC_S_FMT = _iowr(5, Format)
VIDIOC_REQBUFS = _iowr(8, RequestBuffers)
VIDIOC_QUERYBUF = _iowr(9, Buffer)
VIDIOC_QBUF = _iowr(15, Buffer)
VIDIOC_DQBUF = _iowr(17, Buffer)
VIDIOC_STREAMON = _iow_int(18)
VIDIOC_STREAMOFF = _iow_int(19)
VIDIOC_G_CTRL = _iowr(27, Control)
VIDIOC_S_CTRL = _iowr(28, Control)
VIDIOC_QUERYCTRL = _iowr(36, QueryCtrl)
VIDIOC_QUERYMENU = _iowr(37, QueryMenu)
VIDIOC_ENUM_FRAMESIZES = _iowr(74, FrmSizeEnum)

BUF_TYPE_VIDEO_CAPTURE = 1
MEMORY_MMAP = 1
FIELD_ANY = 0
FRMSIZE_TYPE_DISCRETE = 1
CAP_VIDEO_CAPTURE = 0x00000001
CAP_DEVICE_CAPS = 0x80000000
CTRL_FLAG_DISABLED = 0x0001
CTRL_FLAG_READ_ONLY = 0x0004
CTRL_FLAG_INACTIVE = 0x0010
CTRL_FLAG_WRITE_ONLY = 0x0040
CTRL_FLAG_NEXT_CTRL = 0x80000000
CTRL_TYPE_INTEGER = 1
CTRL_TYPE_BOOLEAN = 2
CTRL_TYPE_MENU = 3
CTRL_TYPE_BUTTON = 4
CTRL_TYPE_CTRL_CLASS = 6
CTRL_TYPE_INTEGER_MENU = 9
BUF_FLAG_ERROR = 0x0040
_TYPE_NAMES = {
    1: "int",
    2: "bool",
    3: "menu",
    4: "button",
    5: "int64",
    7: "string",
    8: "bitmask",
    9: "intmenu",
}


def fourcc(code: str) -> int:
    a, b, c, d = (ord(ch) for ch in code)
    return a | (b << 8) | (c << 16) | (d << 24)


def fourcc_str(value: int) -> str:
    return "".join(chr((value >> (8 * i)) & 0xFF) for i in range(4))


YUYV = fourcc("YUYV")
MJPG = fourcc("MJPG")

# Control ids (<linux/v4l2-controls.h>); stable across kernels, unlike the display names
# (5.x renamed "Exposure, Auto" -> "Auto Exposure", "Focus, Auto" -> "Focus, Automatic
# Continuous", ...). An alias resolves by id, so both kernels answer to the same word.
_BASE = 0x00980900
_CAMERA = 0x009A0900
CID = {
    "brightness": _BASE + 0,
    "contrast": _BASE + 1,
    "saturation": _BASE + 2,
    "wb_auto": _BASE + 12,
    "gain": _BASE + 19,
    "power_line_frequency": _BASE + 24,
    "wb": _BASE + 26,
    "sharpness": _BASE + 27,
    "backlight_compensation": _BASE + 28,
    "exposure_auto": _CAMERA + 1,
    "exposure": _CAMERA + 2,
    "exposure_auto_priority": _CAMERA + 3,
    "pan": _CAMERA + 8,
    "tilt": _CAMERA + 9,
    "focus": _CAMERA + 10,
    "focus_auto": _CAMERA + 12,
    "zoom": _CAMERA + 13,
}
EXPOSURE_MANUAL = 1  # V4L2_EXPOSURE_MANUAL; 3 = aperture priority (the UVC "auto")
EXPOSURE_APERTURE_PRIORITY = 3


class V4L2Error(Exception):
    """A device or driver refused an operation; the message names the device and the call."""


def ctrl_var_name(name: str) -> str:
    """The `v4l2-ctl` variable name of a driver control name: "Exposure Time, Absolute" ->
    "exposure_time_absolute" (lowercase, every run of non-alphanumerics -> one `_`)."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


@dataclass(frozen=True)
class ControlInfo:
    id: int
    name: str  # v4l2-ctl style variable name
    label: str  # the driver's display name
    type: str
    minimum: int
    maximum: int
    step: int
    default: int
    flags: int
    value: int | None
    menu: dict[int, str] = field(default_factory=dict)

    @property
    def inactive(self) -> bool:
        return bool(self.flags & CTRL_FLAG_INACTIVE)

    @property
    def read_only(self) -> bool:
        return bool(self.flags & CTRL_FLAG_READ_ONLY)

    def as_dict(self) -> dict[str, Any]:
        alias = next((a for a, cid in CID.items() if cid == self.id), None)
        out: dict[str, Any] = {
            "name": self.name,
            "alias": alias,
            "label": self.label,
            "type": self.type,
            "min": self.minimum,
            "max": self.maximum,
            "step": self.step,
            "default": self.default,
            "value": self.value,
            "inactive": self.inactive,
            "read_only": self.read_only,
        }
        if self.menu:
            out["menu"] = {str(k): v for k, v in self.menu.items()}
        return out


@dataclass(frozen=True)
class FrameSet:
    """Frames grabbed in one streaming session (identical format, consecutive)."""

    width: int
    height: int
    pixelformat: str
    frames: list[bytes]
    dropped: int  # frames the driver flagged as corrupt or that came short
    seconds: float


def _cstr(raw: Iterable[int]) -> str:
    return bytes(raw).split(b"\0", 1)[0].decode("utf-8", "replace")


class Device:
    """An open V4L2 capture node. Use as a context manager; every call is synchronous."""

    def __init__(self, path: str) -> None:
        self.path = path
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as exc:
            raise V4L2Error(f"{path}: cannot open: {exc.strerror}") from exc

    def __enter__(self) -> Device:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def _ioctl(self, request: int, arg: Any, what: str) -> None:
        try:
            fcntl.ioctl(self.fd, request, arg)
        except OSError as exc:
            raise V4L2Error(f"{self.path}: {what}: {exc.strerror or exc}") from exc

    # ---- identity / formats ----------------------------------------------------------
    def capability(self) -> dict[str, Any]:
        cap = Capability()
        self._ioctl(VIDIOC_QUERYCAP, cap, "VIDIOC_QUERYCAP")
        caps = (
            cap.device_caps if cap.capabilities & CAP_DEVICE_CAPS else cap.capabilities
        )
        return {
            "driver": _cstr(cap.driver),
            "card": _cstr(cap.card),
            "bus_info": _cstr(cap.bus_info),
            "capture": bool(caps & CAP_VIDEO_CAPTURE),
        }

    def formats(self) -> dict[str, list[tuple[int, int]]]:
        """{fourcc: [(w, h), ...]} for every capture format with discrete frame sizes."""
        out: dict[str, list[tuple[int, int]]] = {}
        index = 0
        while True:
            desc = FmtDesc(index=index, type=BUF_TYPE_VIDEO_CAPTURE)
            try:
                fcntl.ioctl(self.fd, VIDIOC_ENUM_FMT, desc)
            except OSError as exc:
                if exc.errno == errno.EINVAL:
                    break
                raise V4L2Error(
                    f"{self.path}: VIDIOC_ENUM_FMT: {exc.strerror}"
                ) from exc
            sizes: list[tuple[int, int]] = []
            sindex = 0
            while True:
                fs = FrmSizeEnum(index=sindex, pixel_format=desc.pixelformat)
                try:
                    fcntl.ioctl(self.fd, VIDIOC_ENUM_FRAMESIZES, fs)
                except OSError:
                    break
                if fs.type == FRMSIZE_TYPE_DISCRETE:
                    sizes.append((fs.u.discrete.width, fs.u.discrete.height))
                else:
                    sw = fs.u.stepwise
                    sizes.append((sw.max_width, sw.max_height))
                    break
                sindex += 1
            out[fourcc_str(desc.pixelformat).strip()] = sizes
            index += 1
        return out

    # ---- controls ------------------------------------------------------------------
    def controls(self) -> list[ControlInfo]:
        """Every user-visible control with its current value (None for write-only ones)."""
        out: list[ControlInfo] = []
        qid = CTRL_FLAG_NEXT_CTRL
        while True:
            q = QueryCtrl(id=qid)
            try:
                fcntl.ioctl(self.fd, VIDIOC_QUERYCTRL, q)
            except OSError as exc:
                if exc.errno == errno.EINVAL:
                    break
                raise V4L2Error(
                    f"{self.path}: VIDIOC_QUERYCTRL: {exc.strerror}"
                ) from exc
            qid = q.id | CTRL_FLAG_NEXT_CTRL
            if q.flags & CTRL_FLAG_DISABLED or q.type == CTRL_TYPE_CTRL_CLASS:
                continue
            value = None
            if q.type not in (CTRL_TYPE_BUTTON,) and not q.flags & CTRL_FLAG_WRITE_ONLY:
                value = self._get(q.id)
            menu = (
                self._menu(q)
                if q.type in (CTRL_TYPE_MENU, CTRL_TYPE_INTEGER_MENU)
                else {}
            )
            label = _cstr(q.name)
            out.append(
                ControlInfo(
                    q.id,
                    ctrl_var_name(label),
                    label,
                    _TYPE_NAMES.get(q.type, str(q.type)),
                    q.minimum,
                    q.maximum,
                    q.step,
                    q.default_value,
                    q.flags,
                    value,
                    menu,
                )
            )
        return out

    def _menu(self, q: QueryCtrl) -> dict[int, str]:
        items: dict[int, str] = {}
        for index in range(q.minimum, q.maximum + 1):
            m = QueryMenu(id=q.id, index=index)
            try:
                fcntl.ioctl(self.fd, VIDIOC_QUERYMENU, m)
            except OSError:
                continue
            items[index] = (
                str(m.u.value) if q.type == CTRL_TYPE_INTEGER_MENU else _cstr(m.u.name)
            )
        return items

    def _get(self, cid: int) -> int:
        c = Control(id=cid)
        self._ioctl(VIDIOC_G_CTRL, c, f"VIDIOC_G_CTRL 0x{cid:08x}")
        return int(c.value)

    def resolve(self, name: str, known: list[ControlInfo] | None = None) -> ControlInfo:
        """A control by v4l2-ctl name, alias (`focus`, `exposure`, `wb`, …) or 0x id."""
        known = known if known is not None else self.controls()
        key = ctrl_var_name(name)
        cid = CID.get(key)
        if cid is None and re.fullmatch(r"0x[0-9a-f]+", name.lower()):
            cid = int(name, 16)
        for c in known:
            if c.id == cid or c.name == key:
                return c
        names = sorted(
            {c.name for c in known}
            | {a for a, i in CID.items() if any(c.id == i for c in known)}
        )
        raise V4L2Error(
            f"{self.path}: no control {name!r}; this camera has: {', '.join(names)}"
        )

    def set_controls(self, values: Mapping[str, int]) -> dict[str, int]:
        """Set controls in the given order; returns {v4l2-ctl name: read-back value}.
        A value outside the control's range is an error, never silently clamped."""
        known = self.controls()
        result: dict[str, int] = {}
        for name, value in values.items():
            info = self.resolve(name, known)
            if info.read_only:
                raise V4L2Error(f"{self.path}: control {info.name} is read-only")
            if info.type in ("int", "bool", "menu", "intmenu") and not (
                info.minimum <= value <= info.maximum
            ):
                raise V4L2Error(
                    f"{self.path}: {info.name}={value} is outside {info.minimum}..{info.maximum}"
                )
            if info.menu and value not in info.menu:
                accepted = ", ".join(f"{k} ({v})" for k, v in info.menu.items())
                raise V4L2Error(
                    f"{self.path}: {info.name}={value} is not one of {accepted}"
                )
            self._ioctl(
                VIDIOC_S_CTRL,
                Control(id=info.id, value=value),
                f"set {info.name}={value}",
            )
            result[info.name] = self._get(info.id)
        return result

    # ---- capture -------------------------------------------------------------------
    def set_format(self, width: int, height: int, pixelformat: int = YUYV) -> PixFormat:
        f = Format(type=BUF_TYPE_VIDEO_CAPTURE)
        f.fmt.pix.width = width
        f.fmt.pix.height = height
        f.fmt.pix.pixelformat = pixelformat
        f.fmt.pix.field = FIELD_ANY
        self._ioctl(
            VIDIOC_S_FMT, f, f"VIDIOC_S_FMT {width}x{height} {fourcc_str(pixelformat)}"
        )
        pix = f.fmt.pix
        if (pix.width, pix.height, pix.pixelformat) != (width, height, pixelformat):
            raise V4L2Error(
                f"{self.path}: asked for {width}x{height} {fourcc_str(pixelformat)}, the driver "
                f"offers {pix.width}x{pix.height} {fourcc_str(pix.pixelformat)} instead"
            )
        return pix

    def capture(
        self,
        width: int,
        height: int,
        count: int = 1,
        skip: int = 0,
        pixelformat: int = YUYV,
        buffers: int = 3,
        timeout: float = 10.0,
    ) -> FrameSet:
        """Stream, drop `skip` frames (the exposure settles), keep the next `count` good ones."""
        if count < 1:
            raise V4L2Error("capture: count must be >= 1")
        pix = self.set_format(width, height, pixelformat)
        expected = pix.bytesperline * height if pixelformat == YUYV else 0
        req = RequestBuffers(
            count=buffers, type=BUF_TYPE_VIDEO_CAPTURE, memory=MEMORY_MMAP
        )
        self._ioctl(VIDIOC_REQBUFS, req, "VIDIOC_REQBUFS")
        maps: list[mmap.mmap] = []
        streaming = False
        start = time.monotonic()
        frames: list[bytes] = []
        dropped = 0
        try:
            for i in range(req.count):
                b = Buffer(index=i, type=BUF_TYPE_VIDEO_CAPTURE, memory=MEMORY_MMAP)
                self._ioctl(VIDIOC_QUERYBUF, b, "VIDIOC_QUERYBUF")
                maps.append(
                    mmap.mmap(
                        self.fd,
                        b.length,
                        mmap.MAP_SHARED,
                        mmap.PROT_READ | mmap.PROT_WRITE,
                        offset=b.m.offset,
                    )
                )
                self._ioctl(VIDIOC_QBUF, b, "VIDIOC_QBUF")
            self._ioctl(
                VIDIOC_STREAMON, ctypes.c_int(BUF_TYPE_VIDEO_CAPTURE), "VIDIOC_STREAMON"
            )
            streaming = True
            seen = 0
            while len(frames) < count:
                ready, _, _ = select.select([self.fd], [], [], timeout)
                if not ready:
                    raise V4L2Error(
                        f"{self.path}: no frame within {timeout:g} s "
                        f"({width}x{height} {fourcc_str(pixelformat)})"
                    )
                b = Buffer(type=BUF_TYPE_VIDEO_CAPTURE, memory=MEMORY_MMAP)
                try:
                    fcntl.ioctl(self.fd, VIDIOC_DQBUF, b)
                except OSError as exc:
                    if exc.errno == errno.EAGAIN:
                        continue
                    raise V4L2Error(
                        f"{self.path}: VIDIOC_DQBUF: {exc.strerror}"
                    ) from exc
                bad = bool(b.flags & BUF_FLAG_ERROR) or (
                    expected and b.bytesused != expected
                )
                if seen >= skip:
                    if bad:
                        dropped += 1
                        if dropped > 3 * count + 10:
                            raise V4L2Error(
                                f"{self.path}: {dropped} corrupt frames in a row"
                            )
                    else:
                        frames.append(bytes(maps[b.index][: b.bytesused]))
                seen += 1
                self._ioctl(VIDIOC_QBUF, b, "VIDIOC_QBUF")
        finally:
            if streaming:
                with contextlib.suppress(OSError):
                    fcntl.ioctl(
                        self.fd, VIDIOC_STREAMOFF, ctypes.c_int(BUF_TYPE_VIDEO_CAPTURE)
                    )
            for m in maps:
                m.close()
            with contextlib.suppress(OSError):
                fcntl.ioctl(
                    self.fd,
                    VIDIOC_REQBUFS,
                    RequestBuffers(
                        count=0, type=BUF_TYPE_VIDEO_CAPTURE, memory=MEMORY_MMAP
                    ),
                )
        return FrameSet(
            width,
            height,
            fourcc_str(pixelformat),
            frames,
            dropped,
            time.monotonic() - start,
        )


# ---- manual lock ---------------------------------------------------------------------
@dataclass(frozen=True)
class Lock:
    """Camera settings for a measurement still; None = leave that control at its default.
    `focus_auto` keeps the camera's continuous autofocus on instead of a manual `focus`.
    `extra` holds any other control by v4l2-ctl name or alias (applied last)."""

    focus: int | None = None
    exposure: int | None = None  # V4L2 units: 100 µs (C920: 3..2047)
    gain: int | None = None
    wb: int | None = None  # kelvin (C920: 2000..6500)
    extra: tuple[tuple[str, int], ...] = ()
    focus_auto: bool = False

    def plan(self) -> dict[str, int]:
        """The ordered control writes: autos off before the manual value they unlock."""
        steps: dict[str, int] = {}
        if self.focus_auto:
            steps["focus_auto"] = 1
        elif self.focus is not None:
            steps["focus_auto"] = 0
            steps["focus"] = self.focus
        if self.exposure is not None:
            steps["exposure_auto"] = EXPOSURE_MANUAL
            steps["exposure_auto_priority"] = 0
            steps["exposure"] = self.exposure
        if self.wb is not None:
            steps["wb_auto"] = 0
            steps["wb"] = self.wb
        if self.gain is not None:
            steps["gain"] = self.gain
        steps.update(dict(self.extra))
        return steps


def _lock_plan(lock: Lock, known: list[ControlInfo]) -> dict[str, int]:
    """`lock.plan()` without the auto toggles the camera lacks (no exposure_auto_priority on
    some UVC cameras); a requested manual value the camera lacks stays and fails loudly."""
    have = {c.id for c in known} | {c.name for c in known}
    return {
        k: v
        for k, v in lock.plan().items()
        if not (k.endswith(("_auto", "_auto_priority")) and CID.get(k) not in have)
    }


# An auto control and the value that unlocks its manual partner for writing.
_AUTO_TO_MANUAL = {"focus_auto": 0, "exposure_auto": EXPOSURE_MANUAL, "wb_auto": 0}


def reset_defaults(dev: Device) -> list[str]:
    """Put every writable control back to the driver's default (`default_value` of
    VIDIOC_QUERYCTRL): autofocus, auto exposure and auto white balance on, factory values
    elsewhere — whatever state earlier runs left behind. The autos are switched to manual
    first so their manual partners take their defaults too, then the autos get theirs.
    Returns the controls that could not be reset (empty = clean)."""
    known = [
        c
        for c in dev.controls()
        if not c.read_only and c.type in ("int", "bool", "menu", "intmenu")
    ]
    autos = {CID[a]: v for a, v in _AUTO_TO_MANUAL.items()}
    failed: list[str] = []

    def write(c: ControlInfo, value: int) -> None:
        try:
            dev.set_controls({c.name: value})
        except V4L2Error as exc:
            failed.append(f"{c.name}={value} ({exc})")

    for c in known:
        if c.id in autos and (not c.menu or autos[c.id] in c.menu):
            write(c, autos[c.id])
    for c in known:
        if c.id not in autos:
            write(c, c.default)
    for c in known:
        if c.id in autos:
            write(c, c.default)
    return failed


@contextlib.contextmanager
def locked(dev: Device, lock: Lock) -> Iterator[dict[str, int]]:
    """Reset the camera to its defaults, apply `lock` for the duration of the block, and
    reset to the defaults again on any exit (`reset_defaults`): a still starts from a known
    state and never leaves the camera locked. Yields the lock's read-back. A reset failure
    after a clean block is an error; after a failing block it is best effort and the
    original error propagates."""
    try:
        failed = reset_defaults(dev)
        if failed:
            raise V4L2Error(
                f"{dev.path}: could not reset to defaults: {', '.join(failed)}"
            )
        yield dev.set_controls(_lock_plan(lock, dev.controls()))
    except BaseException:
        reset_defaults(dev)
        raise
    failed = reset_defaults(dev)
    if failed:
        raise V4L2Error(f"{dev.path}: could not reset to defaults: {', '.join(failed)}")


def yuyv_luma(frame: bytes) -> bytes:
    """The Y (luma) plane of a YUYV frame: every even byte. Fast on a Pi Zero (C slice)."""
    return frame[0::2]
