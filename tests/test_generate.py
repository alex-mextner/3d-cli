"""Unit tests for the `3d generate` pipeline (lib/ai/design.py) + argv plumbing.

Pure logic only — NEVER a real model call. The backend is a scripted stub and the
verification step (`evaluate_scad`, which shells out to OpenSCAD) is monkeypatched, so
the loop's status decision, monotonic keep-best, and error feedback are tested
deterministically without OpenSCAD installed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from ai import design
from ai.design import (
    GenerateRequest,
    GateResult,
    _apply_visual,
    _build_review,
    _candidate_status,
    _CandidateEval,
    _judge_rationales,
    _outcome_from_score,
    _parse_check_gates,
    _VisualReview,
    dims_present_in_scad,
    evaluate_scad,
    extract_scad,
    parse_dim_flag,
    render_constants_block,
)
from errors import InvalidArgument, UsageError


# ── dims parsing + injection ─────────────────────────────────────────────────
def test_parse_dim_flag_splits_name_and_raw_value() -> None:
    assert parse_dim_flag("width=20") == ("width", "20")
    assert parse_dim_flag("wall=2.4") == ("wall", "2.4")
    # value kept as a raw OpenSCAD token (expression allowed)
    assert parse_dim_flag("gap=wall/2") == ("gap", "wall/2")


def test_parse_dim_flag_rejects_bad_name_or_missing_value() -> None:
    with pytest.raises(InvalidArgument):
        parse_dim_flag("2bad=10")
    with pytest.raises(UsageError):
        parse_dim_flag("width")
    with pytest.raises(UsageError):
        parse_dim_flag("width=")


def test_render_constants_block_emits_top_of_file_declarations() -> None:
    block = render_constants_block({"width": "20", "wall": "2.4"})
    assert "width = 20;" in block
    assert "wall = 2.4;" in block


def test_dims_present_detects_real_assignments_not_prose_or_comparisons() -> None:
    scad = (
        "width = 20;\n"
        "// depth = 99;  (a comment, must NOT count)\n"
        "if (height == 5) { }  // comparison, must NOT count\n"
        "wall=2;\n"
    )
    present = dims_present_in_scad(scad, {"width": "20", "depth": "0", "height": "0", "wall": "2"})
    assert present == {"width": True, "depth": False, "height": False, "wall": True}


def test_dims_present_rejects_nested_and_block_commented_assignments() -> None:
    scad = (
        "width = 20;\n"                    # top-level -> counts
        "/* radius = 5;\n   still a comment */\n"  # block comment -> must NOT count
        "module part() {\n"
        "    depth = 30;\n"                # local inside a module body -> must NOT count
        "}\n"
    )
    present = dims_present_in_scad(scad, {"width": "0", "radius": "0", "depth": "0"})
    assert present == {"width": True, "radius": False, "depth": False}


# ── model-output extraction ──────────────────────────────────────────────────
def test_extract_scad_prefers_a_fenced_block() -> None:
    assert extract_scad("prose\n```scad\ncube(1);\n```\ntail").strip() == "cube(1);"
    assert extract_scad("```openscad\nsphere(2);\n```").strip() == "sphere(2);"


def test_extract_scad_falls_back_to_whole_text() -> None:
    assert extract_scad("cube([1,2,3]);").strip() == "cube([1,2,3]);"


# ── gate parsing + candidate status ──────────────────────────────────────────
def test_parse_check_gates_reads_the_breakdown_lines() -> None:
    log = (
        "=== check (acceptance gate) ===\n"
        "  MANIFOLD     PASS  1 file(s) clean\n"
        "  CONSISTENCY  SKIP  no assert()\n"
        "  PRINTABILITY FAIL  wall too thin\n"
        ">>> CHECK: FAIL\n"
    )
    gates = {g.name: g.status for g in _parse_check_gates(log)}
    assert gates == {"manifold": "pass", "consistency": "skip", "printability": "fail"}


def test_candidate_status_ok_only_when_hard_gates_pass_and_dims_present() -> None:
    good = [GateResult("manifold", "pass"), GateResult("printability", "pass")]
    assert _candidate_status(good, missing=[]) == "ok"
    assert _candidate_status(good, missing=["radius"]) == "diagnostic"
    warn = [GateResult("manifold", "pass"), GateResult("printability", "skip")]
    assert _candidate_status(warn, missing=[]) == "diagnostic"


# ── the loop: status decision, keep-best, feedback, on-disk best ─────────────
class _ScriptedBackend:
    """A deterministic stub returning successive canned .scad sources per round and
    recording the user prompts it saw (to prove error feedback is threaded in)."""

    name = "scripted"

    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self.prompts: list[str] = []

    def complete(self, system: str, user: str, images: object = None, timeout: float = 0.0) -> str:
        self.prompts.append(user)
        return self._responses[min(len(self.prompts) - 1, len(self._responses) - 1)]


def _request(tmp_path: Path, rounds: int = 3) -> GenerateRequest:
    return GenerateRequest(
        description="a part", dims={"width": "20"},
        out_path=str(tmp_path / "out.scad"), rounds=rounds, backend=None,
    )


def _patch_backend(monkeypatch: pytest.MonkeyPatch, backend: _ScriptedBackend) -> None:
    monkeypatch.setattr(design, "resolve_backend", lambda *a, **k: backend)


def test_loop_stops_early_on_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _ScriptedBackend(["width = 20;\ncube(width);\n"])
    _patch_backend(monkeypatch, backend)
    monkeypatch.setattr(
        design, "evaluate_scad",
        lambda p, d, s, review=None: _CandidateEval("ok", [GateResult("manifold", "pass")], ""),
    )
    result = design.generate(_request(tmp_path))
    assert result.status == "ok"
    assert result.rounds == 1
    assert len(backend.prompts) == 1  # never asked again


def test_loop_feeds_error_back_and_recovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _ScriptedBackend(["bad;\n", "width = 20;\ncube(width);\n"])
    _patch_backend(monkeypatch, backend)
    calls = {"n": 0}

    def fake_eval(p: str, d: dict[str, str], s: str, review: object = None) -> _CandidateEval:
        calls["n"] += 1
        if calls["n"] == 1:
            return _CandidateEval("failure", [GateResult("render", "fail")], "RENDER-ERROR: boom")
        return _CandidateEval("ok", [GateResult("manifold", "pass")], "")

    monkeypatch.setattr(design, "evaluate_scad", fake_eval)
    result = design.generate(_request(tmp_path))
    assert result.status == "ok"
    assert result.rounds == 2
    # the round-2 prompt must carry the round-1 error + previous scad
    assert "RENDER-ERROR: boom" in backend.prompts[1]
    assert "bad;" in backend.prompts[1]
    # on disk = the winning (round-2) source
    assert "cube(width)" in (tmp_path / "out.scad").read_text()


def test_loop_keeps_best_earlier_candidate_on_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _ScriptedBackend(["GOOD = 1;\n", "WORSE = 2;\n"])
    _patch_backend(monkeypatch, backend)
    calls = {"n": 0}

    def fake_eval(p: str, d: dict[str, str], s: str, review: object = None) -> _CandidateEval:
        calls["n"] += 1
        status = "diagnostic" if calls["n"] == 1 else "failure"
        return _CandidateEval(status, [GateResult("check", status)], "err")

    monkeypatch.setattr(design, "evaluate_scad", fake_eval)
    result = design.generate(_request(tmp_path, rounds=2))
    assert result.status == "diagnostic"   # the better, earlier round wins
    assert result.rounds == 2
    # the BEST source (round 1) is what remains on disk, not the last (worse) write
    assert "GOOD = 1;" in (tmp_path / "out.scad").read_text()


def test_openscad_absent_yields_diagnostic_without_calling_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no OpenSCAD, the .scad is still written and the status degrades to diagnostic
    (verification skipped) — the pipeline never hard-fails purely on a missing tool."""
    backend = _ScriptedBackend(["width = 20;\ncube(width);\n"])
    _patch_backend(monkeypatch, backend)
    monkeypatch.setattr(design, "find_openscad", lambda: None)
    result = design.generate(_request(tmp_path))
    assert result.status == "diagnostic"
    assert result.dims_present_in_scad == {"width": True}
    assert any("skip" == g.status for g in result.gate_results)
    assert (tmp_path / "out.scad").exists()


def test_result_json_shape_is_stable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _ScriptedBackend(["width = 20;\n"])
    _patch_backend(monkeypatch, backend)
    monkeypatch.setattr(
        design, "evaluate_scad",
        lambda p, d, s, review=None: _CandidateEval("ok", [GateResult("manifold", "pass", "clean")], ""),
    )
    payload = json.loads(json.dumps(design.generate(_request(tmp_path)).to_jsonable()))
    assert set(payload) >= {
        "status", "rounds", "scad_path", "backend",
        "requested_dims", "dims_present_in_scad", "gate_results",
    }
    assert payload["gate_results"][0] == {"name": "manifold", "status": "pass", "detail": "clean"}


# ── opt-in visual review ─────────────────────────────────────────────────────
def _visual_score(mean: float, *, blind: bool = False, judges: list[Any] | None = None) -> object:
    """Build a minimal VisualScore (the judge's public result) for the tests. `judges` may
    hold lightweight duck-typed fakes (only `.blind`/`.canonical.raw` are read downstream)."""
    from ai.judge import VisualScore

    per_dim = {"silhouette_proportion": mean, "feature_completeness": mean}
    return VisualScore(
        per_dim=per_dim, mean=mean, label="ok" if not blind else "blind",
        stability_unstable=False, cross_judge_spread=None, cross_judge_low_agreement=False,
        blind=blind, single_judge=True, judges=cast(Any, judges or []), notes=[],
    )


def test_outcome_from_score_low_fails_with_critique_high_passes_blind_skips() -> None:
    low = _outcome_from_score(_visual_score(1.0), threshold=3.0)
    assert not low.passed and not low.blind
    assert low.gate.status == "fail"
    assert "VISUAL-REVIEW" in low.critique and "1.0/4" in low.critique

    high = _outcome_from_score(_visual_score(3.5), threshold=3.0)
    assert high.passed and high.gate.status == "pass" and high.critique == ""

    blind = _outcome_from_score(_visual_score(0.0, blind=True), threshold=3.0)
    assert blind.blind and not blind.passed
    assert blind.gate.status == "skip" and blind.critique == ""


def test_apply_visual_only_downgrades_ok_and_threads_critique() -> None:
    fail = _outcome_from_score(_visual_score(1.0), threshold=3.0)
    status, gates, err = _apply_visual("ok", [GateResult("manifold", "pass")], "", fail)
    assert status == "diagnostic"                       # ok downgraded, geometry gate kept
    assert gates[-1].name == "visual" and gates[-1].status == "fail"
    assert "VISUAL-REVIEW" in err                       # critique threaded into feedback

    ok = _outcome_from_score(_visual_score(4.0), threshold=3.0)
    status2, _, err2 = _apply_visual("ok", [GateResult("manifold", "pass")], "", ok)
    assert status2 == "ok" and err2 == ""               # a passing render leaves ok intact

    blind = _outcome_from_score(_visual_score(0.0, blind=True), threshold=3.0)
    status3, _, err3 = _apply_visual("ok", [GateResult("manifold", "pass")], "geom-log", blind)
    assert status3 == "diagnostic" and err3 == "geom-log"  # inconclusive downgrades, no critique


def test_judge_rationales_extracts_sighted_free_text() -> None:
    from types import SimpleNamespace

    sighted = SimpleNamespace(blind=False, canonical=SimpleNamespace(
        raw='prose {"silhouette_proportion": 2, "rationale": "spout is too short"} tail'))
    blind = SimpleNamespace(blind=True, canonical=SimpleNamespace(raw='{"rationale": "ignored"}'))
    rationales = _judge_rationales(_visual_score(2.0, judges=[sighted, blind]))
    assert rationales == ["spout is too short"]


def test_build_review_is_none_when_disabled_and_config_when_enabled() -> None:
    off = GenerateRequest(description="d", dims={"w": "1"}, out_path="o.scad")
    assert _build_review(off) is None
    on = GenerateRequest(
        description="d", dims={"w": "1"}, out_path="o.scad",
        visual_review=True, reference="ref.png", visual_threshold=2.5,
    )
    review = _build_review(on)
    assert isinstance(review, _VisualReview)
    assert review.reference == "ref.png" and review.threshold == 2.5


class _JudgeRecorder:
    """Stand-in for ai.judge.judge: records (render, reference) and returns a canned score."""

    def __init__(self, *scores: object) -> None:
        self._scores = list(scores)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, render: object, reference: object, **_: object) -> object:
        self.calls.append((str(render), str(reference)))
        return self._scores[min(len(self.calls) - 1, len(self._scores) - 1)]


def _stub_gates_for_visual(monkeypatch: pytest.MonkeyPatch, recorder: _JudgeRecorder) -> None:
    """Stub the shell-out gates so a candidate renders + passes geometry, and point the
    judge at our recorder (no OpenSCAD, no real model, no real backend)."""
    monkeypatch.setattr(design, "find_openscad", lambda: "/usr/bin/openscad")

    def fake_run_3d(args: list[str]) -> tuple[int, str]:
        if args[0] == "render":
            out = args[args.index("-o") + 1]
            with open(out, "wb") as fh:
                fh.write(b"\x89PNG\r\n")
            return 0, "rendered"
        if args[0] == "check":
            return 0, "  MANIFOLD     PASS ok\n  PRINTABILITY PASS ok\n"
        return 0, "ok"

    monkeypatch.setattr(design, "_run_3d", fake_run_3d)
    monkeypatch.setattr("ai.backends.resolve_backend", lambda *a, **k: object())
    monkeypatch.setattr("ai.judge.judge", recorder)


def test_evaluate_scad_off_never_calls_judge_and_stays_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: with review OFF the judge is NEVER imported/called and the
    geometry-only status logic is exactly as before (ok on clean gates + dims present)."""
    recorder = _JudgeRecorder(_visual_score(0.0))
    _stub_gates_for_visual(monkeypatch, recorder)
    ev = evaluate_scad(str(tmp_path / "m.scad"), {"width": "20"}, "width = 20;\ncube(width);\n")
    assert ev.status == "ok"
    assert recorder.calls == []                          # judge untouched on the default path
    assert not any(g.name == "visual" for g in ev.gate_results)


def test_evaluate_scad_on_low_score_blocks_ok_and_feeds_critique(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _JudgeRecorder(_visual_score(1.0))
    _stub_gates_for_visual(monkeypatch, recorder)
    review = _VisualReview(reference=str(tmp_path / "ref.png"), threshold=3.0, backend=None, config_path=None)
    scad = str(tmp_path / "m.scad")
    ev = evaluate_scad(scad, {"width": "20"}, "width = 20;\ncube(width);\n", review=review)
    # judge saw the candidate render AND the reference
    assert len(recorder.calls) == 1
    render_png, ref = recorder.calls[0]
    assert render_png.endswith("render.png") and ref == str(tmp_path / "ref.png")
    # geometry passed but the low visual score blocks ok, and the critique is fed back
    assert ev.status == "diagnostic"
    assert any(g.name == "visual" and g.status == "fail" for g in ev.gate_results)
    assert "VISUAL-REVIEW" in ev.error_text


def test_evaluate_scad_on_high_score_allows_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _JudgeRecorder(_visual_score(4.0))
    _stub_gates_for_visual(monkeypatch, recorder)
    review = _VisualReview(reference=str(tmp_path / "ref.png"), threshold=3.0, backend=None, config_path=None)
    ev = evaluate_scad(str(tmp_path / "m.scad"), {"width": "20"}, "width = 20;\ncube(width);\n", review=review)
    assert ev.status == "ok"
    assert any(g.name == "visual" and g.status == "pass" for g in ev.gate_results)
    assert ev.error_text == ""


def test_generate_loop_refines_toward_reference_across_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end at the engine level: a first render that is visually wrong (score 1.0)
    blocks ok and its critique reaches the round-2 prompt; the improved render (score 4.0)
    then passes. The judge is called with the reference each round."""
    backend = _ScriptedBackend(["width = 20;\ncube(width);\n", "width = 20;\nsphere(width);\n"])
    _patch_backend(monkeypatch, backend)
    recorder = _JudgeRecorder(_visual_score(1.0), _visual_score(4.0))
    _stub_gates_for_visual(monkeypatch, recorder)
    req = GenerateRequest(
        description="a part", dims={"width": "20"}, out_path=str(tmp_path / "out.scad"),
        rounds=3, visual_review=True, reference=str(tmp_path / "ref.png"), visual_threshold=3.0,
    )
    result = design.generate(req)
    assert result.status == "ok" and result.rounds == 2
    assert len(recorder.calls) == 2
    assert all(ref == str(tmp_path / "ref.png") for _, ref in recorder.calls)
    assert "VISUAL-REVIEW" in backend.prompts[1]         # critique threaded into round 2
