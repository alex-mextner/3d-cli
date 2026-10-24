"""library/actions.py — the action contract shared by the TUI (S7) and the CLI flags/`--json` of the library.

WHAT: eight pure functions — `search, rank, download, open_slicer, launch_agent, queue, photo, publish` — that
take plain arguments and return a JSON-able `ActionResult` (`schema: "3d.action/1"`, `ok`, `data`, `errors[]`,
`warnings[]`). They never print, prompt or `sys.exit`. The TUI binds a key to `ACTIONS[name]`; the CLI exposes
the same function as flags with `--json`; that is the TUI <-> CLI parity guarantee of umbrella #570.

ACCESSED VIA: `ACTIONS[name](**kwargs)` or `invoke(name, **kwargs)`. `invoke` is the edge for UIs: it turns a
raised `ThreeDError` (exit-code classes of `errors.py`) or `NotImplementedByDesign` into an `ok: false` result
with a machine-readable error `code`, so a key press never crashes the UI.

OWNERSHIP: this slice (S0, #571) fixes the signatures and the result shape and implements only what is
trivial (`queue`, and `photo` for an already existing file). Every other action raises
`NotImplementedByDesign(owner_issue)`: a typed seam for the slice named in `OWNERS`, not a fake feature.

IMPORT RULE: stdlib-only at module top (checked by tests/test_imports.py); the library DB is imported lazily
inside the actions that need it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypedDict

from errors import InvalidArgument, ThreeDError

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


# --------------------------------------------------------------------------- the eight actions
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
    from library import rankflow, rankstore
    from library import db as librarydb
    from library.llm import load_llm_config, with_overrides

    if mode == "deterministic":
        conn = librarydb.open_library(db_path)
        try:
            row = rankstore.session_row(conn, session)
            seen: set[tuple[str, str]] = set()
            ranked: list[dict[str, Any]] = []
            for s in rankstore.tree_sessions(conn, int(row["id"])):
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
                        {
                            "result": uid,
                            "session": s["uid"],
                            "site": r["site"],
                            "model_id": r["model_id"],
                            "rank": len(ranked) + 1,
                            "score": r["score"],
                            "verdict": r["verdict"],
                            "rationale": r["rationale"],
                        }
                    )
            data: dict[str, Any] = {
                "session": row["uid"],
                "query": row["query"],
                "ranked": ranked[:limit] if limit else ranked,
                "counts": {"results": len(ranked)},
                "queries": [
                    p
                    for p in rankstore.list_plan_queries(conn, int(row["id"]))
                    if p["kind"] != "followup"
                ],
                "questions": rankstore.list_questions(conn, int(row["id"])),
                "followups": [
                    p
                    for p in rankstore.list_plan_queries(conn, int(row["id"]))
                    if p["kind"] == "followup"
                ],
                "llm": None,
            }
        finally:
            conn.close()
        return ok_result("rank", data)

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
        followups = [
            p
            for p in rankstore.list_plan_queries(conn, int(row["id"]))
            if p["kind"] == "followup"
        ]
    finally:
        conn.close()
    ranked_out = [
        {
            "result": results.get((e["site"], e["id"])),
            "session": e["search"],
            "site": e["site"],
            "model_id": e["id"],
            "rank": n,
            "score": e["score"],
            "verdict": e["verdict"],
            "rationale": e["rationale"],
        }
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
    path: str, *, slicer: str | None = None, dry_run: bool = True
) -> ActionResult:
    """Open a model/3MF in the desktop slicer; `dry_run` (default) only reports the command it would run.

    `data`: `{slicer, command: [...], started: bool}`.
    """
    raise NotImplementedByDesign("open_slicer", 578, "slicer discovery and launch")


def launch_agent(
    prompt: str,
    *,
    cwd: str | None = None,
    backend: str = "omp",
    dry_run: bool = True,
    context: dict[str, Any] | None = None,
) -> ActionResult:
    """Start a coding agent (`backend` in BACKENDS) on `prompt` in `cwd`; `dry_run` (default) reports only.

    `data`: `{backend, command: [...], cwd, started: bool}`.
    """
    if backend not in BACKENDS:
        raise InvalidArgument("backend", backend, list(BACKENDS), command="library")
    raise NotImplementedByDesign("launch_agent", 578, "agent launch (tmux/omp)")


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
    """Add a make to the print queue: get-or-create the design by name and insert a `queued` make.

    This records intent only; it never uploads or starts a print. `source` is the uid of the `model_source`
    the design was made from; `source_key` makes the call idempotent. `data`: `{design, make, status}` (uids).
    """
    from library import db as librarydb

    conn = librarydb.open_library(db_path)
    try:
        existing = conn.execute(
            "SELECT id, uid FROM design WHERE name = ?", (design,)
        ).fetchone()
        design_row = (
            (int(existing["id"]), str(existing["uid"]))
            if existing
            else _inserted(librarydb.insert_row(conn, "design", {"name": design}))
        )
        source_id: int | None = None
        if source is not None:
            src = conn.execute(
                "SELECT id FROM model_source WHERE uid = ?", (source,)
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
            source_id = int(src["id"])
        now = librarydb.db.utc_now()
        made = librarydb.insert_row(
            conn,
            "make",
            {
                "design_id": design_row[0],
                "source_id": source_id,
                "printer": printer,
                "filament": filament,
                "status": "queued",
                "queued_at": now,
                "gcode": gcode,
            },
            source_key=source_key,
        )
        return ok_result(
            "queue",
            {
                "design": design_row[1],
                "make": made.uid,
                "status": "queued",
                "created": made.created,
            },
        )
    finally:
        conn.close()


def photo(
    path: str | None = None,
    *,
    make: str | None = None,
    design: str | None = None,
    kind: str = "table",
    z_mm: float | None = None,
    capture: bool = False,
    db_path: str | None = None,
) -> ActionResult:
    """Attach a photo of a make/design (`kind` in PHOTO_KINDS): register `path`, or `capture` one.

    Registering an existing file is implemented here; `capture=True` (top camera, bed lift to `z_mm`) belongs
    to the make/photo slice. `data`: `{photo: uid, path, kind}`.
    """
    if kind not in PHOTO_KINDS:
        raise InvalidArgument("kind", kind, list(PHOTO_KINDS), command="library")
    if capture or path is None:
        raise NotImplementedByDesign("photo", 579, "photo capture with bed lift")
    if (make is None) == (design is None):
        raise InvalidArgument(
            "make/design",
            "both" if make else "neither",
            ["exactly one of make, design"],
            command="library",
        )
    from pathlib import Path

    from library import db as librarydb

    conn = librarydb.open_library(db_path)
    try:
        column, table, uid = (
            ("make_id", "make", make) if make else ("design_id", "design", design)
        )
        row = conn.execute(f"SELECT id FROM {table} WHERE uid = ?", (uid,)).fetchone()
        if row is None:
            return error_result(
                "photo",
                [
                    issue(
                        "not_found",
                        f"{table} {uid!r} does not exist",
                        ["Use the uid printed by `queue`."],
                    )
                ],
            )
        ins = librarydb.insert_row(
            conn,
            "photo",
            {
                column: int(row["id"]),
                "path": str(Path(path).resolve()),
                "kind": kind,
                "z_mm": z_mm,
                "taken_at": librarydb.db.utc_now(),
            },
        )
        return ok_result(
            "photo", {"photo": ins.uid, "path": str(Path(path).resolve()), "kind": kind}
        )
    finally:
        conn.close()


def publish(
    design: str,
    *,
    platforms: Sequence[str] = (),
    dry_run: bool = True,
    db_path: str | None = None,
) -> ActionResult:
    """Publish a design to `platforms`; `dry_run` (default) plans only. Records `publication` rows.

    `data`: `{design: uid, publications: [{platform, state, url}]}`.
    """
    raise NotImplementedByDesign(
        "publish", 579, "publishing (lands #564 models publish)"
    )


def _inserted(ins: Any) -> tuple[int, str]:
    return ins.id, ins.uid


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
}

# action -> issue of the slice that implements it (None = implemented in this contract slice).
OWNERS: dict[str, int | None] = {
    "search": 576,
    "rank": 577,
    "download": 576,
    "open_slicer": 578,
    "launch_agent": 578,
    "queue": None,
    "photo": 579,
    "publish": 579,
}


def invoke(name: str, **kwargs: Any) -> ActionResult:
    """Run an action by name and never raise: unknown names, structured errors and unimplemented seams
    become `ok: false` results (codes `unknown_action`, `invalid_argument`, `error`, `not_implemented`)."""
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
    except ThreeDError as e:
        return error_result(name, [issue("error", e.message, e.remediation)])
    except TypeError as e:  # unknown/missing keyword for this action
        return error_result(
            name, [issue("invalid_argument", str(e), [f"See `help(actions.{name})`."])]
        )
