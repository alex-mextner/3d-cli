"""Unit tests for the `3d generate` pipeline (lib/ai/design.py) + argv plumbing.

Pure logic only — NEVER a real model call. The backend is a scripted stub and the
verification step (`evaluate_scad`, which shells out to OpenSCAD) is monkeypatched, so
the loop's status decision, monotonic keep-best, and error feedback are tested
deterministically without OpenSCAD installed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai import design
from ai.design import (
    GenerateRequest,
    GateResult,
    _candidate_status,
    _CandidateEval,
    _parse_check_gates,
    dims_present_in_scad,
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
        lambda p, d, s: _CandidateEval("ok", [GateResult("manifold", "pass")], ""),
    )
    result = design.generate(_request(tmp_path))
    assert result.status == "ok"
    assert result.rounds == 1
    assert len(backend.prompts) == 1  # never asked again


def test_loop_feeds_error_back_and_recovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _ScriptedBackend(["bad;\n", "width = 20;\ncube(width);\n"])
    _patch_backend(monkeypatch, backend)
    calls = {"n": 0}

    def fake_eval(p: str, d: dict[str, str], s: str) -> _CandidateEval:
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

    def fake_eval(p: str, d: dict[str, str], s: str) -> _CandidateEval:
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
        lambda p, d, s: _CandidateEval("ok", [GateResult("manifold", "pass", "clean")], ""),
    )
    payload = json.loads(json.dumps(design.generate(_request(tmp_path)).to_jsonable()))
    assert set(payload) >= {
        "status", "rounds", "scad_path", "backend",
        "requested_dims", "dims_present_in_scad", "gate_results",
    }
    assert payload["gate_results"][0] == {"name": "manifold", "status": "pass", "detail": "clean"}
