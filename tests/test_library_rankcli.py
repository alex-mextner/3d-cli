"""Flag handling of `3d models search --rank` / `rerank` (#577): which flags switch ranking on, how they override
the `llm` section of ai.json, and which combinations are usage errors. The stream itself is tested in
test_library_rank.py and the real CLI in tests/e2e/test_models_rank_workflows.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from errors import InvalidArgument, UsageError
from library import rankcli, rankstore
from library.rankflow import RankOptions


@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("THREED_AI_CONFIG", raising=False)


def options(*flags: str, rerank: bool = False) -> RankOptions | None:
    opts: dict[str, str] = {}
    multi: dict[str, list[str]] = {}
    for flag in flags:
        name, _, value = flag.partition("=")
        if name in rankcli.REPEAT_FLAGS:
            multi.setdefault(name, []).append(value)
        else:
            opts[name] = value or "1"
    return rankcli.rank_options(opts, multi, rerank=rerank)


def ranked(*flags: str, rerank: bool = False) -> RankOptions:
    rank = options(*flags, rerank=rerank)
    assert rank is not None
    return rank


def test_no_ranking_flag_means_the_unchanged_search() -> None:
    assert options() is None and options("--no-llm") is None


def test_ndjson_alone_streams_without_a_model() -> None:
    rank = options("--ndjson")
    assert rank is not None and rank.enabled is False


@pytest.mark.parametrize(
    "flags",
    [
        ("--rank",),
        ("--rank=haiku",),
        ("--classifier=haiku",),
        ("--planner=sonnet",),
        ("--llm=require",),
        ("--answer=need_cad=yes",),
        ("--follow=1",),
    ],
)
def test_these_flags_switch_ranking_on(flags: tuple[str, ...]) -> None:
    rank = options(*flags)
    assert rank is not None and rank.enabled is True


def test_models_and_guards_override_the_config_and_rewrite_off_is_a_flag() -> None:
    rank = options(
        "--rank=my-haiku",
        "--planner=my-sonnet",
        "--max-calls=3",
        "--token-budget=5000",
        "--batch-size=7",
        "--max-queries=3",
        "--llm-timeout=45.5",
        "--rewrite=off",
        "--refresh",
    )
    assert rank is not None
    cfg = rank.config
    assert (
        cfg.classifier,
        cfg.planner,
        cfg.max_calls,
        cfg.token_budget,
        cfg.batch_size,
        cfg.max_queries,
    ) == ("my-haiku", "my-sonnet", 3, 5000, 7, 3)
    assert (
        cfg.timeout_s == 45.5
        and rank.rewrite is False
        and rank.refresh is True
        and rank.require is False
    )
    assert ranked("--rewrite=big-model").config.planner == "big-model"


def test_require_and_answers_and_follows_are_parsed() -> None:
    rank = options(
        "--llm=require",
        "--answer=need_cad=yes",
        "--answer=max_size=60",
        "--follow=2",
        "--followup=1",
    )
    assert rank is not None and rank.require is True
    assert dict(rank.answers) == {"need_cad": "yes", "max_size": "60"} and list(
        rank.follow
    ) == [2, 1]


def test_ai_json_can_switch_ranking_on_and_no_llm_wins(tmp_path: Path) -> None:
    config = tmp_path / "xdg" / "3d-cli"
    config.mkdir(parents=True)
    (config / "ai.json").write_text(
        json.dumps({"llm": {"enabled": True, "classifier": "cfg-haiku"}}),
        encoding="utf-8",
    )
    rank = options()
    assert rank is not None and rank.enabled and rank.config.classifier == "cfg-haiku"
    assert ranked("--classifier=flag-haiku").config.classifier == "flag-haiku"
    off = options("--no-llm")
    assert (
        off is not None and off.enabled is False
    )  # ai.json enables ranking, --no-llm still wins


def test_rerank_is_always_ranking_unless_no_llm() -> None:
    assert ranked(rerank=True).enabled is True
    assert ranked("--no-llm", rerank=True).enabled is False


@pytest.mark.parametrize(
    "flags",
    [
        ("--no-llm", "--rank"),
        ("--no-llm", "--classifier=haiku"),
        ("--no-llm", "--llm=require"),
    ],
)
def test_no_llm_with_a_ranking_flag_is_a_usage_error(flags: tuple[str, ...]) -> None:
    with pytest.raises(UsageError, match="cannot be combined"):
        options(*flags)


@pytest.mark.parametrize(
    "flags",
    [
        ("--llm=sometimes",),
        ("--max-queries=1",),
        ("--max-calls=0",),
        ("--batch-size=x",),
        ("--llm-timeout=0",),
        ("--follow=0",),
        ("--token-budget=-1",),
    ],
)
def test_bad_values_list_what_is_accepted(flags: tuple[str, ...]) -> None:
    with pytest.raises(InvalidArgument):
        options(*flags)


def test_the_default_limit_is_fifty_only_when_ranking_and_unspecified() -> None:
    rank = options("--rank")
    assert rank is not None and rankcli.default_limit({}, rank) == 50
    assert rankcli.default_limit({"--limit": "10"}, rank) is None
    off = options("--ndjson")
    assert off is not None and rankcli.default_limit({}, off) is None


def test_answer_arguments_need_key_equals_value() -> None:
    assert rankstore.parse_answer_args(["a=1", "b=x=y", "a=2"]) == {
        "a": "2",
        "b": "x=y",
    }
    for bad in ("novalue", "=x"):
        with pytest.raises(InvalidArgument):
            rankstore.parse_answer_args([bad])
