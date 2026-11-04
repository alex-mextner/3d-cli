"""`3d printer bed check` and `bed reference --capture --best-view` (issue #723): the bed is moved in Z
only (the guarded move of lib/bed_view.py), a fresh frame is judged from where the camera sees the
plate, the bed is put back, and the answer is in the words of the check (empty / objects /
part-present / part-gone / out-of-view / unknown / busy).

The printer is `FakePrinter` and the camera `ZCamera` of test_bed_view (saved frames by the bed's Z);
nothing here touches a real printer or camera.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from printer_ops import bed as pbed
from test_bed_view import CFG, FRAMES, FakePrinter, ZCamera, capture, device, moves

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


def check(
    printer: FakePrinter, *, mode: str = "force", expect: str | None = None
) -> dict[str, Any]:
    return B.check_view(
        device(),
        CFG,
        mode=mode,
        expect=expect,
        part_height=20.0,
        adapter=printer,
        camera=ZCamera(printer, FRAMES),
        sleep=lambda s: None,
        settle_s=0.0,
    )


def test_check_moves_the_bed_to_the_camera_and_reports_empty_with_both_frames(
    refs: None,
) -> None:
    """The incident: at Z 200 the sliver of the plate reads 'parts'; from Z 70 the plate is clear."""
    printer = FakePrinter("complete", 200.0)
    c = check(printer)
    assert (c["result"], c["ok"]) == ("empty", True)
    assert (
        c["z"] == 70.0
        and c["view"]["action"] == "moved"
        and c["view"]["restored"] is True
    )
    assert c["fraction"] is not None and c["fraction"] < 0.03
    assert Path(c["frame"]).is_file() and Path(c["raw_frame"]).is_file()
    assert Path(c["raw_frame"]).name == Path(c["frame"]).stem + ".raw.jpg"
    assert printer.z == 200.0  # put back
    assert moves(printer) == ["G1 Z70.000 F600", "G1 Z200.000 F600"]


def test_check_moves_even_when_auto_view_is_off_and_never_with_no_move(
    refs: None,
) -> None:
    off = pbed.BedConfig(enabled=True, auto_view=False)
    printer = FakePrinter("complete", 200.0)
    moved = B.check_view(
        device(),
        off,
        part_height=20.0,
        adapter=printer,
        camera=ZCamera(printer, FRAMES),
        sleep=lambda s: None,
        settle_s=0.0,
    )
    assert moved["view"]["action"] == "moved"
    printer = FakePrinter("complete", 200.0)
    still = check(printer, mode="never")
    assert moves(printer) == [] and still["view"]["action"] == "not moved"
    assert still["z"] == 200.0


def test_expect_part_reads_the_same_pixels_as_part_present_or_gone(
    refs: None,
) -> None:
    printer = FakePrinter("complete", 200.0)
    gone = check(printer, expect="part")
    assert (gone["result"], gone["ok"]) == ("part-gone", False)
    # the frame the camera shows at Z 70 with parts on the bed: occupied
    present = B.check_view(
        device(),
        CFG,
        expect="part",
        part_height=20.0,
        adapter=(p2 := FakePrinter("complete", 200.0)),
        camera=ZCamera(
            p2, {200.0: "u1_occupied_1400.jpg", 70.0: "u1_occupied_1351.jpg"}
        ),
        sleep=lambda s: None,
        settle_s=0.0,
    )
    assert present["result"] in ("part-present", "unknown"), present["reason"]


def test_check_while_printing_moves_nothing_and_says_busy(refs: None) -> None:
    printer = FakePrinter("printing", 150.0)
    cam = ZCamera(printer, FRAMES)
    c = B.check_view(
        device(),
        CFG,
        adapter=printer,
        camera=cam,
        sleep=lambda s: None,
        settle_s=0.0,
    )
    assert (c["result"], c["ok"]) == ("busy", False)
    assert (
        printer.scripts == [] and cam.grabs == 0
    )  # the bed belongs to the job: no move, no camera
    assert "3d printer watch" in c["reason"] or "belongs to the job" in c["reason"]


def test_check_without_a_view_says_out_of_view_not_empty(refs: None) -> None:
    printer = FakePrinter(
        "complete", 275.0, homed=""
    )  # not homed: cannot move, Z 275 sees nothing
    c = check(printer)
    assert c["result"] == "out-of-view" and c["ok"] is False
    assert printer.scripts == []


# ---- bed reference --capture --best-view ------------------------------------------------------


def test_a_reference_is_taken_from_the_best_view_and_the_bed_goes_back() -> None:
    printer = FakePrinter("complete", 200.0)
    r = B.capture_reference_best_view(
        device(),
        CFG,
        plate=True,
        part_height=20.0,
        adapter=printer,
        camera=ZCamera(printer, FRAMES),
        now=1.0,
        sleep=lambda s: None,
        settle_s=0.0,
    )
    assert r["kind"] == "plate" and r["view"]["action"] == "moved"
    assert 35.0 <= r["z"] < 200.0 and r["view"]["pick"]["visible"] >= 0.85
    assert printer.z == 200.0 and len(moves(printer)) == 2
    stored = B.list_references("u1")
    assert [round(float(s["z"]), 1) for s in stored] == [round(r["z"], 1)]


def test_a_reference_is_never_taken_while_a_job_runs() -> None:
    from errors import UsageError

    printer = FakePrinter("printing", 150.0)
    with pytest.raises(UsageError, match="printing"):
        B.capture_reference_best_view(
            device(),
            CFG,
            plate=True,
            adapter=printer,
            camera=ZCamera(printer, FRAMES),
            sleep=lambda s: None,
            settle_s=0.0,
        )
    assert printer.scripts == [] and B.list_references("u1") == []


# ---- the words --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bed", "reason", "expect", "result", "ok"),
    [
        ("clear", "", None, "empty", True),
        ("clear", "", "empty", "empty", True),
        ("clear", "", "part", "part-gone", False),
        ("occupied", "", None, "objects", True),
        ("occupied", "", "part", "part-present", True),
        ("occupied", "", "empty", "objects", False),
        (
            "unknown",
            "the bed is out of the camera's view at Z 275.0",
            None,
            "out-of-view",
            False,
        ),
        ("unknown", "a small part or a shadow?", None, "unknown", False),
        ("printing", "printing: bed not moved", None, "busy", False),
    ],
)
def test_check_result_words_and_exit_logic(
    bed: str, reason: str, expect: str | None, result: str, ok: bool
) -> None:
    assert B.check_result({"bed": bed, "reason": reason}, expect) == (result, ok)
