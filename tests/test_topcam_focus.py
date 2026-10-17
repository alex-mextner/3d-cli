"""topcam.focus: focus-mode parsing, the sharpness score and the software sweep."""

from __future__ import annotations

import pytest
from topcam_fixtures import C920_CONTROLS, FakeCamera, FakeDevice, stripes

from topcam import v4l2
from topcam.focus import FocusError, FocusMode, parse_focus, parse_roi, sharpness, sweep
from topcam.node import capture_still, lock_for


@pytest.mark.parametrize(("text", "mode"), [
    (None, FocusMode("default")),
    ("auto", FocusMode("auto")),
    ("40", FocusMode("manual", 40)),
    (40, FocusMode("manual", 40)),
    ("sweep", FocusMode("sweep")),
    ("sweep:20-120", FocusMode("sweep", None, 20, 120)),
    ("sweep:20-120:10", FocusMode("sweep", None, 20, 120, 10)),
])
def test_parse_focus(text: object, mode: FocusMode) -> None:
    assert parse_focus(text) == mode  # type: ignore[arg-type]


@pytest.mark.parametrize("text", ["near", "sweep:120-20", "sweep:a-b", "sweep:0-10:0", "sweep:0-10:5:1"])
def test_parse_focus_rejects(text: str) -> None:
    with pytest.raises(FocusError):
        parse_focus(text)


def test_parse_roi() -> None:
    assert parse_roi("0.1,0.2,0.9,0.8") == (0.1, 0.2, 0.9, 0.8)
    with pytest.raises(FocusError):
        parse_roi("0.5,0.5,0.4,0.9")


def test_sharpness_grows_with_contrast() -> None:
    w, h = 64, 32
    soft = v4l2.yuyv_luma(stripes(w, h, 0, 40))
    sharp = v4l2.yuyv_luma(stripes(w, h, 40, 40))
    flat = bytes([128]) * (w * h)
    assert sharpness(sharp, w, h) > sharpness(soft, w, h) > sharpness(flat, w, h) == 0.0


def test_sweep_finds_the_sharpest_position_coarse_then_fine() -> None:
    cam = FakeCamera(focus_target=135)
    dev = FakeDevice(cam, cam.path)
    dev.set_controls({"focus_auto": 0})
    got = sweep(dev, FocusMode("sweep"), res=(640, 480), settle_s=0)
    assert got["best"] == 135 and cam.values["focus"] == 135
    positions = [p for p, _ in got["scores"]]
    assert {0, 25, 125, 150, 250} <= set(positions) and 135 in positions


def test_capture_still_sweeps_keeps_focus_for_the_series_then_resets() -> None:
    cam = FakeCamera(focus_target=80)
    dev = FakeDevice(cam, cam.path)
    focus = FocusMode("sweep", None, 0, 250, 25)
    shot, readback, swept = capture_still(dev, 640, 480, 3, 0, lock_for(focus, 300, None, None), focus,
                                          (0.25, 0.25, 0.75, 0.75))
    assert swept is not None and swept["best"] == 80
    assert cam.captures[-1]["controls"]["focus"] == 80 and cam.captures[-1]["count"] == 3
    assert readback["focus_absolute"] == 80 and readback["focus_automatic_continuous"] == 0
    assert cam.values == {c[0]: c[7] for c in C920_CONTROLS}
