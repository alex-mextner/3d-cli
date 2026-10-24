"""3d library — bootstrap and inspect the local model library database (umbrella #570, S0).

The library DB is a separate local SQLite file (searches, model sources, designs, makes, photos,
publications, relations). This command only creates, migrates and reads it; the writers are the search,
download, queue and publish actions (`lib/library/actions.py`). Heavy logic lives in `lib/library/db.py`,
imported lazily so command discovery stays stdlib-only.
"""

from __future__ import annotations

import json
from typing import Any

from cli.registry import Command
from errors import InvalidArgument, UsageError

USAGE = """3d library <path|migrate|status|export|makes|publications|relations|graph> [--db PATH] [--json]
  Local model-library database (searches, sources, designs, makes, photos, publications, relations).
  Works offline; the file is created on first use.

Subcommands:
  path       print the database path ($THREED_LIBRARY_DB, else <data dir>/library/library.db). Never creates it.
             Example: 3d library path
  migrate    create the database if needed and apply pending migrations (idempotent; `init` is an alias).
             Example: 3d library migrate --json
  status     path, applied/pending migrations and row counts (migrates a fresh database first).
             Example: 3d library status --json
  export     every table as JSON rows (JSON columns decoded) on stdout, for backup or an agent.
             Example: 3d library export > library-backup.json
  makes list [--design D]
             the makes (print queue and history): status, printer, filament, photo count, source.
             Example: 3d library makes list --json
  publications list [--design D]
             where a design was published (platform, state planned|draft|published|failed|removed, URL).
             Example: 3d library publications list --design mole
  relations list [--design D]
             remix_of / make_of / derived_from links between sources, designs and makes.
             Example: 3d library relations list --json
  graph DESIGN
             the provenance around one design: source -> remix -> make, in both directions.
             Example: 3d library graph mole --json

Options:
  --db PATH  use this database file instead of the default.
             Example: 3d library status --db /tmp/library.db
  --design D restrict makes/publications/relations to one design (name or uid).
             Example: 3d library makes list --design mole
  --json     machine-readable output with a stable `schema` key (`3d.library/1`; export is always JSON).
             Example: 3d library path --json
  -h, --help print this help
"""

_SUBCOMMANDS = ("path", "migrate", "init", "status", "export")
_VIEWS = ("makes", "publications", "relations", "graph")


def _parse(argv: list[str]) -> tuple[str, str | None, bool]:
    sub = argv[0]
    if sub not in _SUBCOMMANDS:
        raise InvalidArgument("subcommand", sub, list(_SUBCOMMANDS), command="library")
    db_path: str | None = None
    as_json = False
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            as_json = True
            i += 1
        elif a == "--db":
            if i + 1 >= len(argv):
                raise UsageError("option --db needs a value", command="library")
            db_path = argv[i + 1]
            i += 2
        else:
            raise UsageError(
                f"unknown option '{a}'",
                command="library",
                remediation=["See `3d library --help`."],
            )
    return sub, db_path, as_json


def _emit(doc: dict[str, Any], as_json: bool, text: str) -> None:
    print(json.dumps(doc, indent=2, sort_keys=True) if as_json else text)


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv[0] in _VIEWS:
        from library import views

        return views.run(argv)
    sub, db_path, as_json = _parse(argv)

    from library import db as librarydb

    if sub == "path":
        path = str(db_path or librarydb.library_db_path())
        _emit({"schema": librarydb.SCHEMA, "path": path}, as_json, path)
        return 0

    conn = librarydb.open_library(db_path)
    try:
        if sub == "export":
            print(json.dumps(librarydb.export(conn), indent=2, sort_keys=True))
            return 0
        doc = librarydb.status(conn, db_path)
        counts = "  ".join(f"{t}={n}" for t, n in doc["counts"].items())
        text = (
            f"library: {doc['path']}\nmigrations: {doc['migrations']} pending: {doc['migrations_pending']}\n"
            f"rows: {counts}"
        )
        _emit(doc, as_json, text)
        return 0
    finally:
        conn.close()


COMMAND = Command(
    name="library",
    group="LIBRARIES",
    summary="local model-library database: path, migrate, status, export (searches, makes, photos, publications)",
    usage=USAGE,
    run=run,
)
