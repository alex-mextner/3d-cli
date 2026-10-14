"""locations.py — the PrintOps location tree: grammar, CRUD, moves, fleet slots, Spoolman sync.

WHAT: every place a spool can be — warehouse cells ("WH:A/S2/B3/04"), dry boxes, dryers
  ("dryer:1") and printer slots ("slot:u1/T3", materialised from fleet/printers.yaml) — as rows
  of the `location` table (migration 0010), addressed by path (plan §5.2, §10.2, issue #159).

ACCESSED VIA: lib/printops/api/routes_locations.py (`3d printops loc …`), and other slices:
  spools/ledger (`get`, `resolve`, `place_spool`), drying (`get`, `humidity_sensor`), tags
  (`resolve`, `ntag` payloads in printops.labels).

PATH GRAMMAR: `<scheme>:<segment>(/<segment>)*`, ≤ 64 chars (Spoolman's `location` limit).
  scheme  [A-Za-z][A-Za-z0-9_-]{0,15}, not `cell` (the QR prefix) — e.g. WH, dryer, slot;
  segment [A-Za-z0-9][A-Za-z0-9._-]{0,31}, not `contents` (reserved by the REST routes).
  The parent of a path is the path without its last segment; a root is `<scheme>:<segment>`.
  Paths are unique case-insensitively and keep the case they were written with.

INVARIANTS:
  - A location's parent exists and is not archived when the location is created, moved or
    unarchived; `parent_id` always equals the row of `parent_path(path)`.
  - The `slot:` scheme belongs to the fleet: only `sync_fleet` creates, archives or revives it;
    manual add/move/archive under it is refused.
  - Spoolman holds, per spool, `location` = path and extra `printops_location_id` = id; both are
    written together by `place_spool`, and `resync` repairs any drift (e.g. after a move whose
    Spoolman writes failed half-way). A spool "is in" a location when its
    printops_location_id is that id, or — set by hand in the Spoolman UI — it has no id and
    its `location` string equals the path.
  - Archiving refuses a location that still holds spools or active children; archived rows stay
    (their ids are on printed NFC tags) and can be revived with `update(..., archived=False)`.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from printops import db, events
from printops.spoolman import JSON, Spoolman, SpoolmanError, decode_extra, encode_extra

KINDS = ("warehouse", "zone", "shelf", "box", "slot", "printer", "ams", "printer_slot", "dryer", "dry_box")
MAX_PATH = 64  # Spoolman spool.location is VARCHAR(64)
FLEET_SCHEME = "slot"
QR_PREFIX = "cell:"
# Schemes whose path already IS the scanned ID in decision D8 (`dryer:1`, `slot:u1/e2`); every
# other location's QR/NFC text is `cell:<path>`.
SELF_ID_SCHEMES = ("dryer", "slot")
RESERVED_SEGMENTS = ("contents",)
TAG_PREFIX = "loc-"  # HA tag id on the location's NTAG213: loc-<id> (stable across moves)
LOCATION_EXTRA = "printops_location_id"

_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,15}")
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
_TAG_UID = re.compile(r"[0-9A-F]{8,20}")
_HA_ENTITY = re.compile(r"[a-z_]+\.[a-z0-9_]+")


class LocationError(ValueError):
    """Bad path/kind/field, or an operation the tree refuses (archive non-empty, move into self…).
    `status` is the HTTP status the API answers with (400 bad input, 409 conflict)."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class LocationNotFound(LookupError):
    """No location with that path / id / scanned payload."""

    def __init__(self, ref: object) -> None:
        super().__init__(f"no location {ref!r}")
        self.ref = ref


@dataclass(frozen=True)
class Location:
    id: int
    path: str
    kind: str
    parent_id: int | None
    tag_uid: str | None
    qr: str | None
    capacity: int | None
    humidity_sensor_entity: str | None
    note: str | None
    source: str
    archived: bool
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Location:
        return cls(
            id=int(row["id"]), path=str(row["path"]), kind=str(row["kind"]),
            parent_id=None if row["parent_id"] is None else int(row["parent_id"]),
            tag_uid=row["tag_uid"], qr=row["qr"],
            capacity=None if row["capacity"] is None else int(row["capacity"]),
            humidity_sensor_entity=row["humidity_sensor_entity"], note=row["note"],
            source=str(row["source"]), archived=bool(row["archived"]),
            created_at=str(row["created_at"]), updated_at=str(row["updated_at"]),
        )

    @property
    def scheme(self) -> str:
        return self.path.split(":", 1)[0]

    @property
    def name(self) -> str:
        """Last segment ("04" for WH:A/S2/B3/04, "A" for WH:A)."""
        return self.path.split(":", 1)[1].rsplit("/", 1)[-1]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["qr_payload"] = qr_payload(self.path)
        d["label_stale"] = self.qr is not None and self.qr != d["qr_payload"]
        return d


# -- grammar ------------------------------------------------------------------------------
def check_path(path: str) -> str:
    """The path, stripped, if it follows the grammar; LocationError naming the broken rule."""
    p = path.strip()
    if len(p) > MAX_PATH:
        raise LocationError(f"location path {p!r} is {len(p)} chars; Spoolman's location holds at most {MAX_PATH}")
    scheme, sep, rest = p.partition(":")
    if not sep or not rest:
        raise LocationError(f"location path {p!r} must look like SCHEME:NAME[/NAME…], e.g. WH:A/S2/B3/04")
    if not _SCHEME.fullmatch(scheme):
        raise LocationError(f"location scheme {scheme!r} in {p!r}: letters, digits, _ or -, starting with a "
                            "letter, at most 16 chars")
    if scheme.lower() == QR_PREFIX.rstrip(":"):
        raise LocationError(f"scheme 'cell' is reserved for label payloads (cell:<path>); got {p!r}")
    for seg in rest.split("/"):
        if not _SEGMENT.fullmatch(seg):
            raise LocationError(f"location segment {seg!r} in {p!r}: letters, digits, '.', '_' or '-', starting "
                                "with a letter or digit, at most 32 chars")
        if seg.lower() in RESERVED_SEGMENTS:
            raise LocationError(f"location segment {seg!r} is reserved (REST route name)")
    return p


def parent_path(path: str) -> str | None:
    scheme, _, rest = path.partition(":")
    head, sep, _ = rest.rpartition("/")
    return f"{scheme}:{head}" if sep else None


def qr_payload(path: str) -> str:
    """The text on the location's QR label and NTAG text record (decision D8)."""
    scheme = path.split(":", 1)[0].lower()
    return path if scheme in SELF_ID_SCHEMES else QR_PREFIX + path


def spoolman_path(loc: Location) -> str:
    """What Spoolman's native `location` holds for a spool stored in `loc`."""
    return loc.path


def _check_kind(kind: object) -> str:
    if not isinstance(kind, str) or kind not in KINDS:
        raise LocationError(f"unknown location kind {kind!r}; accepted: {', '.join(KINDS)}")
    return kind


def _check_tag_uid(uid: object) -> str | None:
    if uid is None or uid == "":
        return None
    if not isinstance(uid, str):
        raise LocationError(f"tag uid must be a hex string, got {uid!r}")
    norm = uid.replace(":", "").replace(" ", "").upper()
    if not _TAG_UID.fullmatch(norm):
        raise LocationError(f"tag uid {uid!r} must be 4–10 bytes of hex (e.g. 04:A2:2B:1A:7F:61:80)")
    return norm


def _check_entity(entity: object) -> str | None:
    if entity is None or entity == "":
        return None
    if not isinstance(entity, str):
        raise LocationError(f"humidity sensor must be an entity id string, got {entity!r}")
    if not _HA_ENTITY.fullmatch(entity):
        raise LocationError(f"humidity sensor {entity!r} must be a Home Assistant entity id like sensor.drybox_1_humidity")
    return entity


def _check_capacity(capacity: object) -> int | None:
    if capacity is None:
        return None
    if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
        raise LocationError(f"capacity must be a positive number of spools, got {capacity!r}")
    return capacity


# -- reads --------------------------------------------------------------------------------
def find(conn: sqlite3.Connection, ref: str | int) -> Location | None:
    """The location with this id (int or digit string) or path (case-insensitive), else None."""
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isascii() and ref.isdigit() and len(ref) < 19):
        row = conn.execute("SELECT * FROM location WHERE id = ?", (int(ref),)).fetchone()
    else:
        row = conn.execute("SELECT * FROM location WHERE path = ?", (str(ref).strip(),)).fetchone()
    return None if row is None else Location.from_row(row)


def get(conn: sqlite3.Connection, path_or_id: str | int) -> Location:
    """Like `find`, but LocationNotFound when absent. Archived locations are returned too."""
    loc = find(conn, path_or_id)
    if loc is None:
        raise LocationNotFound(path_or_id)
    return loc


def resolve(conn: sqlite3.Connection, scanned: str) -> Location:
    """A location from anything a person or reader may hand us: its path or id, a QR/NFC text
    (`cell:<path>`, `dryer:1`, `slot:u1/T3`), an HA tag id `loc-<id>`, or an NTAG UID."""
    s = scanned.strip()
    if s.lower().startswith(QR_PREFIX):
        s = s[len(QR_PREFIX):]
    elif s.lower().startswith(TAG_PREFIX) and s[len(TAG_PREFIX):].isdigit():
        s = s[len(TAG_PREFIX):]
    loc = find(conn, s) if s else None
    if loc is None and s:
        try:
            uid = _check_tag_uid(s)
        except LocationError:
            uid = None
        if uid is not None:
            row = conn.execute("SELECT * FROM location WHERE tag_uid = ?", (uid,)).fetchone()
            loc = None if row is None else Location.from_row(row)
        if loc is None:  # a label printed before a move still carries the old path
            row = conn.execute("SELECT * FROM location WHERE qr = ? ORDER BY archived, id", (scanned.strip(),)).fetchone()
            loc = None if row is None else Location.from_row(row)
    if loc is None:
        raise LocationNotFound(scanned)
    return loc


def all_locations(conn: sqlite3.Connection, *, include_archived: bool = False) -> list[Location]:
    sql = "SELECT * FROM location" + ("" if include_archived else " WHERE archived = 0") + " ORDER BY path"
    return [Location.from_row(r) for r in conn.execute(sql)]


def subtree(conn: sqlite3.Connection, loc: Location, *, include_archived: bool = True) -> list[Location]:
    """`loc` and every location below it, by path."""
    rows = conn.execute(
        "SELECT * FROM location WHERE (path = ? OR substr(path, 1, ?) = ? COLLATE NOCASE)"
        + ("" if include_archived else " AND archived = 0") + " ORDER BY path",
        (loc.path, len(loc.path) + 1, loc.path + "/"),
    )
    return [Location.from_row(r) for r in rows]


def children(conn: sqlite3.Connection, loc: Location, *, include_archived: bool = False) -> list[Location]:
    sql = "SELECT * FROM location WHERE parent_id = ?" + ("" if include_archived else " AND archived = 0")
    return [Location.from_row(r) for r in conn.execute(sql + " ORDER BY path", (loc.id,))]


def ancestors(conn: sqlite3.Connection, loc: Location) -> list[Location]:
    """Parent first, root last."""
    out: list[Location] = []
    cur = loc
    while cur.parent_id is not None:
        cur = get(conn, cur.parent_id)
        out.append(cur)
    return out


def humidity_sensor(conn: sqlite3.Connection, loc: Location) -> str | None:
    """The HA RH sensor of `loc` or of its nearest ancestor that has one."""
    for node in (loc, *ancestors(conn, loc)):
        if node.humidity_sensor_entity:
            return node.humidity_sensor_entity
    return None


def tree(locs: Sequence[Location]) -> list[dict[str, Any]]:
    """Nested [{...location, children: [...]}] from a flat list (orphans become roots)."""
    nodes = {loc.id: {**loc.to_dict(), "children": []} for loc in locs}
    roots: list[dict[str, Any]] = []
    for loc in locs:
        parent = nodes.get(loc.parent_id) if loc.parent_id is not None else None
        (parent["children"] if parent is not None else roots).append(nodes[loc.id])
    return roots


# -- writes -------------------------------------------------------------------------------
def _active_parent(conn: sqlite3.Connection, path: str) -> Location | None:
    pp = parent_path(path)
    if pp is None:
        return None
    parent = find(conn, pp)
    if parent is None:
        raise LocationError(f"parent {pp!r} of {path!r} does not exist; add it first "
                            f"(3d printops loc add {pp} --kind …)", status=409)
    if parent.archived:
        raise LocationError(f"parent {pp!r} of {path!r} is archived; unarchive it first", status=409)
    return parent


def _refuse_fleet(path: str, action: str) -> None:
    if path.split(":", 1)[0].lower() == FLEET_SCHEME:
        raise LocationError(f"cannot {action} {path!r}: `slot:` locations are the printer slots of "
                            "fleet/printers.yaml and follow it automatically (edit the fleet instead)", status=409)


def _insert(conn: sqlite3.Connection, path: str, kind: str, parent: Location | None, *, capacity: int | None,
            humidity_sensor_entity: str | None, tag_uid: str | None, note: str | None, source: str) -> Location:
    now = db.utc_now()
    try:
        cur = conn.execute(
            "INSERT INTO location (path, kind, parent_id, tag_uid, capacity, humidity_sensor_entity, note, source,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (path, kind, parent.id if parent else None, tag_uid, capacity, humidity_sensor_entity, note, source,
             now, now),
        )
    except sqlite3.IntegrityError as e:
        raise _conflict(conn, path, tag_uid, e) from e
    return get(conn, int(cur.lastrowid or 0))


def _conflict(conn: sqlite3.Connection, path: str, tag_uid: str | None, e: sqlite3.IntegrityError) -> LocationError:
    existing = find(conn, path)
    if existing is not None:
        hint = " (archived: revive it with `3d printops loc unarchive`)" if existing.archived else ""
        return LocationError(f"location {existing.path!r} already exists (id {existing.id}){hint}", status=409)
    if tag_uid is not None:
        row = conn.execute("SELECT path FROM location WHERE tag_uid = ?", (tag_uid,)).fetchone()
        if row is not None:
            return LocationError(f"tag {tag_uid} is already on location {row['path']!r}", status=409)
    return LocationError(f"location {path!r} conflicts: {e}", status=409)


def add(conn: sqlite3.Connection, path: str, kind: str, *, actor: str, capacity: int | None = None,
        humidity_sensor_entity: str | None = None, tag_uid: str | None = None, note: str | None = None) -> Location:
    """Create one location under an existing parent; emits `location.created`."""
    p = check_path(path)
    _refuse_fleet(p, "add")
    kind = _check_kind(kind)
    if kind in ("printer", "ams", "printer_slot"):
        raise LocationError(f"kind {kind!r} is only for fleet printer slots (slot:…)")
    with db.transaction(conn):
        loc = _insert(conn, p, kind, _active_parent(conn, p), capacity=_check_capacity(capacity),
                      humidity_sensor_entity=_check_entity(humidity_sensor_entity), tag_uid=_check_tag_uid(tag_uid),
                      note=note or None, source="manual")
        events.emit(conn, "location.created", f"location:{loc.id}", {"path": loc.path, "kind": loc.kind}, actor)
    return loc


UPDATABLE = ("kind", "capacity", "humidity_sensor_entity", "tag_uid", "qr", "note", "archived")


def update(conn: sqlite3.Connection, loc: Location, changes: Mapping[str, Any], *, actor: str) -> Location:
    """Change attributes (not the path: see `move`; not archiving: see `archive`). `archived=False`
    revives an archived location whose parent is active. Emits `location.updated`."""
    unknown = sorted(set(changes) - set(UPDATABLE))
    if unknown:
        raise LocationError(f"cannot update {', '.join(unknown)}; updatable: {', '.join(UPDATABLE)} (path: use move)")
    if changes.get("archived") is True:
        raise LocationError("archive with DELETE /v1/locations/{path} (`3d printops loc archive`), which checks it is empty")
    clean: dict[str, Any] = {}
    for key, value in changes.items():
        if key == "kind":
            _refuse_fleet(loc.path, "change the kind of")
            clean[key] = _check_kind(str(value))
            if clean[key] in ("printer", "ams", "printer_slot"):
                raise LocationError(f"kind {clean[key]!r} is only for fleet printer slots (slot:…)")
        elif key == "capacity":
            clean[key] = _check_capacity(value)
        elif key == "humidity_sensor_entity":
            clean[key] = _check_entity(value)
        elif key == "tag_uid":
            clean[key] = _check_tag_uid(value)
        elif key in ("qr", "note"):
            if value is not None and not isinstance(value, str):
                raise LocationError(f"{key} must be a string, got {value!r}")
            clean[key] = value or None
        elif key == "archived":
            _refuse_fleet(loc.path, "unarchive")
            clean[key] = 0
    if not clean:
        return loc
    with db.transaction(conn):
        if "archived" in clean and loc.archived:
            _active_parent(conn, loc.path)
        sets = ", ".join(f"{k} = ?" for k in clean)
        try:
            conn.execute(f"UPDATE location SET {sets}, updated_at = ? WHERE id = ?",  # noqa: S608 — keys whitelisted
                         (*clean.values(), db.utc_now(), loc.id))
        except sqlite3.IntegrityError as e:
            raise _conflict(conn, "", clean.get("tag_uid"), e) from e
        new = get(conn, loc.id)
        payload = {k: (bool(v) if k == "archived" else v) for k, v in clean.items()}
        events.emit(conn, "location.updated", f"location:{loc.id}", {"path": new.path, **payload}, actor)
    return new


# -- Spoolman side ------------------------------------------------------------------------
def _spool_location_id(spool: Mapping[str, Any]) -> int | None:
    raw = (spool.get("extra") or {}).get(LOCATION_EXTRA)
    try:
        value = decode_extra(raw)
    except ValueError:
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def spools_in(spools: Iterable[Mapping[str, Any]], locs: Sequence[Location]) -> list[JSON]:
    """The spools stored in any of `locs` (see the module invariant for "stored in")."""
    ids = {loc.id for loc in locs}
    paths = {loc.path.lower() for loc in locs}
    out: list[JSON] = []
    for s in spools:
        sid = _spool_location_id(s)
        if sid in ids or (sid is None and str(s.get("location") or "").lower() in paths):
            out.append(dict(s))
    return out


def contents(conn: sqlite3.Connection, spoolman: Spoolman, loc: Location, *, recursive: bool = False) -> list[JSON]:
    """Active (non-archived) Spoolman spools in `loc` (and, recursive, below it)."""
    locs = subtree(conn, loc) if recursive else [loc]
    return spools_in(spoolman.find("spool"), locs)


def place_spool(spoolman: Spoolman, spool_id: int, loc: Location | None, *,
                extra: Mapping[str, Any] | None = None) -> JSON:
    """Put a spool in `loc` (None = nowhere) in ONE Spoolman PATCH: `location` = path and extra
    `printops_location_id` = id, plus the caller's other extra fields (python values)."""
    if loc is not None and loc.archived:
        raise LocationError(f"location {loc.path!r} is archived", status=409)
    fields = {k: encode_extra(v) if v is not None else None for k, v in (extra or {}).items()}
    fields[LOCATION_EXTRA] = None if loc is None else encode_extra(loc.id)
    body: dict[str, Any] = {"location": "" if loc is None else spoolman_path(loc), "extra": fields}
    return spoolman.update("spool", spool_id, body)


@dataclass
class SyncReport:
    """Spoolman writes of a move/resync: spools updated, and ones whose PATCH failed."""
    updated: list[int] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"updated": self.updated, "failed": self.failed}


def resync(conn: sqlite3.Connection, spoolman: Spoolman, spools: Iterable[Mapping[str, Any]] | None = None) -> SyncReport:
    """Rewrite Spoolman `location` of every spool whose printops_location_id names an existing
    location but whose string differs from that location's path."""
    report = SyncReport()
    paths = {loc.id: loc.path for loc in all_locations(conn, include_archived=True)}
    for s in spoolman.find("spool") if spools is None else spools:
        lid = _spool_location_id(s)
        if lid is None or lid not in paths or s.get("location") == paths[lid]:
            continue
        try:
            spoolman.update("spool", int(s["id"]), {"location": paths[lid]})
            report.updated.append(int(s["id"]))
        except SpoolmanError as e:
            report.failed.append({"spool_id": int(s["id"]), "error": e.message})
    return report


# -- move / archive -----------------------------------------------------------------------
@dataclass
class MoveResult:
    location: Location
    renamed: list[tuple[str, str]]
    spoolman: SyncReport

    def to_dict(self) -> dict[str, Any]:
        return {"location": self.location.to_dict(), "renamed": [list(r) for r in self.renamed],
                "spoolman": self.spoolman.to_dict()}


def move(conn: sqlite3.Connection, spoolman: Spoolman | None, loc: Location, new_path: str, *,
         actor: str) -> MoveResult:
    """Rename/re-parent `loc` and its whole subtree to `new_path`, then re-sync Spoolman's
    `location` of every spool inside. The DB move is one transaction; Spoolman writes follow it
    (a failed one is reported, and `resync` repairs it). Emits `location.moved`."""
    new = check_path(new_path)
    _refuse_fleet(loc.path, "move")
    _refuse_fleet(new, "move into")
    if loc.archived:
        raise LocationError(f"location {loc.path!r} is archived; unarchive it before moving", status=409)
    old = loc.path
    if new == old:
        return MoveResult(loc, [], SyncReport())
    if new.lower().startswith(old.lower() + "/"):
        raise LocationError(f"cannot move {old!r} into its own subtree ({new!r})", status=409)
    with db.transaction(conn):
        nodes = subtree(conn, loc)
        renamed = [(n.path, new + n.path[len(old):]) for n in nodes]
        too_long = [p for _, p in renamed if len(p) > MAX_PATH]
        if too_long:
            raise LocationError(f"moving {old!r} to {new!r} makes {len(too_long)} path(s) longer than {MAX_PATH} chars, "
                                f"e.g. {too_long[0]!r}")
        parent = _active_parent(conn, new)
        clash = find(conn, new)
        if clash is not None and clash.id != loc.id:
            raise LocationError(f"location {clash.path!r} already exists (id {clash.id})", status=409)
        now = db.utc_now()
        # One pass is enough: the new root is free (checked above) and every new path lies under
        # it, so no rewritten path can collide with a row outside the subtree, and a case-only
        # rename only "collides" with the row itself.
        for n, (_, new_p) in zip(nodes, renamed, strict=True):
            conn.execute("UPDATE location SET path = ?, updated_at = ? WHERE id = ?", (new_p, now, n.id))
        conn.execute("UPDATE location SET parent_id = ? WHERE id = ?", (parent.id if parent else None, loc.id))
        moved = get(conn, loc.id)
        events.emit(conn, "location.moved", f"location:{loc.id}",
                    {"from": old, "to": moved.path, "subtree": len(nodes)}, actor)
    report = SyncReport()
    if spoolman is not None:
        moved_nodes = [get(conn, n.id) for n in nodes]
        try:
            inside = spools_in(spoolman.find("spool"), [*moved_nodes, *nodes])
        except SpoolmanError as e:  # the move is committed; `resync` repairs once Spoolman is back
            report.failed.append({"spool_id": None, "error": e.message})
            inside = []
        ids = {n.id for n in nodes}
        by_id = {n.id: n for n in moved_nodes}
        for s in inside:
            lid = _spool_location_id(s)
            target = by_id[lid] if lid in ids else _by_old_path(nodes, moved_nodes, str(s.get("location") or ""))
            if target is None:
                continue
            try:
                place_spool(spoolman, int(s["id"]), target)
                report.updated.append(int(s["id"]))
            except SpoolmanError as e:
                report.failed.append({"spool_id": int(s["id"]), "error": e.message})
    return MoveResult(get(conn, loc.id), renamed, report)


def _by_old_path(old_nodes: Sequence[Location], new_nodes: Sequence[Location], path: str) -> Location | None:
    for o, n in zip(old_nodes, new_nodes, strict=True):
        if o.path.lower() == path.lower():
            return n
    return None


def archive(conn: sqlite3.Connection, spoolman: Spoolman | None, loc: Location, *, actor: str) -> Location:
    """Archive an empty leaf: refuses while active children or spools remain. Emits
    `location.archived`. Needs Spoolman to prove emptiness (None → refused)."""
    _refuse_fleet(loc.path, "archive")
    return _archive(conn, spoolman, loc, actor=actor)


def _archive(conn: sqlite3.Connection, spoolman: Spoolman | None, loc: Location, *, actor: str) -> Location:
    if loc.archived:
        return loc
    kids = children(conn, loc)
    if kids:
        raise LocationError(f"location {loc.path!r} has {len(kids)} active child location(s) "
                            f"({', '.join(k.path for k in kids[:3])}{'…' if len(kids) > 3 else ''}); archive them first",
                            status=409)
    if spoolman is None:
        raise LocationError("Spoolman is not configured: cannot check the location is empty", status=503)
    inside = contents(conn, spoolman, loc)
    if inside:
        raise LocationError(f"location {loc.path!r} still holds {len(inside)} spool(s) "
                            f"(#{', #'.join(str(s['id']) for s in inside[:5])}); move them out first", status=409)
    with db.transaction(conn):
        conn.execute("UPDATE location SET archived = 1, updated_at = ? WHERE id = ?", (db.utc_now(), loc.id))
        events.emit(conn, "location.archived", f"location:{loc.id}", {"path": loc.path}, actor)
    return get(conn, loc.id)


# -- fleet printer slots ------------------------------------------------------------------
@dataclass(frozen=True)
class FleetSlot:
    """One location the fleet implies: `slot:<printer>[/<unit>]/<label>`."""
    path: str
    kind: str  # printer | ams | printer_slot
    note: str | None = None


def fleet_slots(fleet: Any) -> list[FleetSlot]:
    """Locations for every printer of a printops.fleet.Fleet (offline ones too: their spools are
    tracked by hand, decision D7): the printer, each head (unusable ones carry the reason), each
    AMS-like unit and its numbered slots. Parents come before children."""
    out: list[FleetSlot] = []
    for name, printer in sorted(fleet.printers.items()):
        base = f"{FLEET_SCHEME}:{name}"
        out.append(FleetSlot(base, "printer", f"{printer.model} ({printer.owner}, {printer.access})"))
        for head in printer.heads:
            note = None if head.usable else f"unusable: {head.reason}"
            out.append(FleetSlot(f"{base}/{head.label}", "printer_slot", note))
        for unit in printer.ams or ():
            out.append(FleetSlot(f"{base}/{unit.id}", "ams", f"{unit.kind}, {unit.slots} slots"))
            out.extend(FleetSlot(f"{base}/{unit.id}/{i}", "printer_slot") for i in range(1, unit.slots + 1))
    return out


@dataclass
class FleetSyncReport:
    created: list[str] = field(default_factory=list)
    revived: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    kept: list[dict[str, str]] = field(default_factory=list)  # gone from the fleet but not archivable

    @property
    def changed(self) -> bool:
        return bool(self.created or self.revived or self.updated or self.archived)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sync_fleet(conn: sqlite3.Connection, slots: Sequence[FleetSlot], spoolman: Spoolman | None, *,
               actor: str = "system") -> FleetSyncReport:
    """Make the `slot:` subtree match `slots`: create missing ones, revive archived ones that are
    back, refresh kind/note, archive (deepest first) fleet slots no longer listed — when empty;
    without Spoolman, or holding spools, they are kept and reported. Idempotent."""
    for s in slots:
        if check_path(s.path).split(":", 1)[0] != FLEET_SCHEME:
            raise LocationError(f"fleet slot {s.path!r} must use the slot: scheme")
    report = FleetSyncReport()
    wanted = {s.path.lower() for s in slots}
    for s in slots:
        with db.transaction(conn):
            have = find(conn, s.path)
            if have is None:
                parent = _active_parent(conn, s.path)
                loc = _insert(conn, s.path, s.kind, parent, capacity=1 if s.kind == "printer_slot" else None,
                              humidity_sensor_entity=None, tag_uid=None, note=s.note, source="fleet")
                events.emit(conn, "location.created", f"location:{loc.id}",
                            {"path": loc.path, "kind": loc.kind, "source": "fleet"}, actor)
                report.created.append(s.path)
                continue
            sets: dict[str, Any] = {}
            if have.archived:
                sets["archived"] = 0
                report.revived.append(s.path)
            if have.kind != s.kind or have.note != s.note or have.source != "fleet":
                sets.update(kind=s.kind, note=s.note, source="fleet")
                if not have.archived:
                    report.updated.append(s.path)
            if sets:
                cols = ", ".join(f"{k} = ?" for k in sets)
                conn.execute(f"UPDATE location SET {cols}, updated_at = ? WHERE id = ?",  # noqa: S608 — fixed keys
                             (*sets.values(), db.utc_now(), have.id))
                payload = {"path": have.path, **{k: bool(v) if k == "archived" else v for k, v in sets.items()}}
                events.emit(conn, "location.updated", f"location:{have.id}", payload, actor)
    gone = [loc for loc in all_locations(conn) if loc.scheme.lower() == FLEET_SCHEME and loc.path.lower() not in wanted]
    for loc in sorted(gone, key=lambda x: -x.path.count("/")):
        refusal = _archive_or_reason(conn, spoolman, loc, actor=actor)
        if refusal is None:
            report.archived.append(loc.path)
        else:
            report.kept.append({"path": loc.path, "reason": refusal})
    return report


def _archive_or_reason(conn: sqlite3.Connection, spoolman: Spoolman | None, loc: Location, *, actor: str) -> str | None:
    try:
        _archive(conn, spoolman, get(conn, loc.id), actor=actor)
    except LocationError as e:
        return e.message
    return None


# database file -> signature (path, mtime, size) of the fleet file last synced into it, per process
_fleet_synced: dict[str, tuple[str, int, int]] = {}


def ensure_fleet_slots(conn: sqlite3.Connection, spoolman: Spoolman | None, *, actor: str = "fleet",
                       force: bool = False) -> FleetSyncReport | None:
    """Sync the printer slots from fleet/printers.yaml (printops.fleet) into this DB when the file
    changed since this process last did so — or always with `force`. None: no fleet file (the
    layer is disabled or absent); an empty report: already in sync. A broken fleet file raises
    printops.fleet.FleetError. Callers needing a slot (`slot:u1/T3`) call this before `get`."""
    from printops.fleet import fleet_path, load_fleet

    path = fleet_path()
    if path is None or not path.is_file():
        return None
    st = path.stat()
    sig = (str(path), st.st_mtime_ns, st.st_size)
    key = str(conn.execute("PRAGMA database_list").fetchone()["file"])
    if not force and _fleet_synced.get(key) == sig:
        return FleetSyncReport()
    fleet = load_fleet(path)
    if fleet is None:
        return None
    report = sync_fleet(conn, fleet_slots(fleet), spoolman, actor=actor)
    _fleet_synced[key] = sig
    return report
