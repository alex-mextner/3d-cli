"""The `bed` line of `3d print-preflight --printer` / `3d printer start` fails only on a CONFIDENT find (#723).

The U1 rests at Z 200 after a job, where its camera sees 15 % of the plate. A window light there read as
"16.0 % changed: occupied" and a print was started with --force. The line now runs the full bed-check path
(best view, the printer's light on, everything put back) and FAILs only when parts show in a view of at
least `watch.bed.auto_view_min` of the plate; `unknown`, and an occupied seen at a poor view, are a WARN
that names the reason and the command to run. The printer is `FakePrinter`/`LightPrinter` and the camera
`ZCamera` (saved frames by the bed's Z); nothing here touches a real printer or camera.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from printer_ops import bed as pbed
from printer_ops import preflight
from test_bed_light import PIN, LightPrinter, collapsed
from test_bed_robust import LIGHT_DRIFT, OLD, u1_ref_1016
from test_bed_state import frame, u1_match
from test_bed_view import CFG, FRAMES, FakePrinter, ZCamera, capture, device

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
import bed_state as B  # noqa: E402  (needs numpy + OpenCV)

DATA = Path(__file__).parent / "data" / "bed"
WATCH = {
    "bed": {"enabled": True, "auto_view": True},
    "vision": {"camera": {"kind": "moonraker-monitor"}},
}


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


def verdict_dict(v: B.Verdict, z: float) -> dict[str, Any]:
    """What `bed_state.py status --json` prints for a classifier verdict (no occupied_confident mark)."""
    return {
        "printer": "u1",
        "bed": v.bed,
        "plate": v.plate,
        "z": z,
        "source": "camera now",
        "age_s": 0.0,
        "reason": v.reason,
        "metrics": {k: round(x, 4) for k, x in v.metrics.items()},
        "frame": "/f.jpg",
    }


def part_at_z70() -> bytes:
    """The empty plate as the camera sees it from Z 70 with a 56 px part standing on it."""
    img = cv2.imread(str(DATA / "u1_z10_clear_ref.jpg"))
    vol, _, _ = B.build_masks(device(), CFG, 70.0, img.shape[:2])
    ys, xs = np.nonzero(vol)
    cy, cx = int(np.percentile(ys, 40)), int(np.percentile(xs, 60))
    img[cy : cy + 56, cx : cx + 56] = (30, 90, 30)
    return cv2.imencode(".jpg", img)[1].tobytes()


def runner_for(printer: FakePrinter, cam: ZCamera, seen: list[list[str]]) -> Any:
    """A runner that does what `bed_state.py status` does, in this process, against the fakes."""

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        mode = (
            "never"
            if "--no-move" in argv
            else "force"
            if "--best-view" in argv
            else "auto"
        )
        max_age = (
            float(argv[argv.index("--max-age") + 1]) if "--max-age" in argv else None
        )
        v = B.status_view(
            device(),
            CFG,
            mode=mode,
            max_age=max_age,
            part_height=20.0,
            adapter=printer,
            camera=cam,
            sleep=lambda s: None,
            settle_s=0.0,
        )
        return subprocess.CompletedProcess(argv, 0, json.dumps(v, default=str), "")

    return run


# ---- the former misfire -----------------------------------------------------------------------


@pytest.mark.parametrize("name", LIGHT_DRIFT)
def test_the_u1_z200_window_light_is_a_warn_and_never_a_fail(name: str) -> None:
    """15 % of the plate in view, 16 % 'changed' by the window light: the classifier says unknown (#723)."""
    v = B.classify(frame(name), [u1_ref_1016()], 200.2, "u1", match=u1_match())
    c = preflight.check_bed_verdict(verdict_dict(v, 200.2))
    assert v.bed == "unknown" and c.status == "WARN", c.detail
    assert "`3d printer bed check u1`" in c.detail


@pytest.mark.parametrize("name", LIGHT_DRIFT)
def test_even_the_old_classifier_s_occupied_at_that_view_is_not_a_fail(
    name: str,
) -> None:
    """Teeth: the pre-#723 classifier called both frames occupied; at a 15 % view that is a suspicion."""
    v = B.classify(frame(name), [u1_ref_1016()], 200.2, "u1", OLD, u1_match())
    assert v.bed == "occupied" and v.metrics["bed_in_view"] < 0.2
    assert not B.occupied_confident(v, OLD, pbed.AUTO_VIEW_MIN)
    c = preflight.check_bed_verdict(verdict_dict(v, 200.2))
    assert (
        c.status == "WARN"
        and "only 15% of the plate is in the camera's view" in c.detail
    )
    assert "`3d printer bed check u1`" in c.detail and "light on" in c.detail


def test_the_incident_frame_at_the_poor_view_is_judged_but_not_failed(
    refs: None,
) -> None:
    """The saved Z 200 frame with parts, judged where the bed rests (--no-bed-move): WARN, with both commands."""
    printer = FakePrinter("complete", 200.0)
    c = preflight.check_bed(
        device(watch=WATCH),
        move=False,
        runner=runner_for(printer, ZCamera(printer, FRAMES), []),
    )
    assert (
        c.status == "WARN"
        and "3d printer bed check u1" in c.detail
        and "15%" in c.detail
    )
    assert printer.scripts == []  # --no-bed-move: nothing moves


# ---- a confident find ----------------------------------------------------------------------------


def test_parts_in_a_good_view_fail_the_line_and_the_bed_is_put_back(refs: None) -> None:
    printer = FakePrinter("complete", 200.0)
    cam = ZCamera(printer, FRAMES)
    cam.frames[70.0] = part_at_z70()
    seen: list[list[str]] = []
    c = preflight.check_bed(device(watch=WATCH), runner=runner_for(printer, cam, seen))
    assert c.status == "FAIL" and "objects on the bed" in c.detail
    assert "judged at Z 70.0 after moving the bed from Z 200.0 and back" in c.detail
    assert printer.z == 200.0 and seen[0][-1] == "--best-view"


def test_the_confident_mark_needs_a_good_view_and_a_region_of_the_normal_size(
    refs: None,
) -> None:
    printer = FakePrinter("complete", 200.0)
    cam = ZCamera(printer, FRAMES)
    cam.frames[70.0] = part_at_z70()
    good = B.status_view(
        device(),
        CFG,
        mode="force",
        part_height=20.0,
        adapter=printer,
        camera=cam,
        sleep=lambda s: None,
        settle_s=0.0,
    )
    assert (
        good["bed"] == "occupied"
        and good["metrics"]["bed_in_view"] >= pbed.AUTO_VIEW_MIN
    )
    assert good["occupied_confident"] is True
    poor = B.status_view(
        device(),
        CFG,
        mode="never",
        adapter=printer,
        camera=ZCamera(printer, FRAMES),
        sleep=lambda s: None,
        settle_s=0.0,
    )
    assert poor["bed"] == "occupied" and poor["metrics"]["bed_in_view"] < 0.2
    assert poor["occupied_confident"] is False
    # the same pixels judged against a stricter watch.bed.auto_view_min than the view allows
    th = B.thresholds_for(CFG)
    v = B.Verdict(
        "occupied",
        "present",
        "r",
        {"changed": 0.2, "blob_px": 900.0, "bed_in_view": 0.9},
    )
    assert B.occupied_confident(v, th, 0.85) and not B.occupied_confident(v, th, 0.95)
    small = B.Verdict(
        "occupied",
        "present",
        "r",
        {"changed": 0.001, "blob_px": 10.0, "bed_in_view": 1.0},
    )
    assert not B.occupied_confident(small, th, 0.85)
    assert B.occupied_confident(
        B.Verdict("occupied", "present", "r", {"changed": 0.1}), th, 0.85
    )  # no calibration


def test_a_missing_plate_still_fails_and_a_clear_bed_still_passes(refs: None) -> None:
    assert (
        preflight.check_bed_verdict(
            {"bed": "unknown", "plate": "missing", "reason": "r"}
        ).status
        == "FAIL"
    )
    ok = preflight.check_bed_verdict(
        {"bed": "clear", "plate": "present", "source": "camera now", "age_s": 0.0}
    )
    assert ok.status == "PASS"


def test_a_confident_flag_from_the_tool_decides_not_the_view_alone() -> None:
    base = {
        "printer": "u1",
        "bed": "occupied",
        "plate": "present",
        "source": "camera now",
        "age_s": 0.0,
        "reason": "r",
        "metrics": {"bed_in_view": 0.9, "changed": 0.3},
    }
    assert (
        preflight.check_bed_verdict({**base, "occupied_confident": True}).status
        == "FAIL"
    )
    unmarked = preflight.check_bed_verdict(base)
    assert (
        unmarked.status == "WARN"
        and "does not mark it as a confident find" in unmarked.detail
    )
    assert (
        preflight.check_bed_verdict({**base, "occupied_confident": False}).status
        == "WARN"
    )


# ---- unknown is a WARN with the way out ---------------------------------------------------------------


def test_unknown_is_a_warn_naming_the_command_and_the_reason() -> None:
    c = preflight.check_bed_verdict(
        {
            "printer": "u1",
            "bed": "unknown",
            "plate": "unknown",
            "reason": "no reference",
            "source": "camera now",
            "age_s": 0.0,
        }
    )
    assert c.status == "WARN" and "no reference" in c.detail
    assert "`3d printer bed check u1`" in c.detail and "light on" in c.detail


# ---- the full path -----------------------------------------------------------------------------------


def test_the_line_runs_the_full_check_with_the_light_and_shows_what_the_light_did(
    refs: None,
) -> None:
    printer = LightPrinter("complete", 200.0)
    c = preflight.check_bed(
        device(watch=WATCH), runner=runner_for(printer, printer.camera(), [])
    )
    assert c.status == "PASS" and "judged at Z 70.0" in c.detail
    assert collapsed(printer.log)[0] == f"SET_PIN PIN={PIN.split()[1]} VALUE=1"
    assert (
        collapsed(printer.log)[-1].endswith("VALUE=0")
        and printer.lit is False
        and printer.z == 200.0
    )
    assert any(
        line.startswith("light: ") and "switched on" in line and "restored" in line
        for line in c.lines
    )


def test_the_line_never_reads_the_watchers_verdict_for_a_poor_z(refs: None) -> None:
    """A fresh monitor verdict at the rest Z used to answer `--max-age`; the full path always takes a frame."""
    printer = FakePrinter("complete", 200.0)
    path = pbed.state_path("u1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "printer": "u1",
                "bed": "occupied",
                "plate": "present",
                "z": 200.0,
                "checked": 4e9,
                "reason": "from the monitor",
            }
        ),
        encoding="utf-8",
    )
    c = preflight.check_bed(
        device(watch=WATCH), runner=runner_for(printer, ZCamera(printer, FRAMES), [])
    )
    assert (
        c.status == "PASS"
        and "camera now" in c.detail
        and "from the monitor" not in c.detail
    )


def test_a_printing_printer_is_a_warn_and_nothing_is_lit_or_moved(refs: None) -> None:
    printer = LightPrinter("printing", 150.0)
    c = preflight.check_bed(
        device(watch=WATCH), runner=runner_for(printer, printer.camera(), [])
    )
    assert c.status == "WARN" and printer.log == []
