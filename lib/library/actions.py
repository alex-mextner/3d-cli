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

from collections.abc import Callable, Sequence
from typing import Any, TypedDict

from errors import InvalidArgument, ThreeDError

SCHEMA = "3d.action/1"
SEARCH_KINDS = ("wide", "narrow", "followup", "rewrite", "user")
PHOTO_KINDS = ("table", "bed", "render")
BACKENDS = ("omp", "claude", "codex", "opencode")


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
) -> ActionResult:
    """Search model sites and record a `search_session` with its `search_result` rows.

    `kind` is one of SEARCH_KINDS, `parent_session` the uid of the session this one refines.
    `data`: `{session: uid, query, results: [{site, model_id, url, title, rank, ...}]}`.
    """
    raise NotImplementedByDesign(
        "search", 576, "search core (50 results/site, analytics, persistence)"
    )


def rank(
    session: str,
    *,
    mode: str = "deterministic",
    limit: int | None = None,
    db_path: str | None = None,
) -> ActionResult:
    """Score and order the results of one session (`mode`: `deterministic` | `llm`); writes score/verdict/rationale.

    `data`: `{session: uid, ranked: [{result: uid, rank, score, verdict, rationale}]}`.
    """
    raise NotImplementedByDesign("rank", 577, "LLM relevance and query planning")


def download(
    url: str,
    *,
    dest_dir: str | None = None,
    select: Sequence[str] | None = None,
    db_path: str | None = None,
) -> ActionResult:
    """Download a model (all files, or the `select`ed names) and record a `model_source` with sha256 + licence.

    `data`: `{source: uid, dir, files: [{name, size, sha256}], license}`.
    """
    raise NotImplementedByDesign(
        "download", 576, "model download with provenance (reuses `3d models download`)"
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
