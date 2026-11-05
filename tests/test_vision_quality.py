"""vision_quality.py — the print-quality tier of `3d printer vision` on REAL U1 frames (tests/data/quality).

`u1_f1` is the empty plate (the print's first frame), `u1_f5` the clean first layer, `u1_f7` / `u1_f10` / `u1_f12`
the stringing debris of layers 2-4 that nothing flagged on 2026-11-05. The tier is fed the way FrameScorer feeds
it: a decoded frame, the print context (Z), a clock. No printer, no camera, no model, no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from printer_ops.vision import PrintContext

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
import vision_quality as VQ  # noqa: E402

DATA = Path(__file__).parent / "data" / "quality"
DEVICES = """u1:
  adapter: moonraker
  host: 127.0.0.1
  port: 1
  model: Snapmaker U1
"""


@pytest.fixture
def device(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from printer_ops.devices import get_device

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    cfg = tmp_path / "config" / "3d-cli"
    cfg.mkdir(parents=True)
    (cfg / "printer-devices.yaml").write_text(DEVICES, encoding="utf-8")
    return get_device("u1")


def frame(name: str) -> Any:
    return cv2.imread(str(DATA / name))


def ctx(z: float) -> PrintContext:
    return PrintContext("sheet.gcode", z, 100.0, 100.0)


def feed_first_layer(tier: VQ.QualityTier) -> None:
    """The empty plate, then four readings of the clean first layer: the job's baseline."""
    now = 1000.0
    assert tier.update(frame("u1_f1.jpg"), ctx(0.2), now) == []
    for i in range(1, 5):
        assert tier.update(frame("u1_f5.jpg"), ctx(0.2), now + 15 * i) == []
    assert tier.judge.base is not None and tier.judge.base.n == 4


def test_stringing_on_real_frames_becomes_one_warning_finding_with_the_marked_frame(
    device, tmp_path: Path
) -> None:
    tier = VQ.QualityTier(device, VQ.QualityConfig(), tmp_path / "evidence")
    feed_first_layer(tier)
    found = []
    for i, name in enumerate(("u1_f7.jpg", "u1_f10.jpg", "u1_f12.jpg", "u1_f12.jpg")):
        found += tier.update(frame(name), ctx(1.0), 1200.0 + 60 * i)
    (f,) = found
    assert f.kind == "quality_stringing_warning" and f.severity == "warning"
    assert "белые ниточки" in f.text and "Что менять" in f.text
    assert (
        f.image is not None
        and cv2.imdecode(np.frombuffer(f.image, np.uint8), cv2.IMREAD_COLOR) is not None
    )
    assert (tmp_path / "evidence" / "u1-quality-worst.jpg").is_file()


def test_the_clean_first_layer_and_the_empty_plate_raise_nothing(device) -> None:
    tier = VQ.QualityTier(device, VQ.QualityConfig())
    feed_first_layer(tier)
    for i in range(5):
        assert tier.update(frame("u1_f5.jpg"), ctx(0.4), 1100.0 + 15 * i) == []
    assert (
        tier.judge.report.per_level["warning"] == 0
        and tier.judge.report.per_level["severe"] == 0
    )
    assert tier.judge.report.skipped.get("absent") == 1, (
        "the empty first frame is the reference, not a reading"
    )


def test_no_print_context_is_blind_not_clean(device) -> None:
    tier = VQ.QualityTier(device, VQ.QualityConfig())
    assert tier.update(frame("u1_f12.jpg"), None, 0.0) == []
    assert (
        tier.once_report()["status"] == "blind" and "Z" in tier.once_report()["reason"]
    )


def test_a_new_print_writes_the_report_of_the_one_before(
    device, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tier = VQ.QualityTier(device, VQ.QualityConfig(), tmp_path / "evidence")
    feed_first_layer(tier)
    for i, name in enumerate(("u1_f7.jpg", "u1_f10.jpg", "u1_f12.jpg")):
        tier.update(frame(name), ctx(1.0), 1200.0 + 60 * i)
    tier.reset_print()
    (d,) = (tmp_path / "evidence").glob("*-u1-monitor-report")
    doc = json.loads((d / "report.json").read_text())
    assert (
        doc["max_level"] == "warning"
        and doc["per_issue"]["stringing"] >= 2
        and (d / "worst-frame.jpg").is_file()
    )
    assert "print-quality monitor:" in capsys.readouterr().err
    assert tier.judge.report.readings == 0 and tier.base is None, (
        "the next print starts from nothing"
    )
    tier.finish()
    assert len(list((tmp_path / "evidence").glob("*-u1-monitor-report"))) == 1, (
        "an empty print writes no report"
    )


@pytest.mark.parametrize(
    ("name", "verdict"),
    [
        ("u1_f5.jpg", None),
        ("u1_f7.jpg", "severe"),
        ("u1_f10.jpg", "warning"),
        ("u1_f12.jpg", "warning"),
    ],
)
def test_once_gives_the_absolute_verdict_of_one_frame(
    device, name: str, verdict: str | None
) -> None:
    """`printer vision NAME --once`: no job history, so only the absolute levels; it says so."""
    tier = VQ.QualityTier(device, VQ.QualityConfig(), once=True)
    found = tier.update(frame(name), ctx(1.2), 0.0)
    report = tier.once_report()
    assert "absolute" in report["basis"]
    assert [f.kind for f in found] == (
        [] if verdict is None else [f"quality_stringing_{verdict}"]
    )
    assert report["fleck_frac"] > 0


def test_a_disabled_tier_is_not_built(device) -> None:
    assert VQ.build_quality(device, None, once=False, disabled=True) is None
    assert VQ.build_quality(device, None, once=False, disabled=False) is not None


class _NoSpaghetti:
    """Stage 1 as it behaved on the day: no box on any frame (p 0.0)."""

    def score_bgr(self, bgr: Any) -> Any:
        from printer_ops.vision import FrameScore

        return FrameScore(p=0.0, boxes=[], luma=70.0)


def test_the_frame_scorer_adds_the_quality_findings_next_to_a_silent_spaghetti_model(
    device, tmp_path: Path
) -> None:
    """The 2026-11-05 situation: p 0.0 / q 0.0 / no boxes — and now the same frame still says stringing."""
    import vision_detect as VD

    tier = VQ.QualityTier(device, VQ.QualityConfig(), tmp_path, once=True)
    scorer = VD.FrameScorer(cast(Any, _NoSpaghetti()), quality=tier)
    scorer.context = ctx(1.2)
    score = scorer((DATA / "u1_f7.jpg").read_bytes())
    assert score.p == 0.0 and score.boxes == []
    assert [f.kind for f in score.findings] == ["quality_stringing_severe"]
    scorer.context = ctx(1.2)
    assert scorer((DATA / "u1_f5.jpg").read_bytes()).findings == []


def test_once_report_carries_the_quality_block(device, tmp_path: Path) -> None:
    import argparse
    from types import SimpleNamespace

    import vision_detect as VD

    tier = VQ.QualityTier(device, VQ.QualityConfig(), tmp_path, once=True)
    scorer = VD.FrameScorer(cast(Any, _NoSpaghetti()), quality=tier)
    scorer.context = ctx(1.2)
    score = scorer((DATA / "u1_f12.jpg").read_bytes())
    rep = SimpleNamespace(
        frame=None,
        score=score,
        events=[],
        as_json=lambda name: {"printer": name},
    )
    monitor = SimpleNamespace(tracker=SimpleNamespace(adjusted=0.0))
    out = VD._once_report(argparse.Namespace(save=None), scorer, monitor, rep, "u1")
    assert out["findings"] == ["quality_stringing_warning"]
    assert out["quality"]["level"] == "warning" and out["quality"]["fleck_frac"] > 0.035
    assert "absolute" in out["quality"]["basis"]


def test_the_scorer_resets_and_finishes_the_tier_with_the_print(
    device, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import vision_detect as VD

    tier = VQ.QualityTier(device, VQ.QualityConfig(), tmp_path)
    scorer = VD.FrameScorer(cast(Any, _NoSpaghetti()), quality=tier)
    for i in range(5):
        scorer.context = ctx(0.2)
        scorer.now = 100.0 + 15 * i
        scorer(frame_jpeg("u1_f1.jpg" if i == 0 else "u1_f5.jpg"))
    scorer.finish_print()
    assert len(list(tmp_path.glob("*-u1-monitor-report"))) == 1
    assert "print-quality monitor:" in capsys.readouterr().err
    scorer.finish_print()
    assert len(list(tmp_path.glob("*-u1-monitor-report"))) == 1, (
        "ending twice writes one report"
    )


def frame_jpeg(name: str) -> bytes:
    return (DATA / name).read_bytes()
