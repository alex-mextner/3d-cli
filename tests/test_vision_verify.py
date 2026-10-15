"""Stage 2 of `3d printer vision` (issue https://github.com/alex-mextner/3d-cli/issues/150): the
CLIP crop verifier (lib/vision_verify.py), the masked/verified frame pipeline and eval metrics in
lib/vision_detect.py, and the verified/vetoed events of VisionMonitor. No model weights: fakes
stand in for Obico and CLIP; the prompt embeddings file is the real one."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from printer_ops import vision
from printer_ops.vision import INFO, WARNING, Box, Frame, FrameScore, VisionMonitor

JPEG = b"\xff\xd8" + b"\x00" * 64 + b"\xff\xd9"
NOW = datetime(2026, 10, 14, 19, 22, tzinfo=timezone.utc).timestamp()


def _frames(n: int) -> list[Frame]:
    return [Frame(NOW + i * 10.0, JPEG + bytes([i % 250]), None) for i in range(n)]


@dataclass
class _Verified:
    """Scorer fake: raw p and verified q per frame."""

    values: list[tuple[float, float]]

    def __call__(self, jpeg: bytes) -> FrameScore:
        p, q = self.values.pop(0) if self.values else (0.0, 0.0)
        return FrameScore(p, [Box(0.1, 0.2, 0.3, 0.4, 0.4)], 90.0, [0.1], q)


def test_signal_is_the_verified_score_when_there_is_one() -> None:
    assert FrameScore(0.9).signal == 0.9
    assert FrameScore(0.9, q=0.05).signal == 0.05


def test_texture_that_fools_the_detector_becomes_one_info_veto_not_a_warning() -> None:
    pytest.importorskip("printer_ops.watch")
    # the 2026-10-14 U1 walls: raw p ~1.0 on speckled grey walls, the verifier says texture
    mon = VisionMonitor("u1", _Verified([(0.0, 0.0)] * 10 + [(1.0, 0.05)] * 30))
    mon.set_job("printing", "walls.gcode")
    events = [e for f in _frames(40) for e in mon.step(f)[1]]
    assert [(e.kind, e.severity) for e in events] == [("vision_spaghetti_vetoed", INFO)]
    assert events[0].data["q"] == 0.05
    assert events[0].data["boxes"][0]["verified"] == 0.1


def test_verified_spaghetti_warns_and_is_not_reported_as_vetoed() -> None:
    pytest.importorskip("printer_ops.watch")
    mon = VisionMonitor("u1", _Verified([(0.0, 0.0)] * 10 + [(2.5, 2.0)] * 5))
    mon.set_job("printing", "house.gcode")
    events = [e for f in _frames(15) for e in mon.step(f)[1]]
    assert [e.kind for e in events][:1] == ["vision_spaghetti"]
    assert events[0].severity == WARNING
    assert "verified score 2.00 of raw 2.50" in events[0].text
    assert "vision_spaghetti_vetoed" not in {e.kind for e in events}


class _Detector:
    def __init__(self, boxes: list[Box]) -> None:
        self.boxes = boxes

    def score_bgr(self, bgr: object) -> FrameScore:
        return FrameScore(sum(b.conf for b in self.boxes), list(self.boxes), 77.0)


class _Clip:
    def __init__(self) -> None:
        self.seen: list[int] = []

    def box_probs(self, bgr: object, boxes: list[Box]) -> list[float]:
        self.seen.append(len(boxes))
        return [0.5] * len(boxes)


def test_frame_scorer_drops_masked_boxes_before_verifying() -> None:
    np = pytest.importorskip("numpy")
    import vision_detect

    boxes = [Box(0.0, 0.0, 0.2, 0.2, 0.4), Box(0.6, 0.6, 0.8, 0.8, 0.3)]
    clip = _Clip()
    scorer = vision_detect.FrameScorer(
        _Detector(boxes),  # type: ignore[arg-type]
        clip,
        ignore=[[(0.0, 0.0), (0.3, 0.0), (0.3, 0.3), (0.0, 0.3)]],  # the dock corner
    )
    img = np.zeros((90, 160, 3), np.uint8)
    s = scorer.score_bgr(img)
    assert (s.masked, s.p, s.probs, clip.seen) == (1, 0.3, [0.5], [1])
    assert s.q == pytest.approx(0.15)
    roi = np.zeros(
        (9, 16), np.uint8
    )  # nothing allowed: every box is outside the bed volume
    scorer.roi = roi
    s = scorer.score_bgr(img)
    assert (s.masked, s.p, s.q) == (2, 0.0, 0.0)


def test_build_scorer_rejects_a_malformed_ignore_polygon() -> None:
    import vision_detect
    from errors import UsageError

    with pytest.raises(UsageError, match="watch.vision.ignore"):
        vision_detect.build_scorer({"ignore": [[1, 2, 3]]}, verify=False)


@pytest.mark.parametrize(
    "poly",
    [
        [[-1, -1], [2, -1], [2, 2], [-1, 2]],  # covers the whole frame: detection off
        [[0.1, 0.1], [0.2, 0.2]],  # not an area
    ],
)
def test_build_scorer_rejects_ignore_polygons_outside_the_frame(poly: list[list[float]]) -> None:
    import vision_detect
    from errors import UsageError

    with pytest.raises(UsageError, match="0..1"):
        vision_detect.build_scorer({"ignore": [poly]}, verify=False)


def test_a_failed_context_poll_drops_the_previous_bed_mask() -> None:
    np = pytest.importorskip("numpy")
    import vision_detect
    from printer_ops.vision import PrintContext

    class _Geometry:
        def update(self, bgr: object, ctx: object, now: float) -> tuple[object, list[object]]:
            return np.zeros((9, 16), np.uint8), []  # nothing inside the bed volume

    scorer = vision_detect.FrameScorer(
        _Detector([Box(0.4, 0.4, 0.6, 0.6, 0.9)]),  # type: ignore[arg-type]
        geometry=_Geometry(),  # type: ignore[arg-type]
    )
    img = np.zeros((90, 160, 3), np.uint8)
    scorer.context = PrintContext("job.gcode", 1.0, 0.0, 0.0)
    assert scorer.score_bgr(img).p == 0.0
    scorer.context = None  # the next poll failed: no mask, not the stale one
    assert scorer.score_bgr(img).p == pytest.approx(0.9)


@pytest.mark.parametrize(
    ("cfg", "flag", "on"),
    [({}, False, False), ({"scene": True}, False, True), ({}, True, True)],
)
def test_bed_change_detectors_are_opt_in(
    monkeypatch: pytest.MonkeyPatch, cfg: dict[str, bool], flag: bool, on: bool
) -> None:
    pytest.importorskip("numpy")
    import vision_detect

    monkeypatch.setattr(vision_detect, "ensure_model", lambda: "model.onnx")
    monkeypatch.setattr(vision_detect, "ObicoScorer", lambda path: path)
    scorer = vision_detect.build_scorer(cfg, verify=False, camera=object(), scene=flag)
    assert scorer.geometry is not None
    assert (scorer.geometry.scene is not None, scorer.geometry.growth is not None) == (on, on)


def test_crop_box_is_square_inside_the_frame_and_has_context() -> None:
    np = pytest.importorskip("numpy")
    import vision_verify

    img = np.zeros((1080, 1920, 3), np.uint8)
    c = vision_verify.crop_box(img, (0.98, 0.98, 1.0, 1.0))  # a tiny box in the corner
    assert c.shape[0] == c.shape[1] == round(vision_verify.CROP_MIN * 1080)
    big = vision_verify.crop_box(img, (0.0, 0.0, 1.0, 1.0))
    assert big.shape[:2] == (1080, 1080)


def test_verified_score_weights_confidence_by_probability() -> None:
    import vision_verify

    boxes = [Box(0, 0, 1, 1, 0.5), Box(0, 0, 1, 1, 0.25), Box(0, 0, 1, 1, 0.1)]
    assert vision_verify.verified_score(boxes, [1.0, 0.2]) == pytest.approx(0.55)


def test_prompt_file_scores_its_own_failure_prompt_as_failure() -> None:
    np = pytest.importorskip("numpy")
    import vision_verify

    ps = vision_verify.PromptSet.load()
    assert ps.classes == ("failure", "healthy") and ps.positive == {"failure"}
    assert ps.emb.shape == (len(ps.prompts), 512)
    fail = ps.emb[[i for i, c in enumerate(ps.owner) if c == 0][0]][None]
    ok = ps.emb[[i for i, c in enumerate(ps.owner) if c == 1][0]][None]
    assert ps.prob(fail)[0] > 0.9 > 0.1 > ps.prob(ok)[0]
    assert np.all((ps.prob(np.vstack([fail, ok])) >= 0) & (ps.prob(fail) <= 1))


def test_prompt_file_matches_the_task_definition() -> None:
    import json

    import vision_verify

    d = json.loads(vision_verify.PROMPTS.read_text())
    assert d["tasks"] == vision_verify.TASKS
    wanted = {
        p for t in d["tasks"].values() for ps in t["prompts"].values() for p in ps
    }
    assert set(d["embeddings"]) == wanted


def test_metrics_report_verified_frames_and_false_alarms_per_day() -> None:
    import vision_detect

    res = [
        vision_detect.SeqResult(
            "pos", "pos", [2.5, 2.5], [None, WARNING], [], [2.0, 2.0]
        ),
        # 8640 healthy frames at 10 s = one printing day with one false alarm episode
        vision_detect.SeqResult(
            "neg",
            "neg",
            [0.9] + [0.0] * 8639,
            [WARNING, WARNING] + [None] * 8638,
            [],
            [0.05] + [0.0] * 8639,
        ),
    ]
    m = vision_detect.metrics(res)
    assert m["per_frame"]["0.5"]["fp"] == 1
    assert m["per_frame_verified"]["0.5"]["fp"] == 0
    assert m["per_frame_verified"]["0.5"]["tp"] == 2
    assert (m["healthy_days"], m["false_alarms_per_day"]) == (1.0, 1.0)


def test_evaluate_lead_in_lets_a_failure_clip_alert_after_warmup(
    tmp_path: Path,
) -> None:
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    import vision_detect

    for i in range(3):
        cv2.imwrite(str(tmp_path / f"{i:03d}.jpg"), np.zeros((8, 8, 3), np.uint8))

    def score(img: object) -> FrameScore:
        return FrameScore(2.5, q=2.0)

    (cold,) = vision_detect.evaluate([(tmp_path, "pos")], score)
    (warm,) = vision_detect.evaluate([(tmp_path, "pos")], score, lead_in=60)
    assert cold.first_alert is None  # inside the 6-frame warm-up
    assert (
        warm.first_alert == 1
    )  # EWM: the second failing frame crosses the alert level
    assert warm.verified == [2.0, 2.0, 2.0]


def test_vision_constants_keep_their_names() -> None:
    assert (vision.INFO, vision.WARNING, vision.CRITICAL) == (
        "info",
        "warning",
        "critical",
    )
