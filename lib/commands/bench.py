"""3d bench — run the auto-scored generative-modeling benchmark (APPLY-RESEARCH P1.3).

WHAT: runs a suite of ModelRift-style cases (text prompt -> `.scad`) through a chosen AI
  backend, gates each on build-success (does OpenSCAD render it?), then scores the
  survivors on silhouette IoU + the geometry/perceptual batteries + an advisory VLM judge,
  and reports the rates + cost/efficiency columns ($/ok, seconds/ok, calls/ok,
  diagnostic-rate, build-success-rate). Each case is persisted to the longitudinal metrics
  store; `--compare` shows the delta versus the previous run.

WHY: ModelRift has the right task but a non-reproducible subjective score; the CAD-LLM
  benchmarks with automated metrics target CadQuery/Blender, not OpenSCAD. This fills that
  gap for `3d`. A mock backend makes the whole suite run offline and deterministically, so
  it proves the SCORING HARNESS works (not that any real backend scores well).

This module is stdlib-only at the top level (the command-authoring contract); the heavy
harness (`ai.bench`) and metric tools are lazy-imported inside `run()`.

Examples:
  3d bench                                  # run the shipped suite with the auto/mock backend
  3d bench --backend mock                   # deterministic offline run (canned per-case replies)
  3d bench --suite path/to/cases --json     # custom case dir, full JSON report
  3d bench --compare                         # show delta vs the previous stored run
  3d ai bench --backend claude               # same runner under the `3d ai` umbrella
"""
from __future__ import annotations

import json
import pathlib
from typing import TYPE_CHECKING

from cli.registry import Command
from errors import UsageError

if TYPE_CHECKING:  # typing-only: keeps the module top-level import-light at runtime
    from ai.bench import CaseResult, SuiteReport

USAGE = """3d bench [options]
  Run the auto-scored generative-modeling benchmark: prompt -> .scad -> build-success gate
  -> metric vector (silhouette IoU, geometry + perceptual batteries, advisory VLM judge).
  Reports build-success-rate, diagnostic-rate, $/ok, seconds/ok, calls/ok. Persists each
  case to ~/.local/share/3d-cli/metrics/bench.jsonl.

Options:
  --backend NAME    AI backend: claude, codex, opencode, ollama, mock (default: auto-pick,
                    or the deterministic mock when $THREED_AI_MOCK_RESPONSE is set)
  --suite DIR       directory of *.json bench cases (default: the shipped lib/data/bench)
  --workdir DIR     scratch dir for candidate .scad/.png/.stl (default: a temp dir)
  --compare         print the delta vs the previous stored suite run, then exit
  --no-store        do not append records to the metrics store
  --json            print the full JSON report instead of the text table

Examples:
  3d bench
  3d bench --backend mock
  3d bench --suite tests/fixtures/bench --json
  3d bench --compare
  3d ai bench --backend claude"""


def _print_usage() -> None:
    print(USAGE)


def _need_value(argv: list[str], i: int, flag: str) -> str:
    if i + 1 >= len(argv):
        raise UsageError(f"option {flag} needs a value", command="bench")
    return argv[i + 1]


def _parse(argv: list[str]) -> dict[str, object]:
    opts: dict[str, object] = {"backend": None, "suite": None, "workdir": None,
                               "compare": False, "store": True, "json": False}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--backend":
            opts["backend"] = _need_value(argv, i, arg)
            i += 2
        elif arg == "--suite":
            opts["suite"] = _need_value(argv, i, arg)
            i += 2
        elif arg == "--workdir":
            opts["workdir"] = _need_value(argv, i, arg)
            i += 2
        elif arg == "--compare":
            opts["compare"] = True
            i += 1
        elif arg == "--no-store":
            opts["store"] = False
            i += 1
        elif arg == "--json":
            opts["json"] = True
            i += 1
        else:
            raise UsageError(f"unknown option '{arg}'", command="bench",
                             remediation=["Run `3d bench --help` for accepted options."])
    return opts


def _fmt(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _print_case_row(case: "CaseResult") -> None:
    exp = "" if case.matched_expectation else f" (expected {case.expected_status.value})"
    stop = f" stop={case.stop_reason}" if case.stop_reason else ""
    cols = ",".join(k for k, v in case.columns.items() if v.available) or "-"
    print(f"    {case.case_id:<20} {case.status.value:<11}{stop}"
          f"  build={_fmt(case.build_success)} cols={cols}{exp}")


def _print_report(report: "SuiteReport") -> None:
    print(f"BENCH backend={report.backend} cases={report.n_cases}")
    print(f"  build-success-rate: {_fmt(report.build_success_rate)} "
          f"(attempted {report.build_attempted})")
    print(f"  ok-rate:            {_fmt(report.ok_rate)}")
    print(f"  diagnostic-rate:    {_fmt(report.diagnostic_rate)}")
    print(f"  failure-rate:       {_fmt(report.failure_rate)}")
    print(f"  expectation-match:  {_fmt(report.expectation_match_rate)}")
    print(f"  seconds/ok:         {_fmt(report.seconds_per_ok)}")
    print(f"  calls/ok:           {_fmt(report.calls_per_ok)}")
    print(f"  $/ok:               {_fmt(report.cost_per_ok)}")
    print("  cases:")
    for case in report.results:
        _print_case_row(case)


def _run_compare(opts: dict[str, object]) -> int:
    from ai.bench import compare_last_two

    delta = compare_last_two()
    if delta is None:
        print("bench --compare: need at least two stored suite runs to diff.")
        return 0
    if opts["json"]:
        print(json.dumps(delta, indent=2, sort_keys=True))
        return 0
    print(f"BENCH compare {delta['previous_timestamp']} -> {delta['current_timestamp']}")
    for key, d in delta["deltas"].items():
        print(f"  {key:<24} {_fmt(d['prev'])} -> {_fmt(d['curr'])}  (delta {_fmt(d['delta'])})")
    return 0


def run(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help", "help"):
        _print_usage()
        return 0
    opts = _parse(argv)
    if opts["compare"]:
        return _run_compare(opts)

    from ai.bench import load_cases, run_suite  # lazy: discovery stays import-light

    suite_dir = pathlib.Path(str(opts["suite"])).expanduser() if opts["suite"] else None
    cases = load_cases(suite_dir)
    workdir = _resolve_workdir(opts)
    report = run_suite(
        cases, backend_name=opts["backend"], workdir=workdir,  # type: ignore[arg-type]
        store=bool(opts["store"]),
    )
    if opts["json"]:
        print(json.dumps(report.to_jsonable(), indent=2, sort_keys=True))
    else:
        _print_report(report)
    # A benchmark RUN always exits 0 (the numbers ARE the result); a per-case failure is a
    # data point, not a nonzero process exit. Environment errors (no backend) raise earlier.
    return 0


def _resolve_workdir(opts: dict[str, object]) -> pathlib.Path:
    import tempfile

    if opts["workdir"]:
        path = pathlib.Path(str(opts["workdir"])).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path
    return pathlib.Path(tempfile.mkdtemp(prefix="3d-bench-"))


COMMAND = Command(
    name="bench",
    group="META",
    summary="run the auto-scored generative-modeling benchmark (build-success + metric vector)",
    usage=USAGE,
    run=run,
    aliases=(),
)
