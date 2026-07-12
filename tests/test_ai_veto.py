"""Unit tests for the semantic-feature veto (lib/ai/veto.py).

Deterministic: the backend is always MockBackend, never a real model call.
"""
from __future__ import annotations

import json

from ai.backends import MockBackend
from ai.veto import (
    TEMPLE_FEATURES,
    build_veto_prompt,
    evaluate,
    parse_features,
    perceive,
    run_veto,
)


def test_parse_features_reads_strict_json() -> None:
    observed = parse_features(json.dumps({"column_count": 6}), TEMPLE_FEATURES)
    assert observed["column_count"] == 6.0


def test_parse_features_regex_fallback_on_chatty_reply() -> None:
    assert parse_features("I count 6 columns.", TEMPLE_FEATURES)["column_count"] == 6.0
    assert parse_features("column_count = 4", TEMPLE_FEATURES)["column_count"] == 4.0


def test_parse_features_unreadable_is_none() -> None:
    assert parse_features("no idea, sorry", TEMPLE_FEATURES)["column_count"] is None


def test_evaluate_passes_on_exact_match() -> None:
    result = evaluate({"column_count": 6.0}, {"column_count": 6.0}, TEMPLE_FEATURES)
    assert result.passed
    assert result.failures == []


def test_evaluate_vetoes_wrong_count_even_though_gate_exists() -> None:
    """The veto must BITE: a wrong structural count fails regardless of any silhouette."""
    result = evaluate({"column_count": 2.0}, {"column_count": 6.0}, TEMPLE_FEATURES)
    assert not result.passed
    assert any("column_count" in f for f in result.failures)


def test_evaluate_fails_closed_on_unreadable_feature() -> None:
    result = evaluate({"column_count": None}, {"column_count": 6.0}, TEMPLE_FEATURES)
    assert not result.passed
    assert any("unreadable" in f for f in result.failures)


def test_evaluate_fails_closed_when_expected_value_missing() -> None:
    result = evaluate({"column_count": 6.0}, {}, TEMPLE_FEATURES)
    assert not result.passed
    assert any("no expected value configured" in f for f in result.failures)


def test_run_veto_with_mock_backend_pass_and_fail() -> None:
    good = MockBackend(response=json.dumps({"column_count": 5}))
    bad = MockBackend(response=json.dumps({"column_count": 8}))
    expected = {"column_count": 5.0}
    assert run_veto(good, "nonexistent.png", expected).passed
    assert not run_veto(bad, "nonexistent.png", expected).passed


def test_perceive_ignores_missing_image_and_uses_mock_reply() -> None:
    backend = MockBackend(response=json.dumps({"column_count": 7}))
    observed = perceive(backend, "does-not-exist.png", TEMPLE_FEATURES)
    assert observed["column_count"] == 7.0


def test_build_veto_prompt_requests_json_for_each_feature() -> None:
    prompt = build_veto_prompt(TEMPLE_FEATURES)
    assert "column_count" in prompt
    assert "JSON" in prompt
