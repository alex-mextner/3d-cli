"""Tests for the LLM ranking of `3d models search --rank` (#577): the classifier (schema validation, batching,
parallelism, cache, retry once, partial answers, timeouts, guards), the planner (schema, dedupe, wide+narrow), and
the streaming pipeline against a scripted backend and a scripted catalogue (events order, phase 1 before any model
call, rewrite thresholds, session linkage, persistence, answers, follow-ups, graceful degradation)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from llm_fixtures import (
    FakeCatalog,
    ScriptedBackend,
    model,
    numbers_in,
    timeout_error,
    verdicts_json,
)

from ai.backends import BackendError
from errors import InvalidArgument, MissingDependency
from library import db as librarydb
from library import planner, rankflow, ranking, rankstore
from library import search as searchcore
from library.llm import (
    InvalidAnswer,
    LlmClient,
    LlmConfig,
    LlmFailure,
    config_from_mapping,
    extract_json_object,
    load_llm_config,
)

QUERY = "soil moisture sensor probe case"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("THREED_KEYCHAIN", "off")
    monkeypatch.delenv("THREED_AI_CONFIG", raising=False)
    monkeypatch.delenv("THREED_OMP", raising=False)


@pytest.fixture
def dbpath(tmp_path: Path) -> str:
    return str(tmp_path / "library.db")


def fits_if_probe(card: dict[str, Any]) -> tuple[str, float, str]:
    title = str(card["title"]).lower()
    if "keychain" in title:
        return "no", 0.05, "a keychain, not a case"
    if "probe" in title:
        return "fits", 0.9, "a case for a probe"
    return "maybe", 0.5, "a generic case"


def classify_handler(role: str, model_name: str, user: str) -> str:
    assert role == "classify", role
    return verdicts_json(user, fits_if_probe)


def entries_for(
    n: int, *, site: str = "printables", title: str = "Probe case"
) -> list[ranking.Entry]:
    return [
        ranking.entry_for(searchcore.result_record(model(site, i, f"{title} {i}"), i))
        for i in range(1, n + 1)
    ]


def client_for(backend: ScriptedBackend, **config: Any) -> LlmClient:
    return LlmClient(backend, LlmConfig(**config))


# --------------------------------------------------------------------------- llm.py: config and JSON
def test_config_defaults_come_from_one_section_and_validate(tmp_path: Path) -> None:
    cfg = load_llm_config()
    assert (cfg.classifier, cfg.planner, cfg.batch_size, cfg.enabled) == (
        "haiku",
        "sonnet",
        25,
        False,
    )
    path = tmp_path / "ai.json"
    path.write_text(
        json.dumps(
            {
                "llm": {
                    "classifier": "claude-haiku-4-5",
                    "batch_size": 10,
                    "enabled": True,
                    "prompts": {"classify": "custom"},
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = load_llm_config({"planner": "opus"}, path=str(path))
    assert (cfg.classifier, cfg.planner, cfg.batch_size, cfg.enabled) == (
        "claude-haiku-4-5",
        "opus",
        10,
        True,
    )
    assert (
        cfg.prompt("classify", "default") == "custom"
        and cfg.prompt("plan", "default") == "default"
    )
    for bad in (
        {"batch_size": 0},
        {"classifier": ""},
        {"unknown": 1},
        {"max_queries": 1},
        {"prompts": {"x": "y"}},
        {"planner_tools": "web"},
        {"timeout_s": -1},
    ):
        with pytest.raises(InvalidArgument):
            config_from_mapping(bad)


@pytest.mark.parametrize(
    "text", ['{"a": 1}', '```json\n{"a": 1}\n```', 'Sure! {"a": 1} Hope this helps.']
)
def test_json_is_found_in_fences_and_prose(text: str) -> None:
    assert extract_json_object(text) == {"a": 1}


@pytest.mark.parametrize("text", ["no json here", '{"a": ', "[1, 2]"])
def test_text_without_a_json_object_is_invalid(text: str) -> None:
    with pytest.raises(InvalidAnswer):
        extract_json_object(text)


# --------------------------------------------------------------------------- ranking.py: the card and the answer
def test_the_card_carries_the_listing_and_the_deterministic_file_facts() -> None:
    rec = searchcore.result_record(
        model(
            "printables",
            7,
            "Probe case",
            files=(
                ("case.step", 1_000_000),
                ("case.stl", 400_000),
                ("lid.stl", 100_000),
            ),
        ),
        1,
    )
    card = ranking.result_card(rec)
    assert (
        card["title"] == "Probe case"
        and card["license"] == "CC-BY"
        and card["price"] == "free"
    )
    files = card["files"]
    assert (
        files["cad"] is True
        and files["cad_formats"] == ["step"]
        and files["mesh_formats"] == ["stl"]
    )
    assert files["multi_part"] is True and files["assembly"] is False
    unknown = searchcore.result_record(
        type(model("printables", 8, "No files"))(
            site="printables", id="8", title="No files", url="u"
        ),
        2,
    )
    assert ranking.result_card(unknown)["files"].startswith("unknown")
    assert ranking.input_hash(card) == ranking.input_hash(dict(card))
    assert ranking.input_hash(card) != ranking.input_hash({**card, "title": "Other"})


def test_verdicts_are_validated_entry_by_entry() -> None:
    good = {"n": 1, "verdict": "fits", "score": 0.8, "rationale": "x"}
    answer = json.dumps(
        {
            "verdicts": [
                good,
                {"n": 2, "verdict": "great", "score": 0.8, "rationale": "x"},
                {"n": 3, "verdict": "no", "score": 1.5, "rationale": "x"},
                {"n": 4, "verdict": "maybe", "score": True, "rationale": "x"},
                {"n": 5, "verdict": "no", "score": 0.1, "rationale": " "},
                {"n": 9, "verdict": "no", "score": 0.1, "rationale": "x"},
                {"n": 1, "verdict": "no", "score": 0.1, "rationale": "dup"},
            ]
        }
    )
    accepted, problems = ranking.parse_verdicts(answer, {1, 2, 3, 4, 5, 6})
    assert (
        list(accepted) == [1]
        and accepted[1].verdict == "fits"
        and accepted[1].score == 0.8
    )
    text = "; ".join(problems)
    for needle in (
        "verdict 'great'",
        "score must be a number",
        "rationale must be",
        "n=9",
        "given twice",
        "no verdict for n=2, 3, 4, 5, 6",
    ):
        assert needle in text, needle
    with pytest.raises(InvalidAnswer):
        ranking.parse_verdicts('{"verdicts": "nope"}', {1})


def test_the_final_order_is_fits_maybe_unjudged_no_then_score_then_search_order() -> (
    None
):
    rows = [
        ("no", 0.1, 1),
        (None, None, 2),
        ("maybe", 0.6, 3),
        ("fits", 0.7, 4),
        ("fits", 0.9, 5),
        ("maybe", 0.6, 6),
    ]
    ordered = sorted(rows, key=lambda r: ranking.sort_key(*r))
    assert [r[2] for r in ordered] == [5, 4, 3, 6, 2, 1]


# --------------------------------------------------------------------------- the classifier
def test_results_are_judged_in_batches_in_parallel_and_streamed_as_they_finish(
    dbpath: str,
) -> None:
    gate = threading.Barrier(
        3, timeout=10
    )  # all three batches must be in flight together

    def handler(role: str, model_name: str, user: str) -> str:
        gate.wait()
        return verdicts_json(user, fits_if_probe)

    backend = ScriptedBackend(handler)
    client = client_for(backend, batch_size=25, parallel=3)
    outcomes = list(
        ranking.classify(client, None, QUERY, {}, entries_for(60), model="haiku")
    )
    assert sorted(
        len(c["user"].splitlines()) and len(numbers_in(c["user"]))
        for c in backend.calls
    ) == [10, 25, 25]
    assert sum(len(o.verdicts) for o in outcomes) == 60 and all(
        o.failure is None for o in outcomes
    )
    assert {c["model"] for c in backend.calls} == {"haiku"} and all(
        c["tools"] == () for c in backend.calls
    )


def test_a_second_run_of_the_same_cards_is_answered_from_the_cache(dbpath: str) -> None:
    conn = librarydb.open_library(dbpath)
    entries = entries_for(30)
    backend = ScriptedBackend(classify_handler)
    first = list(
        ranking.classify(client_for(backend), conn, QUERY, {}, entries, model="haiku")
    )
    assert len(backend.calls) == 2 and not any(o.cached for o in first)
    again = list(
        ranking.classify(client_for(backend), conn, QUERY, {}, entries, model="haiku")
    )
    assert (
        len(backend.calls) == 2
        and [o.cached for o in again] == [True]
        and len(again[0].verdicts) == 30
    )
    assert all(v.cached for v in again[0].verdicts.values())
    refreshed = list(
        ranking.classify(
            client_for(backend), conn, QUERY, {}, entries, model="haiku", refresh=True
        )
    )
    assert len(backend.calls) == 4 and not any(o.cached for o in refreshed)
    # another model, another question, another answered constraint, a changed card: all re-judge
    list(
        ranking.classify(
            client_for(backend), conn, QUERY, {}, entries[:3], model="sonnet"
        )
    )
    list(
        ranking.classify(
            client_for(backend), conn, "other query", {}, entries[:3], model="haiku"
        )
    )
    list(
        ranking.classify(
            client_for(backend),
            conn,
            QUERY,
            {"need_cad": "yes"},
            entries[:3],
            model="haiku",
        )
    )
    changed = entries_for(3, title="Different case")
    list(ranking.classify(client_for(backend), conn, QUERY, {}, changed, model="haiku"))
    assert len(backend.calls) == 4 + 4
    assert "need_cad = yes" in backend.calls[-2]["user"]


def test_invalid_json_is_retried_once_and_the_retry_names_the_problem() -> None:
    answers = iter(["I cannot do that", None])

    def handler(role: str, model_name: str, user: str) -> str:
        reply = next(answers)
        return reply if reply is not None else verdicts_json(user, fits_if_probe)

    backend = ScriptedBackend(handler)
    client = client_for(backend)
    [outcome] = ranking.classify(client, None, QUERY, {}, entries_for(5), model="haiku")
    assert (
        outcome.failure is None
        and len(outcome.verdicts) == 5
        and len(backend.calls) == 2
    )
    assert (
        "Your previous answer was rejected" in backend.calls[1]["user"]
        and "holds no JSON object" in backend.calls[1]["user"]
    )
    assert [r.status for r in client.records] == ["invalid", "ok"]


def test_invalid_twice_leaves_the_results_unjudged_with_a_failure() -> None:
    backend = ScriptedBackend(lambda *_: "still not json")
    client = client_for(backend)
    [outcome] = ranking.classify(client, None, QUERY, {}, entries_for(4), model="haiku")
    assert (
        len(backend.calls) == 2
        and outcome.verdicts == {}
        and len(outcome.unjudged) == 4
    )
    assert outcome.failure is not None and outcome.failure.code == "invalid"


def test_a_partial_answer_keeps_the_good_entries_and_retries_only_the_missing() -> None:
    calls = {"n": 0}

    def handler(role: str, model_name: str, user: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            wanted = numbers_in(user)
            return json.dumps(
                {
                    "verdicts": [
                        {
                            "n": wanted[0],
                            "verdict": "fits",
                            "score": 0.9,
                            "rationale": "ok",
                        },
                        {
                            "n": wanted[1],
                            "verdict": "nope",
                            "score": 0.9,
                            "rationale": "bad verdict",
                        },
                    ]
                }
            )
        assert numbers_in(user) == [2, 3, 4, 5], user
        return verdicts_json(user, fits_if_probe)

    backend = ScriptedBackend(handler)
    [outcome] = ranking.classify(
        client_for(backend), None, QUERY, {}, entries_for(5), model="haiku"
    )
    assert (
        len(outcome.verdicts) == 5
        and outcome.failure is None
        and len(backend.calls) == 2
    )


def test_a_timeout_is_not_retried_and_leaves_the_batch_unjudged() -> None:
    backend = ScriptedBackend(lambda *_: timeout_error())
    client = client_for(backend)
    [outcome] = ranking.classify(client, None, QUERY, {}, entries_for(3), model="haiku")
    assert len(backend.calls) == 1 and len(outcome.unjudged) == 3
    assert outcome.failure is not None and outcome.failure.code == "timeout"
    assert [r.status for r in client.records] == ["timeout"]


def test_the_call_limit_stops_new_batches_and_reports_them_unjudged() -> None:
    backend = ScriptedBackend(classify_handler)
    client = client_for(backend, batch_size=5, parallel=1, max_calls=2)
    outcomes = list(
        ranking.classify(client, None, QUERY, {}, entries_for(20), model="haiku")
    )
    assert len(backend.calls) == 2
    judged = sum(len(o.verdicts) for o in outcomes)
    skipped = [
        o for o in outcomes if o.failure is not None and o.failure.code == "budget"
    ]
    assert judged == 10 and sum(len(o.unjudged) for o in skipped) == 10
    assert client.summary()["by_status"].get("budget", 0) >= 1


def test_the_token_budget_stops_calls_once_spent() -> None:
    backend = ScriptedBackend(classify_handler)  # every call reports 1120 tokens
    client = client_for(backend, batch_size=5, parallel=1, token_budget=2000)
    outcomes = list(
        ranking.classify(client, None, QUERY, {}, entries_for(20), model="haiku")
    )
    assert len(backend.calls) == 2 and any(
        o.failure and o.failure.code == "budget" for o in outcomes
    )
    summary = client.summary()
    assert (
        summary["total_tokens"] == 2240
        and summary["input_tokens"] == 200
        and summary["cost_usd"] == 0.02
    )


# --------------------------------------------------------------------------- the planner
def plan_json(**overrides: Any) -> str:
    doc: dict[str, Any] = {
        "schema": planner.SCHEMA,
        "summary": "descriptive phrase -> part names",
        "queries": [
            {"query": "sensor enclosure", "kind": "wide", "rationale": "the category"},
            {
                "query": "capacitive probe case v1.2",
                "kind": "narrow",
                "rationale": "the exact part",
            },
            {"query": "Sensorgehäuse Erde", "kind": "rewrite", "rationale": "German"},
        ],
        "followups": [
            {"query": "plant stake sensor holder", "rationale": "a stake variant"}
        ],
        "questions": [
            {
                "key": "need_cad",
                "type": "single",
                "prompt": "Do you need CAD files?",
                "choices": ["yes", "no"],
            },
            {"key": "printer", "type": "text", "prompt": "Which printer?"},
        ],
    }
    doc.update(overrides)
    return json.dumps(doc)


def test_a_plan_has_wide_narrow_and_rewrite_queries_followups_and_typed_questions() -> (
    None
):
    plan = planner.parse_plan(plan_json(), mode="rewrite")
    assert [q.kind for q in plan.queries] == [
        "wide",
        "narrow",
        "rewrite",
    ] and plan.problems == ()
    assert [q.kind for q in plan.followups] == ["followup"]
    assert [(q.key, q.type, q.choices) for q in plan.questions] == [
        ("need_cad", "single", ("yes", "no")),
        ("printer", "text", ()),
    ]
    suggest = planner.parse_plan(plan_json(), mode="suggest")
    assert (
        suggest.queries == () and len(suggest.followups) == 1
    )  # a suggest plan runs nothing


def test_a_plan_without_a_narrow_query_names_the_gap_and_bad_entries_are_dropped() -> (
    None
):
    doc = plan_json(
        queries=[
            {"query": "sensor enclosure", "kind": "wide"},
            {"query": "", "kind": "narrow"},
            {"query": "x y", "kind": "odd"},
        ],
        questions=[
            {"key": "Bad Key", "type": "text", "prompt": "p"},
            {"key": "k1", "type": "single", "prompt": "p", "choices": ["only"]},
            {"key": "k2", "type": "text", "prompt": "p", "choices": ["a", "b"]},
            {"key": "k3", "type": "multi", "prompt": "p", "choices": ["a", "b"]},
        ],
    )
    plan = planner.parse_plan(doc, mode="rewrite", answered=["k9"])
    assert [q.query for q in plan.queries] == ["sensor enclosure"] and [
        q.key for q in plan.questions
    ] == ["k3"]
    text = "; ".join(plan.problems)
    assert (
        "at least one `narrow`" in text
        and "kind 'odd'" in text
        and "`key` must match" in text
    )
    assert "at least 2 choices" in text and "text question takes no choices" in text


def test_answered_questions_are_not_asked_again() -> None:
    plan = planner.parse_plan(plan_json(), mode="suggest", answered=["need_cad"])
    assert [q.key for q in plan.questions] == ["printer"]


def test_queries_equal_after_normalising_are_skipped_and_wide_and_narrow_survive_the_cap() -> (
    None
):
    planned = [
        planner.PlannedQuery("Sensor  Enclosure!", "wide"),
        planner.PlannedQuery("probe case", "rewrite"),
        planner.PlannedQuery("case for probe", "rewrite"),
        planner.PlannedQuery("probe case.", "narrow"),
        planner.PlannedQuery("sensor housing", "narrow"),
    ]
    kept, skipped = planner.dedupe_queries(planned, ["sensor enclosure", "soil probe"])
    assert [q.query for q in kept] == ["probe case", "case for probe", "sensor housing"]
    assert [(q.query, why) for q, why in skipped] == [
        ("Sensor  Enclosure!", "already run"),
        ("probe case.", "already run"),
    ]
    wide = [
        planner.PlannedQuery("a", "rewrite"),
        planner.PlannedQuery("b", "rewrite"),
        planner.PlannedQuery("c", "wide"),
        planner.PlannedQuery("d", "narrow"),
    ]
    assert [q.query for q in planner.select_queries(wide, 2)] == ["c", "d"]
    assert [q.query for q in planner.select_queries(wide, 3)] == ["a", "c", "d"]


def test_the_planner_gets_its_tools_a_longer_timeout_and_one_retry() -> None:
    answers = iter(["nope", plan_json()])
    backend = ScriptedBackend(lambda *_: next(answers))
    client = client_for(backend, planner_timeout_s=77.0)
    plan = planner.make_plan(client, "sonnet", mode="rewrite", user="q")
    assert len(plan.queries) == 3 and len(backend.calls) == 2
    assert (
        backend.calls[0]["tools"] == ("web_search", "read")
        and backend.calls[0]["timeout"] == 77.0
    )
    assert backend.calls[0]["model"] == "sonnet"


def test_a_retry_that_still_lacks_a_kind_keeps_the_valid_plan_with_its_problems() -> (
    None
):
    lacking = plan_json(queries=[{"query": "sensor enclosure", "kind": "wide"}])
    backend = ScriptedBackend(lambda *_: lacking)
    plan = planner.make_plan(client_for(backend), "sonnet", mode="rewrite", user="q")
    assert (
        len(backend.calls) == 2
        and [q.kind for q in plan.queries] == ["wide"]
        and plan.problems
    )
    broken = ScriptedBackend(lambda *_: "garbage")
    with pytest.raises(LlmFailure) as exc:
        planner.make_plan(client_for(broken), "sonnet", mode="rewrite", user="q")
    assert exc.value.code == "invalid" and len(broken.calls) == 2


# --------------------------------------------------------------------------- the pipeline
def good_catalog(n: int = 6) -> dict[str, dict[str, list[Any]]]:
    return {
        QUERY: {
            "printables": [
                model("printables", i, f"Soil probe case {i}") for i in range(1, n + 1)
            ],
            "thingiverse": [
                model("thingiverse", i, f"Keychain {i}") for i in range(1, n + 1)
            ],
        }
    }


def run_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    dbpath: str,
    catalog: FakeCatalog,
    backend: ScriptedBackend | None,
    *,
    query: str = QUERY,
    follow: tuple[int, ...] = (),
    rewrite: bool = True,
    answers: dict[str, str] | None = None,
    **config: Any,
) -> tuple[list[dict[str, Any]], rankflow.Pipeline]:
    monkeypatch.setattr("marketplaces.catalog.search", catalog)
    options = rankflow.RankOptions(
        LlmConfig(**config),
        enabled=backend is not None,
        rewrite=rewrite,
        answers=answers or {},
        follow=follow,
        backend=backend,
    )
    pipeline = rankflow.Pipeline(
        options,
        searchcore.SearchOptions(
            sites=("printables", "thingiverse"), limit=50, db_path=dbpath
        ),
    )
    return list(pipeline.search(query)), pipeline


def names(events: list[dict[str, Any]]) -> list[str]:
    return [e["event"] for e in events]


def test_phase_one_results_are_emitted_before_the_first_model_call(
    monkeypatch: pytest.MonkeyPatch, dbpath: str
) -> None:
    backend = ScriptedBackend(
        lambda role, m, user: (
            verdicts_json(user, fits_if_probe) if role == "classify" else plan_json()
        )
    )
    monkeypatch.setattr("marketplaces.catalog.search", FakeCatalog(good_catalog()))
    options = rankflow.RankOptions(LlmConfig(), backend=backend)
    pipeline = rankflow.Pipeline(
        options,
        searchcore.SearchOptions(
            sites=("printables", "thingiverse"), limit=50, db_path=dbpath
        ),
    )
    for event in pipeline.search(
        QUERY
    ):  # what a consumer sees, in the order it sees it
        backend.events.append(f"event:{event['event']}")
    first_call = backend.events.index("call:classify")
    before = backend.events[:first_call]
    assert (
        before.count("event:result") == 12
        and "event:search" in before
        and "event:verdict" not in before
    )
    assert backend.events[first_call - 1] == "event:phase"


def test_a_search_is_streamed_as_search_results_verdicts_and_done(
    monkeypatch: pytest.MonkeyPatch, dbpath: str
) -> None:
    backend = ScriptedBackend(classify_handler)
    events, pipeline = run_pipeline(
        monkeypatch, dbpath, FakeCatalog(good_catalog()), backend, rewrite=False
    )
    order = names(events)
    assert order[:2] == ["start", "search"] and order[-1] == "done"
    assert order.count("result") == 12 and order.count("verdict") == 12
    assert order.index("phase") == 2 + 12  # phase 2 starts after every phase-1 result
    assert all(e["schema"] == rankflow.EVENT_SCHEMA for e in events)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    results = [e for e in events if e["event"] == "result"]
    assert [(r["site"], r["rank"]) for r in results[:3]] == [
        ("printables", 1),
        ("printables", 2),
        ("printables", 3),
    ]
    assert (
        results[0]["card"]["title"] == "Soil probe case 1"
        and results[0]["position"] == 1
    )
    done = events[-1]
    assert done["counts"] == {
        "results": 12,
        "fits": 6,
        "maybe": 0,
        "no": 6,
        "unjudged": 0,
    }
    assert [r["verdict"] for r in done["ranking"]] == ["fits"] * 6 + ["no"] * 6
    assert (
        done["ranking"][0]["site"] == "printables"
        and done["llm"]["calls"] == 1
        and done["llm"]["total_tokens"] == 1120
    )
    assert pipeline.degraded is None and backend.roles() == ["classify"]


def test_enough_fits_means_no_rewrite_but_followups_and_questions_are_still_planned(
    monkeypatch: pytest.MonkeyPatch, dbpath: str
) -> None:
    modes: list[str] = []

    def handler(role: str, model_name: str, user: str) -> str:
        if role == "classify":
            return verdicts_json(user, fits_if_probe)
        modes.append(user.splitlines()[0])
        return plan_json()

    catalog = FakeCatalog(good_catalog())
    events, _ = run_pipeline(
        monkeypatch, dbpath, catalog, ScriptedBackend(handler), rewrite_below=3
    )
    assert modes == ["Mode: suggest (return queries: [])"] and catalog.queries() == [
        QUERY
    ]
    plan = next(e for e in events if e["event"] == "plan" and e["status"] == "ready")
    assert plan["mode"] == "suggest" and plan["queries"] == []
    assert names(events).count("question") == 2
    suggested = [
        e for e in events if e["event"] == "query" and e["status"] == "suggested"
    ]
    assert [(e["n"], e["query"], e["kind"]) for e in suggested] == [
        (1, "plant stake sensor holder", "followup")
    ]
    assert [e["event"] for e in events[-4:]] == [
        "question",
        "question",
        "query",
        "done",
    ]


def test_too_few_fits_reworks_the_query_into_linked_wide_and_narrow_sessions(
    monkeypatch: pytest.MonkeyPatch, dbpath: str
) -> None:
    def handler(role: str, model_name: str, user: str) -> str:
        if role == "classify":
            return verdicts_json(user, fits_if_probe)
        return plan_json(
            queries=[
                {
                    "query": "sensor enclosure",
                    "kind": "wide",
                    "rationale": "the category",
                },
                {
                    "query": "capacitive probe case v1.2",
                    "kind": "narrow",
                    "rationale": "the exact part",
                },
                {
                    "query": QUERY.upper(),
                    "kind": "rewrite",
                    "rationale": "the original again",
                },
            ]
        )

    catalog = FakeCatalog(
        {
            **good_catalog(),
            "sensor enclosure": {
                "printables": [
                    model("printables", 3, "Soil probe case 3"),
                    model("printables", 40, "Generic enclosure 40"),
                ]
            },
            "capacitive probe case v1.2": {
                "printables": [model("printables", 50, "Probe case v1.2 50")],
                "thingiverse": [model("thingiverse", 3, "Keychain 3")],
            },
        }
    )
    catalog.answers[QUERY]["printables"] = [
        model("printables", i, f"Generic case {i}") for i in range(1, 4)
    ]  # 0 fits
    catalog.answers[QUERY]["thingiverse"] = []
    backend = ScriptedBackend(handler)
    events, pipeline = run_pipeline(
        monkeypatch, dbpath, catalog, backend, rewrite_below=3
    )
    assert catalog.queries().count(QUERY) == 1 and sorted(catalog.queries()) == sorted(
        [QUERY, "sensor enclosure", "capacitive probe case v1.2"]
    )
    plan = next(e for e in events if e["event"] == "plan" and e["status"] == "ready")
    assert plan["mode"] == "rewrite" and plan["trigger"] == "0 fits < rewrite_below 3"
    assert [q["kind"] for q in plan["queries"]] == ["wide", "narrow"]
    assert [(s["query"], s["reason"]) for s in plan["skipped"]] == [
        (QUERY.upper(), "already run")
    ]
    ran = [e for e in events if e["event"] == "query" and e["status"] == "run"]
    assert [
        (e["query"], e["kind"], e["results"], e["new"], e["duplicates"]) for e in ran
    ] == [
        ("sensor enclosure", "wide", 2, 1, 1),
        ("capacitive probe case v1.2", "narrow", 2, 2, 0),
    ]
    # de-duplicated by (site, id): the printables hit 3 of the wide query was already in the pool
    pooled = [(e["site"], e["id"]) for e in events if e["event"] == "result"]
    assert len(pooled) == len(set(pooled)) == 3 + 1 + 2
    assert {e["kind"] for e in events if e["event"] == "result"} == {
        "user",
        "wide",
        "narrow",
    }
    conn = librarydb.open_library(dbpath)
    rows = conn.execute(
        "SELECT id, uid, query, kind, parent_id FROM search_session ORDER BY id"
    ).fetchall()
    sessions = {
        s["query"]: s for s in rows
    }  # the extra queries run in parallel: their ids come in either order
    assert {(s["query"], s["kind"]) for s in rows} == {
        (QUERY, "user"),
        ("sensor enclosure", "wide"),
        ("capacitive probe case v1.2", "narrow"),
    }
    root_id = sessions[QUERY]["id"]
    assert (
        sessions["sensor enclosure"]["parent_id"]
        == sessions["capacitive probe case v1.2"]["parent_id"]
        == root_id
    )
    done = events[-1]
    assert done["counts"]["results"] == 6 and [q["query"] for q in done["queries"]] == [
        "sensor enclosure",
        "capacitive probe case v1.2",
    ]
    # the duplicate carries the pooled verdict on the child's own row, and everything is visible read back
    doc = searchcore.session_doc(conn, str(sessions["sensor enclosure"]["uid"]))
    assert all("verdict" in rec for rec in doc["results"]["printables"])
    root = searchcore.session_doc(conn, str(sessions[QUERY]["uid"]))
    assert [(q["query"], q["status"]) for q in root["queries"]] == [
        ("sensor enclosure", "run"),
        ("capacitive probe case v1.2", "run"),
        (
            QUERY.upper(),
            "skipped",
        ),  # a repeat of the original is recorded as skipped, never run
        ("plant stake sensor holder", "suggested"),
    ]
    assert [q["key"] for q in root["questions"]] == [
        "need_cad",
        "printer",
    ] and pipeline.degraded is None


def test_a_zero_result_search_is_reworked_too(
    monkeypatch: pytest.MonkeyPatch, dbpath: str
) -> None:
    def handler(role: str, model_name: str, user: str) -> str:
        return plan_json() if role == "plan" else verdicts_json(user, fits_if_probe)

    catalog = FakeCatalog(
        {"sensor enclosure": {"printables": [model("printables", 1, "Probe case 1")]}}
    )
    events, _ = run_pipeline(
        monkeypatch,
        dbpath,
        catalog,
        ScriptedBackend(handler),
        query="a little mole shaped box",
    )
    assert any(e["event"] == "plan" and e["mode"] == "rewrite" for e in events)
    assert "sensor enclosure" in catalog.queries() and events[-1]["counts"]["fits"] == 1


def test_every_verdict_plan_question_and_call_is_persisted(
    monkeypatch: pytest.MonkeyPatch, dbpath: str
) -> None:
    def handler(role: str, model_name: str, user: str) -> str:
        return plan_json() if role == "plan" else verdicts_json(user, fits_if_probe)

    events, pipeline = run_pipeline(
        monkeypatch, dbpath, FakeCatalog(good_catalog()), ScriptedBackend(handler)
    )
    conn = librarydb.open_library(dbpath)
    rows = conn.execute(
        "SELECT site, verdict, score, rationale FROM search_result ORDER BY id"
    ).fetchall()
    assert len(rows) == 12 and {r["verdict"] for r in rows} == {"fits", "no"}
    assert all(r["rationale"] for r in rows) and {r["score"] for r in rows} == {
        0.9,
        0.05,
    }
    questions = rankstore.list_questions(conn, 1)
    assert [(q["key"], q["type"], q["choices"], q["answer"]) for q in questions] == [
        ("need_cad", "single", ["yes", "no"], None),
        ("printer", "text", [], None),
    ]
    calls = rankstore.list_llm_calls(conn, 1)
    assert [(c["role"], c["status"], c["model"]) for c in calls] == [
        ("classify", "ok", "haiku"),
        ("plan", "ok", "sonnet"),
    ]
    assert calls[0]["input_tokens"] == 100 and calls[0]["cache_read_tokens"] == 1000
    cached = conn.execute("SELECT COUNT(*) FROM llm_verdict_cache").fetchone()[0]
    assert cached == 12 and events[-1]["questions"][0]["id"] == questions[0]["id"]


def test_no_llm_builds_no_backend_and_matches_the_plain_search(
    monkeypatch: pytest.MonkeyPatch, dbpath: str
) -> None:
    monkeypatch.setenv(
        "THREED_OMP", "/nonexistent/omp"
    )  # a backend would be resolved from this: it must not be
    catalog = FakeCatalog(good_catalog())
    events, pipeline = run_pipeline(monkeypatch, dbpath, catalog, None)
    assert names(events) == ["start", "search", *["result"] * 12, "done"]
    assert (
        events[0]["llm"]["enabled"] is False
        and events[-1]["llm"] is None
        and events[-1]["degraded"] is None
    )
    assert pipeline.client is None
    plain = searchcore.run_search(
        QUERY,
        searchcore.SearchOptions(
            sites=("printables", "thingiverse"), limit=50, save=False
        ),
    ).doc
    assert [r["card"] for r in events if r["event"] == "result"] == [
        rec for site in plain["sites"] for rec in plain["results"][site]
    ]


def test_a_missing_backend_keeps_the_results_with_one_unavailable_line(
    monkeypatch: pytest.MonkeyPatch, dbpath: str
) -> None:
    monkeypatch.setenv("THREED_OMP", "/nonexistent/omp")
    monkeypatch.setattr("marketplaces.catalog.search", FakeCatalog(good_catalog()))
    pipeline = rankflow.Pipeline(
        rankflow.RankOptions(LlmConfig()),
        searchcore.SearchOptions(sites=("printables",), db_path=dbpath),
    )
    events = list(pipeline.search(QUERY))
    warnings = [e for e in events if e["event"] == "warning"]
    assert (
        len(warnings) == 1
        and warnings[0]["message"].startswith("LLM: unavailable (")
        and warnings[0]["code"] == "llm_unavailable"
    )
    assert (
        names(events).count("result") == 6
        and "verdict" not in names(events)
        and events[-1]["counts"]["unjudged"] == 6
    )
    assert events[-1]["degraded"] is not None and pipeline.degraded is not None


def test_require_fails_with_exit_127_before_any_search(
    monkeypatch: pytest.MonkeyPatch, dbpath: str
) -> None:
    monkeypatch.setenv("THREED_OMP", "/nonexistent/omp")
    catalog = FakeCatalog(good_catalog())
    monkeypatch.setattr("marketplaces.catalog.search", catalog)
    options = rankflow.RankOptions(LlmConfig(), require=True)
    pipeline = rankflow.Pipeline(
        options, searchcore.SearchOptions(sites=("printables",), db_path=dbpath)
    )
    with pytest.raises(MissingDependency) as exc:
        next(pipeline.search(QUERY))
    assert exc.value.exit_code == 127 and catalog.asked == []


@pytest.mark.parametrize(
    "failure",
    [
        timeout_error(),
        BackendError("omp exited 3 without an answer", command="ai"),
        "not json at all",
    ],
)
def test_a_failing_classifier_leaves_the_deterministic_results_and_one_warning(
    monkeypatch: pytest.MonkeyPatch, dbpath: str, failure: Any
) -> None:
    backend = ScriptedBackend(lambda *_: failure)
    events, pipeline = run_pipeline(
        monkeypatch, dbpath, FakeCatalog(good_catalog()), backend
    )
    warnings = [e for e in events if e["event"] == "warning"]
    assert len(warnings) == 1 and warnings[0]["message"].startswith(
        "LLM: unavailable ("
    )
    assert "verdict" not in names(events) and names(events).count("result") == 12
    assert (
        "plan" not in names(events)
        and backend.roles()[0] == "classify"
        and set(backend.roles()) == {"classify"}
    )
    conn = librarydb.open_library(dbpath)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM search_result WHERE verdict IS NOT NULL"
        ).fetchone()[0]
        == 0
    )
    assert events[-1]["counts"]["unjudged"] == 12 and pipeline.degraded


def test_a_failing_planner_costs_only_its_own_warning(
    monkeypatch: pytest.MonkeyPatch, dbpath: str
) -> None:
    def handler(role: str, model_name: str, user: str) -> str | BaseException:
        return (
            timeout_error()
            if role == "plan"
            else verdicts_json(user, lambda c: ("no", 0.1, "other"))
        )

    events, _ = run_pipeline(
        monkeypatch, dbpath, FakeCatalog(good_catalog()), ScriptedBackend(handler)
    )
    warnings = [e["message"] for e in events if e["event"] == "warning"]
    assert warnings == ["LLM: unavailable (planner: omp timed out after 1s)"]
    assert names(events).count("verdict") == 12 and any(
        e["event"] == "plan" and e["status"] == "failed" for e in events
    )


def test_rewrite_off_makes_no_planner_call(
    monkeypatch: pytest.MonkeyPatch, dbpath: str
) -> None:
    backend = ScriptedBackend(classify_handler)
    monkeypatch.setattr("marketplaces.catalog.search", FakeCatalog(good_catalog()))
    options = rankflow.RankOptions(LlmConfig(), rewrite=False, backend=backend)
    pipeline = rankflow.Pipeline(
        options,
        searchcore.SearchOptions(sites=("printables", "thingiverse"), db_path=dbpath),
    )
    events = list(pipeline.search(QUERY))
    assert (
        backend.roles() == ["classify"]
        and "plan" not in names(events)
        and "question" not in names(events)
    )


def test_answers_are_saved_applied_as_constraints_and_rejudge_on_rerank(
    monkeypatch: pytest.MonkeyPatch, dbpath: str
) -> None:
    def handler(role: str, model_name: str, user: str) -> str:
        return plan_json() if role == "plan" else verdicts_json(user, fits_if_probe)

    backend = ScriptedBackend(handler)
    _, first = run_pipeline(monkeypatch, dbpath, FakeCatalog(good_catalog()), backend)
    root = first.root_uid
    assert root is not None and backend.roles() == ["classify", "plan"]
    # rerank with nothing changed: every verdict from the cache, no classifier call, the plan is not repeated
    options = rankflow.RankOptions(LlmConfig(), backend=backend)
    again = rankflow.Pipeline(options, searchcore.SearchOptions(db_path=dbpath))
    events = list(again.session(root))
    assert backend.roles() == ["classify", "plan"] and all(
        e["cached"] for e in events if e["event"] == "verdict"
    )
    assert names(events).count("question") == 2 and any(
        e["event"] == "query" and e["status"] == "suggested" for e in events
    )
    # a new answer re-judges with the constraint in the prompt, is persisted, and is validated against its question
    answered = rankflow.Pipeline(
        rankflow.RankOptions(LlmConfig(), backend=backend, answers={"need_cad": "YES"}),
        searchcore.SearchOptions(db_path=dbpath),
    )
    events = list(answered.session(root))
    assert (
        backend.roles()[2:] == ["classify"]
        and "need_cad = yes" in backend.calls[2]["user"]
    )
    assert events[0]["answers"] == {"need_cad": "yes"} and not any(
        e["cached"] for e in events if e["event"] == "verdict"
    )
    conn = librarydb.open_library(dbpath)
    assert rankstore.answers_map(conn, 1) == {"need_cad": "yes"}
    with pytest.raises(InvalidArgument):
        list(
            rankflow.Pipeline(
                rankflow.RankOptions(
                    LlmConfig(), backend=backend, answers={"need_cad": "maybe"}
                ),
                searchcore.SearchOptions(db_path=dbpath),
            ).session(root)
        )
    free = rankflow.Pipeline(
        rankflow.RankOptions(LlmConfig(), backend=backend, answers={"max_mm": "60"}),
        searchcore.SearchOptions(db_path=dbpath),
    )
    list(free.session(root))
    assert rankstore.answers_map(conn, 1) == {"need_cad": "yes", "max_mm": "60"}


def test_a_follow_up_runs_as_a_followup_session_under_the_search(
    monkeypatch: pytest.MonkeyPatch, dbpath: str
) -> None:
    def handler(role: str, model_name: str, user: str) -> str:
        return plan_json() if role == "plan" else verdicts_json(user, fits_if_probe)

    catalog = FakeCatalog(
        {
            **good_catalog(),
            "plant stake sensor holder": {
                "printables": [model("printables", 70, "Probe stake holder 70")]
            },
        }
    )
    backend = ScriptedBackend(handler)
    _, first = run_pipeline(monkeypatch, dbpath, catalog, backend)
    pipeline = rankflow.Pipeline(
        rankflow.RankOptions(LlmConfig(), backend=backend, follow=(1,)),
        searchcore.SearchOptions(sites=("printables",), db_path=dbpath),
    )
    events = list(pipeline.session(str(first.root_uid)))
    ran = [e for e in events if e["event"] == "query" and e["status"] == "run"]
    assert [(e["query"], e["kind"], e["new"]) for e in ran] == [
        ("plant stake sensor holder", "followup", 1)
    ]
    conn = librarydb.open_library(dbpath)
    child = conn.execute(
        "SELECT * FROM search_session WHERE kind = 'followup'"
    ).fetchone()
    assert child["parent_id"] == 1 and child["query"] == "plant stake sensor holder"
    assert rankstore.list_plan_queries(conn, 1)[-1]["status"] == "run"
    assert events[-1]["counts"]["results"] == 13
    # following the same number again, or one that does not exist, is a warning and changes nothing
    twice = rankflow.Pipeline(
        rankflow.RankOptions(LlmConfig(), backend=backend, follow=(1, 9)),
        searchcore.SearchOptions(sites=("printables",), db_path=dbpath),
    )
    warnings = [
        e["message"]
        for e in twice.session(str(first.root_uid))
        if e["event"] == "warning"
    ]
    assert any("already run" in w for w in warnings) and any(
        "no follow-up 9" in w for w in warnings
    )


def test_a_slow_model_does_not_delay_phase_one_and_the_stream_is_lazy(
    monkeypatch: pytest.MonkeyPatch, dbpath: str
) -> None:
    backend = ScriptedBackend(classify_handler, delay_s=0.6)
    monkeypatch.setattr("marketplaces.catalog.search", FakeCatalog(good_catalog()))
    pipeline = rankflow.Pipeline(
        rankflow.RankOptions(LlmConfig(), rewrite=False, backend=backend),
        searchcore.SearchOptions(sites=("printables", "thingiverse"), db_path=dbpath),
    )
    stream = pipeline.search(QUERY)
    started = time.monotonic()
    seen = []
    for event in stream:
        seen.append(event["event"])
        if event["event"] == "phase":
            break
    assert (
        time.monotonic() - started < 0.5 and backend.calls == []
    )  # nothing waited on the model
    stream.close()  # type: ignore[attr-defined]  # a generator, typed as Iterator


def test_the_deterministic_rank_action_reads_the_stored_order_and_the_llm_action_streams(
    monkeypatch: pytest.MonkeyPatch, dbpath: str
) -> None:
    from library import actions

    _, first = run_pipeline(monkeypatch, dbpath, FakeCatalog(good_catalog()), None)
    plain = actions.rank(str(first.root_uid), db_path=dbpath, limit=3)
    assert (
        plain["ok"]
        and plain["schema"] == "3d.action/1"
        and len(plain["data"]["ranked"]) == 3
    )
    assert [r["rank"] for r in plain["data"]["ranked"]] == [1, 2, 3] and plain["data"][
        "llm"
    ] is None
    assert plain["data"]["ranked"][0]["verdict"] is None and plain["data"][
        "counts"
    ] == {"results": 12}
    monkeypatch.setenv("THREED_OMP", "/nonexistent/omp")
    unavailable = actions.invoke(
        "rank", session=str(first.root_uid), mode="llm", db_path=dbpath
    )
    assert (
        unavailable["ok"] is True
        and unavailable["warnings"][0]["code"] == "llm_unavailable"
    )
    required = actions.invoke(
        "rank", session=str(first.root_uid), mode="llm", db_path=dbpath, require=True
    )
    assert required["ok"] is False and required["errors"][0]["code"] == "error"
    bad = actions.invoke("rank", session="x", mode="telepathy", db_path=dbpath)
    assert bad["ok"] is False and bad["errors"][0]["code"] == "invalid_argument"
    assert bad["errors"][0]["message"].startswith("got mode='telepathy'")
