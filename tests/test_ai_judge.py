"""Unit tests for the VLM-judge harness (lib/ai/judge.py).

NO real model calls, NO network. Sighted judges are exercised with a deterministic
in-test Backend double that returns canned rubric JSON (a stand-in for a
vision-capable, temperature>0 backend); the blind (text-only) path uses the real
MockBackend (supports_images=False). The pure parsing / stability / aggregation math
is tested in isolation from any backend.

What these tests PROVE (and what they do not):
  - rubric JSON is parsed robustly (fences, prose, out-of-range clamp) and the mean is
    recomputed, never trusted from the model;
  - the stability flag fires when N samples disagree by more than the threshold (the
    FlipFlop detector) and stays quiet when they agree;
  - >=2 distinct judges are aggregated and cross-judge spread is measured;
  - position-swap averaging is applied for pairwise A/B;
  - a supports_images=False backend is surfaced as BLIND and never reported as a real
    visual score.
They do NOT prove the judge scores REAL renders well — a canned mock says nothing about
whether a real VLM's rubric reads match a human's. That needs a labelled photo set.
"""
from __future__ import annotations

import pathlib

import pytest

from ai.backends import Backend, MockBackend
from ai.judge import (
    DEFAULT_RUBRIC,
    JudgeError,
    PairwiseResult,
    VisualScore,
    _extract_json_object,
    aggregate_dims,
    cross_judge_spread,
    judge,
    judge_pairwise,
    parse_rubric_response,
    stability_report,
)

# A throwaway path — no backend here reads image bytes, so it need not exist.
_RENDER = pathlib.Path("/tmp/render.png")
_REFERENCE = pathlib.Path("/tmp/reference.png")


def _score_json(sil: int, feat: int, struct: int, detail: int, *, mean: object = None) -> str:
    obj = {
        "silhouette_proportion": sil,
        "feature_completeness": feat,
        "structural_correctness": struct,
        "detail_fidelity": detail,
        "rationale": "canned",
    }
    if mean is not None:
        obj["mean"] = mean  # intentionally wrong — the harness must ignore it
    import json

    return json.dumps(obj)


class _FakeVisionBackend(Backend):
    """A deterministic SIGHTED judge double. `responses` may be a single string (returned
    every call) or a list consumed FIFO. Implements the real Backend interface so the
    judge's parsing/plumbing is exercised, not a mock's own behaviour."""

    supports_images = True

    def __init__(
        self,
        responses: str | list[str],
        *,
        name: str = "fake",
        model: str | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self._constant = responses if isinstance(responses, str) else None
        self._queue = list(responses) if isinstance(responses, list) else []
        self.calls = 0

    def available(self) -> bool:
        return True

    def complete(
        self,
        system: str,
        user: str,
        images: list[pathlib.Path] | None = None,
        timeout: float = 1200.0,
    ) -> str:
        self.calls += 1
        if self._constant is not None:
            return self._constant
        if not self._queue:
            raise AssertionError("FakeVisionBackend queue exhausted")
        return self._queue.pop(0)


# ── Pure parsing ─────────────────────────────────────────────────────────────
def test_parse_recomputes_mean_and_ignores_model_mean() -> None:
    score = parse_rubric_response(_score_json(4, 2, 3, 3, mean=99), DEFAULT_RUBRIC)
    assert score.dims == {
        "silhouette_proportion": 4.0,
        "feature_completeness": 2.0,
        "structural_correctness": 3.0,
        "detail_fidelity": 3.0,
    }
    assert score.mean == 3.0  # (4+2+3+3)/4 — NOT the model's bogus 99


def test_parse_extracts_json_from_prose_and_fences() -> None:
    noisy = "Sure! Here is my assessment:\n```json\n" + _score_json(1, 1, 1, 1) + "\n```\nThanks."
    score = parse_rubric_response(noisy, DEFAULT_RUBRIC)
    assert score.mean == 1.0


def test_parse_clamps_out_of_range_scores() -> None:
    score = parse_rubric_response(_score_json(9, -3, 2, 2), DEFAULT_RUBRIC)
    assert score.dims["silhouette_proportion"] == 4.0  # clamped from 9
    assert score.dims["feature_completeness"] == 0.0  # clamped from -3


def test_parse_missing_dimension_raises() -> None:
    import json

    bad = json.dumps({"silhouette_proportion": 3, "feature_completeness": 3})
    with pytest.raises(JudgeError, match="missing rubric dimension"):
        parse_rubric_response(bad, DEFAULT_RUBRIC)


def test_parse_boolean_score_raises() -> None:
    # float(True) == 1.0 would silently accept a bool; it must be rejected as malformed.
    bad = _score_json(3, 3, 3, 3).replace('"detail_fidelity": 3', '"detail_fidelity": true')
    with pytest.raises(JudgeError, match="boolean"):
        parse_rubric_response(bad, DEFAULT_RUBRIC)


def test_parse_json_with_brace_inside_a_string_still_parses() -> None:
    import json

    obj = {
        "silhouette_proportion": 2,
        "feature_completeness": 2,
        "structural_correctness": 2,
        "detail_fidelity": 2,
        "rationale": "the left edge is off {see the marked spot}",
    }
    score = parse_rubric_response("here: " + json.dumps(obj), DEFAULT_RUBRIC)
    assert score.mean == 2.0


def test_parse_non_numeric_raises() -> None:
    bad = _score_json(3, 3, 3, 3).replace('"detail_fidelity": 3', '"detail_fidelity": "high"')
    with pytest.raises(JudgeError, match="not a number"):
        parse_rubric_response(bad, DEFAULT_RUBRIC)


def test_extract_json_no_object_raises() -> None:
    with pytest.raises(JudgeError, match="no parseable JSON"):
        _extract_json_object("there is no json here, only prose")


# ── Pure stability / aggregation math ────────────────────────────────────────
def test_stability_stable_when_samples_agree() -> None:
    samples = [parse_rubric_response(_score_json(3, 3, 3, 3), DEFAULT_RUBRIC) for _ in range(5)]
    unstable, mean_range, per_dim = stability_report(samples, DEFAULT_RUBRIC)
    assert unstable is False
    assert mean_range == 0.0
    assert all(v == 0.0 for v in per_dim.values())


def test_stability_flags_when_mean_diverges() -> None:
    samples = [
        parse_rubric_response(_score_json(4, 4, 4, 4), DEFAULT_RUBRIC),
        parse_rubric_response(_score_json(1, 1, 1, 1), DEFAULT_RUBRIC),
    ]
    unstable, mean_range, _ = stability_report(samples, DEFAULT_RUBRIC)
    assert unstable is True
    assert mean_range == 3.0


def test_stability_flags_single_dimension_flip_even_if_mean_steady() -> None:
    # Means are close (3.0 vs 2.25) but detail_fidelity flips 4 -> 1 (range 3 > 1).
    samples = [
        parse_rubric_response(_score_json(2, 3, 3, 4), DEFAULT_RUBRIC),  # mean 3.0
        parse_rubric_response(_score_json(4, 3, 3, 1), DEFAULT_RUBRIC),  # mean 2.75
    ]
    unstable, _, per_dim = stability_report(samples, DEFAULT_RUBRIC)
    assert unstable is True
    assert per_dim["detail_fidelity"] == 3.0


def test_stability_boundary_exactly_one_point_stays_stable() -> None:
    # The doc says flag when samples disagree by MORE THAN 1 point (strict >). A range of
    # exactly 1.0 must NOT flag.
    samples = [
        parse_rubric_response(_score_json(3, 3, 3, 3), DEFAULT_RUBRIC),  # mean 3.0
        parse_rubric_response(_score_json(4, 4, 4, 4), DEFAULT_RUBRIC),  # mean 4.0
    ]
    unstable, mean_range, per_dim = stability_report(samples, DEFAULT_RUBRIC)
    assert mean_range == 1.0
    assert all(v == 1.0 for v in per_dim.values())
    assert unstable is False


def test_cross_judge_spread_none_for_single_judge() -> None:
    one = [parse_rubric_response(_score_json(3, 3, 3, 3), DEFAULT_RUBRIC)]
    assert cross_judge_spread(one) is None


def test_cross_judge_spread_range_of_means() -> None:
    scores = [
        parse_rubric_response(_score_json(4, 4, 4, 4), DEFAULT_RUBRIC),  # mean 4
        parse_rubric_response(_score_json(1, 1, 1, 1), DEFAULT_RUBRIC),  # mean 1
    ]
    assert cross_judge_spread(scores) == 3.0


def test_aggregate_dims_means_per_dimension() -> None:
    scores = [
        parse_rubric_response(_score_json(4, 2, 0, 4), DEFAULT_RUBRIC),
        parse_rubric_response(_score_json(2, 2, 4, 0), DEFAULT_RUBRIC),
    ]
    agg = aggregate_dims(scores, DEFAULT_RUBRIC)
    assert agg == {
        "silhouette_proportion": 3.0,
        "feature_completeness": 2.0,
        "structural_correctness": 2.0,
        "detail_fidelity": 2.0,
    }


# ── judge() orchestration ────────────────────────────────────────────────────
def test_judge_two_stable_agreeing_judges_labels_ok() -> None:
    b1 = _FakeVisionBackend(_score_json(3, 3, 3, 3), name="j1", model="m1")
    b2 = _FakeVisionBackend(_score_json(3, 3, 3, 3), name="j2", model="m2")
    result = judge(_RENDER, _REFERENCE, backend=[b1, b2], judges=2, stability_n=3)
    assert isinstance(result, VisualScore)
    assert result.label == "ok"
    assert result.mean == 3.0
    assert result.blind is False
    assert result.single_judge is False
    assert result.stability_unstable is False
    assert result.cross_judge_spread == 0.0
    assert len(result.judges) == 2
    assert result.judges[0].model == "m1"


def test_judge_multi_judge_aggregation_and_spread() -> None:
    b1 = _FakeVisionBackend(_score_json(4, 4, 4, 4), name="j1")  # mean 4
    b2 = _FakeVisionBackend(_score_json(2, 2, 2, 2), name="j2")  # mean 2
    result = judge(_RENDER, _REFERENCE, backend=[b1, b2], judges=2, stability_n=0)
    assert result.mean == 3.0  # aggregate of the two canonical means
    assert result.per_dim["silhouette_proportion"] == 3.0
    assert result.cross_judge_spread == 2.0
    assert result.cross_judge_low_agreement is True  # 2.0 > threshold 1.0
    assert result.label == "low-confidence"


def test_judge_flags_unstable_when_samples_vary() -> None:
    # Canonical steady, but the stability samples flip a dimension hard.
    responses = [
        _score_json(4, 4, 4, 4),  # canonical
        _score_json(4, 4, 4, 4),
        _score_json(0, 4, 4, 4),  # silhouette flips 4 -> 0
        _score_json(4, 4, 4, 4),
    ]
    b = _FakeVisionBackend(responses, name="jitter")
    result = judge(_RENDER, _REFERENCE, backend=b, judges=1, stability_n=3)
    assert result.stability_unstable is True
    assert result.label == "low-confidence"
    assert any("stability" in n for n in result.notes)


def test_judge_single_sighted_judge_is_low_confidence() -> None:
    b = _FakeVisionBackend(_score_json(3, 3, 3, 3), name="solo")
    result = judge(_RENDER, _REFERENCE, backend=b, judges=2, stability_n=2)
    assert result.single_judge is True
    assert result.label == "low-confidence"
    assert result.cross_judge_spread is None
    assert any("single sighted judge" in n for n in result.notes)


def test_judge_dedupes_identical_backends_so_agreement_is_not_faked() -> None:
    # Two DISTINCT instances of the SAME (name, model) must collapse to one judge — else
    # they trivially "agree" and fake a high-confidence 'ok'.
    dup1 = _FakeVisionBackend(_score_json(3, 3, 3, 3), name="claude", model="opus")
    dup2 = _FakeVisionBackend(_score_json(3, 3, 3, 3), name="claude", model="opus")
    result = judge(_RENDER, _REFERENCE, backend=[dup1, dup2], judges=2, stability_n=0)
    assert len(result.judges) == 1  # deduped
    assert result.single_judge is True
    assert result.label == "low-confidence"


def test_judge_keeps_full_explicit_panel_without_truncation() -> None:
    b1 = _FakeVisionBackend(_score_json(3, 3, 3, 3), name="j1")
    b2 = _FakeVisionBackend(_score_json(3, 3, 3, 3), name="j2")
    b3 = _FakeVisionBackend(_score_json(3, 3, 3, 3), name="j3")
    # judges=2 must NOT drop the third explicitly-supplied judge.
    result = judge(_RENDER, _REFERENCE, backend=[b1, b2, b3], judges=2, stability_n=0)
    assert len(result.judges) == 3


def test_judge_blind_backend_is_never_a_real_visual_score() -> None:
    blind = MockBackend(_score_json(4, 4, 4, 4))  # supports_images=False
    result = judge(_RENDER, _REFERENCE, backend=blind, judges=1, stability_n=1)
    assert result.blind is True
    assert result.label == "blind"
    assert result.judges[0].blind is True
    assert result.judges[0].supports_images is False
    assert any("BLIND" in n for n in result.notes)


def test_judge_excludes_blind_judge_from_sighted_aggregate() -> None:
    sighted = _FakeVisionBackend(_score_json(2, 2, 2, 2), name="see")  # mean 2
    blind = MockBackend(_score_json(4, 4, 4, 4))  # would pull the mean to 3 if counted
    result = judge(_RENDER, _REFERENCE, backend=[sighted, blind], judges=2, stability_n=0)
    assert result.blind is False  # a sighted judge exists
    assert result.mean == 2.0  # blind judge's 4s excluded
    assert any("excluded blind" in n for n in result.notes)


# ── judge_pairwise() comparative position-swap ───────────────────────────────
def _pw_json(winner: str, margin: int = 3) -> str:
    import json

    return json.dumps({"winner": winner, "margin": margin, "rationale": "canned"})


def test_pairwise_consistent_winner_across_orders() -> None:
    # Order (A,B): winner=first -> A. Order (B,A): winner=second -> A. Both name A.
    b = _FakeVisionBackend([_pw_json("first", 4), _pw_json("second", 2)], name="ab")
    res = judge_pairwise(_RENDER, pathlib.Path("/tmp/b.png"), _REFERENCE, backend=b)
    assert isinstance(res, PairwiseResult)
    assert res.winner == "A"
    assert res.position_consistent is True
    assert res.margin == 3.0  # (4 + 2) / 2
    assert b.calls == 2  # exactly one call per order — no redundant pointwise calls


def test_pairwise_position_flip_is_detected_and_forced_to_tie() -> None:
    # Order (A,B): winner=first -> A. Order (B,A): winner=first -> B. The judge just
    # favoured whatever was shown first: position bias -> tie, low confidence.
    b = _FakeVisionBackend([_pw_json("first"), _pw_json("first")], name="ab")
    res = judge_pairwise(_RENDER, pathlib.Path("/tmp/b.png"), _REFERENCE, backend=b)
    assert res.winner == "tie"
    assert res.position_consistent is False
    assert any("position bias" in n for n in res.notes)


def test_pairwise_true_tie() -> None:
    b = _FakeVisionBackend([_pw_json("tie"), _pw_json("tie")], name="ab")
    res = judge_pairwise(_RENDER, pathlib.Path("/tmp/b.png"), _REFERENCE, backend=b)
    assert res.winner == "tie"
    assert res.position_consistent is True


def test_pairwise_blind_backend_never_yields_a_real_winner() -> None:
    # A blind backend that answers CONSISTENTLY (first,second -> both pick A) would look
    # like a genuine winner; it must still be forced to tie, never reported as visual.
    blind = MockBackend(_pw_json("first"))
    res = judge_pairwise(_RENDER, pathlib.Path("/tmp/b.png"), _REFERENCE, backend=blind)
    assert res.blind is True
    assert res.winner == "tie"
    assert res.position_consistent is False
    assert any("BLIND" in n for n in res.notes)


def test_judge_accepts_backend_name_string_without_iterating_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A bare string is a backend NAME, not a Sequence of single-char backends.
    monkeypatch.setenv("THREED_AI_MOCK_RESPONSE", _score_json(2, 2, 2, 2))
    result = judge(_RENDER, _REFERENCE, backend="mock", judges=1, stability_n=0)
    assert result.blind is True  # mock is text-only
    assert result.label == "blind"


def test_pairwise_backend_none_auto_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    # backend=None must auto-resolve; the mock hook makes that deterministic (and blind).
    monkeypatch.setenv("THREED_AI_MOCK_RESPONSE", _pw_json("first"))
    res = judge_pairwise(_RENDER, pathlib.Path("/tmp/b.png"), _REFERENCE)
    assert res.blind is True
    assert res.winner == "tie"


def test_pairwise_backend_name_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THREED_AI_MOCK_RESPONSE", _pw_json("second"))
    res = judge_pairwise(_RENDER, pathlib.Path("/tmp/b.png"), _REFERENCE, backend="mock")
    assert res.blind is True
