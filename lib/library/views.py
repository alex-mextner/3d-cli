"""library/views.py — `3d library makes|publications|relations list` and `3d library graph DESIGN`.

Read-only listings of the rows `3d make`, `3d photo` and `3d models publish` write (#579): the makes with their
status/printer/filament/photo count, the publications with their state and URL, the relations with labelled
endpoints, and the provenance graph around one design (source -> remix -> make). Every document carries a stable
`schema` key; a human table is the default, `--json` the machine form. Nothing is written, nothing is deleted.
"""

from __future__ import annotations

import json
from typing import Any

from errors import InvalidArgument, UsageError

LISTINGS = ("makes", "publications", "relations")
SUBCOMMANDS = (*LISTINGS, "graph")
SCHEMA = "3d.library.{}/1"


def _options(
    argv: list[str], *, positional: bool
) -> tuple[str | None, str | None, str | None, bool]:
    """(positional, --db, --design, --json) of one view invocation."""
    pos: str | None = None
    db_path = design = None
    as_json = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            as_json = True
        elif a in ("--db", "--design"):
            if i + 1 >= len(argv):
                raise UsageError(f"option {a} needs a value", command="library")
            if a == "--db":
                db_path = argv[i + 1]
            else:
                design = argv[i + 1]
            i += 1
        elif a.startswith("--"):
            raise UsageError(
                f"unknown option '{a}'",
                command="library",
                remediation=["See `3d library --help`."],
            )
        elif positional and pos is None:
            pos = a
        else:
            raise UsageError(
                f"unexpected argument '{a}'",
                command="library",
                remediation=["See `3d library --help`."],
            )
        i += 1
    return pos, db_path, design, as_json


def table(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    if not rows:
        return "(none)"
    cells = [
        [str(r.get(key) if r.get(key) is not None else "-") for key, _ in columns]
        for r in rows
    ]
    widths = [
        max(len(head), *(len(c[i]) for c in cells))
        for i, (_, head) in enumerate(columns)
    ]

    def line(parts: list[str]) -> str:
        return "  ".join(
            p.ljust(w) for p, w in zip(parts, widths, strict=True)
        ).rstrip()

    return "\n".join([line([h for _, h in columns]), *(line(c) for c in cells)])


def run(argv: list[str]) -> int:
    """`argv` = [subcommand, ...]: `makes|publications|relations list [--design D]` or `graph DESIGN`."""
    from library import db as librarydb
    from library import records

    sub = argv[0]
    if sub == "graph":
        design, db_path, _, as_json = _options(argv[1:], positional=True)
        if design is None:
            raise UsageError(
                "graph needs a DESIGN (name or uid)",
                command="library",
                remediation=["3d library graph mole"],
            )
        conn = librarydb.open_library(db_path)
        try:
            doc = records.graph(conn, design)
        finally:
            conn.close()
        print(
            json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False)
            if as_json
            else doc["text"]
        )
        return 0
    verb = argv[1] if len(argv) > 1 else "list"
    if verb != "list":
        raise InvalidArgument(f"library {sub}", verb, ["list"], command="library")
    _, db_path, design, as_json = _options(argv[2:], positional=False)
    conn = librarydb.open_library(db_path)
    try:
        if sub == "makes":
            rows = records.list_makes(conn, design)
            cols = [
                ("uid", "make"),
                ("design", "design"),
                ("status", "status"),
                ("printer", "printer"),
                ("filament", "filament"),
                ("photos", "photos"),
                ("source_url", "source"),
            ]
        elif sub == "publications":
            rows = records.list_publications(conn, design)
            cols = [
                ("uid", "publication"),
                ("design", "design"),
                ("platform", "platform"),
                ("state", "state"),
                ("url", "url"),
            ]
        else:
            rows = records.list_relations(conn, design)
            for r in rows:
                r["from_label"], r["to_label"] = r["from"]["label"], r["to"]["label"]
            cols = [("from_label", "from"), ("rel", "relation"), ("to_label", "to")]
    finally:
        conn.close()
    if as_json:
        print(
            json.dumps(
                {"schema": SCHEMA.format(sub), sub: rows},
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
        )
    else:
        print(table(rows, cols))
    return 0
