"""library/rankcli.py — the command-line side of `3d models search --rank`, `rerank` and `followups` (#577).

WHAT: turns the flags into a `RankOptions` (`rank_options`), drives a `rankflow.Pipeline` and renders its event
stream in the three output modes: text (the S5 view first, then verdict lines as they arrive, then the ranking,
questions and follow-ups), `--ndjson` (one `3d.models.rank/1` event per line, flushed as it happens: what agents
read) and `--json` (one `3d.models.rank-result/1` document at the end: the S5 documents with the verdict fields
merged in, plus the ranking, queries, questions and follow-ups). `--background` prints phase 1 and hands the model
work to a detached `3d models rerank` process that writes its verdicts to the library DB.

ACCESSED VIA: `commands/models.py` (`search`, `rerank`, `followups`); the flag tables below are shared with it.

INVARIANTS:
  - Without a ranking flag (and with `--no-llm`) `rank_options` returns None and `commands/models.py` takes the
    unchanged S5 path: the output is byte-for-byte what S5 printed.
  - `omp` is never started unless ranking is on; `--llm require` fails with exit 127 BEFORE the search when the
    backend is missing, otherwise a missing or failing model costs one `LLM: unavailable (...)` line.
  - Heavy imports (the library DB, the pipeline) happen inside the functions; the module top is stdlib-only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from library.rankflow import Pipeline, RankOptions

from errors import InvalidArgument, UsageError

RANK_DEFAULT_LIMIT = 50  # results per site when ranking is on and --limit is absent
TEXT_TOP = 15  # ranked results the text view lists at the end
LLM_MODES = ("auto", "require")

VALUE_FLAGS = frozenset(
    {
        "--classifier",
        "--planner",
        "--rewrite",
        "--max-queries",
        "--max-calls",
        "--token-budget",
        "--llm-timeout",
        "--batch-size",
        "--llm",
    }
)
REPEAT_FLAGS = frozenset({"--answer", "--follow", "--followup"})
BOOL_FLAGS = frozenset({"--no-llm", "--ndjson", "--background", "--wait", "--refresh"})
OPTIONAL_FLAGS = frozenset({"--rank"})

RankPrinter = Callable[[dict[str, Any], dict[str, Any]], None]


def _positive(flag: str, raw: str | None, minimum: int = 1) -> int | None:
    if raw is None:
        return None
    if not (raw.isascii() and raw.isdecimal()) or int(raw) < minimum:
        raise InvalidArgument(flag, raw, [f"an integer >= {minimum}"], command="models")
    return int(raw)


def _seconds(flag: str, raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        raise InvalidArgument(flag, raw, ["a number of seconds > 0"], command="models")
    return value


def rank_options(
    opts: Mapping[str, str], multi: Mapping[str, list[str]], *, rerank: bool = False
) -> RankOptions | None:
    """`RankOptions` for the parsed flags, or None when no ranking flag was given (the unchanged S5 path).

    Ranking is ON for `--rank[=MODEL]`, `--classifier`, `--planner`, `--llm require`, `--answer`, `--follow`, an
    `llm.enabled: true` in ai.json, and always for `rerank`; `--no-llm` turns it off and wins over ai.json
    (but not over an explicit `--rank`, which is a usage error together with it)."""
    from library import rankflow, rankstore
    from library.llm import load_llm_config

    no_llm = "--no-llm" in opts
    llm_mode = opts.get("--llm", "auto")
    if llm_mode not in LLM_MODES:
        raise InvalidArgument("--llm", llm_mode, list(LLM_MODES), command="models")
    if no_llm and (
        "--rank" in opts
        or llm_mode == "require"
        or "--classifier" in opts
        or "--planner" in opts
    ):
        raise UsageError(
            "--no-llm cannot be combined with --rank, --classifier, --planner or --llm require",
            command="models",
            remediation=[
                "Drop --no-llm, or drop the ranking flags for the deterministic search."
            ],
        )
    rewrite_raw = opts.get("--rewrite")
    rank_model = opts.get("--rank")
    classifier = opts.get("--classifier") or (
        rank_model if rank_model not in (None, "auto") else None
    )
    planner = opts.get("--planner") or (
        rewrite_raw if rewrite_raw not in (None, "off", "auto") else None
    )
    candidates: dict[str, object] = {
        "classifier": classifier,
        "planner": planner,
        "batch_size": _positive("--batch-size", opts.get("--batch-size")),
        "max_queries": _positive("--max-queries", opts.get("--max-queries"), minimum=2),
        "max_calls": _positive("--max-calls", opts.get("--max-calls")),
        "token_budget": _positive(
            "--token-budget", opts.get("--token-budget"), minimum=0
        ),
        "timeout_s": _seconds("--llm-timeout", opts.get("--llm-timeout")),
    }
    overrides = {k: v for k, v in candidates.items() if v is not None}
    config = load_llm_config(overrides)
    answers = rankstore.parse_answer_args(multi.get("--answer", []))
    follow = [
        _positive("--follow", n) or 0
        for n in [*multi.get("--follow", []), *multi.get("--followup", [])]
    ]
    asked = bool(
        rank_model is not None
        or classifier
        or planner
        or llm_mode == "require"
        or answers
        or follow
        or rerank
        or config.enabled
    )
    streaming = "--ndjson" in opts
    if not asked and not streaming:
        return None
    enabled = asked and not no_llm
    return rankflow.RankOptions(
        config,
        enabled=enabled,
        require=llm_mode == "require" and enabled,
        rewrite=rewrite_raw != "off",
        answers=answers,
        follow=tuple(follow),
        refresh="--refresh" in opts,
    )


def default_limit(opts: Mapping[str, str], rank: RankOptions) -> int | None:
    """50 per site when ranking is on and `--limit` was not given, else None (keep S5's default)."""
    return RANK_DEFAULT_LIMIT if rank.enabled and "--limit" not in opts else None


# --------------------------------------------------------------------------- output modes
def emit_ndjson(events: Iterator[dict[str, Any]]) -> None:
    """One JSON object per line on stdout, flushed per event so a reader sees phase 1 before any model answers."""
    for event in events:
        sys.stdout.write(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        sys.stdout.flush()


def _tag(site: str, model_id: str) -> str:
    return f"[{site}:{model_id}]"


def emit_text(
    events: Iterator[dict[str, Any]],
    on_search: RankPrinter,
    *,
    top: int = TEXT_TOP,
    width: int = 120,
) -> None:
    """Print the S5 view at the `search` event, then follow the stream line by line: verdicts as they arrive,
    the planner and its queries, the new hits they found, and a ranked summary with the questions and follow-ups."""
    from library.textview import clip, flat

    titles: dict[tuple[str, str], str] = {}
    for ev in events:
        name = ev["event"]
        if name == "search":
            on_search(ev, {})
        elif name == "warning":
            if ev["code"] == "llm_unavailable":
                print(ev["message"])
            elif ev["code"] != "search_warning":
                print(f"note: {ev['message']}")
        elif name == "result":
            titles[(ev["site"], ev["id"])] = flat(str(ev.get("title") or ""))
            if ev["kind"] != "user":
                print(
                    clip(
                        f"    + {_tag(ev['site'], ev['id'])} {titles[(ev['site'], ev['id'])]}",
                        width,
                    )
                )
        elif name == "verdict":
            title = titles.get((ev["site"], ev["id"]), "")
            head = f"  {ev['verdict']:<5} {ev['score']:.2f}  {_tag(ev['site'], ev['id'])} {title}"
            print(clip(f"{clip(head, 70)}  — {ev['rationale']}", width))
        elif name == "plan":
            if ev["status"] == "running":
                what = (
                    "reworking the query"
                    if ev["mode"] == "rewrite"
                    else "suggesting next steps"
                )
                print(f"planner ({ev['model']}): {ev['trigger']} -> {what}")
            elif ev["status"] == "ready" and ev.get("summary"):
                print(f"plan: {ev['summary']}")
        elif name == "query":
            if ev["status"] == "run":
                print(
                    f"query [{ev['kind']}] {ev['query']!r}: {ev['results']} results, {ev['new']} new, "
                    f"{ev['duplicates']} already shown  (3d models searches show {ev['search']})"
                )
            elif ev["status"] == "suggested":
                why = f"  — {ev['rationale']}" if ev.get("rationale") else ""
                print(
                    f"follow-up {ev['n']}: {ev['query']!r}{why}  (--follow {ev['n']})"
                )
        elif name == "done":
            _print_done(ev, titles, top, width)
        sys.stdout.flush()  # a pipe is block-buffered: phase 1 must reach the reader before any model answers


def _print_done(
    ev: Mapping[str, Any], titles: Mapping[tuple[str, str], str], top: int, width: int
) -> None:
    from library.textview import clip

    counts, llm = ev["counts"], ev.get("llm")
    if llm is not None:
        tokens = (
            f", {llm['total_tokens']} tokens"
            if llm.get("total_tokens") is not None
            else ""
        )
        print(
            f"ranked: {counts['fits']} fit, {counts['maybe']} maybe, {counts['no']} no, {counts['unjudged']} unjudged"
            f"  ({llm['calls']} model calls{tokens}, {ev['elapsed_s']:.1f}s)"
        )
        shown = [r for r in ev["ranking"] if r["verdict"] in ("fits", "maybe")][:top]
        for n, r in enumerate(shown, start=1):
            title = titles.get((r["site"], r["id"]), "")
            print(
                clip(
                    f"  {n:>2}. {r['verdict']:<5} {r['score']:.2f}  {_tag(r['site'], r['id'])} {title}  — {r.get('rationale') or ''}",
                    width,
                )
            )
    if ev.get("questions"):
        print("questions (answer with `3d models rerank ID --answer KEY=VALUE`):")
        for q in ev["questions"]:
            choices = f"  [{' | '.join(q['choices'])}]" if q["choices"] else ""
            state = f"  = {q['answer']}" if q.get("answer") is not None else ""
            print(f"  {q['key']}  ({q['type']})  {q['prompt']}{choices}{state}")
    if ev.get("search"):
        print(f"saved: search {ev['search']} (3d models searches show {ev['search']})")


# --------------------------------------------------------------------------- the --json document
RESULT_SCHEMA = "3d.models.rank-result/1"


def _annotate(
    doc: dict[str, Any], verdicts: Mapping[tuple[str, str], Any]
) -> dict[str, Any]:
    for records in doc["results"].values():
        for rec in records:
            v = verdicts.get((str(rec["site"]), str(rec["id"])))
            if v is not None:
                rec["verdict"], rec["score"], rec["rationale"] = (
                    v.verdict,
                    v.score,
                    v.rationale,
                )
    return doc


def result_doc(pipeline: Pipeline, done: Mapping[str, Any]) -> dict[str, Any]:
    """`3d.models.rank-result/1`: the root search document and one document per child session (both
    `3d.models.search/2` with `verdict`/`score`/`rationale` merged into judged results), the final ranking, the
    queries run and suggested, the questions with their answers, and the model-call summary."""
    from library import db as librarydb
    from library import rankstore
    from library import search as searchcore

    verdicts = {i.key: i.verdict for i in pipeline.items if i.verdict is not None}
    children: list[dict[str, Any]] = []
    followups: list[dict[str, Any]] = []
    root = _annotate(dict(pipeline.doc), verdicts)
    if pipeline.root_id is not None:
        try:
            conn = librarydb.open_library(getattr(pipeline.options, "db_path", None))
        except Exception:  # noqa: BLE001 — the document is still useful without the DB read-back
            conn = None
        if conn is not None:
            try:
                children.extend(
                    _annotate(searchcore.session_doc(conn, str(s["uid"])), verdicts)
                    for s in rankstore.tree_sessions(conn, pipeline.root_id)[1:]
                )
                followups = [
                    p
                    for p in rankstore.list_plan_queries(conn, pipeline.root_id)
                    if p["kind"] == "followup"
                ]
            finally:
                conn.close()
    return {
        "schema": RESULT_SCHEMA,
        "search": root,
        "children": children,
        "ranking": done["ranking"],
        "counts": done["counts"],
        "queries": done["queries"],
        "followups": followups,
        "questions": done["questions"],
        "llm": done["llm"],
        "degraded": done["degraded"],
        "elapsed_s": done["elapsed_s"],
    }


# --------------------------------------------------------------------------- background
def spawn_background(search_uid: str, argv_tail: Sequence[str]) -> int:
    """Start `3d models rerank SEARCH_ID ...` detached (own session, no terminal); returns its pid."""
    from cli.env import repo_root

    command = [
        sys.executable,
        os.path.join(repo_root(), "bin", "3d"),
        "models",
        "rerank",
        search_uid,
        *argv_tail,
    ]
    proc = subprocess.Popen(  # noqa: S603 — our own entry point with validated arguments
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return proc.pid
