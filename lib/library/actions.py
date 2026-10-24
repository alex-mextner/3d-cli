"""library/actions.py — the action contract shared by the TUI (S7) and the CLI flags/`--json` of the library.

WHAT: eight pure functions — `search, rank, download, open_slicer, launch_agent, queue, photo, publish` — that
take plain arguments and return a JSON-able `ActionResult` (`schema: "3d.action/1"`, `ok`, `data`, `errors[]`,
`warnings[]`). They never print, prompt or `sys.exit`. The TUI binds a key to `ACTIONS[name]`; the CLI exposes
the same function as flags with `--json`; that is the TUI <-> CLI parity guarantee of umbrella #570.

ACCESSED VIA: `ACTIONS[name](**kwargs)` or `invoke(name, **kwargs)`. `invoke` is the edge for UIs: it turns a
raised `ThreeDError` (exit-code classes of `errors.py`) or `NotImplementedByDesign` into an `ok: false` result
with a machine-readable error `code`, so a key press never crashes the UI.

OWNERSHIP: slice S0 (#571) fixed the signatures and the result shape and implemented `queue`; slice S8 (#579)
implements `photo` (register a file, or capture the table photo with a lifted bed) and `publish`. Every other
action raises `NotImplementedByDesign(owner_issue)`: a typed seam for the slice named in `OWNERS`, not a fake
feature. A seam is dropped from `OWNERS` (`None`) when its slice lands.

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
                "path": str(Path(path).resolve()),
                "kind": kind,
                "created": created,
            },
        )
    finally:
        conn.close()


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

    from library import db as librarydb
    from library import records
    from publishing import flow, plan

    conn = librarydb.open_library(db_path)
    try:
        row = records.resolve(conn, "design", design, command="publish")
    finally:
        conn.close()
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
    if prep.ev.gate is not None or prep.ev.all_problems():
        refusals = [prep.ev.gate.message] if prep.ev.gate is not None else []
        refusals += list(prep.ev.all_problems())
        return error_result(
            "publish",
            [
                issue("refused", r, ["Fix it, then run the plan again."])
                for r in refusals
            ],
            data,
        )
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
    "photo": None,
    "publish": None,
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
