"""library/actions.py — the action contract shared by the TUI (S7) and the CLI flags/`--json` of the library.

WHAT: eight pure functions — `search, rank, download, open_slicer, launch_agent, queue, photo, publish` — plus the two
read/launch helpers of the model browser (`open_browser`, `preview`, S7 #578) that
take plain arguments and return a JSON-able `ActionResult` (`schema: "3d.action/1"`, `ok`, `data`, `errors[]`,
`warnings[]`). They never print, prompt or `sys.exit`. The TUI binds a key to `ACTIONS[name]`; the CLI exposes
the same function as flags with `--json`; that is the TUI <-> CLI parity guarantee of umbrella #570.

ACCESSED VIA: `ACTIONS[name](**kwargs)` or `invoke(name, **kwargs)`. `invoke` is the edge for UIs: it turns a
raised `ThreeDError` (exit-code classes of `errors.py`) or `NotImplementedByDesign` into an `ok: false` result
with a machine-readable error `code`, so a key press never crashes the UI.

OWNERSHIP: slice S0 (#571) fixed the signatures and the result shape; each other slice implemented its actions
(`OWNERS`). An action whose slice has not landed raises `NotImplementedByDesign(owner_issue)`: a typed seam for
that slice, not a fake feature. The table-photo / publish slice (S8, #579) implements `photo` (register a file,
or capture the table photo with a lifted bed) and `publish`; the model browser (S7, #578) implements
`open_slicer`, `launch_agent`, `open_browser` and `preview`; the programs they start come from `library/launch.py`.

IMPORT RULE: stdlib-only at module top (checked by tests/test_imports.py); the library DB is imported lazily
inside the actions that need it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, TypedDict

from errors import InvalidArgument, MissingDependency, ThreeDError

if TYPE_CHECKING:
    from publishing.plan import Evaluation

SCHEMA = "3d.action/1"
SEARCH_KINDS = ("wide", "narrow", "followup", "rewrite", "user")
PHOTO_KINDS = ("table", "bed", "render")
BACKENDS = ("omp", "claude", "codex", "opencode")
RANK_MODES = ("deterministic", "llm")


class ActionIssue(TypedDict):
    """One entry of `errors[]` / `warnings[]`."""

    code: str
    message: str
    remediation: list[str]


class ActionResult(TypedDict):
    schema: str  # always SCHEMA
    action: str
    ok: bool
    data: dict[str, Any]
    errors: list[ActionIssue]
    warnings: list[ActionIssue]


class NotImplementedByDesign(NotImplementedError):
    """The action's contract exists but its implementation belongs to another slice of #570."""

    def __init__(self, action: str, owner_issue: int, what: str) -> None:
        super().__init__(
            f"{action}: {what} — implemented by slice issue #{owner_issue}"
        )
        self.action = action
        self.owner_issue = owner_issue


def issue(code: str, message: str, remediation: Sequence[str] = ()) -> ActionIssue:
    return {"code": code, "message": message, "remediation": list(remediation)}


def ok_result(
    action: str, data: dict[str, Any], warnings: Sequence[ActionIssue] = ()
) -> ActionResult:
    return {
        "schema": SCHEMA,
        "action": action,
        "ok": True,
        "data": data,
        "errors": [],
        "warnings": list(warnings),
    }


def error_result(
    action: str,
    errors: Sequence[ActionIssue],
    data: dict[str, Any] | None = None,
    warnings: Sequence[ActionIssue] = (),
) -> ActionResult:
    return {
        "schema": SCHEMA,
        "action": action,
        "ok": False,
        "data": data or {},
        "errors": list(errors),
        "warnings": list(warnings),
    }


# --------------------------------------------------------------------------- the actions
def search(
    query: str,
    *,
    sites: Sequence[str] | None = None,
    limit: int = 10,
    kind: str = "wide",
    parent_session: str | None = None,
    db_path: str | None = None,
    deep: bool = False,
    idempotency_key: str | None = None,
) -> ActionResult:
    """Search model sites and record a `search_session` with its `search_result` rows.

    `kind` is one of SEARCH_KINDS, `parent_session` the uid of the session this one refines, `deep` measures
    the smallest mesh of every hit (trimesh), `idempotency_key` makes a retried call return the first session.
    `data`: `{session: uid, query, results: [{site, model_id, url, title, rank, ...}]}`; a site that failed is a
    warning with code `site_failed`; when no site answered the result is `ok: false`.
    """
    from library import search as searchcore
    from marketplaces.base import SITES

    run = searchcore.run_search(
        query,
        searchcore.SearchOptions(
            sites=tuple(sites) if sites else SITES,
            limit=limit,
            deep=deep,
            kind=kind,
            parent_session=parent_session,
            idempotency_key=idempotency_key,
            db_path=db_path,
        ),
    )
    doc = run.doc
    problems = [
        issue("site_failed", f"{e['site']}: {e['message']}", e.get("remediation", []))
        for e in doc["errors"]
    ]
    warnings = problems + [
        issue("search_warning", f"{w['site']}: {w['message']}") for w in doc["warnings"]
    ]
    if not doc["results"]:
        return error_result(
            "search",
            problems,
            {"session": doc["search_id"], "query": query, "results": []},
        )
    flat = [
        {
            "site": site,
            "model_id": rec["id"],
            "url": rec["url"],
            "title": rec["title"],
            "rank": rec["rank"],
            "image_url": rec["image"] or None,
            "license": rec["license"],
            "paid": rec["paid"],
            "cad": rec["cad"],
            "size_mm": rec.get("size_mm"),
            "reference_suitable": rec["reference_suitable"],
        }
        for site, records in doc["results"].items()
        for rec in records
    ]
    return ok_result(
        "search",
        {
            "session": doc["search_id"],
            "query": query,
            "results": flat,
            "timings": doc["timings"],
        },
        warnings,
    )


def rank(
    session: str,
    *,
    mode: str = "deterministic",
    limit: int | None = None,
    db_path: str | None = None,
    classifier: str | None = None,
    planner: str | None = None,
    rewrite: bool = True,
    answers: Mapping[str, str] | None = None,
    follow: Sequence[int] = (),
    refresh: bool = False,
    require: bool = False,
) -> ActionResult:
    """Order the results of one saved search tree; `mode` `deterministic` | `llm`.

    `deterministic` reads the stored order (the search's own, plus any verdicts a previous run saved) and calls no
    model. `llm` runs the ranking pipeline of `3d models rerank` (library/rankflow.py, events
    docs/contracts/search-events.md) on the saved search `session` (uid, id or `latest`): the classifier judges every
    result (`fits|maybe|no`, `score 0..1`, `rationale`; cached per query+model+card), `answers` ({question key:
    value}) are applied as constraints and saved, `follow` runs suggested follow-up queries by number, and when
    few results fit and `rewrite` is true the planner proposes and runs wide/narrow/rewrite queries
    (`classifier`/`planner` override the configured models; `refresh` ignores cached verdicts). A model that is
    unavailable is a warning `llm_unavailable`, never an error, unless `require` (then MissingDependency).

    `data`: `{session: uid, query, ranked: [{result: uid|None, session: uid, site, model_id, rank, score, verdict,
    rationale}], counts: {results, fits, maybe, no, unjudged}, queries: [...], questions: [...], followups: [...],
    llm: {backend, calls, tokens...} | None}`. `rank` is the 1-based final position; `limit` cuts the list.
    """
    if mode not in RANK_MODES:
        raise InvalidArgument("mode", mode, list(RANK_MODES), command="models")
    if mode == "deterministic":
        return _rank_deterministic(session, limit, db_path)
    return _rank_llm(
        session,
        limit=limit,
        db_path=db_path,
        classifier=classifier,
        planner=planner,
        rewrite=rewrite,
        answers=answers,
        follow=follow,
        refresh=refresh,
        require=require,
    )


def _ranked_entry(
    result: str | None,
    session: str,
    site: str,
    model_id: str,
    position: int,
    score: Any,
    verdict: Any,
    rationale: Any,
) -> dict[str, Any]:
    """One `ranked` row of `rank`'s `data`."""
    return {
        "result": result,
        "session": session,
        "site": site,
        "model_id": model_id,
        "rank": position,
        "score": score,
        "verdict": verdict,
        "rationale": rationale,
    }


def _plan_queries(conn: Any, session_id: int, *, followup: bool) -> list[Any]:
    """The saved plan queries of a search session that are follow-ups (or, with `followup=False`, are not)."""
    from library import rankstore

    return [
        p
        for p in rankstore.list_plan_queries(conn, session_id)
        if (p["kind"] == "followup") == followup
    ]


def _stored_ranking(conn: Any, root_id: int) -> list[dict[str, Any]]:
    """Results of a search tree in stored order, first occurrence of each (site, model_id) only."""
    from library import rankstore

    seen: set[tuple[str, str]] = set()
    ranked: list[dict[str, Any]] = []
    for s in rankstore.tree_sessions(conn, root_id):
        for r in rankstore.session_results(conn, int(s["id"])):
            key = (r["site"], r["model_id"])
            if key in seen:
                continue
            seen.add(key)
            uid = conn.execute(
                "SELECT uid FROM search_result WHERE session_id = ? AND site = ? AND model_id = ?",
                (s["id"], r["site"], r["model_id"]),
            ).fetchone()["uid"]
            ranked.append(
                _ranked_entry(
                    uid,
                    s["uid"],
                    r["site"],
                    r["model_id"],
                    len(ranked) + 1,
                    r["score"],
                    r["verdict"],
                    r["rationale"],
                )
            )
    return ranked


def _rank_deterministic(
    session: str, limit: int | None, db_path: str | None
) -> ActionResult:
    """`rank` in `deterministic` mode: the stored order, no model."""
    from library import db as librarydb
    from library import rankstore

    conn = librarydb.open_library(db_path)
    try:
        row = rankstore.session_row(conn, session)
        ranked = _stored_ranking(conn, int(row["id"]))
        data: dict[str, Any] = {
            "session": row["uid"],
            "query": row["query"],
            "ranked": ranked[:limit] if limit else ranked,
            "counts": {"results": len(ranked)},
            "queries": _plan_queries(conn, int(row["id"]), followup=False),
            "questions": rankstore.list_questions(conn, int(row["id"])),
            "followups": _plan_queries(conn, int(row["id"]), followup=True),
            "llm": None,
        }
    finally:
        conn.close()
    return ok_result("rank", data)


def _rank_llm(
    session: str,
    *,
    limit: int | None,
    db_path: str | None,
    classifier: str | None,
    planner: str | None,
    rewrite: bool,
    answers: Mapping[str, str] | None,
    follow: Sequence[int],
    refresh: bool,
    require: bool,
) -> ActionResult:
    """`rank` in `llm` mode: run the ranking pipeline on the saved search."""
    from library import db as librarydb
    from library import rankflow, rankstore
    from library.llm import load_llm_config, with_overrides
    from library.search import SearchOptions

    config = with_overrides(load_llm_config(), classifier=classifier, planner=planner)
    options = rankflow.RankOptions(
        config,
        enabled=True,
        require=require,
        rewrite=rewrite,
        answers=dict(answers or {}),
        follow=tuple(follow),
        refresh=refresh,
    )
    pipeline = rankflow.Pipeline(options, SearchOptions(db_path=db_path))
    warnings: list[ActionIssue] = []
    done: dict[str, Any] | None = None
    for event in pipeline.session(session):
        if event["event"] == "warning":
            warnings.append(issue(str(event["code"]), str(event["message"])))
        elif event["event"] == "done":
            done = event
    if done is None:
        return error_result(
            "rank", [issue("error", "the ranking stream ended without a result")]
        )
    conn = librarydb.open_library(db_path)
    try:
        row = rankstore.session_row(conn, str(done["search"]))
        results = {
            (r["site"], r["model_id"]): r["uid"]
            for r in conn.execute(
                "SELECT site, model_id, uid FROM search_result WHERE session_id IN "
                "(SELECT id FROM search_session WHERE id = ? OR parent_id = ?) ORDER BY id DESC",
                (row["id"], row["id"]),
            )
        }
        followups = _plan_queries(conn, int(row["id"]), followup=True)
    finally:
        conn.close()
    ranked_out = [
        _ranked_entry(
            results.get((e["site"], e["id"])),
            e["search"],
            e["site"],
            e["id"],
            n,
            e["score"],
            e["verdict"],
            e["rationale"],
        )
        for n, e in enumerate(done["ranking"], start=1)
    ]
    data = {
        "session": done["search"],
        "query": done["query"],
        "ranked": ranked_out[:limit] if limit else ranked_out,
        "counts": done["counts"],
        "queries": done["queries"],
        "questions": done["questions"],
        "followups": followups,
        "llm": done["llm"],
    }
    return ok_result("rank", data, warnings)


def download(
    url: str,
    *,
    dest_dir: str | None = None,
    select: Sequence[str] | None = None,
    db_path: str | None = None,
    role: str | None = None,
    vitamin_kind: str | None = None,
    deep: bool = False,
) -> ActionResult:
    """Download a model (all files, or the `select`ed names) and record a `model_source` with sha256 + licence.

    `role` (printed|vitamin|reference|stand_in) and `vitamin_kind` (pcb|connector|screw|wire) go into the
    `model_source`; a vitamin/reference download also measures the meshes. `data`: `{source: uid, dir,
    files: [{name, size, sha256}], license, role}`; `source` is null when the library DB was unavailable
    (a warning says so, the files are still on disk).
    """
    from pathlib import Path

    from library import sources

    result = sources.download_model(
        url,
        Path(dest_dir) if dest_dir else None,
        select=select,
        role=role,
        vitamin_kind=vitamin_kind,
        deep=deep,
        db_path=db_path,
    )
    source = result["source"]
    return ok_result(
        "download",
        {
            "source": source["uid"] if source else None,
            "dir": result["dir"],
            "files": [
                {k: f[k] for k in ("name", "size", "sha256")} for f in result["files"]
            ],
            "license": result["model"]["license"] or None,
            "role": role,
        },
        [issue(w["code"], w["message"]) for w in result["warnings"]],
    )


def open_slicer(
    path: str | Sequence[str],
    *,
    slicer: str | None = None,
    dry_run: bool = True,
) -> ActionResult:
    """Open one model file (or several, as plates of one project) in the desktop slicer.

    `slicer` is a kind (`orca`, `snapmaker-orca`, `creality-print`, `bambu-studio`) or a command with `{path}` /
    `{paths}`; absent it comes from `$THREED_SLICER`, `browser.json` or the first installed slicer
    (`library/launch.py`). `dry_run` (default) only reports the command. A missing file is `not_found`, a missing
    slicer MissingDependency (127). `data`: `{slicer, command: [...], paths: [...], started: bool, pid}`.
    """
    from pathlib import Path

    from library import launch

    files = [path] if isinstance(path, str) else list(path)
    if not files:
        raise InvalidArgument(
            "path", "(none)", ["one or more model files"], command="models"
        )
    missing = [f for f in files if not Path(f).is_file()]
    if missing:
        return error_result(
            "open_slicer",
            [
                issue("not_found", f"no such file: {m}", ["Download the model first."])
                for m in missing
            ],
        )
    choice = launch.resolve_slicer(slicer)
    absolute = [str(Path(f).resolve()) for f in files]
    command = launch.slicer_argv(choice, absolute)
    data: dict[str, Any] = {
        "slicer": choice.kind,
        "command": command,
        "paths": absolute,
        "started": False,
        "pid": None,
    }
    if not dry_run:
        data["pid"] = launch.spawn_detached(command)
        data["started"] = True
    return ok_result("open_slicer", data)


def launch_agent(
    prompt: str,
    *,
    cwd: str | None = None,
    backend: str | None = None,
    dry_run: bool = True,
    context: dict[str, Any] | None = None,
    mode: str = "auto",
) -> ActionResult:
    """Start a coding agent (`backend` in BACKENDS, default omp) on `prompt` in `cwd`, seeded with a brief.

    `context` (`{query, session, models: [{title, url, verdict, dir, files, provenance...}]}`) is written with
    `prompt` as `brief.md` + `context.json` under the state dir (`library/launch.py`) and the agent is started with
    the brief attached. `mode`: `attach` (this terminal, waits), `tmux` (a detached tmux session), `detached` (no
    terminal, output in the bundle's log) or `auto` (attach on a terminal, else tmux, else detached). `dry_run`
    (default) reports the command and writes nothing. `data`: `{backend, command: [...], cwd, mode, brief,
    started: bool, pid | tmux | exit_code}`.
    """
    import os
    from pathlib import Path

    from library import launch

    if backend is not None and backend not in BACKENDS:
        raise InvalidArgument("backend", backend, list(BACKENDS), command="library")
    if mode not in launch.AGENT_MODES:
        raise InvalidArgument("mode", mode, list(launch.AGENT_MODES), command="models")
    chosen = launch.agent_backend(backend)
    work = str(Path(cwd).resolve()) if cwd else os.getcwd()
    if not Path(work).is_dir():
        return error_result(
            "launch_agent", [issue("not_found", f"no such directory: {work}")]
        )
    ctx = dict(context or {})
    key = str(ctx.get("session") or "adhoc")
    plan = launch.plan_agent(prompt, cwd=work, backend=chosen, bundle_key=key)
    mode = _agent_mode(mode)
    data: dict[str, Any] = {
        "backend": plan.backend,
        "command": plan.argv,
        "cwd": work,
        "mode": mode,
        "brief": str(plan.brief),
        "started": False,
    }
    if dry_run:
        return ok_result("launch_agent", data)
    launch.write_brief(plan, prompt, ctx)
    if mode == "attach":
        data["exit_code"] = launch.run_attached(plan.argv, cwd=work)
    elif mode == "tmux":
        data["tmux"] = launch.start_in_tmux(
            plan.argv, cwd=work, name=launch.tmux_session_name(key)
        )
    else:
        data["pid"] = launch.spawn_detached(
            plan.argv, cwd=work, log=plan.brief.parent / "agent.log"
        )
    data["started"] = True
    return ok_result("launch_agent", data)


def _agent_mode(mode: str) -> str:
    """`auto` resolved to attach (on a terminal), else tmux (when installed), else detached; others unchanged."""
    import shutil
    import sys

    if mode != "auto":
        return mode
    if sys.stdin.isatty() and sys.stdout.isatty():
        return "attach"
    return "tmux" if shutil.which("tmux") else "detached"


def open_browser(
    urls: str | Sequence[str],
    *,
    browser: str | None = None,
    dry_run: bool = True,
) -> ActionResult:
    """Open model pages in the web browser (`browser` command with `{url}`, `$THREED_BROWSER`, `browser.json`,
    `$BROWSER`, else the platform opener). `dry_run` (default) reports the commands. `data`: `{urls,
    commands: [[...]], started: bool}`."""
    from library import launch

    wanted = [urls] if isinstance(urls, str) else list(urls)
    if not wanted or not all(u.startswith(("http://", "https://")) for u in wanted):
        raise InvalidArgument(
            "urls",
            ", ".join(wanted) or "(none)",
            ["one or more http(s) URLs"],
            command="models",
        )
    commands = [launch.browser_argv(u, explicit=browser) for u in wanted]
    started = False
    if not dry_run:
        for command in commands:
            launch.spawn_detached(command)
        started = True
    return ok_result(
        "open_browser", {"urls": wanted, "commands": commands, "started": started}
    )


def preview(
    card: Mapping[str, Any],
    *,
    image: str | None = None,
    cols: int = 36,
) -> ActionResult:
    """What the browser's preview pane shows for one result card (`3d.models.search/2` record): details,
    description, tags, the file list and the analytics, plus the verdict when the card carries one.

    `image` is a terminal protocol (`kitty`, `iterm2`, `sixel`, `blocks`) or `auto` (what the terminal can show,
    `blocks` as the fallback, nothing without a TTY); the picture is fetched and encoded about `cols` cells wide.
    `data.image`: `{url, protocol, sequence}`; `sequence` is null when there is no picture or no support. Read-only.
    """
    if image not in (None, "auto", "kitty", "iterm2", "sixel", "blocks"):
        raise InvalidArgument(
            "image",
            str(image),
            ["auto", "kitty", "iterm2", "sixel", "blocks"],
            command="models",
        )
    data = _preview_details(card)
    warnings: list[ActionIssue] = []
    if image is not None and card.get("image"):
        warnings = _preview_image(card, image, cols, data["image"])
    return ok_result("preview", data, warnings)


def _preview_details(card: Mapping[str, Any]) -> dict[str, Any]:
    """The picture-less `data` of `preview` for one result card."""
    from library import textview

    facts = card.get("analytics") or {}
    files = [
        {k: f.get(k) for k in ("name", "size", "class")} for f in card.get("files", [])
    ]
    return {
        "site": card.get("site"),
        "id": card.get("id"),
        "title": card.get("title"),
        "url": card.get("url"),
        "author": card.get("author"),
        "license": card.get("license") or None,
        "price": card.get("price") or None,
        "description": textview.flat(str(card.get("summary") or "")),
        "tags": list(card.get("tags") or []),
        "files": files,
        "cad": card.get("cad"),
        "cad_formats": list(card.get("cad_formats") or []),
        "size_mm": card.get("size_mm"),
        "analytics": {
            k: facts[k]
            for k in (
                "file_count",
                "formats",
                "total_bytes",
                "is_multi_part",
                "is_assembly",
                "reference",
                "deep",
            )
            if k in facts
        },
        "verdict": card.get("verdict"),
        "score": card.get("score"),
        "rationale": card.get("rationale"),
        "image": {"url": card.get("image") or None, "protocol": None, "sequence": None},
    }


def _preview_image(
    card: Mapping[str, Any], image: str, cols: int, slot: dict[str, Any]
) -> list[ActionIssue]:
    """Fetch and encode the card's picture into `slot` (`protocol`, `sequence`); the warnings it raised."""
    import sys

    from cli import termimage
    from library import images as imagelib

    protocol = (
        termimage.preview_protocol(is_tty=sys.stdout.isatty())
        if image == "auto"
        else image
    )
    if protocol is None:
        return []
    shown = imagelib.render_images(
        [(str(card.get("site")), str(card["image"]))], protocol, cols=cols
    )
    sequence = shown.get(str(card["image"]))
    slot["protocol"] = protocol
    slot["sequence"] = sequence
    if sequence is not None:
        return []
    return [
        issue(
            "image_unavailable",
            "the picture could not be fetched or encoded; details only",
        )
    ]


def queue(
    design: str,
    *,
    printer: str | None = None,
    filament: str | None = None,
    gcode: str | None = None,
    source: str | None = None,
    source_key: str | None = None,
    db_path: str | None = None,
) -> ActionResult:
    """Add a make to the print queue: get-or-create the design by name and insert a `queued` make, through
    the same writer as `make add` (so the make's `make_of` and, with a source, `derived_from` relations exist
    for the provenance graph).

    This records intent only; it never uploads or starts a print. `source` is the uid of the `model_source`
    the design was made from; `source_key` makes the call idempotent. `data`: `{design, make, status}` (uids).
    """
    if not isinstance(design, str) or not design.strip():
        raise InvalidArgument(
            "design", repr(design), ["a non-empty design name"], command="library"
        )
    from library import db as librarydb
    from library import records

    conn = librarydb.open_library(db_path)
    try:
        if (
            source_key is not None
        ):  # an idempotent retry creates nothing, not even the design
            hit = conn.execute(
                "SELECT id, uid FROM make WHERE source_key = ?", (source_key,)
            ).fetchone()
            if hit is not None:
                return _queued_result(conn, int(hit["id"]), str(hit["uid"]), False)
        source_uid: str | None = None
        if source is not None:
            src = conn.execute(
                "SELECT uid FROM model_source WHERE uid = ?", (source,)
            ).fetchone()
            if src is None:
                return error_result(
                    "queue",
                    [
                        issue(
                            "not_found",
                            f"model_source {source!r} does not exist",
                            ["Use the uid printed by `download`."],
                        )
                    ],
                )
            source_uid = str(src["uid"])
        made, created = records.add_make(
            conn,
            design,
            printer=printer,
            filament=filament,
            gcode=gcode,
            source_uid=source_uid,
            source_key=source_key,
        )
        return _queued_result(conn, int(made["id"]), str(made["uid"]), created)
    finally:
        conn.close()


def _queued_result(
    conn: Any, make_id: int, make_uid: str, created: bool
) -> ActionResult:
    """The `queue` result for a make; an idempotency hit reports the make as it is now, not the new arguments."""
    state = conn.execute(
        "SELECT d.uid AS design_uid, m.status FROM make m JOIN design d ON d.id = m.design_id"
        " WHERE m.id = ?",
        (make_id,),
    ).fetchone()
    return ok_result(
        "queue",
        {
            "design": state["design_uid"],
            "make": make_uid,
            "status": state["status"],
            "created": created,
        },
    )


def photo(
    path: str | None = None,
    *,
    make: str | None = None,
    design: str | None = None,
    kind: str = "table",
    z_mm: float | None = None,
    capture: bool = False,
    db_path: str | None = None,
    source_key: str | None = None,
    printer: str | None = None,
    lift: str | float = "auto",
    camera: str | None = None,
    part_height_mm: float | None = None,
    out: str | None = None,
    dry_run: bool = True,
) -> ActionResult:
    """Attach a photo of a make/design (`kind` in PHOTO_KINDS): register the file at `path`, or `capture` one.

    Registering (`path` + exactly one of `make`/`design`) is immediate; `source_key` makes it idempotent.
    `capture=True` takes the table photo of a MAKE: the printer's bed is lifted to `lift` (`"auto"` or a toolhead
    Z in mm) by the guarded Z move of `bed_view`, a fresh camera frame is saved to `out`, the bed is put back
    (also on error) and the file is registered with its `z_mm`. `dry_run` (default) only reads the printer and
    reports the lift it would make. A busy printer, an unknown height of the parts on the bed or a lift outside
    the allowed Z range raise `GateFailure` (nothing moves). `data`: `{photo: uid|None, path, kind, z_mm}`; for a
    capture also `{make, plan, moved, restored, restore_error, fresh, dry_run}`.
    """
    if kind not in PHOTO_KINDS:
        raise InvalidArgument("kind", kind, list(PHOTO_KINDS), command="library")
    if capture:
        if make is None or design is not None or path is not None:
            raise InvalidArgument(
                "capture",
                "make/design/path",
                [
                    "--make ID only: a capture belongs to a make and takes its file from the camera"
                ],
                command="photo",
            )
        from library import capture as capturing

        return capturing.capture(
            make=make,
            kind=kind,
            printer=printer,
            lift=lift,
            camera=camera,
            part_height_mm=part_height_mm,
            out=out,
            dry_run=dry_run,
            db_path=db_path,
        )
    if path is None:
        raise InvalidArgument(
            "path", "(missing)", ["a photo file, or capture=True"], command="library"
        )
    if (make is None) == (design is None):
        raise InvalidArgument(
            "make/design",
            "both" if make else "neither",
            ["exactly one of make, design"],
            command="library",
        )
    from pathlib import Path

    from library import db as librarydb
    from library import records

    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise InvalidArgument("path", path, ["an existing file"], command="library")
    conn = librarydb.open_library(db_path)
    try:
        try:
            row, created = records.add_photo(
                conn,
                path,
                make=make,
                design=design,
                kind=kind,
                z_mm=z_mm,
                source_key=source_key,
            )
        except records.RecordNotFound as exc:
            return error_result(
                "photo", [issue("not_found", exc.message, exc.remediation)]
            )
        return ok_result(
            "photo",
            {
                "photo": str(row["uid"]),
                "path": str(resolved),
                "kind": kind,
                "created": created,
            },
        )
    finally:
        conn.close()


def _publish_design(db_path: str | None, design: str, *, write: bool) -> sqlite3.Row:
    """The design row `publish` acts on. A plan only reads: the library is opened without migrating or
    creating it (as `flow.prepare` does); only a real publish (`write`) opens it migrating."""
    from library import records
    from publishing import flow

    with flow.library(db_path, create=write) as conn:
        if conn is None:
            raise records.RecordNotFound("design", design, command="publish")
        return records.resolve(conn, "design", design, command="publish")


def _refused_plan(ev: Evaluation, data: dict[str, Any]) -> ActionResult | None:
    """The `ok: false` result of a plan that must not be sent (the gate message, then every problem), if any."""
    refusals = [ev.gate.message] if ev.gate is not None else []
    refusals += ev.all_problems()
    if not refusals:
        return None
    return error_result(
        "publish",
        [issue("refused", r, ["Fix it, then run the plan again."]) for r in refusals],
        data,
    )


def publish(
    design: str,
    *,
    platforms: Sequence[str] = (),
    dry_run: bool = True,
    kind: str = "auto",
    of: str | None = None,
    make: str | None = None,
    allow_nonprintable: Sequence[str] = (),
    reason: str = "",
    drop_nonprintable: bool = False,
    db_path: str | None = None,
) -> ActionResult:
    """Publish a design to `platforms` (default: all); `dry_run` (default) plans only and writes nothing.

    The design's `project_dir` (set by `3d make add --project-dir` / `queue`) is the project that is published;
    `kind`: auto | original | remix | make (`of` = the source model, `make` = the make whose photos go).
    Publishing (`dry_run=False`) records `publication` rows (planned -> draft|published|failed), the `remix_of`
    relation and the render photos. A refused plan (licence, missing 3mf/scad, a non-printable part in the
    publish set) is an `ok: false` result carrying the whole plan; nothing is sent.
    `data`: `{design: uid, kind, dry_run, publications: [{platform, state, url}], plan: {3d.publish/1}}`.
    """
    from pathlib import Path

    from publishing import flow, plan

    row = _publish_design(db_path, design, write=not dry_run)
    if not row["project_dir"]:
        return error_result(
            "publish",
            [
                issue(
                    "no_project",
                    f"design {row['name']!r} has no project directory to publish",
                    ["Record it: `3d make add --design NAME --project-dir DIR`."],
                )
            ],
        )
    prep = flow.prepare(
        Path(str(row["project_dir"])),
        platform=",".join(platforms) or "all",
        kind=kind,
        of=of,
        design=str(row["name"]),
        make=make,
        db_path=db_path,
        allow=tuple(allow_nonprintable),
        reason=reason,
        drop=drop_nonprintable,
    )
    doc = plan.to_json(prep.ev, dry_run=dry_run)
    data: dict[str, Any] = {
        "design": str(row["uid"]),
        "kind": prep.kind,
        "dry_run": dry_run,
        "publications": [],
        "plan": doc,
    }
    refused = _refused_plan(prep.ev, data)
    if refused is not None:
        return refused
    if dry_run:
        data["publications"] = [
            {"platform": p.platform, "state": "planned", "url": None}
            for p in prep.ev.plans
        ]
        return ok_result(
            "publish",
            data,
            [issue("dry_run", "nothing was sent", ["Pass dry_run=False to publish."])],
        )
    outcomes = flow.send(prep)
    data["publications"] = [
        {"platform": o.platform, "state": o.state, "url": o.url} for o in outcomes
    ]
    failed = [o for o in outcomes if not o.ok]
    if failed:
        return error_result(
            "publish",
            [
                issue(
                    "publish_failed",
                    f"{o.platform}: {o.message}",
                    ["See the message; the row stays `failed`."],
                )
                for o in failed
            ],
            data,
        )
    return ok_result("publish", data)


# --------------------------------------------------------------------------- registry
ACTIONS: dict[str, Callable[..., ActionResult]] = {
    "search": search,
    "rank": rank,
    "download": download,
    "open_slicer": open_slicer,
    "launch_agent": launch_agent,
    "queue": queue,
    "photo": photo,
    "publish": publish,
    "open_browser": open_browser,
    "preview": preview,
}

# action -> issue of the slice that implements it (None = implemented in this contract slice).
OWNERS: dict[str, int | None] = {
    "search": 576,
    "rank": 577,
    "download": 576,
    "open_slicer": 578,
    "launch_agent": 578,
    "queue": None,
    "photo": None,
    "publish": None,
    "open_browser": 578,
    "preview": 578,
}


def invoke(name: str, **kwargs: Any) -> ActionResult:
    """Run an action by name and never raise: unknown names, structured errors, storage failures and
    unimplemented seams become `ok: false` results (codes `unknown_action`, `invalid_argument`,
    `missing_dependency`, `error`, `storage_error`, `not_implemented`)."""
    fn = ACTIONS.get(name)
    if fn is None:
        return error_result(
            name,
            [
                issue(
                    "unknown_action",
                    f"unknown action {name!r}",
                    [f"Accepted: {', '.join(ACTIONS)}"],
                )
            ],
        )
    try:
        return fn(**kwargs)
    except NotImplementedByDesign as e:
        return error_result(
            name, [issue("not_implemented", str(e), [f"Tracked in #{e.owner_issue}."])]
        )
    except InvalidArgument as e:
        return error_result(name, [issue("invalid_argument", e.message, e.remediation)])
    except MissingDependency as e:
        return error_result(
            name, [issue("missing_dependency", e.message, e.remediation)]
        )
    except ThreeDError as e:
        return error_result(name, [issue("error", e.message, e.remediation)])
    except TypeError as e:  # unknown/missing keyword for this action
        return error_result(
            name, [issue("invalid_argument", str(e), [f"See `help(actions.{name})`."])]
        )
    except (
        sqlite3.Error,
        OSError,
        RuntimeError,  # printops.db.MigrationError: a mismatched or unreadable migration
    ) as e:  # locked/unreadable database, unwritable data dir
        return error_result(
            name,
            [
                issue(
                    "storage_error",
                    f"{type(e).__name__}: {e}",
                    [
                        "Check `3d library path` and that no other process holds the database."
                    ],
                )
            ],
        )
