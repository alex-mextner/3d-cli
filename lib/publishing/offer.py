"""Publish-offer hints that must stay cheap to import: the mode default and the bed-free message lines.

WHAT: `default_mode` (`$THREED_PUBLISH_OFFER`, else `offer` in `<config dir>/publish.json`, else `ask`),
`candidate_for_printer` (the make a printer's bed-free event is about) and `agent_lines` (what the omp session
subscribed to `3d printer subscribe --on bed-free` is told about the offer).

WHY SEPARATE: `printer_ops.subscriptions` imports this from the `3d printer vision` process, which runs under
`uv run --with VISION_DEPS` with nothing else installed; `publishing.offer_run` pulls in the whole publishing and
library stack (keychain, trimesh), so it must not be reachable from there (tests/test_printer_vision.py).
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from errors import InvalidArgument
from library import db as librarydb

MODES = ("auto", "ask", "off")
ENV_MODE = "THREED_PUBLISH_OFFER"
CONFIG_NAME = "publish.json"


def config_path() -> Path:
    from cli import paths

    return paths.config_dir() / CONFIG_NAME


def default_mode() -> str:
    """`$THREED_PUBLISH_OFFER`, else the config file's `offer`, else `ask`."""
    raw = os.environ.get(ENV_MODE)
    source = f"${ENV_MODE}"
    if raw is None:
        try:
            data = json.loads(config_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        raw = data.get("offer") if isinstance(data, dict) else None
        source = str(config_path())
    if raw is None:
        return "ask"
    if raw not in MODES:
        raise InvalidArgument(
            f"offer ({source})", str(raw), list(MODES), command="models"
        )
    return str(raw)


def latest_make(conn: sqlite3.Connection, printer: str) -> sqlite3.Row | None:
    return conn.execute(  # type: ignore[no-any-return]
        "SELECT * FROM make WHERE printer = ? AND status IN ('printing', 'done') ORDER BY id DESC LIMIT 1",
        (printer,),
    ).fetchone()


def candidate_for_printer(
    printer: str, db_path: str | None = None
) -> dict[str, str] | None:
    """The make a bed-free event of `printer` is about, with its project: None when the library does not
    know a printing/done make there, or the design has no project directory."""
    path = Path(db_path).expanduser() if db_path else librarydb.library_db_path()
    if not path.exists():
        return None
    conn = librarydb.open_library(path)
    try:
        make = latest_make(conn, printer)
        if make is None:
            return None
        design = conn.execute(
            "SELECT * FROM design WHERE id = ?", (make["design_id"],)
        ).fetchone()
        if design is None or not design["project_dir"]:
            return None
        return {
            "make": str(make["uid"]),
            "design": str(design["name"]),
            "project": str(design["project_dir"]),
        }
    finally:
        conn.close()


def agent_lines(printer: str, db_path: str | None = None) -> list[str]:
    """Lines for the bed-free message an omp session receives: the publish offer, when there is one to make.
    [] when the mode is off or the library knows no print on `printer`; the agent only ASKS the user."""
    try:
        if default_mode() == "off":
            return []
        found = candidate_for_printer(printer, db_path)
    except Exception:  # an offer hint must never break the bed-free event itself
        return []
    if found is None:
        return []
    return [
        f"4. Publish offer: the library has a make of '{found['design']}' on {printer}. Run "
        f"`3d models offer --make {found['make']} --trigger print --json` (dry run, publishes nothing), "
        "show the user the plan (platforms, CAD files, renders, photos, licence) and ASK before publishing; "
        f"only after a yes run the `command` of that document (`--yes`). `--publish off` silences this.",
        f"   Table photo for the listing: `3d photo capture --make {found['make']} --lift auto` "
        "(dry run; add --yes to move the bed).",
    ]
