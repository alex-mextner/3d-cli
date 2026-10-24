"""3d make — record the prints (makes) of a design in the local library: queue -> printing -> done|failed.

A make is the row that photos, publications and relations hang on (umbrella #570, S8 #579). This command only
writes the local library database; it never contacts a printer, a marketplace or a site. Heavy logic lives in
`lib/library/records.py`, imported lazily so command discovery stays stdlib-only.
"""

from __future__ import annotations

import json
from typing import Any

from cli.registry import Command
from errors import InvalidArgument, UsageError

USAGE = """3d make <add|status|list> [options]
  Record prints of a design in the local library (status queued -> printing -> done|failed). Local database only:
  nothing here touches a printer or a website.

Subcommands:
  add --design NAME [--source-url URL|MODEL.JSON] [--printer P] [--filament F] [--status S]
      [--gcode FILE] [--project-dir DIR] [--db PATH] [--dry-run] [--json]
             add a make of design NAME (the design is created when new). --source-url records the model it was
             printed from (a marketplace URL or a model.json of `3d models download`) as a model_source and links
             design --derived_from--> source and make --make_of--> design.
             Example: 3d make add --design mole --printer k1se --filament "Rapid PLA+" --status printing
  status MAKE STATUS [--db PATH] [--json]
             move a make to queued|printing|done|failed (`finished_at` is set for done/failed). MAKE is the full
             uid or a unique prefix of at least 8 characters.
             Example: 3d make status 0199a1c2-7d3e done
  list [--design D] [--db PATH] [--json]
             the makes with status, printer, filament, photo count and source.
             Example: 3d make list --design mole --json

Options:
  --design NAME      the design (created when new); list: restrict to it.
                     Example: 3d make add --design mole
  --source-url SRC   the source model (URL or model.json). Example: 3d make add --design mole --source-url https://www.printables.com/model/311253-spoon-drawer-pull
  --printer P        printer name as in `3d printers`. Example: 3d make add --design mole --printer k1se
  --filament F       filament description. Example: 3d make add --design mole --filament "Rapid PLA+"
  --status S         queued (default) | printing | done | failed. Example: 3d make add --design mole --status done
  --gcode FILE       the sliced file of this print. Example: 3d make add --design mole --gcode print/mole_plate.gcode
  --project-dir DIR  the project directory of the design (used by `3d models offer`). Example: 3d make add --design mole --project-dir projects/mole
  --db PATH          library database file. Example: 3d make list --db /tmp/library.db
  --dry-run          add: validate and print the row that would be written, write nothing. Example: 3d make add --design mole --dry-run
  --json             machine-readable output, `schema: 3d.make/1`. Example: 3d make list --json
  -h, --help         print this help

Exit codes: 0 ok; 2 bad usage or an unknown make/design (the error lists the accepted values).
"""

SCHEMA = "3d.make/1"
_VALUE = {
    "--design",
    "--source-url",
    "--printer",
    "--filament",
    "--status",
    "--gcode",
    "--project-dir",
    "--db",
}
_FLAGS = {"--dry-run", "--json"}


def _parse(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    positional: list[str] = []
    opts: dict[str, str] = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        name, eq, inline = a.partition("=")
        if name in _VALUE:
            if eq:
                opts[name] = inline
            elif i + 1 < len(argv):
                i += 1
                opts[name] = argv[i]
            else:
                raise UsageError(
                    f"{name} needs a value",
                    command="make",
                    remediation=["See `3d make --help`."],
                )
        elif a in _FLAGS:
            opts[a] = "1"
        elif a.startswith("--"):
            raise UsageError(
                f"unknown option {a}",
                command="make",
                remediation=["See `3d make --help`."],
            )
        else:
            positional.append(a)
        i += 1
    return positional, opts


def _emit(doc: dict[str, Any], opts: dict[str, str], text: str) -> None:
    print(
        json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False)
        if "--json" in opts
        else text
    )


def _add(positional: list[str], opts: dict[str, str]) -> int:
    from library import db as librarydb
    from library import records

    if positional:
        raise UsageError(
            f"unexpected argument {positional[0]!r}",
            command="make",
            remediation=["3d make add --design NAME"],
        )
    if "--design" not in opts:
        raise UsageError(
            "make add needs --design NAME",
            command="make",
            remediation=["3d make add --design mole --printer k1se"],
        )
    status = opts.get("--status", "queued")
    if status not in records.MAKE_STATUSES:
        raise InvalidArgument(
            "--status", status, list(records.MAKE_STATUSES), command="make"
        )
    row = {
        k[2:].replace("-", "_"): opts.get(k)
        for k in (
            "--design",
            "--printer",
            "--filament",
            "--gcode",
            "--source-url",
            "--project-dir",
        )
    }
    row["status"] = status
    if "--dry-run" in opts:
        _emit(
            {"schema": SCHEMA, "dry_run": True, "make": row},
            opts,
            "dry run, nothing written: " + json.dumps(row, sort_keys=True),
        )
        return 0
    conn = librarydb.open_library(opts.get("--db"))
    try:
        source_uid = None
        if "--source-url" in opts:
            from publishing import flow
            from publishing.source import load_source

            src = load_source(opts["--source-url"], conn)
            site, model_id = (
                (src.site, src.model_id) if src.site else flow.site_key(src.url)
            )
            srow, _ = records.ensure_source(
                conn,
                site=site,
                model_id=model_id,
                url=src.url,
                license=src.license or None,
                author=src.author or None,
            )
            source_uid = str(srow["uid"])
        make, created = records.add_make(
            conn,
            opts["--design"],
            printer=opts.get("--printer"),
            filament=opts.get("--filament"),
            status=status,
            gcode=opts.get("--gcode"),
            source_uid=source_uid,
            project_dir=opts.get("--project-dir"),
        )
        design = conn.execute(
            "SELECT uid FROM design WHERE id = ?", (make["design_id"],)
        ).fetchone()
        doc = {
            "schema": SCHEMA,
            "dry_run": False,
            "created": created,
            "make": str(make["uid"]),
            "design": str(design["uid"]),
            "design_name": opts["--design"],
            "status": str(make["status"]),
            "source": source_uid,
        }
        _emit(
            doc, opts, f"make {doc['make']} of '{opts['--design']}' ({doc['status']})"
        )
        return 0
    finally:
        conn.close()


def _status(positional: list[str], opts: dict[str, str]) -> int:
    from library import db as librarydb
    from library import records

    if len(positional) != 2:
        raise UsageError(
            "make status needs MAKE and STATUS",
            command="make",
            remediation=["3d make status 0199a1c2-7d3e done"],
        )
    conn = librarydb.open_library(opts.get("--db"))
    try:
        row = records.set_make_status(conn, positional[0], positional[1])
        doc = {
            "schema": SCHEMA,
            "make": str(row["uid"]),
            "status": str(row["status"]),
            "finished_at": row["finished_at"],
        }
        _emit(doc, opts, f"make {doc['make']} is {doc['status']}")
        return 0
    finally:
        conn.close()


def _list(positional: list[str], opts: dict[str, str]) -> int:
    from library import db as librarydb
    from library import records, views

    if positional:
        raise UsageError(
            f"unexpected argument {positional[0]!r}",
            command="make",
            remediation=["3d make list [--design D]"],
        )
    conn = librarydb.open_library(opts.get("--db"))
    try:
        rows = records.list_makes(conn, opts.get("--design"))
    finally:
        conn.close()
    cols = [
        ("uid", "make"),
        ("design", "design"),
        ("status", "status"),
        ("printer", "printer"),
        ("filament", "filament"),
        ("photos", "photos"),
    ]
    _emit({"schema": SCHEMA, "makes": rows}, opts, views.table(rows, cols))
    return 0


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    subs = {"add": _add, "status": _status, "list": _list}
    if argv[0] not in subs:
        raise InvalidArgument("subcommand", argv[0], list(subs), command="make")
    if any(a in ("-h", "--help") for a in argv[1:]):
        print(USAGE)
        return 0
    positional, opts = _parse(argv[1:])
    return subs[argv[0]](positional, opts)


COMMAND = Command(
    name="make",
    group="LIBRARIES",
    summary="record prints (makes) of a design in the local library: add, status, list",
    usage=USAGE,
    run=run,
)
