"""Tests for the auto-scored generative-modeling benchmark harness (ai.bench) and the
`3d bench` / `3d ai bench` command.

These run FULLY OFFLINE and DETERMINISTICALLY: the whole suite is driven through the
MockBackend against canned per-case responses. No real model, no network. The good-case
happy path is exercised with OpenSCAD monkeypatched ABSENT so the assertions do not depend
on whether this machine has OpenSCAD installed (when it does, the good cases render to OK;
when it doesn't, they land as DIAGNOSTIC/renderer_unavailable — both are asserted).

The point of a mock bench is to prove the SCORING HARNESS works — build-success gating,
the metric-column plumbing, the failure-path handling, aggregation, persistence, and
compare — NOT that any real backend scores well.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from ai import bench
from ai.backends import MockBackend
from ai.bench import (
    Budget,
    CaseResult,
    ProofStatus,
    advisory_judge,
    aggregate,
    extract_scad,
    load_cases,
    parse_case,
    run_case,
    run_suite,
    scad_safety_check,
)

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_THREED = os.path.join(_REPO, "bin", "3d")


# ── SCAD extraction ──────────────────────────────────────────────────────────
def test_extract_scad_fenced_block() -> None:
    text = "Sure:\n```scad\ncube(20, center=true);\n```\nDone."
    assert extract_scad(text) == "cube(20, center=true);"


def test_extract_scad_raw_when_looks_like_scad() -> None:
    assert extract_scad("difference() { cube(10); sphere(4); }") is not None


def test_extract_scad_refusal_is_none() -> None:
    assert extract_scad("I'm sorry, I can't help with that.") is None


def test_extract_scad_empty_is_none() -> None:
    assert extract_scad("   ") is None
    assert extract_scad("") is None


def test_extract_scad_prose_without_primitives_is_none() -> None:
    assert extract_scad("A gear is a toothed wheel; decide the module and tooth count.") is None


# ── Safety gate ──────────────────────────────────────────────────────────────
def test_safety_accepts_plain_scad() -> None:
    safe, reason = scad_safety_check("cube(10); difference(){cylinder(h=3,d=8);}")
    assert safe and reason is None


@pytest.mark.parametrize(
    "scad",
    [
        "include </etc/passwd>\ncube(10);",
        "use </abs/lib.scad>\ncube(10);",
        "include <../secrets.scad>\ncube(10);",
        'import("/etc/hosts");',
        'import("../../thing.stl");',
        "system(\"rm -rf /\");",
        "echo(`whoami`);",
    ],
)
def test_safety_rejects_escapes(scad: str) -> None:
    safe, reason = scad_safety_check(scad)
    assert not safe and reason


# ── Case loading ─────────────────────────────────────────────────────────────
def test_load_shipped_suite() -> None:
    cases = load_cases()
    ids = {c.id for c in cases}
    assert {"cube-basic", "l-bracket", "washer", "refusal-blob", "unsafe-include",
            "budget-zero-calls", "empty-reply", "prose-no-code"} <= ids
    assert len(cases) >= 5


def test_parse_case_defaults() -> None:
    case = parse_case({"id": "x", "description": "d"}, Path("/tmp"))
    assert case.prompt == "d"
    assert case.expected_status is ProofStatus.OK
    assert case.budget == Budget()


def test_budget_from_mapping_bad_values() -> None:
    b = Budget.from_mapping({"max_renders": "nope", "max_backend_calls": None})
    assert b.max_renders == 2 and b.max_backend_calls == 1


def test_load_cases_rejects_empty_dir(tmp_path: Path) -> None:
    from errors import InvalidArgument

    with pytest.raises(InvalidArgument):
        load_cases(tmp_path)


@pytest.mark.parametrize("bad_id", ["../evil", "a/b", "..", "/abs", "x\\y", ".hidden"])
def test_case_id_traversal_rejected(bad_id: str) -> None:
    # A case id becomes a workdir dir + a .scad filename — a traversal id must be rejected.
    from errors import InvalidArgument

    with pytest.raises(InvalidArgument):
        parse_case({"id": bad_id, "description": "d"}, Path("/tmp"))


def test_case_id_safe_basename_accepted() -> None:
    assert parse_case({"id": "cube-basic.v2", "description": "d"}, Path("/tmp")).id == "cube-basic.v2"


# ── Per-case failure paths (offline; never reach OpenSCAD) ───────────────────
def _case(**kw: Any) -> Any:
    base = {"id": "t", "description": "d", "expected_status": "failure",
            "budget": {"max_backend_calls": 1, "max_renders": 2}}
    base.update(kw)
    return parse_case(base, Path("/tmp"))


def test_refusal_is_caught_failure(tmp_path: Path) -> None:
    case = _case(mock_response="I can't do that.")
    res = run_case(case, backend_name="mock", workdir=tmp_path)
    assert res.status is ProofStatus.FAILURE and res.stop_reason == bench.STOP_NO_SCAD
    assert res.build_success is None  # never attempted a build


def test_unsafe_include_is_caught_failure(tmp_path: Path) -> None:
    case = _case(mock_response="```scad\ninclude </etc/passwd>\ncube(10);\n```")
    res = run_case(case, backend_name="mock", workdir=tmp_path)
    assert res.status is ProofStatus.FAILURE and res.stop_reason == bench.STOP_UNSAFE


def test_budget_zero_calls_stops_before_backend(tmp_path: Path) -> None:
    case = _case(budget={"max_backend_calls": 0}, mock_response="```scad\ncube(10);\n```")
    res = run_case(case, backend_name="mock", workdir=tmp_path)
    assert res.status is ProofStatus.FAILURE and res.stop_reason == bench.STOP_BUDGET
    assert res.backend_calls == 0


def test_empty_reply_is_caught_failure(tmp_path: Path) -> None:
    res = run_case(_case(mock_response="   "), backend_name="mock", workdir=tmp_path)
    assert res.status is ProofStatus.FAILURE and res.stop_reason == bench.STOP_NO_SCAD


def test_good_case_without_renderer_is_diagnostic(tmp_path: Path, monkeypatch: Any) -> None:
    # Force OpenSCAD absent so this is deterministic on any machine: a safe .scad that
    # cannot be verified is DIAGNOSTIC (environment gap), NOT a model FAILURE.
    monkeypatch.setattr(bench, "_find_openscad", lambda: None)
    case = _case(expected_status="ok", mock_response="```scad\ncube(20);\n```")
    res = run_case(case, backend_name="mock", workdir=tmp_path)
    assert res.status is ProofStatus.DIAGNOSTIC
    assert res.stop_reason == bench.STOP_RENDERER_MISSING
    assert res.build_success is None
    assert res.scad_path and Path(res.scad_path).read_text().strip() == "cube(20);"


def test_render_budget_zero_is_diagnostic(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(bench, "_find_openscad", lambda: "/usr/bin/openscad")
    case = _case(expected_status="ok", budget={"max_backend_calls": 1, "max_renders": 0},
                 mock_response="```scad\ncube(20);\n```")
    res = run_case(case, backend_name="mock", workdir=tmp_path)
    assert res.status is ProofStatus.DIAGNOSTIC and res.stop_reason == bench.STOP_BUDGET


# ── Advisory judge catches a malformed reply (never crashes the suite) ───────
def test_advisory_judge_catches_malformed_json(tmp_path: Path, monkeypatch: Any) -> None:
    render = tmp_path / "r.png"
    ref = tmp_path / "ref.png"
    render.write_bytes(b"x")
    ref.write_bytes(b"y")
    # A mock backend that returns non-rubric text: the judge parser raises JudgeError, which
    # advisory_judge MUST catch and turn into an unavailable column (this IS the "malformed
    # backend JSON -> caught" failure path).
    monkeypatch.setattr("ai.judge.resolve_backend", lambda *a, **k: MockBackend("not json at all"))
    col = advisory_judge(render, ref, backend=None)
    assert col.available is False and col.reason


# ── Aggregation math ─────────────────────────────────────────────────────────
def _result(status: ProofStatus, *, build: bool | None, wall: float = 1.0,
            calls: int = 1) -> CaseResult:
    return CaseResult(
        case_id="c", status=status, expected_status=status, stop_reason=None,
        build_success=build, backend_calls=calls, renders=1, rounds=1, wall_time=wall,
        tokens=None, cost=None, columns={}, scad_path=None,
    )


def test_aggregate_rates_and_efficiency() -> None:
    results = [
        _result(ProofStatus.OK, build=True, wall=2.0, calls=1),
        _result(ProofStatus.OK, build=True, wall=4.0, calls=1),
        _result(ProofStatus.DIAGNOSTIC, build=None),
        _result(ProofStatus.FAILURE, build=None),
    ]
    rep = aggregate(results, "mock")
    assert rep.n_cases == 4
    assert rep.build_attempted == 2  # only the two that actually rendered
    assert rep.build_success_rate == 1.0
    assert rep.ok_rate == 0.5
    assert rep.diagnostic_rate == 0.25
    assert rep.failure_rate == 0.25
    assert rep.seconds_per_ok == 3.0  # mean over the OK cases only
    assert rep.calls_per_ok == 1.0
    assert rep.cost_per_ok is None  # mock returns no token cost — honestly n/a


def test_aggregate_build_rate_none_when_nothing_attempted() -> None:
    rep = aggregate([_result(ProofStatus.FAILURE, build=None)], "mock")
    assert rep.build_success_rate is None


# ── Whole-suite run + persistence + resilience ───────────────────────────────
def test_run_suite_offline_completes_and_persists(tmp_path: Path) -> None:
    cases = load_cases()
    data_dir = tmp_path / "data"
    report = run_suite(cases, backend_name="mock", workdir=tmp_path / "work",
                       store=True, data_dir=data_dir)
    # Every case is present — the suite completes even though 5 cases fail.
    assert report.n_cases == len(cases)
    by_id = {r.case_id: r for r in report.results}
    for fid in ("refusal-blob", "empty-reply", "prose-no-code", "unsafe-include",
                "budget-zero-calls"):
        assert by_id[fid].status is ProofStatus.FAILURE
    assert by_id["unsafe-include"].stop_reason == bench.STOP_UNSAFE
    assert by_id["budget-zero-calls"].stop_reason == bench.STOP_BUDGET
    # Good cases: OK when this machine has OpenSCAD, DIAGNOSTIC when it doesn't.
    for gid in ("cube-basic", "l-bracket", "washer"):
        assert by_id[gid].status in (ProofStatus.OK, ProofStatus.DIAGNOSTIC)
    assert report.failure_rate == pytest.approx(5 / len(cases), abs=0.001)
    # Persistence: per-case rows + one suite-aggregate row.
    store = data_dir / "metrics" / "bench.jsonl"
    lines = [json.loads(x) for x in store.read_text().splitlines() if x.strip()]
    assert len(lines) == len(cases) + 1
    assert any(rec["metrics"].get("suite") for rec in lines)


def test_run_suite_survives_store_failure(tmp_path: Path, monkeypatch: Any) -> None:
    # A full-disk / read-only store must NOT abort the benchmark — the measurement stands.
    def boom(*a: Any, **k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("registries.metrics.append_record", boom)
    report = run_suite(load_cases(), backend_name="mock", workdir=tmp_path, store=True)
    assert report.n_cases >= 5  # completed and aggregated despite the store blowing up


def test_run_suite_never_raises_on_harness_bug(tmp_path: Path, monkeypatch: Any) -> None:
    # If run_case itself blew up, the suite must still finish, recording a harness_error.
    monkeypatch.setattr(bench, "run_case", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    report = run_suite(load_cases(), backend_name="mock", workdir=tmp_path, store=False)
    assert all(r.status is ProofStatus.FAILURE for r in report.results)
    assert all(r.stop_reason == bench.STOP_HARNESS_ERROR for r in report.results)


def test_compare_last_two(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    cases = load_cases()
    run_suite(cases, backend_name="mock", workdir=tmp_path / "w1", store=True, data_dir=data_dir)
    run_suite(cases, backend_name="mock", workdir=tmp_path / "w2", store=True, data_dir=data_dir)
    delta = bench.compare_last_two(data_dir=data_dir)
    assert delta is not None
    assert "failure_rate" in delta["deltas"]
    assert delta["deltas"]["failure_rate"]["delta"] == 0.0


def test_compare_none_with_single_run(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    run_suite(load_cases(), backend_name="mock", workdir=tmp_path, store=True, data_dir=data_dir)
    assert bench.compare_last_two(data_dir=data_dir) is None


def test_compare_refuses_mismatched_suites(tmp_path: Path) -> None:
    # A full-suite run then a DIFFERENT (single-case) suite into the same store must NOT be
    # diffed against each other — the latest run's signature has only one occurrence.
    data_dir = tmp_path / "data"
    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "only.json").write_text(json.dumps(
        {"id": "only", "description": "d", "expected_status": "failure",
         "mock_response": "no code here"}))
    run_suite(load_cases(), backend_name="mock", workdir=tmp_path / "a", store=True, data_dir=data_dir)
    run_suite(load_cases(custom), backend_name="mock", workdir=tmp_path / "b", store=True, data_dir=data_dir)
    assert bench.compare_last_two(data_dir=data_dir) is None  # signatures differ -> no delta


def test_unknown_backend_is_environment_error_not_data(tmp_path: Path) -> None:
    # A bogus backend is an ENVIRONMENT error (pre-flight raises), NOT an all-failed report.
    from errors import InvalidArgument

    with pytest.raises(InvalidArgument):
        run_suite(load_cases(), backend_name="bogus", workdir=tmp_path, store=False)


# ── Metric-vector column plumbing (offline: the subprocess is mocked) ─────────
def test_score_iou_column_parses(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(bench, "_run",
                        lambda *a, **k: bench._Proc(0, "IoU=0.8123\nAE=42\nFRAME=100x100\n", ""))
    col = bench._score_iou(tmp_path / "r.png", tmp_path / "ref.png")
    assert col.available and col.value and col.value["iou"] == 0.8123


def test_score_iou_column_missing_magick(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(bench, "_run", lambda *a, **k: bench._Proc(127, "", "no magick"))
    col = bench._score_iou(tmp_path / "r.png", tmp_path / "ref.png")
    assert col.available is False and col.reason


def test_perceptual_column_parses(monkeypatch: Any, tmp_path: Path) -> None:
    payload = json.dumps({"convention": {}, "psnr": {"available": True, "value": 30.0}})
    monkeypatch.setattr(bench, "_run", lambda *a, **k: bench._Proc(0, payload, ""))
    col = bench._perceptual_column(tmp_path / "r.png", tmp_path / "ref.png")
    assert col.available and col.value is not None
    assert col.value["psnr"]["value"] == 30.0


def test_geometry_column_parses_and_degrades(monkeypatch: Any, tmp_path: Path) -> None:
    payload = json.dumps({"convention": {}, "f_score": {"value": 0.9, "primary": True}})
    monkeypatch.setattr(bench, "_run", lambda *a, **k: bench._Proc(0, payload, ""))
    ok = bench._geometry_column(tmp_path / "c.stl", tmp_path / "t.stl")
    assert ok.available and ok.value is not None
    assert ok.value["f_score"]["value"] == 0.9
    monkeypatch.setattr(bench, "_run", lambda *a, **k: bench._Proc(127, "", "no trimesh"))
    missing = bench._geometry_column(tmp_path / "c.stl", tmp_path / "t.stl")
    assert missing.available is False


def test_score_columns_wires_all_channels(monkeypatch: Any, tmp_path: Path) -> None:
    # With BOTH a reference image and a target mesh, the metric vector wires all four
    # columns (silhouette, perceptual, judge, geometry). The individual scorers are stubbed
    # so this asserts the PLUMBING offline, without OpenSCAD/ImageMagick/trimesh.
    ref = tmp_path / "ref.png"
    tgt = tmp_path / "t.stl"
    ref.write_bytes(b"x")
    tgt.write_bytes(b"y")
    case = _case(expected_status="ok", reference_image=str(ref), target_mesh=str(tgt))
    monkeypatch.setattr(bench, "_score_iou", lambda *a: bench.MetricColumn("silhouette", True))
    monkeypatch.setattr(bench, "_perceptual_column", lambda *a: bench.MetricColumn("perceptual", True))
    monkeypatch.setattr(bench, "advisory_judge", lambda *a, **k: bench.MetricColumn("judge", True))
    monkeypatch.setattr(bench, "_render_stl", lambda *a: tmp_path / "c.stl")
    monkeypatch.setattr(bench, "_geometry_column", lambda *a: bench.MetricColumn("geometry", True))
    cols = bench._score_columns(case, tmp_path / "r.png", tmp_path / "c.scad",
                                tmp_path, "/usr/bin/openscad", "mock")
    assert set(cols) == {"silhouette", "perceptual", "judge", "geometry"}
    assert all(c.available for c in cols.values())


# ── e2e through bin/3d ───────────────────────────────────────────────────────
def _run_cli(args: list[str], env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["REPO_ROOT"] = _REPO
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, _THREED, *args],
                          capture_output=True, text=True, timeout=300, env=env)


def test_cli_bench_help() -> None:
    assert _run_cli(["bench", "--help"]).returncode == 0


def test_cli_bench_mock_json(tmp_path: Path) -> None:
    proc = _run_cli(["bench", "--backend", "mock", "--json", "--no-store",
                     "--workdir", str(tmp_path)])
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["n_cases"] >= 5
    statuses = {c["case_id"]: c["status"] for c in data["cases"]}
    assert statuses["refusal-blob"] == "failure"
    assert statuses["unsafe-include"] == "failure"


def test_cli_bench_unknown_backend_exits_nonzero(tmp_path: Path) -> None:
    proc = _run_cli(["bench", "--backend", "bogus", "--no-store", "--workdir", str(tmp_path)])
    assert proc.returncode != 0  # environment error, not an all-failed exit-0 report


def test_cli_ai_bench_forwarder(tmp_path: Path) -> None:
    proc = _run_cli(["ai", "bench", "--backend", "mock", "--no-store", "--workdir", str(tmp_path)])
    assert proc.returncode == 0, proc.stderr
    assert "BENCH backend=mock" in proc.stdout


def test_cli_bench_compare_roundtrip(tmp_path: Path) -> None:
    xdg = tmp_path / "xdg"
    env = {"XDG_DATA_HOME": str(xdg)}
    _run_cli(["bench", "--backend", "mock", "--workdir", str(tmp_path / "a")], env)
    _run_cli(["bench", "--backend", "mock", "--workdir", str(tmp_path / "b")], env)
    proc = _run_cli(["bench", "--compare"], env)
    assert proc.returncode == 0
    assert "BENCH compare" in proc.stdout
