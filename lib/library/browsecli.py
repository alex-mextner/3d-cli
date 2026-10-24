"""library/browsecli.py — `3d models browse`, `search --tui` and `3d models act` (#578, S7).

WHAT: the command layer of the model browser. `browse` / `search --tui` open the full-screen Textual browser
(`browse_tui`) or, without a terminal, the numbered prompt (`browse_plain`); `act` is the non-interactive twin of
every key (`--select ... --do download|slicer|agent|open|preview|queue|list`, `--answer`, `--follow`, `--refine`,
`--filter`, `--json` / `--ndjson`). All three build the same `BrowserState`, the same `ActParams` and call the same
`browse_run.perform`, so a key and its flags run the same `library.actions` functions and return the same
`3d.action/1` results (docs/commands/tui.md, docs/agents/search-workflow.md).

ACCESSED VIA: `commands/models.py` (`browse`, `act`, and `search --tui`), which owns argument parsing.

INVARIANTS: no full-screen UI without a TTY on both stdin and stdout, and never with `THREED_NO_TUI=1` (`search
--tui` then behaves as the plain search it extends); `textual` missing on a TTY is MissingDependency (127) naming
the `tui` extra; `act` changes nothing (no download, no process) unless `--yes` is given; a dry run prints the plan
and exits 0. Stdlib-only at module top.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from errors import InvalidArgument, MissingDependency, UsageError
from library import browse_run, browse_state
from library.browse_run import ACT_SCHEMA, ActParams, PipelineSource
from library.browse_state import BrowserState, Row

NO_TUI_ENV = "THREED_NO_TUI"
DO_VALUES = ("list", "preview", "open", "download", "slicer", "agent", "queue")
DO_ALIASES = {"browser": "open", "preview-url": "preview"}
MUTATING = frozenset({"open", "download", "slicer", "agent", "queue"})
UI_VALUE_FLAGS = frozenset(
    {
        "--dir",
        "--slicer",
        "--browser",
        "--agent",
        "--agent-mode",
        "--prompt",
        "--agent-cwd",
    }
)  # how the d / s / a / b keys behave: accepted by `search --tui`, `browse` and `act`
VALUE_FLAGS = UI_VALUE_FLAGS | frozenset(
    {
        "--select",
        "--select-site",
        "--select-verdict",
        "--do",
        "--action",
        "--filter",
        "--refine",
        "--session",
        "--db",
    }
)
BOOL_FLAGS = frozenset({"--plain", "--yes", "--tui", "--json"})


def require_textual() -> None:
    """Raise MissingDependency (127) naming the `tui` extra when textual is not importable. Called BEFORE
    `library.browse_tui` is imported (it imports textual at module top)."""
    import importlib.util

    if importlib.util.find_spec("textual") is None:
        raise MissingDependency(
            "textual",
            install="uv sync --extra tui    (or: pip install 'textual>=1.0')",
            degrades="the interactive model browser; `3d models act` and `3d models search --json` work without it",
            command="models",
        )


def interactive() -> bool:
    """A full-screen UI needs a terminal on stdin AND stdout and no `THREED_NO_TUI`."""
    return not os.environ.get(NO_TUI_ENV) and sys.stdin.isatty() and sys.stdout.isatty()


def _picture_protocol(images: str | None, *, tui: bool) -> str | None:
    """The preview picture protocol. On the browser's terminal `auto` is resolved HERE, on the real stdout (a worker
    thread's `sys.stdout` is Textual's capture and says it is no TTY); `act` keeps `auto` for the action to decide."""
    if images == "none":
        return None
    if tui and images in (None, "auto"):
        from cli import termimage

        return termimage.preview_protocol(is_tty=sys.stdout.isatty())
    return images


def act_params(opts: Mapping[str, str], *, dry_run: bool, tui: bool) -> ActParams:
    """`ActParams` from the shared flags; validates the backend and agent mode before anything starts."""
    from library import launch

    backend = opts.get("--agent")
    if backend is not None and backend not in launch.BACKENDS:
        raise InvalidArgument(
            "--agent", backend, list(launch.BACKENDS), command="models"
        )
    mode = opts.get("--agent-mode", "attach" if tui else "auto")
    if mode not in launch.AGENT_MODES:
        raise InvalidArgument(
            "--agent-mode", mode, list(launch.AGENT_MODES), command="models"
        )
    images = opts.get("--images")
    return ActParams(
        db_path=opts.get("--db"),
        dest_dir=opts.get("--dir"),
        slicer=opts.get("--slicer"),
        browser=opts.get("--browser"),
        agent_backend=backend,
        agent_mode=mode,
        agent_prompt=opts.get("--prompt", ""),
        agent_cwd=opts.get("--agent-cwd"),
        dry_run=dry_run,
        image=_picture_protocol(images, tui=tui),
    )


def _load(source: PipelineSource, ref: str) -> BrowserState:
    state = BrowserState()
    for event in source.saved(ref):
        state.ingest(event)
    return state


def _saved_search(db_path: str | None, ref: str) -> dict[str, Any]:
    """The stored root document of `ref` (sites, limit, query); UsageError naming `3d models searches list` when
    there is no such search."""
    from library import db as librarydb
    from library import search as searchcore

    conn = librarydb.open_library(db_path)
    try:
        return searchcore.session_doc(
            conn, str(browse_run.root_session(conn, ref)["uid"])
        )
    finally:
        conn.close()


def _source(
    opts: Mapping[str, str],
    multi: Mapping[str, list[str]],
    stored: Mapping[str, Any] | None,
) -> PipelineSource:
    from library import rankcli
    from library import search as searchcore
    from marketplaces.base import SITES

    rank = rankcli.rank_options(opts, multi, rerank=True)
    assert rank is not None  # rerank=True always ranks
    options = searchcore.SearchOptions(
        sites=tuple(stored["sites"] or SITES) if stored else tuple(SITES),
        limit=int(stored["limit"] or rankcli.RANK_DEFAULT_LIMIT)
        if stored
        else rankcli.RANK_DEFAULT_LIMIT,
        db_path=opts.get("--db"),
    )
    return PipelineSource(rank, options)


def _open_ui(
    state: BrowserState,
    source: PipelineSource,
    params: ActParams,
    start: Any,
    opts: Mapping[str, str],
) -> int:
    if "--plain" in opts or not interactive():
        from library import browse_plain

        return browse_plain.run_plain(
            state, source, params, start=start, stdin=sys.stdin, stdout=sys.stdout
        )
    require_textual()
    from library import browse_tui

    return browse_tui.run_browser(state, source, params, start=start)


def browse(
    positional: Sequence[str], opts: Mapping[str, str], multi: Mapping[str, list[str]]
) -> int:
    """`3d models browse [SEARCH_ID]`: resume a saved search tree (default `latest`) in the browser."""
    if len(positional) > 1:
        raise UsageError(
            f"expected at most one SEARCH_ID, got {len(positional)}",
            command="models",
            remediation=[
                "Run '3d models searches list' for the ids; `latest` is the default."
            ],
        )
    stored = _saved_search(
        opts.get("--db"),
        positional[0] if positional else opts.get("--session", "latest"),
    )
    ref = str(stored["search_id"])
    source = _source(opts, multi, stored)
    state = BrowserState(query=str(stored["query"]))
    if "--plain" not in opts and interactive():
        require_textual()
    return _open_ui(
        state,
        source,
        act_params(opts, dry_run=False, tui=True),
        lambda: source.saved(ref),
        opts,
    )


def search_tui(query: str, options: Any, rank: Any, opts: Mapping[str, str]) -> int:
    """`3d models search QUERY --tui` on a terminal: the results stream into the browser as the sites and the model
    answer. The caller has already decided that a UI is wanted (`interactive()`)."""
    if "--plain" not in opts:
        require_textual()
    source = PipelineSource(rank, options)
    state = BrowserState(query=query)
    return _open_ui(
        state,
        source,
        act_params(opts, dry_run=False, tui=True),
        lambda: source.search(query),
        opts,
    )


# --------------------------------------------------------------------------- act
def _do(opts: Mapping[str, str]) -> str:
    raw = opts.get("--do") or opts.get("--action") or "list"
    value = DO_ALIASES.get(raw, raw)
    if value not in DO_VALUES:
        raise InvalidArgument("--do", raw, [*DO_VALUES, *DO_ALIASES], command="models")
    return value


def _row_doc(n: int, row: Row, folder: str | None) -> dict[str, Any]:
    card = row.card
    return {
        "n": n,
        "key": row.key,
        "site": row.site,
        "id": row.model_id,
        "title": row.title,
        "url": row.url,
        "verdict": row.verdict,
        "score": row.score,
        "rationale": row.rationale or None,
        "size_mm": card.get("size_mm"),
        "cad": card.get("cad"),
        "license": card.get("license") or None,
        "price": card.get("price") or None,
        "found_by": row.found_by,
        "downloaded": folder,
    }


def _emit(doc: Mapping[str, Any], opts: Mapping[str, str]) -> None:
    if "--ndjson" in opts:
        sys.stdout.write(
            json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
    else:
        print(json.dumps(doc, indent=2, ensure_ascii=False))
    sys.stdout.flush()


def act(
    positional: Sequence[str], opts: Mapping[str, str], multi: Mapping[str, list[str]]
) -> int:
    """`3d models act [SEARCH_ID] --select ... --do ...`: the key twins of the browser. Returns the exit code."""
    if len(positional) > 1:
        raise UsageError(
            f"expected at most one SEARCH_ID, got {len(positional)}",
            command="models",
            remediation=[
                "Run '3d models searches list' for the ids; `latest` is the default."
            ],
        )
    if "--json" in opts and "--ndjson" in opts:
        raise UsageError(
            "--ndjson and --json are exclusive",
            command="models",
            remediation=["Pick one output mode."],
        )
    action = _do(opts)
    machine = "--json" in opts or "--ndjson" in opts
    stored = _saved_search(
        opts.get("--db"),
        positional[0] if positional else opts.get("--session", "latest"),
    )
    ref = str(stored["search_id"])
    reruns = bool(
        multi.get("--answer")
        or multi.get("--follow")
        or multi.get("--followup")
        or opts.get("--refine")
    )
    source = _source(opts, multi, stored)
    if reruns:
        ref = _rerun(source, ref, opts, multi, machine)
    state = _load(source, ref)
    if opts.get("--filter"):
        state.filter = opts["--filter"]
    params = act_params(
        opts, dry_run="--yes" not in opts and action in MUTATING, tui=False
    )
    if action == "list":
        return _list(
            state,
            ref,
            opts,
            params,
            selector=any(
                k in opts for k in ("--select", "--select-site", "--select-verdict")
            ),
        )
    rows = browse_run.select_rows(
        state,
        select=opts.get("--select"),
        sites=opts.get("--select-site"),
        verdicts=opts.get("--select-verdict"),
    )
    if state.filter:
        visible = {r.key for r in state.visible()}
        rows = [r for r in rows if r.key in visible]
    if not rows:
        raise UsageError(
            "the selection matched no result",
            command="models",
            remediation=[f"List the rows with `3d models act {ref} --do list`."],
        )
    outcome = browse_run.perform(
        action, rows, session=state.session, query=state.query, params=params
    )
    doc = outcome.to_json(rows)
    doc.update(
        {"search": state.session, "query": state.query, "exit_code": outcome.exit_code}
    )
    if machine:
        _emit(doc, opts)
    else:
        _print_outcome(doc, outcome, params)
    return outcome.exit_code


def _rerun(
    source: PipelineSource,
    ref: str,
    opts: Mapping[str, str],
    multi: Mapping[str, list[str]],
    machine: bool,
) -> str:
    """The `--answer` / `--follow` / `--refine` twins: run the ranking pipeline, stream its events as `--ndjson`,
    and return the root session to read the result back from."""
    from library import rankstore

    def show(events: Any) -> None:
        for event in events:
            if "--ndjson" in opts:
                sys.stdout.write(
                    json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
                )
                sys.stdout.flush()

    if multi.get("--answer") or multi.get("--follow") or multi.get("--followup"):
        answers = rankstore.parse_answer_args(multi.get("--answer", []))
        follow = [
            int(n)
            for n in [*multi.get("--follow", []), *multi.get("--followup", [])]
            if n.isascii() and n.isdecimal() and int(n) > 0
        ]
        if len(follow) != len(
            [*multi.get("--follow", []), *multi.get("--followup", [])]
        ):
            raise InvalidArgument(
                "--follow",
                ",".join(multi.get("--follow", [])),
                ["a follow-up number >= 1"],
                command="models",
            )
        show(source.session(ref, answers=answers, follow=follow))
    if opts.get("--refine"):
        show(source.search(str(opts["--refine"]), parent=ref))
    if not machine:
        print("re-ranked and saved", file=sys.stderr)
    return ref


def _list(
    state: BrowserState,
    ref: str,
    opts: Mapping[str, str],
    params: ActParams,
    *,
    selector: bool,
) -> int:
    rows = state.numbered()
    if selector:
        rows = browse_run.select_rows(
            state,
            select=opts.get("--select"),
            sites=opts.get("--select-site"),
            verdicts=opts.get("--select-verdict"),
        )
    if state.filter:
        visible = {r.key for r in state.visible()}
        rows = [r for r in rows if r.key in visible]
    numbers = {r.key: i for i, r in enumerate(state.numbered(), start=1)}
    shown = [
        _row_doc(
            numbers[r.key],
            r,
            (browse_run.local_copy(params.db_path, r) or {}).get("dir"),
        )
        for r in rows
    ]
    doc = {
        "schema": ACT_SCHEMA,
        "action": "list",
        "ok": True,
        "search": state.session,
        "query": state.query,
        "counts": state.counts,
        "results": shown,
        "questions": list(state.questions.values()),
        "followups": [f for _, f in sorted(state.followups.items())],
        "queries": state.queries,
        "cli": f"3d models act {state.session or ref} --do list --json",
    }
    if "--json" in opts or "--ndjson" in opts:
        _emit(doc, opts)
        return 0
    print(f"search {state.session}  {state.query!r}  {len(state.rows)} results")
    for d in shown:
        print(f"{d['n']:>4}. {browse_state.row_text(state.rows[d['key']])}")
        if d["rationale"]:
            print(f"      {d['verdict']}: {d['rationale']}")
        if d["downloaded"]:
            print(f"      downloaded: {d['downloaded']}")
    for q in state.questions.values():
        choices = f"  [{' | '.join(q['choices'])}]" if q["choices"] else ""
        answer = f"  = {q['answer']}" if q["answer"] is not None else ""
        print(f"question {q['key']} ({q['type']}): {q['prompt']}{choices}{answer}")
    for n, f in sorted(state.followups.items()):
        print(f"follow-up {n} [{f['status']}]: {f['query']!r}")
    return 0


def _print_outcome(
    doc: Mapping[str, Any], outcome: browse_run.ActOutcome, params: ActParams
) -> None:
    print(
        f"search {doc['search']}  {doc['query']!r}  action {outcome.action}"
        + ("  (dry run)" if params.dry_run else "")
    )
    for s in doc["selected"]:
        print(f"{s['n']:>4}. {s['title']}  [{s['site']}]  {s['url']}")
    for line in outcome.plan:
        print(f"  {line}")
    for result in outcome.results:
        if result["ok"]:
            print(f"  ok: {browse_run._ok_summary(result)}")
        for warning in result["warnings"]:
            print(f"  warning: {warning['message']}")
        for error in result["errors"]:
            print(f"  error: {error['message']}", file=sys.stderr)
            for step in error["remediation"]:
                print(f"    {step}", file=sys.stderr)
    if params.dry_run:
        print(f"dry run: nothing was started. Run it with: {outcome.cli}")
    else:
        print(f"equivalent: {outcome.cli}")
