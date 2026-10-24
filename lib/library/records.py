"""library/records.py — the typed writes and reads of designs, makes, photos, publications and relations.

WHAT: the one place that turns "this design was printed", "this photo belongs to that make", "this design was
published to Printables" and "this design is a remix of that source" into library rows (umbrella #570, S8 #579).
`3d make`, `3d photo`, `3d library makes|publications|relations|graph`, `3d models publish` and the
`library.actions` photo/publish actions all go through these functions, so a row is written one way only.

ACCESSED VIA: the commands above and `library.actions`; they pass an open connection from
`library.db.open_library`. No function prints, prompts or exits.

INVARIANTS:
  - Nothing is ever deleted. A publication that failed or was withdrawn changes its `state`; a make moves
    queued -> printing -> done|failed by UPDATE of `status` only.
  - Every write goes through `library.db.insert_row` (full UUIDv7 uid, `library.<table>.created` event) or
    `update_row` (`library.<table>.updated` event in the same transaction).
  - A relation points at rows by FULL uid; `relate` is idempotent (`source_key` = rel + endpoints).
  - A CLI reference (`--make ID`, `--design NAME`) resolves by full uid, by a unique uid prefix of at least 8
    characters, or (design only) by name; output always prints the full uid (#452).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from typing import Any

from errors import InvalidArgument, UsageError
from library import db as librarydb
from printops import db, events

MAKE_STATUSES = ("queued", "printing", "done", "failed")
PHOTO_KINDS = ("table", "bed", "render")
PUBLICATION_STATES = ("planned", "draft", "published", "failed", "removed")
RELATIONS = ("remix_of", "make_of", "derived_from")
ROW_KINDS = ("design", "make", "model_source")
MIN_PREFIX = 8
ACTOR = "cli:library"


class RecordNotFound(UsageError):
    """A reference names no row (or several). Exit 2 with the accepted forms."""

    def __init__(
        self, table: str, ref: str, *, command: str = "library", why: str = ""
    ) -> None:
        super().__init__(
            f"{table} {ref!r} {why or 'does not exist'}",
            command=command,
            remediation=[
                f"Use the full uid printed by `3d library {table}s list`"
                + (
                    " or the design name."
                    if table == "design"
                    else f" (or a unique prefix of >= {MIN_PREFIX} characters)."
                )
            ],
        )


def resolve(
    conn: sqlite3.Connection, table: str, ref: str, *, command: str = "library"
) -> sqlite3.Row:
    """The row of `table` that `ref` names: full uid, unique uid prefix (>= 8 chars) or, for designs, the name."""
    if table not in librarydb.TABLES:
        raise KeyError(f"unknown library table {table!r}")
    row = conn.execute(f"SELECT * FROM {table} WHERE uid = ?", (ref,)).fetchone()
    if row is not None:
        return row  # type: ignore[no-any-return]
    if table == "design":
        row = conn.execute("SELECT * FROM design WHERE name = ?", (ref,)).fetchone()
        if row is not None:
            return row  # type: ignore[no-any-return]
    if len(ref) >= MIN_PREFIX and all(c in "0123456789abcdef-" for c in ref.lower()):
        hits = conn.execute(
            f"SELECT * FROM {table} WHERE uid LIKE ? ESCAPE '\\' LIMIT 2",
            (ref.lower() + "%",),
        ).fetchall()
        if len(hits) == 1:
            return hits[0]  # type: ignore[no-any-return]
        if len(hits) > 1:
            raise RecordNotFound(
                table,
                ref,
                command=command,
                why="matches several rows; use more characters of the uid",
            )
    raise RecordNotFound(table, ref, command=command)


def _choice(flag: str, value: str, accepted: Sequence[str], command: str) -> str:
    if value not in accepted:
        raise InvalidArgument(flag, value, list(accepted), command=command)
    return value


def update_row(
    conn: sqlite3.Connection,
    table: str,
    uid: str,
    values: dict[str, Any],
    *,
    actor: str = ACTOR,
) -> None:
    """UPDATE the listed insertable columns of one row and append `library.<table>.updated` (one transaction)."""
    columns = librarydb.TABLES[table][0]
    unknown = set(values) - set(columns)
    if unknown:
        raise KeyError(
            f"{table}: unknown column(s) {sorted(unknown)}; updatable: {list(columns)}"
        )
    sets = ", ".join(f"{k} = ?" for k in values)
    with db.transaction(conn):
        cur = conn.execute(
            f"UPDATE {table} SET {sets} WHERE uid = ?", (*values.values(), uid)
        )
        if cur.rowcount != 1:
            raise RecordNotFound(table, uid)
        events.emit(
            conn,
            f"library.{table}.updated",
            f"{table}:{uid}",
            {"set": sorted(values)},
            actor,
        )


# --------------------------------------------------------------------------- designs and makes
def ensure_design(
    conn: sqlite3.Connection, name: str, project_dir: str | None = None
) -> sqlite3.Row:
    """The design called `name` (created when new); a known project directory is filled in when it was unset."""
    if not name.strip():
        raise InvalidArgument("design", name, ["a non-empty name"], command="library")
    row = conn.execute("SELECT * FROM design WHERE name = ?", (name,)).fetchone()
    if row is None:
        librarydb.insert_row(conn, "design", {"name": name, "project_dir": project_dir})
    elif project_dir and not row["project_dir"]:
        update_row(conn, "design", str(row["uid"]), {"project_dir": project_dir})
    return resolve(conn, "design", name)


def add_make(
    conn: sqlite3.Connection,
    design: str,
    *,
    printer: str | None = None,
    filament: str | None = None,
    status: str = "queued",
    gcode: str | None = None,
    source_uid: str | None = None,
    project_dir: str | None = None,
    source_key: str | None = None,
) -> tuple[sqlite3.Row, bool]:
    """Record one make of `design` (created by name when new). -> (row, created)."""
    _choice("--status", status, MAKE_STATUSES, "make")
    d = ensure_design(conn, design, project_dir)
    src_id = (
        int(resolve(conn, "model_source", source_uid, command="make")["id"])
        if source_uid
        else None
    )
    now = db.utc_now()
    ins = librarydb.insert_row(
        conn,
        "make",
        {
            "design_id": int(d["id"]),
            "source_id": src_id,
            "printer": printer,
            "filament": filament,
            "status": status,
            "queued_at": now,
            "finished_at": now if status in ("done", "failed") else None,
            "gcode": gcode,
        },
        source_key=source_key,
    )
    if ins.created:
        relate(conn, "make_of", "make", ins.uid, "design", str(d["uid"]))
        if src_id is not None:
            src = conn.execute(
                "SELECT uid FROM model_source WHERE id = ?", (src_id,)
            ).fetchone()
            relate(
                conn,
                "derived_from",
                "design",
                str(d["uid"]),
                "model_source",
                str(src["uid"]),
            )
    return resolve(conn, "make", ins.uid), ins.created


def set_make_status(
    conn: sqlite3.Connection, make_ref: str, status: str
) -> sqlite3.Row:
    """queued -> printing -> done|failed; `finished_at` is set when it ends. Never deletes."""
    _choice("--status", status, MAKE_STATUSES, "make")
    row = resolve(conn, "make", make_ref, command="make")
    values: dict[str, Any] = {"status": status}
    if status in ("done", "failed"):
        values["finished_at"] = db.utc_now()
    update_row(conn, "make", str(row["uid"]), values)
    return resolve(conn, "make", str(row["uid"]))


# --------------------------------------------------------------------------- photos
def add_photo(
    conn: sqlite3.Connection,
    path: str,
    *,
    make: str | None = None,
    design: str | None = None,
    kind: str = "table",
    z_mm: float | None = None,
    source_key: str | None = None,
) -> tuple[sqlite3.Row, bool]:
    """Register the file at `path` as a photo of exactly one make or design. -> (row, created)."""
    from pathlib import Path

    _choice("--kind", kind, PHOTO_KINDS, "photo")
    if (make is None) == (design is None):
        raise InvalidArgument(
            "make/design",
            "both" if make else "neither",
            ["exactly one of --make, --design"],
            command="photo",
        )
    column, table, ref = (
        ("make_id", "make", make) if make else ("design_id", "design", design)
    )
    owner = resolve(conn, table, str(ref), command="photo")
    ins = librarydb.insert_row(
        conn,
        "photo",
        {
            column: int(owner["id"]),
            "path": str(Path(path).resolve()),
            "kind": kind,
            "z_mm": z_mm,
            "taken_at": db.utc_now(),
        },
        source_key=source_key,
    )
    return resolve(conn, "photo", ins.uid), ins.created


def photos_of(
    conn: sqlite3.Connection,
    *,
    design_id: int | None = None,
    make_id: int | None = None,
) -> list[dict[str, Any]]:
    """Photos of a design and of all its makes (or of one make), oldest first."""
    if make_id is not None:
        rows = conn.execute(
            "SELECT * FROM photo WHERE make_id = ? ORDER BY id", (make_id,)
        ).fetchall()
    elif design_id is not None:
        rows = conn.execute(
            "SELECT p.* FROM photo p LEFT JOIN make m ON p.make_id = m.id "
            "WHERE p.design_id = ? OR m.design_id = ? ORDER BY p.id",
            (design_id, design_id),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM photo ORDER BY id").fetchall()
    return [photo_view(r) for r in rows]


def photo_view(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "uid": r["uid"],
        "path": r["path"],
        "kind": r["kind"],
        "z_mm": r["z_mm"],
        "taken_at": r["taken_at"],
    }


# --------------------------------------------------------------------------- publications and relations
def add_publication(
    conn: sqlite3.Connection,
    design: str,
    platform: str,
    *,
    state: str,
    url: str | None = None,
    project_dir: str | None = None,
    source_key: str | None = None,
) -> sqlite3.Row:
    """Record one publication of `design` on `platform`. A rerun with the same `source_key` returns that row."""
    _choice("state", state, PUBLICATION_STATES, "models")
    d = ensure_design(conn, design, project_dir)
    ins = librarydb.insert_row(
        conn,
        "publication",
        {"design_id": int(d["id"]), "platform": platform, "url": url, "state": state},
        source_key=source_key,
    )
    return resolve(conn, "publication", ins.uid)


def set_publication(
    conn: sqlite3.Connection, uid: str, *, state: str, url: str | None = None
) -> sqlite3.Row:
    _choice("state", state, PUBLICATION_STATES, "models")
    values: dict[str, Any] = {"state": state}
    if url is not None:
        values["url"] = url
    update_row(conn, "publication", uid, values)
    return resolve(conn, "publication", uid)


def relate(
    conn: sqlite3.Connection,
    rel: str,
    from_kind: str,
    from_uid: str,
    to_kind: str,
    to_uid: str,
) -> tuple[sqlite3.Row, bool]:
    """Idempotently record `from --rel--> to` (full uids of existing rows). -> (row, created)."""
    _choice("rel", rel, RELATIONS, "library")
    _choice("from_kind", from_kind, ROW_KINDS, "library")
    _choice("to_kind", to_kind, ROW_KINDS, "library")
    for kind, uid in ((from_kind, from_uid), (to_kind, to_uid)):
        resolve(conn, kind, uid)
    ins = librarydb.insert_row(
        conn,
        "relation",
        {
            "from_kind": from_kind,
            "from_id": from_uid,
            "to_kind": to_kind,
            "to_id": to_uid,
            "rel": rel,
        },
        source_key=f"relation:{rel}:{from_kind}:{from_uid}:{to_kind}:{to_uid}",
    )
    return resolve(conn, "relation", ins.uid), ins.created


def ensure_source(
    conn: sqlite3.Connection,
    *,
    site: str,
    model_id: str,
    url: str,
    license: str | None = None,
    author: str | None = None,
    dir: str | None = None,
) -> tuple[sqlite3.Row, bool]:
    """The `model_source` of (site, model_id), created when new. S5's download writes the full row; a source
    first met through `publish --of URL` carries only what that URL and its model.json say."""
    row = conn.execute(
        "SELECT * FROM model_source WHERE site = ? AND model_id = ?", (site, model_id)
    ).fetchone()
    if row is not None:
        return row, False
    librarydb.insert_row(
        conn,
        "model_source",
        {
            "site": site,
            "model_id": model_id,
            "url": url,
            "license": license,
            "author": author,
            "dir": dir,
        },
        source_key=f"source:{site}:{model_id}",
    )
    row = conn.execute(
        "SELECT * FROM model_source WHERE site = ? AND model_id = ?", (site, model_id)
    ).fetchone()
    return row, True


# --------------------------------------------------------------------------- listings
def _label(conn: sqlite3.Connection, kind: str, uid: str) -> str:
    """A short human label of a relation endpoint; the full uid stays in the data."""
    if kind == "design":
        r = conn.execute("SELECT name FROM design WHERE uid = ?", (uid,)).fetchone()
        return f"design {r['name']}" if r else f"design {uid}"
    if kind == "make":
        r = conn.execute(
            "SELECT d.name, m.status, m.printer FROM make m JOIN design d ON d.id = m.design_id WHERE m.uid = ?",
            (uid,),
        ).fetchone()
        return (
            f"make of {r['name']} ({r['status']}{', ' + r['printer'] if r['printer'] else ''})"
            if r
            else f"make {uid}"
        )
    r = conn.execute(
        "SELECT site, url FROM model_source WHERE uid = ?", (uid,)
    ).fetchone()
    return f"source {r['url']}" if r else f"source {uid}"


def list_makes(
    conn: sqlite3.Connection, design: str | None = None
) -> list[dict[str, Any]]:
    where, args = (
        ("WHERE d.uid = ?", (resolve(conn, "design", design)["uid"],))
        if design
        else ("", ())
    )
    rows = conn.execute(
        f"SELECT m.*, d.name AS design_name, d.uid AS design_uid, s.uid AS source_uid, s.url AS source_url, "
        f"(SELECT COUNT(*) FROM photo p WHERE p.make_id = m.id) AS photos "
        f"FROM make m JOIN design d ON d.id = m.design_id LEFT JOIN model_source s ON s.id = m.source_id "
        f"{where} ORDER BY m.id",
        args,
    ).fetchall()
    return [
        {
            "uid": r["uid"],
            "design": r["design_name"],
            "design_uid": r["design_uid"],
            "status": r["status"],
            "printer": r["printer"],
            "filament": r["filament"],
            "gcode": r["gcode"],
            "queued_at": r["queued_at"],
            "finished_at": r["finished_at"],
            "source": r["source_uid"],
            "source_url": r["source_url"],
            "photos": r["photos"],
        }
        for r in rows
    ]


def list_publications(
    conn: sqlite3.Connection, design: str | None = None
) -> list[dict[str, Any]]:
    where, args = (
        ("WHERE d.uid = ?", (resolve(conn, "design", design)["uid"],))
        if design
        else ("", ())
    )
    rows = conn.execute(
        f"SELECT p.*, d.name AS design_name, d.uid AS design_uid FROM publication p "
        f"JOIN design d ON d.id = p.design_id {where} ORDER BY p.id",
        args,
    ).fetchall()
    return [
        {
            "uid": r["uid"],
            "design": r["design_name"],
            "design_uid": r["design_uid"],
            "platform": r["platform"],
            "state": r["state"],
            "url": r["url"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def list_relations(
    conn: sqlite3.Connection, design: str | None = None
) -> list[dict[str, Any]]:
    """Every relation, endpoints labelled; with `design`, only those that touch the design or its makes."""
    rows = conn.execute("SELECT * FROM relation ORDER BY id").fetchall()
    if design:
        wanted = _design_uids(conn, str(resolve(conn, "design", design)["uid"]))
        rows = [r for r in rows if r["from_id"] in wanted or r["to_id"] in wanted]
    return [
        {
            "uid": r["uid"],
            "rel": r["rel"],
            "from": {
                "kind": r["from_kind"],
                "uid": r["from_id"],
                "label": _label(conn, r["from_kind"], r["from_id"]),
            },
            "to": {
                "kind": r["to_kind"],
                "uid": r["to_id"],
                "label": _label(conn, r["to_kind"], r["to_id"]),
            },
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def _design_uids(conn: sqlite3.Connection, design_uid: str) -> set[str]:
    """The design's uid and the uids of its makes: the endpoints a relation of this design can name."""
    uids = {design_uid}
    uids.update(
        str(r["uid"])
        for r in conn.execute(
            "SELECT m.uid FROM make m JOIN design d ON d.id = m.design_id WHERE d.uid = ?",
            (design_uid,),
        )
    )
    return uids


def graph(conn: sqlite3.Connection, design: str) -> dict[str, Any]:
    """The provenance graph around one design: every row reachable through relations, in both directions.

    Walks `remix_of` / `make_of` / `derived_from` edges breadth-first from the design and its makes, so
    source -> remix -> make chains and the makes of a remix all show up. -> `{schema, design, nodes, edges,
    text}`; `text` is the indented tree (sources at the top)."""
    start = resolve(conn, "design", design)
    seen = _design_uids(conn, str(start["uid"]))
    edges: dict[str, dict[str, Any]] = {}
    frontier = set(seen)
    while frontier:
        marks = ",".join("?" for _ in frontier)
        found = conn.execute(
            f"SELECT * FROM relation WHERE from_id IN ({marks}) OR to_id IN ({marks}) ORDER BY id",
            (*frontier, *frontier),
        ).fetchall()
        frontier = set()
        for r in found:
            edges[str(r["uid"])] = {
                "rel": r["rel"],
                "from": {"kind": r["from_kind"], "uid": r["from_id"]},
                "to": {"kind": r["to_kind"], "uid": r["to_id"]},
            }
            for uid in (r["from_id"], r["to_id"]):
                if uid not in seen:
                    seen.add(uid)
                    frontier.add(uid)
        # a make reached through a relation brings its design along (and the design its other makes)
        extra = _siblings(conn, seen)
        frontier |= extra - seen
        seen |= extra
    nodes: dict[str, dict[str, str]] = {}
    for uid in sorted(seen):
        kind = _kind_of(conn, uid)
        if kind:
            nodes[uid] = {"kind": kind, "label": _label(conn, kind, uid)}
    ordered = list(edges.values())
    return {
        "schema": "3d.library.graph/1",
        "design": {"uid": str(start["uid"]), "name": str(start["name"])},
        "nodes": [{"uid": u, **n} for u, n in nodes.items()],
        "edges": [
            {
                **e,
                "from": {
                    **e["from"],
                    "label": nodes.get(e["from"]["uid"], {}).get("label", ""),
                },
                "to": {
                    **e["to"],
                    "label": nodes.get(e["to"]["uid"], {}).get("label", ""),
                },
            }
            for e in ordered
        ],
        "text": _tree_text(nodes, ordered, str(start["uid"])),
    }


def _siblings(conn: sqlite3.Connection, uids: Iterable[str]) -> set[str]:
    """For each make in `uids`: its design uid; for each design in `uids`: its makes."""
    out: set[str] = set()
    for uid in uids:
        r = conn.execute(
            "SELECT d.uid FROM make m JOIN design d ON d.id = m.design_id WHERE m.uid = ?",
            (uid,),
        ).fetchone()
        if r:
            out.add(str(r[0]))
        out.update(
            str(x[0])
            for x in conn.execute(
                "SELECT m.uid FROM make m JOIN design d ON d.id = m.design_id WHERE d.uid = ?",
                (uid,),
            )
        )
    return out


def _kind_of(conn: sqlite3.Connection, uid: str) -> str | None:
    for kind in ROW_KINDS:
        if conn.execute(f"SELECT 1 FROM {kind} WHERE uid = ?", (uid,)).fetchone():
            return kind
    return None


def _tree_text(
    nodes: dict[str, dict[str, str]], edges: list[dict[str, Any]], root: str
) -> str:
    """Render the graph as `from --rel--> to` lines under the design, sources first."""
    if not edges:
        return f"{nodes.get(root, {}).get('label', root)}: no relations yet"
    order = {"model_source": 0, "design": 1, "make": 2}
    lines = []
    for e in sorted(
        edges,
        key=lambda x: (order[x["to"]["kind"]], order[x["from"]["kind"]], x["rel"]),
    ):
        a = nodes.get(e["from"]["uid"], {}).get("label", e["from"]["uid"])
        b = nodes.get(e["to"]["uid"], {}).get("label", e["to"]["uid"])
        lines.append(f"{b}\n  <-- {e['rel']} -- {a}")
    return "\n".join(lines)
