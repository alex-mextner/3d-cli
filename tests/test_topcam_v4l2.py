"""topcam.v4l2: kernel ABI (struct sizes, ioctl numbers on x86_64 and armhf) and control logic."""

from __future__ import annotations

import ctypes
from typing import Any

import pytest
from topcam_fixtures import C920_CONTROLS, FakeCamera, FakeDevice

from topcam import v4l2

_NATIVE = {
    ctypes.c_long: ctypes.c_int32,
    ctypes.c_ulong: ctypes.c_uint32,
    ctypes.c_void_p: ctypes.c_uint32,
}


def _armhf(cls: Any) -> Any:
    """The same struct/union with 32-bit longs and pointers (Raspberry Pi OS armhf)."""
    if cls in _NATIVE:
        return _NATIVE[cls]
    if isinstance(cls, type) and issubclass(cls, ctypes.Array):
        return _armhf(cls._type_) * cls._length_
    if isinstance(cls, type) and issubclass(cls, ctypes.Structure | ctypes.Union):
        attrs: dict[str, Any] = {
            "_fields_": [(n, _armhf(t)) for n, t, *_ in cls._fields_]
        }
        if hasattr(cls, "_pack_"):
            attrs["_pack_"] = cls._pack_
        base = ctypes.Union if issubclass(cls, ctypes.Union) else ctypes.Structure
        return type(cls.__name__ + "32", (base,), attrs)
    return cls


def _number(direction: int, nr: int, size: int) -> int:
    return v4l2._ioc(direction, nr, size)


def test_struct_sizes_match_the_64_bit_kernel_abi() -> None:
    if ctypes.sizeof(ctypes.c_long) != 8:
        pytest.skip("64-bit layout check")
    sizes = {
        v4l2.Buffer: 88,
        v4l2.Format: 208,
        v4l2.QueryCtrl: 68,
        v4l2.Control: 8,
        v4l2.QueryMenu: 44,
        v4l2.FmtDesc: 64,
        v4l2.FrmSizeEnum: 44,
        v4l2.RequestBuffers: 20,
        v4l2.Capability: 104,
    }
    assert {c.__name__: ctypes.sizeof(c) for c in sizes} == {
        c.__name__: s for c, s in sizes.items()
    }


def test_ioctl_numbers_match_videodev2_h_on_x86_64() -> None:
    if ctypes.sizeof(ctypes.c_long) != 8:
        pytest.skip("64-bit numbers")
    assert v4l2.VIDIOC_QUERYCAP == 0x80685600
    assert v4l2.VIDIOC_S_FMT == 0xC0D05605
    assert v4l2.VIDIOC_REQBUFS == 0xC0145608
    assert v4l2.VIDIOC_QUERYBUF == 0xC0585609
    assert v4l2.VIDIOC_QBUF == 0xC058560F
    assert v4l2.VIDIOC_DQBUF == 0xC0585611
    assert v4l2.VIDIOC_STREAMON == 0x40045612
    assert v4l2.VIDIOC_G_CTRL == 0xC008561B
    assert v4l2.VIDIOC_S_CTRL == 0xC008561C
    assert v4l2.VIDIOC_QUERYCTRL == 0xC0445624
    assert v4l2.VIDIOC_QUERYMENU == 0xC02C5625
    assert v4l2.VIDIOC_ENUM_FRAMESIZES == 0xC02C564A


def test_armhf_layout_is_the_kernel_time32_abi() -> None:
    """Pi Zero W runs 32-bit Raspberry Pi OS: v4l2_buffer is 68 bytes and v4l2_format 204,
    giving VIDIOC_QBUF 0xc044560f and VIDIOC_S_FMT 0xc0cc5605 (the kernel's 32-bit numbers)."""
    buf32, fmt32 = _armhf(v4l2.Buffer), _armhf(v4l2.Format)
    assert ctypes.sizeof(buf32) == 68
    assert ctypes.sizeof(fmt32) == 204
    rw = v4l2._IOC_READ | v4l2._IOC_WRITE
    assert _number(rw, 15, ctypes.sizeof(buf32)) == 0xC044560F
    assert _number(rw, 5, ctypes.sizeof(fmt32)) == 0xC0CC5605
    for fixed in (v4l2.QueryCtrl, v4l2.Control, v4l2.QueryMenu, v4l2.RequestBuffers):
        assert ctypes.sizeof(_armhf(fixed)) == ctypes.sizeof(fixed)


def test_fourcc_round_trip() -> None:
    assert v4l2.fourcc_str(v4l2.YUYV) == "YUYV"
    assert v4l2.fourcc("MJPG") == v4l2.MJPG == 0x47504A4D


@pytest.mark.parametrize(
    ("label", "var"),
    [
        ("Exposure Time, Absolute", "exposure_time_absolute"),
        ("Focus, Automatic Continuous", "focus_automatic_continuous"),
        ("White Balance Temperature", "white_balance_temperature"),
        ("Exposure (Absolute)", "exposure_absolute"),
    ],
)
def test_control_names_follow_v4l2_ctl(label: str, var: str) -> None:
    assert v4l2.ctrl_var_name(label) == var


def test_lock_plan_turns_autos_off_before_manual_values() -> None:
    lock = v4l2.Lock(
        focus=40, exposure=300, gain=10, wb=4500, extra=(("sharpness", 0),)
    )
    assert list(lock.plan().items()) == [
        ("focus_auto", 0),
        ("focus", 40),
        ("exposure_auto", v4l2.EXPOSURE_MANUAL),
        ("exposure_auto_priority", 0),
        ("exposure", 300),
        ("wb_auto", 0),
        ("wb", 4500),
        ("gain", 10),
        ("sharpness", 0),
    ]
    assert v4l2.Lock().plan() == {}


def test_apply_lock_on_a_c920_reads_back_manual_values() -> None:
    cam = FakeCamera()
    dev = FakeDevice(cam, cam.path)
    with v4l2.locked(dev, v4l2.Lock(focus=40, exposure=300, gain=0, wb=4500)) as got:
        pass
    assert got == {
        "focus_automatic_continuous": 0,
        "focus_absolute": 40,
        "auto_exposure": 1,
        "exposure_dynamic_framerate": 0,
        "exposure_time_absolute": 300,
        "white_balance_automatic": 0,
        "white_balance_temperature": 4500,
        "gain": 0,
    }


def test_manual_value_while_auto_is_on_is_refused_like_the_driver() -> None:
    cam = FakeCamera()
    dev = FakeDevice(cam, cam.path)
    with pytest.raises(v4l2.V4L2Error, match="Permission denied"):
        dev.set_controls({"exposure": 300})


def test_resolve_by_alias_name_and_id_and_unknown_lists_names() -> None:
    cam = FakeCamera()
    dev = FakeDevice(cam, cam.path)
    assert dev.resolve("exposure").name == "exposure_time_absolute"
    assert dev.resolve("exposure_time_absolute").id == v4l2.CID["exposure"]
    assert dev.resolve("Focus, Absolute").name == "focus_absolute"
    assert dev.resolve(hex(v4l2.CID["gain"])).name == "gain"
    with pytest.raises(v4l2.V4L2Error, match="no control 'zoomy'.*focus_absolute"):
        dev.resolve("zoomy")


def test_out_of_range_and_bad_menu_values_are_errors_not_clamped() -> None:
    cam = FakeCamera()
    dev = FakeDevice(cam, cam.path)
    with pytest.raises(v4l2.V4L2Error, match=r"gain=300 is outside 0\.\.255"):
        dev.set_controls({"gain": 300})
    with pytest.raises(
        v4l2.V4L2Error, match="auto_exposure=2 is not one of 1 \\(Manual Mode\\)"
    ):
        dev.set_controls({"exposure_auto": 2})
    assert cam.writes == []


def test_yuyv_luma_takes_every_even_byte() -> None:
    assert v4l2.yuyv_luma(bytes([1, 128, 2, 129, 3, 130, 4, 131])) == bytes(
        [1, 2, 3, 4]
    )


def test_control_info_as_dict_names_the_alias() -> None:
    cam = FakeCamera()
    info = {c.name: c.as_dict() for c in FakeDevice(cam, cam.path).controls()}
    assert info["exposure_time_absolute"]["alias"] == "exposure"
    assert info["exposure_time_absolute"]["inactive"] is True
    assert info["auto_exposure"]["menu"] == {
        "1": "Manual Mode",
        "3": "Aperture Priority Mode",
    }


def test_locked_resets_every_control_to_the_camera_defaults() -> None:
    cam = FakeCamera()
    cam.values.update(focus_auto=0, focus=40, exposure_auto=1, exposure=900, brightness=10)  # left by someone
    dev = FakeDevice(cam, cam.path)
    with v4l2.locked(dev, v4l2.Lock(focus=100, exposure=300, wb=5000)) as got:
        assert cam.values["focus"] == 100 and cam.values["exposure"] == 300
        assert cam.values["brightness"] == 128  # reset before the lock, not the stale 10
    assert got["focus_absolute"] == 100
    defaults = {c[0]: c[7] for c in C920_CONTROLS}
    assert cam.values == defaults  # autofocus/auto exposure/auto WB back on, factory values


def test_locked_resets_even_when_the_block_fails() -> None:
    cam = FakeCamera()
    dev = FakeDevice(cam, cam.path)
    with pytest.raises(RuntimeError, match="boom"), v4l2.locked(dev, v4l2.Lock(focus=60, exposure=200)):
        raise RuntimeError("boom")
    assert cam.values == {c[0]: c[7] for c in C920_CONTROLS}


def test_autofocus_mode_turns_autofocus_on() -> None:
    assert v4l2.Lock(focus=30, focus_auto=True).plan() == {"focus_auto": 1}
