"""Readable e2e workflows for the LLM ranking of `3d models` (#577): the real bin/3d against the loopback fakes of the
four sites (tests/search_fixtures.py) and a FAKE `omp` executable (tests/llm_fixtures.py) that answers like
`omp -p --mode json` from a script. Phase 1 before any model answers, the graceful degradation lines and exit codes,
`--no-llm` never starting omp, the `3d.models.rank/1` event stream driven by an agent with `--answer` / `--follow`,
and everything read back through `searches show`. No real site and no real model is contacted."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from llm_fixtures import fake_calls, write_fake_omp
from marketplace_fixtures import write_fake_curl
from search_fixtures import BulkServer

from .workflow_helper import THREED, isolated_env, run_cli

PLAN = {
    "schema": "3d.models.plan/1",
    "summary": "a descriptive phrase became part names",
    "queries": [
        {"query": "sensor enclosure", "kind": "wide", "rationale": "the category"},
        {
            "query": "capacitive probe case",
            "kind": "narrow",
            "rationale": "the exact part",
        },
        {"query": "Sensorgehäuse", "kind": "rewrite", "rationale": "German"},
    ],
    "followups": [{"query": "plant stake holder", "rationale": "a stake variant"}],
    "questions": [
        {
            "key": "need_cad",
            "type": "single",
            "prompt": "Do you need CAD files?",
            "choices": ["yes", "no"],
        },
        {"key": "max_size", "type": "text", "prompt": "Maximum size in mm?"},
    ],
}


def _env(
    server: BulkServer, tmp_path: Path, omp: Path | str, **extra: str
) -> dict[str, str]:
    binary, _ = write_fake_curl(tmp_path / "curl-bin")
    return {
        **server.env(),
        "THREED_CURL_IMPERSONATE": str(binary),
        "THREED_KEYCHAIN": "off",
        "THREED_OMP": str(omp),
        **extra,
    }


def _run(
    tmp_path: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    return run_cli(tmp_path, "models", *args, env_extra=env)


def _events(stdout: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def test_the_results_are_printed_before_the_model_has_answered(tmp_path: Path) -> None:
    """A user searches with --rank and a slow model: the site results appear on screen right away, the verdicts
    follow when the model is done."""
    omp, state = write_fake_omp(
        tmp_path, {"delay_classify": 3.0, "plan": PLAN, "fits_mod": 2}
    )
    with BulkServer(total=60) as server:
        env = isolated_env(tmp_path) | _env(server, tmp_path, omp)
        started = time.monotonic()
        proc = subprocess.Popen(
            [
                sys.executable,
                str(THREED),
                "models",
                "search",
                "case",
                "--site",
                "printables",
                "--limit",
                "10",
                "--rank",
            ],
            cwd=str(tmp_path),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdout is not None
        first_phase_one_line = ""
        while not first_phase_one_line.startswith("printables ("):
            first_phase_one_line = proc.stdout.readline()
            assert first_phase_one_line, (
                "the process ended before printing the site results"
            )
        seen_after = time.monotonic() - started
        rest, err = proc.communicate(timeout=60)
        finished_after = time.monotonic() - started
    assert proc.returncode == 0, err
    assert finished_after - seen_after > 2.0, (
        seen_after,
        finished_after,
    )  # phase 1 did not wait for the 3 s model
    assert "  fits  0.90" in rest and "ranked: 5 fit, 1 maybe, 4 no, 0 unjudged" in rest
    assert [c["role"] for c in fake_calls(state)][0] == "classify"


def test_an_invalid_model_answer_costs_one_line_and_keeps_the_results(
    tmp_path: Path,
) -> None:
    """A user ranks a search and the model answers garbage: one warning line, the unranked results still print."""
    omp, state = write_fake_omp(tmp_path, {"mode_classify": "garbage"})
    with BulkServer(total=60) as server:
        done = _run(
            tmp_path,
            _env(server, tmp_path, omp),
            "search",
            "case",
            "--site",
            "printables",
            "--limit",
            "5",
            "--rank",
        )
    assert done.returncode == 0, done.stderr
    assert (
        done.stdout.count("LLM: unavailable (") == 1 and "printables (5)" in done.stdout
    )
    assert (
        "Printables model 5" in done.stdout
        and "fits" not in done.stdout.split("LLM: unavailable")[1].split("saved:")[0]
    )
    assert [c["role"] for c in fake_calls(state)] == [
        "classify",
        "classify",
    ]  # one retry, then give up


def test_a_failing_omp_costs_one_line_and_keeps_the_results(tmp_path: Path) -> None:
    """A user ranks a search while omp fails: one warning line, the results still print."""
    omp, _ = write_fake_omp(tmp_path, {"mode_classify": "fail"})
    with BulkServer(total=60) as server:
        done = _run(
            tmp_path,
            _env(server, tmp_path, omp),
            "search",
            "case",
            "--site",
            "printables",
            "--limit",
            "5",
            "--rank",
        )
    assert (
        done.returncode == 0
        and "LLM: unavailable (omp exited 3 without an answer" in done.stdout
    )
    assert "printables (5)" in done.stdout


def test_llm_require_without_omp_exits_127_before_searching(tmp_path: Path) -> None:
    """A user requires LLM ranking without omp installed: exit 127 before any site is searched."""
    with BulkServer(total=60) as server:
        done = _run(
            tmp_path,
            _env(server, tmp_path, tmp_path / "no-such-omp"),
            "search",
            "case",
            "--site",
            "printables",
            "--rank",
            "--llm",
            "require",
        )
        searched = dict(server.calls)
    assert done.returncode == 127 and "omp" in done.stderr and done.stdout == ""
    assert searched == {}  # not one request went out
    with BulkServer(total=60) as server:
        soft = _run(
            tmp_path,
            _env(server, tmp_path, tmp_path / "no-such-omp"),
            "search",
            "case",
            "--site",
            "printables",
            "--limit",
            "3",
            "--rank",
        )
    assert (
        soft.returncode == 0
        and "LLM: unavailable (" in soft.stdout
        and "printables (3)" in soft.stdout
    )


def test_no_llm_never_starts_omp_even_when_ai_json_enables_ranking(
    tmp_path: Path,
) -> None:
    """A user passes --no-llm while ai.json enables ranking: omp is never started."""
    omp, state = write_fake_omp(tmp_path, {})
    config = tmp_path / "xdg-config" / "3d-cli"
    config.mkdir(parents=True, exist_ok=True)
    (config / "ai.json").write_text(
        json.dumps({"llm": {"enabled": True}}), encoding="utf-8"
    )
    with BulkServer(total=60) as server:
        env = _env(server, tmp_path, omp)
        plain = _run(
            tmp_path,
            env,
            "search",
            "case",
            "--site",
            "printables",
            "--limit",
            "5",
            "--no-llm",
        )
        explicit = _run(
            tmp_path,
            env,
            "search",
            "case",
            "--site",
            "printables",
            "--limit",
            "5",
            "--no-llm",
            "--ndjson",
        )
        assert (
            fake_calls(state) == []
        )  # --no-llm started omp zero times, with ranking enabled in ai.json
        enabled = _run(
            tmp_path, env, "search", "case", "--site", "printables", "--limit", "5"
        )
    assert plain.returncode == explicit.returncode == 0 and "LLM" not in plain.stdout
    assert [e["event"] for e in _events(explicit.stdout)] == [
        "start",
        "search",
        *["result"] * 5,
        "done",
    ]
    # ai.json llm.enabled switches ranking on; --no-llm (above) always wins over it
    assert enabled.returncode == 0 and "ranked:" in enabled.stdout
    assert fake_calls(state)[0]["role"] == "classify"
    contradiction = _run(
        tmp_path, _env(server, tmp_path, omp), "search", "case", "--rank", "--no-llm"
    )
    assert (
        contradiction.returncode == 2
        and "--no-llm cannot be combined" in contradiction.stderr
    )


def test_the_plain_search_output_is_identical_with_and_without_the_ranking_code(
    tmp_path: Path,
) -> None:
    """`--no-llm` and no flag at all print exactly what the search core printed before #577."""
    omp, state = write_fake_omp(tmp_path, {})
    with BulkServer(total=60) as server:
        env = _env(server, tmp_path, omp)
        bare = _run(
            tmp_path,
            env,
            "search",
            "case",
            "--site",
            "printables",
            "--limit",
            "5",
            "--no-save",
        )
        no_llm = _run(
            tmp_path,
            env,
            "search",
            "case",
            "--site",
            "printables",
            "--limit",
            "5",
            "--no-save",
            "--no-llm",
        )

    def strip(out: str) -> str:  # the per-site timing line differs between runs
        return "\n".join(
            line for line in out.splitlines() if not line.startswith("printables (")
        )

    assert bare.returncode == no_llm.returncode == 0 and strip(bare.stdout) == strip(
        no_llm.stdout
    )
    assert fake_calls(state) == []


def _assert_first_pass_events(events: list[dict[str, Any]]) -> None:
    order = [e["event"] for e in events]
    assert order[:2] == ["start", "search"] and order[-1] == "done"
    last_result = max(i for i, n in enumerate(order) if n == "result")
    # every phase-1 result precedes phase 2
    assert order.index("phase") > last_result
    assert order.count("result") >= 10 and order.count("verdict") == 10
    assert all(e["schema"] == "3d.models.rank/1" for e in events)
    _assert_plan_questions_and_totals(events)


def _assert_plan_questions_and_totals(events: list[dict[str, Any]]) -> None:
    plan = next(e for e in events if e["event"] == "plan" and e["status"] == "ready")
    assert plan["mode"] == "rewrite"
    assert [q["kind"] for q in plan["queries"]] == ["wide", "narrow", "rewrite"]
    questions = [e for e in events if e["event"] == "question"]
    assert [(q["key"], q["type"], q["choices"]) for q in questions] == [
        ("need_cad", "single", ["yes", "no"]),
        ("max_size", "text", []),
    ]
    suggested = [
        e for e in events if e["event"] == "query" and e["status"] == "suggested"
    ]
    assert [(e["n"], e["query"]) for e in suggested] == [(1, "plant stake holder")]
    done = events[-1]
    assert done["counts"]["results"] == 10 and done["counts"]["fits"] == 1
    assert done["llm"]["calls"] >= 2
    assert done["llm"]["input_tokens"] == 12 * done["llm"]["calls"]
    assert done["llm"]["total_tokens"] == 102 * done["llm"]["calls"]


def _assert_answered_followup(
    tmp_path: Path, env: dict[str, str], search_id: str
) -> None:
    second = _run(
        tmp_path,
        env,
        "rerank",
        search_id,
        "--answer",
        "need_cad=yes",
        "--answer",
        "max_size=60",
        "--follow",
        "1",
        "--ndjson",
    )
    assert second.returncode == 0, second.stderr
    again = _events(second.stdout)
    ran = [e for e in again if e["event"] == "query" and e["status"] == "run"]
    assert [(e["query"], e["kind"]) for e in ran] == [("plant stake holder", "followup")]
    assert again[0]["answers"] == {"max_size": "60", "need_cad": "yes"}
    bad = _run(tmp_path, env, "rerank", search_id, "--answer", "need_cad=maybe")
    assert bad.returncode == 2
    assert "accepted: yes, no" in bad.stderr


def _assert_saved_flow(tmp_path: Path, env: dict[str, str], search_id: str) -> None:
    listed = json.loads(_run(tmp_path, env, "followups", search_id, "--json").stdout)
    assert listed["schema"] == "3d.models.followups/1"
    assert listed["answers"] == {"max_size": "60", "need_cad": "yes"}
    assert [(f["n"], f["status"]) for f in listed["followups"]] == [(1, "run")]
    assert [(q["query"], q["status"]) for q in listed["queries"]] == [
        ("sensor enclosure", "run"),
        ("capacitive probe case", "run"),
        ("Sensorgehäuse", "run"),
    ]
    shown = json.loads(
        _run(tmp_path, env, "searches", "show", search_id, "--json").stdout
    )
    rows = shown["results"]["printables"]
    verdicts = [r["verdict"] for r in rows]
    assert len(verdicts) == 10 and set(verdicts) == {"fits", "maybe", "no"}
    assert all(r["rationale"] for r in rows)
    assert len(shown["queries"]) == 4
    assert {q["key"] for q in shown["questions"]} == {"need_cad", "max_size"}


def _assert_omp_invocations(state: Path) -> None:
    calls = fake_calls(state)
    classify = [c for c in calls if c["role"] == "classify"]
    planner = [c for c in calls if c["role"] == "plan"]
    assert len(planner) == 1 and planner[0]["model"] == "sonnet"
    assert "--model" in planner[0]["argv"]
    assert planner[0]["argv"][planner[0]["argv"].index("--tools") + 1] == "web_search,read"
    assert classify[0]["model"] == "haiku"
    assert "--no-tools" in classify[0]["argv"]
    assert "--no-session" in classify[0]["argv"]


def test_an_agent_drives_the_whole_flow_with_ndjson_answers_and_follows(
    tmp_path: Path,
) -> None:
    """search --rank --ndjson -> read the questions and follow-ups -> rerank --answer ... --follow 1 -> read it all
    back with followups / searches show. Nothing interactive."""
    omp, state = write_fake_omp(tmp_path, {"plan": PLAN})
    with BulkServer(total=60) as server:
        env = _env(server, tmp_path, omp)
        first = _run(
            tmp_path,
            env,
            "search",
            "case",
            "--site",
            "printables",
            "--limit",
            "10",
            "--rank",
            "--ndjson",
        )
        assert first.returncode == 0, first.stderr
        events = _events(first.stdout)
        _assert_first_pass_events(events)
        search_id = events[1]["search"]
        assert events[-1]["event"] == "done" and search_id
        _assert_answered_followup(tmp_path, env, search_id)
        _assert_saved_flow(tmp_path, env, search_id)
    _assert_omp_invocations(state)


def test_the_json_document_carries_the_search_with_verdicts_and_the_ranking(
    tmp_path: Path,
) -> None:
    """A user asks for --json and gets the search with verdicts and the ranking in one document."""
    omp, _ = write_fake_omp(tmp_path, {"plan": PLAN, "fits_mod": 2})
    with BulkServer(total=60) as server:
        done = _run(
            tmp_path,
            _env(server, tmp_path, omp),
            "search",
            "case",
            "--site",
            "printables",
            "--limit",
            "10",
            "--classifier",
            "haiku-test",
            "--planner",
            "sonnet-test",
            "--json",
        )
    assert done.returncode == 0, done.stderr
    doc = json.loads(done.stdout)
    assert (
        doc["schema"] == "3d.models.rank-result/1"
        and doc["search"]["schema"] == "3d.models.search/2"
    )
    cards = doc["search"]["results"]["printables"]
    assert len(cards) == 10 and all(
        c["verdict"] in ("fits", "maybe", "no") and c["rationale"] for c in cards
    )
    assert [r["verdict"] for r in doc["ranking"]][:5] == ["fits"] * 5 and doc["counts"][
        "fits"
    ] == 5
    assert doc["llm"]["backend"] == "omp" and doc["degraded"] is None
    assert doc["questions"] == [] or all(q["key"] for q in doc["questions"])


def test_a_rewrite_creates_wide_and_narrow_searches_linked_to_the_original(
    tmp_path: Path,
) -> None:
    """A user asks for a query rewrite: wide and narrow searches are created and linked to the original."""
    omp, _ = write_fake_omp(tmp_path, {"plan": PLAN})
    with BulkServer(total=60) as server:
        env = _env(server, tmp_path, omp)
        first = _run(
            tmp_path,
            env,
            "search",
            "case",
            "--site",
            "printables",
            "--limit",
            "10",
            "--rank",
            "--max-queries",
            "2",
            "--ndjson",
        )
        assert first.returncode == 0, first.stderr
        events = _events(first.stdout)
        ran = [e for e in events if e["event"] == "query" and e["status"] == "run"]
        assert sorted(e["kind"] for e in ran) == [
            "narrow",
            "wide",
        ]  # --max-queries 2 keeps one of each
        root = events[1]["search"]
        assert all(
            e["parent"] == root and e["new"] == 0 and e["duplicates"] == 10 for e in ran
        )  # the fake site answers every query alike
        child = json.loads(
            _run(tmp_path, env, "searches", "show", ran[0]["search"], "--json").stdout
        )
        assert (
            child["parent"] == root
            and child["kind"] == ran[0]["kind"]
            and child["query"] == ran[0]["query"]
        )
        assert all(
            "verdict" in r for r in child["results"]["printables"]
        )  # duplicates carry the pooled verdict
        listed = json.loads(_run(tmp_path, env, "searches", "list", "--json").stdout)
        assert sorted(s["kind"] for s in listed["searches"]) == [
            "narrow",
            "user",
            "wide",
        ]


def test_background_prints_the_results_and_a_detached_worker_saves_the_verdicts(
    tmp_path: Path,
) -> None:
    """A user runs with --background: results print at once and a detached worker saves the verdicts later."""
    omp, state = write_fake_omp(
        tmp_path, {"delay_classify": 1.0, "plan": PLAN, "fits_mod": 2}
    )
    with BulkServer(total=60) as server:
        env = _env(server, tmp_path, omp)
        started = time.monotonic()
        first = _run(
            tmp_path,
            env,
            "search",
            "case",
            "--site",
            "printables",
            "--limit",
            "10",
            "--rank",
            "--background",
        )
        returned = time.monotonic() - started
        assert first.returncode == 0, first.stderr
        assert (
            "printables (10)" in first.stdout
            and "ranking continues in the background" in first.stdout
        )
        assert returned < 6 and "ranked:" not in first.stdout
        search_id = first.stdout.rsplit("3d models searches show ", 1)[-1].strip()
        deadline = time.monotonic() + 40
        verdicts: list[Any] = []
        while time.monotonic() < deadline and len(verdicts) < 10:
            time.sleep(0.5)
            shown = json.loads(
                _run(tmp_path, env, "searches", "show", search_id, "--json").stdout
            )
            verdicts = [
                r.get("verdict")
                for r in shown["results"]["printables"]
                if r.get("verdict")
            ]
        assert len(verdicts) == 10 and verdicts.count("fits") == 5
    assert fake_calls(state)[0]["role"] == "classify"
