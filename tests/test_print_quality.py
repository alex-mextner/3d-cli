"""lib/print_quality.py — the pixel half of the print-quality monitor, on REAL frames and synthetic ones.

Real: tests/data/quality, downscaled to 640 px — the Snapmaker U1 camera during the 10 h SIMBOL sheet job of
2026-11-05 (`u1_f1` empty plate, `u1_f5` clean first layer, `u1_f7` / `u1_f10` / `u1_f12` stringing debris of
layers 2-4 that nothing flagged on the day) and three phone photos of the failed first attempt (a stringy first
layer, a close-up of it, a coupon with a blob at its end). The numbers are calibrated on exactly these (and on the
other seven full-size frames, docs/research/print-quality-monitor.md), so this file pins the calibration: a change
of the constants that stops separating clean from stringy frames fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from printer_ops import quality as Q

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
import print_quality as PQ  # noqa: E402

DATA = Path(__file__).parent / "data" / "quality"
DEVICES = """u1:
  adapter: moonraker
  host: 127.0.0.1
  port: 1
  model: Snapmaker U1
"""


@pytest.fixture
def u1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (tmp_path / "config" / "3d-cli").mkdir(parents=True)
    (tmp_path / "config" / "3d-cli" / "printer-devices.yaml").write_text(
        DEVICES, encoding="utf-8"
    )
    return "u1"


def read(name: str, u1: str, z: float = 0.6) -> dict:
    return PQ.quality_numbers((DATA / name).read_bytes(), name=u1, z=z)


def whole(name: str) -> dict:
    return PQ.quality_numbers((DATA / name).read_bytes(), name=None, z=0.0)


def test_clean_first_layer_vs_stringing_debris_separate_on_the_real_camera(
    u1: str,
) -> None:
    clean = read("u1_f5.jpg", u1)
    assert clean["status"] == "ok" and clean["region_source"].startswith(
        "plate surface of u1"
    )
    assert clean["fleck_frac"] < 0.02 and clean["order"] > 0.8
    for name in ("u1_f7.jpg", "u1_f10.jpg", "u1_f12.jpg"):
        bad = read(name, u1)
        assert bad["fleck_frac"] > 2.4 * clean["fleck_frac"], name
        assert bad["fleck_frac"] > 0.035 and bad["order"] < 0.75, name


def test_the_empty_plate_has_its_own_glints_which_is_why_presence_is_not_the_numbers_job(
    u1: str,
) -> None:
    """The bare textured plate sparkles (fleck_frac 0.1): the decision side only judges a plate with a part."""
    assert read("u1_f1.jpg", u1)["fleck_frac"] > 0.06


def test_the_numbers_do_not_depend_on_the_camera_resolution(
    u1: str, tmp_path: Path
) -> None:
    img = cv2.imread(str(DATA / "u1_f7.jpg"))
    big = cv2.resize(img, (1920, 1080), interpolation=cv2.INTER_LINEAR)
    ok, buf = cv2.imencode(".jpg", big, [cv2.IMWRITE_JPEG_QUALITY, 90])
    assert ok
    a, b = read("u1_f7.jpg", u1), PQ.quality_numbers(bytes(buf), name=u1, z=0.6)
    assert (
        abs(a["fleck_frac"] - b["fleck_frac"]) < 0.03
        and abs(a["order"] - b["order"]) < 0.05
    )


@pytest.mark.parametrize(
    "name", ["photo_599472757.jpg", "photo_599472758.jpg", "photo_599472764.jpg"]
)
def test_phone_photos_of_the_failed_first_layer_read_far_below_a_clean_first_layer(
    name: str,
) -> None:
    """Whole-frame look (no camera calibration for a phone): the beads are a mess, `order` is far under the
    first-layer floor, a clean first layer reads 0.83+."""
    n = whole(name)
    assert n["status"] == "ok" and n["region_source"] == "whole frame"
    assert n["order"] < Q.QualityConfig().first_layer_order
    issue, level = Q.absolute_verdict(n, 0.2, Q.QualityConfig())
    assert issue == Q.FIRST_LAYER and level in (Q.WARNING, Q.SEVERE)


def synth(white_specks: int = 0, lines: bool = True, seed: int = 1) -> bytes:
    """A 640x360 beige surface of parallel beads, optionally peppered with white specks."""
    rng = np.random.default_rng(seed)
    img = np.full((360, 640, 3), (120, 165, 205), np.uint8)  # BGR beige
    if lines:
        for y in range(0, 360, 9):
            img[y : y + 3] = (
                104,
                148,
                188,
            )  # shallow gaps between beads: far under the fleck threshold
    for _ in range(white_specks):
        x, y = int(rng.integers(10, 630)), int(rng.integers(10, 350))
        cv2.circle(img, (x, y), int(rng.integers(1, 4)), (245, 245, 245), -1)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return bytes(buf)


def test_synthetic_white_specks_raise_fleck_frac_and_lost_lines_lower_order() -> None:
    clean = PQ.quality_numbers(synth(0), name=None, z=0.0)
    specks = PQ.quality_numbers(synth(250), name=None, z=0.0)
    mush = PQ.quality_numbers(synth(0, lines=False), name=None, z=0.0)
    assert clean["fleck_frac"] < 0.005 and specks["fleck_frac"] > 0.015
    assert clean["order"] > 0.9 and specks["order"] < clean["order"]
    assert mush["order"] < clean["order"], (
        "an even surface with no bead pattern has nothing to be ordered"
    )


def test_a_region_that_is_too_small_or_a_broken_frame_is_blind_never_clean() -> None:
    tiny = PQ.quality_numbers(
        synth(0), name=None, z=0.0, roi=[(0.1, 0.1), (0.12, 0.1), (0.12, 0.12)]
    )
    assert tiny["status"] == "blind" and "under" in tiny["reason"]
    broken = PQ.quality_numbers(b"not a jpeg", name=None, z=0.0)
    assert broken["status"] == "blind" and "decodable" in broken["reason"]


def test_roi_parsing_names_the_bad_part() -> None:
    assert PQ.parse_roi("0,0;1,0;1,1") == [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]
    for bad in ("0,0;1,0", "0,0;2,0;1,1", "a,b;c,d;e,f"):
        with pytest.raises(ValueError):
            PQ.parse_roi(bad)


def test_the_annotated_frame_marks_the_debris_in_red_and_is_a_jpeg(
    u1: str, tmp_path: Path
) -> None:
    out = tmp_path / "a" / "marked.jpg"
    n = PQ.quality_numbers(
        (DATA / "u1_f7.jpg").read_bytes(), name=u1, z=0.6, annotated=out
    )
    assert n["annotated"] == str(out) and out.read_bytes()[:2] == b"\xff\xd8"
    marked = cv2.imread(str(out))
    assert (marked[..., 2].astype(int) - marked[..., 0].astype(int) > 150).sum() > 200


def test_the_command_line_prints_one_json_object_and_help(
    u1: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        PQ.main(
            ["check", u1, "--frame", str(DATA / "u1_f5.jpg"), "--z", "0.6", "--json"]
        )
        == 0
    )
    doc = json.loads(capsys.readouterr().out)
    assert doc["status"] == "ok" and {
        "fleck_frac",
        "fleck_per10k",
        "order",
        "luma",
        "region_px",
    } <= set(doc)
    assert (
        PQ.main(
            [
                "check",
                u1,
                "--frame",
                str(DATA / "photo_599472757.jpg"),
                "--roi",
                "whole",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["region_source"] == "whole frame"
    assert (
        PQ.main(["--help"]) == 0
        and "print_quality.py check NAME" in capsys.readouterr().out
    )
    assert PQ.main([]) == 1


# ---- the table of the calibration, end to end through the judge --------------------------------


def test_verdict_table_of_the_session_frames_through_the_judge(u1: str) -> None:
    """f1 empty (reference, not judged); the first layer f5 four times = the baseline; then the debris frames in
    the order they happened. Nothing before the debris, a WARNING on it, never above WARNING-severity events."""
    judge = Q.QualityJudge(Q.QualityConfig())
    rows = []
    sequence = [("u1_f1.jpg", False, 1)] + [("u1_f5.jpg", True, 1)] * 5
    sequence += [
        ("u1_f7.jpg", True, 3),
        ("u1_f10.jpg", True, 3),
        ("u1_f12.jpg", True, 4),
        ("u1_f12.jpg", True, 4),
    ]
    for i, (name, present, layer) in enumerate(sequence):
        v = judge.feed(
            Q.QualityReading(60.0 * i, read(name, u1), layer=layer, present=present)
        )
        rows.append((name, v.state, v.level, [f.kind for f in v.new]))
    assert [r[1] for r in rows[:6]] == [
        "absent",
        "learning",
        "learning",
        "learning",
        "learning",
        "ok",
    ]
    assert all(r[2] == "ok" for r in rows[:6])
    warned = [r for r in rows if r[3]]
    assert [r[3] for r in warned] == [["quality_stringing_warning"]]
    assert judge.report.worst is not None
    assert judge.report.max_level == "warning" and judge.report.worst["layer"] == 3
    assert all(f["level"] in ("warning", "severe") for f in judge.report.alerts)
