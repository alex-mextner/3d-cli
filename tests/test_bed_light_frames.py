"""The printer's light around every camera frame of the bed (issue #723): `bed check`, `bed status`, the
reference capture and the job watch's frame (`LiveSource.grab`), against `LightPrinter` (test_bed_light:
a FakePrinter that lists and drives a light and logs every script in order) and `ZCamera` (saved frames
by the bed's Z). Nothing here touches a real printer or camera.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from printer_ops import bed as pbed
from printer_ops import bed_light as BL
from printer_ops.jobwatch_sources import LiveSource
from test_bed_light import NOW, PIN, LightPrinter, collapsed
from test_bed_view import CFG, capture, device

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
import bed_state as B  # noqa: E402  (needs numpy + OpenCV)


@pytest.fixture(autouse=True)
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture()
def refs() -> None:
    capture(200.0, "u1_z200_clear_ref.jpg")
    capture(70.0, "u1_z10_clear_ref.jpg")


# ---- the bed check: light on, bed move, capture, bed restore, light restore ------------------------


def test_check_view_drives_the_light_around_the_whole_move_and_capture(
    refs: None,
) -> None:
    p = LightPrinter("complete", 200.0)
    c = B.check_view(
        device(),
        CFG,
        adapter=p,
        camera=p.camera(),
        part_height=20.0,
        sleep=NOW,
        settle_s=0.0,
    )
    sequence = collapsed(p.log)
    print(
        "\n".join(f"  {i}. {entry}" for i, entry in enumerate(sequence, 1))
    )  # the demo evidence
    assert sequence == [
        "SET_PIN PIN=chamber_light VALUE=1",
        "G90 | G1 Z70.000 F600 | M400",
        "capture lit=True",
        "G90 | G1 Z200.000 F600 | M400",
        "SET_PIN PIN=chamber_light VALUE=0",
    ]
    assert (c["result"], c["ok"]) == ("empty", True) and p.z == 200.0 and p.lit is False
    assert (
        c["light"]["switched"] is True
        and c["light"]["restored"] is True
        and c["light"]["object"] == PIN
    )


def test_a_light_that_is_already_on_stays_on_through_the_check(refs: None) -> None:
    p = LightPrinter("complete", 200.0, lit=True)
    c = B.check_view(
        device(),
        CFG,
        adapter=p,
        camera=p.camera(),
        part_height=20.0,
        sleep=NOW,
        settle_s=0.0,
    )
    assert [e for e in p.log if e.startswith("SET_")] == [] and p.lit is True
    assert c["light"]["was_on"] is True and c["light"]["switched"] is False


def test_the_light_is_restored_when_the_camera_fails_mid_check(refs: None) -> None:
    p = LightPrinter("complete", 200.0)
    cam = p.camera()
    cam.fail = OSError("camera unplugged")
    with pytest.raises(OSError, match="unplugged"):
        B.check_view(
            device(),
            CFG,
            adapter=p,
            camera=cam,
            part_height=20.0,
            sleep=NOW,
            settle_s=0.0,
        )
    assert p.lit is False and p.z == 200.0
    assert collapsed(p.log)[-1] == "SET_PIN PIN=chamber_light VALUE=0"


def test_no_move_check_still_lights_the_frame(refs: None) -> None:
    p = LightPrinter("complete", 200.0)
    c = B.check_view(
        device(),
        CFG,
        mode="never",
        adapter=p,
        camera=p.camera(),
        sleep=NOW,
        settle_s=0.0,
    )
    assert collapsed(p.log) == [
        "SET_PIN PIN=chamber_light VALUE=1",
        "capture lit=True",
        "SET_PIN PIN=chamber_light VALUE=0",
    ]
    assert c["view"]["action"] == "not moved"


def test_a_printing_printer_is_not_asked_and_the_light_is_not_touched(
    refs: None,
) -> None:
    p = LightPrinter("printing", 150.0)
    c = B.check_view(
        device(), CFG, adapter=p, camera=p.camera(), sleep=NOW, settle_s=0.0
    )
    assert c["result"] == "busy" and p.log == []
    assert "left alone" in c["light"]["text"]


def test_a_printer_without_a_light_says_so_in_the_verdict_and_is_judged_anyway(
    refs: None,
) -> None:
    p = LightPrinter("complete", 200.0, light=None)
    c = B.check_view(
        device(),
        CFG,
        adapter=p,
        camera=p.camera(),
        part_height=20.0,
        sleep=NOW,
        settle_s=0.0,
    )
    assert c["result"] == "empty" and c["light"]["found"] is False
    assert c["light"]["text"].startswith("no light found")


def test_light_config_off_leaves_the_light_alone_for_the_check(refs: None) -> None:
    p = LightPrinter("complete", 200.0)
    cfg = replace(CFG, light=BL.LightConfig(enabled=False))
    c = B.check_view(
        device(),
        cfg,
        adapter=p,
        camera=p.camera(),
        part_height=20.0,
        sleep=NOW,
        settle_s=0.0,
    )
    assert [e for e in p.log if e.startswith("SET_")] == [] and c["light"]["skipped"]


def test_a_read_only_printer_gets_no_light_script(refs: None) -> None:
    p = LightPrinter("complete", 200.0)
    c = B.check_view(
        device(access="read-only"),
        CFG,
        adapter=p,
        camera=p.camera(),
        part_height=20.0,
        sleep=NOW,
        settle_s=0.0,
    )
    assert p.log[:1] == ["capture lit=False"] or all(
        not e.startswith("SET_") for e in p.log
    )
    assert "no writes" in c["light"]["text"]


def test_status_with_a_fresh_monitor_verdict_does_not_light_the_chamber(
    refs: None,
) -> None:
    """--max-age: a verdict the monitor confirmed recently is served without a frame, so without the light."""
    import json
    import time

    p = LightPrinter("complete", 200.0)
    path = pbed.state_path("u1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "printer": "u1",
                "bed": "clear",
                "plate": "present",
                "z": 200.0,
                "checked": time.time(),
                "reason": "r",
            }
        ),
        encoding="utf-8",
    )
    v = B.status_view(
        device(),
        CFG,
        mode="never",
        max_age=60.0,
        adapter=p,
        camera=p.camera(),
        sleep=NOW,
        settle_s=0.0,
    )
    assert v["source"] == "watch" and p.log == []
    stale = B.status_view(
        device(),
        CFG,
        mode="never",
        max_age=0.0001,
        adapter=p,
        camera=p.camera(),
        sleep=NOW,
        settle_s=0.0,
    )
    assert (
        stale["source"] == "camera now"
        and collapsed(p.log)[0] == "SET_PIN PIN=chamber_light VALUE=1"
    )


# ---- reference capture ----------------------------------------------------------------------------


def test_a_best_view_reference_is_taken_lit_and_the_light_is_restored_last() -> None:
    p = LightPrinter("complete", 200.0)
    r = B.capture_reference_best_view(
        device(),
        CFG,
        plate=True,
        part_height=20.0,
        adapter=p,
        camera=p.camera(),
        now=1.0,
        sleep=NOW,
        settle_s=0.0,
    )
    seq = collapsed(p.log)
    assert (
        seq[0] == "SET_PIN PIN=chamber_light VALUE=1"
        and seq[-1] == "SET_PIN PIN=chamber_light VALUE=0"
    )
    assert seq[-2].startswith("G90 | G1 Z200") and "capture lit=True" in seq
    assert r["light"]["switched"] is True and r["light"]["restored"] is True


def test_a_reference_from_a_saved_frame_never_touches_the_light() -> None:
    p = LightPrinter("complete", 200.0)
    data = Path(__file__).parent / "data" / "bed" / "u1_z200_clear_ref.jpg"
    B.capture_reference(
        device(),
        CFG,
        plate=True,
        frame=data.read_bytes(),
        frame_z=200.0,
        now=1.0,
        before_frame=lambda: p.log.append("light!"),
    )
    assert p.log == []


# ---- the job watch frame ----------------------------------------------------------------------------


def live(
    p: LightPrinter, light_cfg: BL.LightConfig | None = None, access: str = "full"
) -> LiveSource:
    dev_ = replace(
        device(access=access, watch={"bed": {"light": {"settle_s": 0}}}),
    )
    p.name = "u1"  # type: ignore[attr-defined]
    p.device = dev_  # type: ignore[attr-defined]
    return LiveSource(p, p.camera(), None)


def test_the_job_watch_frame_is_taken_with_the_light_on_and_restored_right_after() -> (
    None
):
    p = LightPrinter("printing", 12.0)
    src = live(p)
    jpeg = src.grab()
    assert jpeg[:2] == b"\xff\xd8"
    assert p.log == [
        "SET_PIN PIN=chamber_light VALUE=1",
        "capture lit=True",
        "SET_PIN PIN=chamber_light VALUE=0",
    ]
    assert (
        p.lit is False
        and src.light_report is not None
        and src.light_report.restored is True
    )
    p.log.clear()
    src.grab()  # the object list is read once, the light again for every frame
    assert len(p.log) == 3


def test_the_job_watch_leaves_a_lit_chamber_alone_and_survives_no_light() -> None:
    lit = LightPrinter("printing", 12.0, lit=True)
    live(lit).grab()
    assert [e for e in lit.log if e.startswith("SET_")] == [] and lit.lit is True
    dark = LightPrinter("printing", 12.0, light=None)
    src = live(dark)
    assert (
        src.grab()[:2] == b"\xff\xd8"
        and src.light_report is not None
        and src.light_report.found is False
    )


def test_the_job_watch_restores_the_light_when_the_camera_fails() -> None:
    p = LightPrinter("printing", 12.0)
    src = live(p)
    src.camera.fail = OSError("snapshot timeout")  # type: ignore[attr-defined]
    with pytest.raises(OSError, match="snapshot"):
        src.grab()
    assert p.lit is False


def test_a_live_source_without_a_device_uses_the_default_light() -> None:
    p = LightPrinter("printing", 12.0)
    p.name = "u1"  # type: ignore[attr-defined]
    src = LiveSource(
        p,
        SimpleNamespace(
            keepalive=lambda: None, grab=lambda: SimpleNamespace(jpeg=b"\xff\xd8x")
        ),
    )
    src.light_config = replace(src.light_config, settle_s=0.0)
    assert (
        src.grab() == b"\xff\xd8x" and p.log[0] == "SET_PIN PIN=chamber_light VALUE=1"
    )
