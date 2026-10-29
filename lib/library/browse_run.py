"""library/browse_run.py — what the browser's keys DO: the shared runtime of the TUI, the plain prompt and `3d models act`.

WHAT: `perform(action, rows, ...)` turns one key / one `--do` into calls of `library.actions` (`preview`,
`open_browser`, `download`, `open_slicer`, `launch_agent`, `queue`) and returns an `ActOutcome` holding the raw
`3d.action/1` results. The interactive front ends and the non-interactive command call this SAME function with the
same arguments, which is the parity guarantee of #578. `EventSource` is where the ranking events come from:
`PipelineSource` (the real `library.rankflow.Pipeline`), a saved search read back from the library DB
(`saved_events`), or a scripted one in tests. `select_rows` is the `--select / --select-site / --select-verdict`
grammar of `3d models act`.

INVARIANTS: nothing here prints; no terminal; every action goes through the `invoke` callable it is given (default
`library.actions.invoke`, which never raises), so a test injects fake actions and nothing real starts; a dry run
starts nothing and downloads nothing (it reports the plan); a download never overwrites (a model already recorded
in the library DB with an existing folder is reused, not fetched again). Stdlib-only at module top.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from errors import InvalidArgument, UsageError
from library import actions, browse_state, launch
from library.browse_state import BrowserState, Row

ACT_SCHEMA = "3d.models.act/1"
EVENT_SCHEMA = "3d.models.rank/1"
Invoke = Callable[..., actions.ActionResult]
EXIT_CODES = {
    "missing_dependency": 127,
    "invalid_argument": 2,
    "not_found": 2,
    "unknown_action": 2,
}


# --------------------------------------------------------------------------- parameters
@dataclass(frozen=True)
class ActParams:
    """How actions run: shared by every front end so the same flags mean the same thing."""

    db_path: str | None = None
    dest_dir: str | None = (
        None  # parent of the per-model download folders; None = ./<site>-<id>-<title>/
    )
    slicer: str | None = None
    browser: str | None = None
    agent_backend: str | None = None
    agent_mode: str = "auto"
    agent_prompt: str = ""
    agent_cwd: str | None = None
    dry_run: bool = False
    image: str | None = (
        None  # preview picture: auto | kitty | iterm2 | sixel | blocks | None
    )
    image_cols: int = 36


@dataclass
class ActOutcome:
    action: str
    keys: list[str]
    results: list[actions.ActionResult] = field(default_factory=list)
    plan: list[str] = field(
        default_factory=list
    )  # what a dry run would do / what was skipped and why
    folders: dict[str, str] = field(
        default_factory=dict
    )  # row key -> local folder after this action
    cli: str = ""
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return all(r["ok"] for r in self.results)

    @property
    def exit_code(self) -> int:
        codes: list[int] = []
        for r in self.results:
            if not r["ok"]:
                codes += [EXIT_CODES.get(e["code"], 1) for e in r["errors"]] or [1]
        return max(codes, default=0)

    def to_json(self, rows: Sequence[Row]) -> dict[str, Any]:
        return {
            "schema": ACT_SCHEMA,
            "action": self.action,
            "ok": self.ok,
            "dry_run": self.dry_run,
            "selected": [
                {
                    "n": n,
                    "key": r.key,
                    "site": r.site,
                    "id": r.model_id,
                    "title": r.title,
                    "url": r.url,
                }
                for n, r in _numbered(rows)
            ],
            "results": self.results,
            "plan": self.plan,
            "cli": self.cli,
        }

    def summary(self) -> str:
        """One status-line sentence."""
        if self.plan and not self.results:
            return f"{self.action}: {self.plan[0]}"
        parts: list[str] = []
        for r in self.results:
            if r["ok"]:
                parts.append(_ok_summary(r))
            else:
                parts.append(
                    f"{r['action']} failed: {r['errors'][0]['message']}"
                    if r["errors"]
                    else f"{r['action']} failed"
                )
        skipped = list(self.plan)
        return "; ".join(parts + skipped) or f"{self.action}: done"


def _numbered(rows: Sequence[Row]) -> Iterator[tuple[int, Row]]:
    yield from enumerate(rows, start=1)


def _ok_summary(r: actions.ActionResult) -> str:
    d = r["data"]
    name = r["action"]
    if name == "download":
        return f"downloaded {len(d.get('files', []))} files to {d.get('dir')}"
    if name == "open_slicer":
        verb = "started" if d.get("started") else "would start"
        program = Path(d["command"][0]).name if d.get("command") else d.get("slicer")
        return f"{verb} {program} with {len(d.get('paths', []))} file(s)"
    if name == "launch_agent":
        where = d.get("tmux") or (
            f"pid {d['pid']}" if d.get("pid") else "this terminal"
        )
        verb = "launched" if d.get("started") else "would launch"
        return f"{verb} {d.get('backend')} ({where}), brief {d.get('brief')}"
    if name == "open_browser":
        return f"{'opened' if d.get('started') else 'would open'} {len(d.get('urls', []))} page(s)"
    if name == "queue":
        return f"queued {d.get('design')}"
    if name == "preview":
        return f"preview of {d.get('title')}"
    return f"{name} ok"


# --------------------------------------------------------------------------- local copies (the library DB)
def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:60] or "model"


def local_copy(db_path: str | None, row: Row) -> dict[str, Any] | None:
    """`{dir, files: [names], source}` of an earlier download of this model, or None (also when its folder is gone)."""
    try:
        from library import db as librarydb

        conn = librarydb.open_library(db_path)
    except (sqlite3.Error, OSError):
        return None
    try:
        rec = conn.execute(
            "SELECT uid, dir, files FROM model_source WHERE site = ? AND model_id = ? ORDER BY id DESC LIMIT 1",
            (row.site, row.model_id),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if rec is None or not rec["dir"] or not Path(rec["dir"]).is_dir():
        return None
    names = [str(f.get("name")) for f in json.loads(rec["files"] or "[]")]
    return {"dir": rec["dir"], "files": names, "source": rec["uid"]}


def _dest(params: ActParams, row: Row) -> str | None:
    if not params.dest_dir:
        return None
    return str(
        Path(params.dest_dir).expanduser()
        / f"{row.site}-{row.model_id}-{slug(row.title)}"
    )


def _card(row: Row) -> dict[str, Any]:
    return {
        **row.card,
        "verdict": row.verdict,
        "score": row.score,
        "rationale": row.rationale,
    }


# --------------------------------------------------------------------------- the actions
def perform(
    action: str,
    rows: Sequence[Row],
    *,
    session: str | None,
    query: str,
    params: ActParams,
    known: Mapping[str, str] | None = None,
    invoke: Invoke = actions.invoke,
) -> ActOutcome:
    """Run `action` (browse_state.ACT_NAMES, or `image`) on `rows`. `known` = row key -> folder already downloaded in
    this run. Returns the raw `3d.action/1` results; a dry run returns the plan instead of results for anything
    that would download."""
    out = ActOutcome(action, [r.key for r in rows], dry_run=params.dry_run)
    extra = (
        ["--dir", params.dest_dir]
        if params.dest_dir and action in ("download", "slicer", "queue", "agent")
        else []
    )
    out.cli = browse_state.cli_line(
        action if action != "image" else "preview", rows, session, extra=extra
    )
    if not rows:
        out.plan.append("nothing selected")
        return out
    if action in ("preview", "image"):
        out.results.append(
            invoke(
                "preview",
                card=_card(rows[0]),
                image=params.image if action == "image" or params.image else None,
                cols=params.image_cols,
            )
        )
    elif action == "open":
        out.results.append(
            invoke(
                "open_browser",
                urls=[r.url for r in rows],
                browser=params.browser,
                dry_run=params.dry_run,
            )
        )
    elif action == "download":
        _download(out, rows, params, known or {}, invoke)
    elif action == "slicer":
        _slicer(out, rows, params, known or {}, invoke)
    elif action == "agent":
        _agent(out, rows, session, query, params, known or {}, invoke)
    elif action == "queue":
        _queue(out, rows, params, known or {}, invoke)
    else:
        raise InvalidArgument(
            "action", action, [*browse_state.ACT_NAMES, "image"], command="models"
        )
    return out


def _download(
    out: ActOutcome,
    rows: Sequence[Row],
    params: ActParams,
    known: Mapping[str, str],
    invoke: Invoke,
) -> list[str]:
    """Download every row that has no local copy yet; returns the keys that are NOT local afterwards (dry run)."""
    pending: list[str] = []
    for row in rows:
        have = known.get(row.key) or (local_copy(params.db_path, row) or {}).get("dir")
        if have:
            out.folders[row.key] = str(have)
            out.plan.append(f"already downloaded: {row.title} -> {have}")
            continue
        if params.dry_run:
            out.plan.append(
                f"would download {row.title} to {_dest(params, row) or f'./{row.site}-{row.model_id}-<title>/'}"
            )
            pending.append(row.key)
            continue
        result = invoke(
            "download", url=row.url, dest_dir=_dest(params, row), db_path=params.db_path
        )
        out.results.append(result)
        if result["ok"]:
            out.folders[row.key] = str(result["data"]["dir"])
        else:
            pending.append(row.key)
    return pending


def best_file(folder: str, names: Sequence[str]) -> str | None:
    chosen = launch.best_model_file(names)
    return str(Path(folder) / chosen) if chosen else None


def _local_files(folder: str, db_params: ActParams, row: Row) -> list[str]:
    copy = local_copy(db_params.db_path, row)
    if copy and Path(copy["dir"]) == Path(folder):
        return list(copy["files"])
    return sorted(
        p.name for p in Path(folder).iterdir() if p.is_file() and p.name != "model.json"
    )


def _slicer(
    out: ActOutcome,
    rows: Sequence[Row],
    params: ActParams,
    known: Mapping[str, str],
    invoke: Invoke,
) -> None:
    pending = _download(out, rows, params, known, invoke)
    if pending:
        if not params.dry_run:
            return  # a download failed: its error result says why; nothing is opened
        out.plan.append("then open the downloaded files in the slicer")
        return
    files: list[str] = []
    for row in rows:
        folder = out.folders[row.key]
        path = best_file(folder, _local_files(folder, params, row))
        if path is None:
            out.results.append(
                actions.error_result(
                    "open_slicer",
                    [
                        actions.issue(
                            "not_found",
                            f"{row.title}: no 3MF/STL/OBJ/STEP file in {folder}",
                            ["Pick another model."],
                        )
                    ],
                )
            )
            return
        files.append(path)
    out.results.append(
        invoke("open_slicer", path=files, slicer=params.slicer, dry_run=params.dry_run)
    )


def agent_context(
    rows: Sequence[Row],
    folders: Mapping[str, str],
    session: str | None,
    query: str,
    params: ActParams,
) -> dict[str, Any]:
    models: list[dict[str, Any]] = []
    for row in rows:
        card = row.card
        entry: dict[str, Any] = {
            "key": row.key,
            "site": row.site,
            "id": row.model_id,
            "title": row.title,
            "url": row.url,
            "verdict": row.verdict,
            "score": row.score,
            "rationale": row.rationale,
            "license": card.get("license") or None,
            "price": card.get("price") or None,
            "size_mm": card.get("size_mm"),
        }
        folder = folders.get(row.key) or (local_copy(params.db_path, row) or {}).get(
            "dir"
        )
        if folder:
            entry["dir"] = str(folder)
            entry["files"] = _local_files(str(folder), params, row)
            entry["provenance"] = launch.read_provenance(Path(folder))
        models.append(entry)
    return {"query": query, "session": session, "models": models}


def _agent(
    out: ActOutcome,
    rows: Sequence[Row],
    session: str | None,
    query: str,
    params: ActParams,
    known: Mapping[str, str],
    invoke: Invoke,
) -> None:
    context = agent_context(rows, known, session, query, params)
    dirs = [Path(m["dir"]) for m in context["models"] if m.get("dir")]
    cwd = params.agent_cwd or (
        str(dirs[0].parent) if dirs and len({d.parent for d in dirs}) == 1 else None
    )
    out.results.append(
        invoke(
            "launch_agent",
            prompt=params.agent_prompt,
            cwd=cwd,
            backend=params.agent_backend,
            dry_run=params.dry_run,
            context=context,
            mode=params.agent_mode,
        )
    )


def _queue(
    out: ActOutcome,
    rows: Sequence[Row],
    params: ActParams,
    known: Mapping[str, str],
    invoke: Invoke,
) -> None:
    pending = _download(out, rows, params, known, invoke)
    if pending:
        if params.dry_run:
            out.plan.append("then add each model to the print queue")
        return
    for row in rows:
        source = (local_copy(params.db_path, row) or {}).get("source")
        if params.dry_run:
            out.plan.append(f"would queue {row.title}")
            continue
        out.results.append(
            invoke(
                "queue",
                design=slug(row.title),
                source=source,
                source_key=f"browse:{row.key}",
                db_path=params.db_path,
            )
        )


# --------------------------------------------------------------------------- selection grammar
def select_rows(
    state: BrowserState,
    *,
    select: str | None = None,
    sites: str | None = None,
    verdicts: str | None = None,
) -> list[Row]:
    """Rows chosen by `--select 1,3,5-7|site:id|all`, `--select-site a,b`, `--select-verdict fits,maybe`; every given
    selector must match. Numbers are positions in the final ranking (`act --do list` prints them)."""
    numbered = state.numbered()
    chosen = list(numbered)
    if select is not None:
        picked: dict[str, Row] = {}
        for token in (t.strip() for t in select.split(",") if t.strip()):
            if token == "all":
                picked.update({r.key: r for r in numbered})
            elif token.isascii() and token.isdecimal():
                n = int(token)
                if not 1 <= n <= len(numbered):
                    raise InvalidArgument(
                        "--select", token, [f"1..{len(numbered)}"], command="models"
                    )
                picked[numbered[n - 1].key] = numbered[n - 1]
            elif re.fullmatch(r"\d+-\d+", token):
                lo, hi = (int(x) for x in token.split("-"))
                if not 1 <= lo <= hi <= len(numbered):
                    raise InvalidArgument(
                        "--select",
                        token,
                        [f"a range inside 1..{len(numbered)}"],
                        command="models",
                    )
                picked.update({r.key: r for r in numbered[lo - 1 : hi]})
            elif token in state.rows:
                picked[token] = state.rows[token]
            else:
                raise InvalidArgument(
                    "--select",
                    token,
                    [f"numbers 1..{len(numbered)}, ranges 2-5, site:id keys, or all"],
                    command="models",
                )
        chosen = [r for r in chosen if r.key in picked]
    if sites is not None:
        wanted = {s.strip() for s in sites.split(",") if s.strip()}
        known_sites = {r.site for r in numbered}
        for s in wanted - known_sites:
            raise InvalidArgument(
                "--select-site",
                s,
                sorted(known_sites) or ["(no results)"],
                command="models",
            )
        chosen = [r for r in chosen if r.site in wanted]
    if verdicts is not None:
        wanted = {v.strip() for v in verdicts.split(",") if v.strip()}
        for v in wanted - {"fits", "maybe", "no", "unjudged"}:
            raise InvalidArgument(
                "--select-verdict",
                v,
                ["fits", "maybe", "no", "unjudged"],
                command="models",
            )
        chosen = [r for r in chosen if (r.verdict or "unjudged") in wanted]
    if select is None and sites is None and verdicts is None:
        raise UsageError(
            "nothing selected",
            command="models",
            remediation=[
                "Pass --select 1,3 | --select-site printables | --select-verdict fits (run --do list to see the numbers)."
            ],
        )
    return chosen


# --------------------------------------------------------------------------- event sources
class EventSource(Protocol):
    """Where the ranking events come from; each method returns a fresh generator."""

    def search(
        self, query: str, *, parent: str | None = None
    ) -> Iterator[dict[str, Any]]: ...

    def session(
        self, ref: str, *, answers: Mapping[str, str] = ..., follow: Sequence[int] = ...
    ) -> Iterator[dict[str, Any]]: ...

    def saved(self, ref: str) -> Iterator[dict[str, Any]]: ...


@dataclass
class PipelineSource:
    """The real thing: `library.rankflow.Pipeline` for new searches and re-ranking, the DB for resuming."""

    rank: Any  # library.rankflow.RankOptions
    options: Any  # library.search.SearchOptions

    def search(
        self, query: str, *, parent: str | None = None
    ) -> Iterator[dict[str, Any]]:
        from library import rankflow

        options = (
            replace(self.options, parent_session=parent) if parent else self.options
        )
        return rankflow.Pipeline(self.rank, options).search(query)

    def session(
        self, ref: str, *, answers: Mapping[str, str] = {}, follow: Sequence[int] = ()
    ) -> Iterator[dict[str, Any]]:
        from library import rankflow

        rank = replace(
            self.rank, answers={**self.rank.answers, **answers}, follow=tuple(follow)
        )
        return rankflow.Pipeline(rank, self.options).session(ref)

    def saved(self, ref: str) -> Iterator[dict[str, Any]]:
        return saved_events(ref, getattr(self.options, "db_path", None))


def root_session(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    """The root of the search tree `ref` belongs to (`latest` is the newest ROOT, not a rewrite under it)."""
    from library import rankstore

    if ref in ("latest", "last"):
        row = conn.execute(
            "SELECT * FROM search_session WHERE parent_id IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            return row
    row = rankstore.session_row(conn, ref)
    while row["parent_id"] is not None:
        row = conn.execute(
            "SELECT * FROM search_session WHERE id = ?", (row["parent_id"],)
        ).fetchone()
    return row


def saved_events(ref: str, db_path: str | None) -> Iterator[dict[str, Any]]:
    """A saved search tree (the root and every rewrite / follow-up under it) as `3d.models.rank/1` events: results in
    discovery order, the stored verdicts, the planner's questions and follow-ups, then `done`. No model, no network."""
    from library import db as librarydb
    from library import rankstore, ranking

    conn = librarydb.open_library(db_path)
    seq = 0
    try:
        root = root_session(conn, ref)
        root_id = int(root["id"])
        sessions = rankstore.tree_sessions(conn, root_id)
        events: list[dict[str, Any]] = []

        def ev(name: str, **fields: Any) -> None:
            nonlocal seq
            seq += 1
            events.append(
                {"schema": EVENT_SCHEMA, "event": name, "seq": seq, "t": 0.0, **fields}
            )

        ev(
            "start",
            kind="session",
            query=root["query"],
            llm={"enabled": False},
            answers=rankstore.answers_map(conn, root_id),
        )
        ev(
            "search",
            search=root["uid"],
            query=root["query"],
            sites=[],
            counts={},
            timings={},
            errors=[],
        )
        seen: set[tuple[str, str]] = set()
        ranking_rows: list[dict[str, Any]] = []
        for s in sessions:
            for stored in rankstore.session_results(conn, int(s["id"])):
                key = (stored["site"], stored["model_id"])
                if key in seen:
                    continue
                seen.add(key)
                position = len(seen)
                ev(
                    "result",
                    search=s["uid"],
                    position=position,
                    site=stored["site"],
                    id=stored["model_id"],
                    rank=stored["record"].get("rank"),
                    title=stored["record"].get("title"),
                    url=stored["record"].get("url"),
                    query=s["query"],
                    kind="user" if s["id"] == root_id else s["kind"],
                    card=stored["record"],
                )
                if stored["verdict"]:
                    ev(
                        "verdict",
                        search=s["uid"],
                        position=position,
                        site=stored["site"],
                        id=stored["model_id"],
                        verdict=stored["verdict"],
                        score=stored["score"],
                        rationale=stored["rationale"],
                        cached=True,
                        model=None,
                    )
                ranking_rows.append(
                    {
                        "position": position,
                        "site": stored["site"],
                        "id": stored["model_id"],
                        "search": s["uid"],
                        "verdict": stored["verdict"],
                        "score": stored["score"],
                        "rationale": stored["rationale"],
                    }
                )
        questions = rankstore.list_questions(conn, root_id)
        for q in questions:
            ev(
                "question",
                search=root["uid"],
                id=q["id"],
                key=q["key"],
                type=q["type"],
                prompt=q["prompt"],
                choices=q["choices"],
                answer=q["answer"],
            )
        plan = rankstore.list_plan_queries(conn, root_id)
        for p in plan:
            if p["kind"] == "followup":  # every follow-up keeps its number, run or not
                ev(
                    "query",
                    status="suggested",
                    id=p["id"],
                    n=p["n"],
                    query=p["query"],
                    kind="followup",
                    rationale=p["rationale"],
                    parent=root["uid"],
                    search=None,
                )
            if p["status"] == "run":
                ev(
                    "query",
                    status="run",
                    search=p["search"],
                    query=p["query"],
                    kind=p["kind"],
                    rationale=p["rationale"],
                    parent=root["uid"],
                    counts={},
                    results=0,
                    new=None,
                    duplicates=0,
                    errors=[],
                )
        counts = {
            "results": len(ranking_rows),
            "fits": 0,
            "maybe": 0,
            "no": 0,
            "unjudged": 0,
        }
        for r in ranking_rows:
            counts[r["verdict"] or "unjudged"] += 1
        ranking_rows.sort(
            key=lambda r: ranking.sort_key(r["verdict"], r["score"], r["position"])
        )
        ev(
            "done",
            search=root["uid"],
            query=root["query"],
            counts=counts,
            ranking=ranking_rows,
            queries=[],
            questions=questions,
            llm=None,
            degraded=None,
            elapsed_s=0.0,
        )
    finally:
        conn.close()
    yield from events


# --------------------------------------------------------------------------- a stream on a worker thread
class Stream:
    """Drains one event generator on a daemon thread and hands every event to `post` (the front end marshals it onto
    its own thread). `close()` stops it between events; closing the generator is safe (rankflow closes its DB)."""

    def __init__(
        self,
        events: Iterator[dict[str, Any]],
        post: Callable[[dict[str, Any]], None],
        finished: Callable[[BaseException | None], None],
    ) -> None:
        self._events = events
        self._post = post
        self._finished = finished
        self._stop = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name="browse-stream", daemon=True
        )

    def start(self) -> Stream:
        self.thread.start()
        return self

    def _run(self) -> None:
        error: BaseException | None = None
        try:
            for event in self._events:
                if self._stop.is_set():
                    break
                self._post(event)
        except Exception as exc:  # noqa: BLE001 — any failure of a worker becomes a status line, never a crash
            error = exc
        finally:
            close = getattr(self._events, "close", None)
            if close is not None:
                close()
            self._finished(error)

    def close(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        self.thread.join(timeout)


def effect_events(
    effect: Any, state: BrowserState, source: EventSource
) -> Iterator[dict[str, Any]] | None:
    """The event stream an effect starts (`Rerank`, `Refine`), or None for the effects that are not streams."""
    if isinstance(effect, browse_state.Rerank):
        if state.session is None:
            return None
        return source.session(
            state.session, answers=dict(effect.answers), follow=effect.follow
        )
    if isinstance(effect, browse_state.Refine):
        return source.search(effect.query, parent=state.session)
    return None
