"""library/search.py — the search core behind `3d models search` and `library.actions.search`.

WHAT: one function, `run_search()`, takes a query to every requested site (`marketplaces.catalog.search`),
turns each hit into a normalised RESULT RECORD (the Model card + `files[]` with extensions/classes + `cad`,
`cad_formats[]`, `analytics{}` from `library.analytics`, `size_mm` when a mesh was measured), optionally
measures meshes (`--deep`: download the smallest mesh of a hit, run trimesh, cache by file key and sha256 in the
library DB), applies the `--cad` / `--for-vitamin` filters and persists the whole search into the library DB
(`search_session` + one `search_result` per hit). It returns the `3d.models.search/2` document the CLI prints.
`get_session` / `list_sessions` read persisted searches back, `session_doc` rebuilds the same document.

ACCESSED VIA: `commands/models.py` (`search`, `searches list|show`), `library.actions.search`.

INVARIANTS:
  - Network failure of one site never hides the others: it lands in `errors[]` and the rest returns.
  - Persisting is best effort: a library DB that cannot be written adds a warning and leaves
    `search_id` null; the search results are still returned.
  - Analytics are deterministic facts; `cad` is false when the file list is unknown (`files_known: false`).
  - Network tests run through the loopback fakes at the HTTP boundary only; this module has no network of its own
    beyond `marketplaces.transport.download` for `--deep`.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
import tempfile
import threading
import time
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cli.resources import pool_size
from errors import InvalidArgument, ThreeDError, UsageError
from library import analytics
from marketplaces import catalog, transport
from marketplaces.base import SITES, Deadline, MarketplaceError, Model, safe_name

SCHEMA = "3d.models.search/2"
DEEP_MAX_BYTES = 25 * 1024 * 1024
DEEP_BUDGET_S = 90.0
DEEP_WORKERS = 4
DETAIL_MAX_AGE_S = 6 * 3600.0  # how long a stored Thingiverse follow-up answer is trusted
VITAMIN_DEEP_TOP = (
    10  # `--for-vitamin` measures this many candidates per site unless told otherwise
)
SEARCH_KINDS = ("wide", "narrow", "followup", "rewrite", "user")

# `--for-vitamin KIND`: the search terms a vitamin needs (added unless the query has one already). Every preset
# also keeps only results that pass analytics.reference_suitability.
VITAMIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "pcb": ("pcb", "board"),
    "connector": ("connector", "socket", "jack", "header"),
    "screw": ("screw", "bolt", "nut"),
    "wire": ("wire", "cable"),
}


@dataclass(frozen=True)
class SearchOptions:
    sites: Sequence[str] = SITES
    limit: int = 10
    details: bool = (
        True  # per-hit follow-up requests (Thingiverse licenses and file lists)
    )
    deep: bool = False
    deep_top: int | None = (
        None  # measure only the first N candidates per site (None = all)
    )
    deep_max_bytes: int = DEEP_MAX_BYTES
    only_cad: bool = False
    vitamin: str | None = None
    save: bool = True
    kind: str = "user"
    parent_session: str | None = None
    idempotency_key: str | None = None
    db_path: str | None = None


def effective_query(query: str, vitamin: str | None) -> str:
    """`query` plus the preset's first keyword when no keyword of the preset is in it already."""
    if vitamin is None:
        return query
    keywords = VITAMIN_KEYWORDS[vitamin]
    words = {w.lower() for w in query.replace("-", " ").split()}
    return query if words & set(keywords) else f"{query} {keywords[0]}"


def check_vitamin(vitamin: str | None) -> str | None:
    if vitamin is not None and vitamin not in VITAMIN_KEYWORDS:
        raise InvalidArgument(
            "--for-vitamin", vitamin, list(VITAMIN_KEYWORDS), command="models"
        )
    return vitamin


# --------------------------------------------------------------------------- records
def file_record(f: Any) -> dict[str, Any]:
    """A `files[]` entry: the site's fields (name, size, ref, kind) plus `ext` and `class`."""
    return {
        "name": f.name,
        "size": f.size,
        "ref": f.ref,
        "kind": f.kind,
        "ext": analytics.extension(f.name),
        "class": analytics.file_class(f.name),
    }


def result_record(
    model: Model, rank: int, *, deep: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The result card: every key `Model.to_json()` had (unchanged) plus `rank`, `files[].ext/class`, `cad`,
    `cad_formats`, `analytics{}` (with `reference{}` and, when measured, `deep{}`), `reference_suitable` and — only
    when a mesh was measured — `size_mm`. `size_mm` is absent, never guessed, for an unmeasured result."""
    record = model.to_json()
    record["files"] = [file_record(f) for f in model.files]
    facts = analytics.file_facts(
        ((f.name, f.size) for f in model.files), files_known=model.files_known
    )
    mesh = deep if deep and deep.get("status") == "ok" else None
    facts["reference"] = analytics.reference_suitability(
        facts, paid=model.paid, license_name=model.license, mesh=mesh
    )
    if deep is not None:
        facts["deep"] = deep
    record["rank"] = rank
    record["cad"] = bool(facts.get("cad", False))
    record["cad_formats"] = list(facts.get("cad_formats", []))
    record["reference_suitable"] = bool(facts["reference"]["suitable"])
    record["analytics"] = facts
    if mesh is not None:
        record["size_mm"] = mesh["bbox_mm"]
    return record


# --------------------------------------------------------------------------- deep analytics cache
def cache_get(conn: sqlite3.Connection | None, key: str) -> dict[str, Any] | None:
    if conn is None:
        return None
    row = conn.execute(
        "SELECT analytics FROM analytics_cache WHERE key = ?", (key,)
    ).fetchone()
    return json.loads(row["analytics"]) if row else None


def cache_by_sha(conn: sqlite3.Connection | None, sha: str) -> dict[str, Any] | None:
    if conn is None:
        return None
    row = conn.execute(
        "SELECT analytics FROM analytics_cache WHERE sha256 = ? LIMIT 1", (sha,)
    ).fetchone()
    return json.loads(row["analytics"]) if row else None


def cache_put(
    conn: sqlite3.Connection | None, key: str, sha: str | None, doc: dict[str, Any]
) -> None:
    if conn is None:
        return
    from printops import db

    with db.transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO analytics_cache (key, sha256, analytics, created_at) VALUES (?, ?, ?, ?)",
            (
                key,
                sha,
                json.dumps(doc, sort_keys=True, separators=(",", ":")),
                db.utc_now(),
            ),
        )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


class LibraryDetailStore:
    """`marketplaces.base.DetailStore` in the library DB (`analytics_cache`, keys `thingiverse:<id>`): the follow-up
    answers of Thingiverse hits survive the process, so the next `3d models search` of the same hits makes no
    follow-up request (the API allows 300 requests per 5 minutes). Entries older than `max_age_s` are ignored.
    Thread-safe (one connection per worker thread, all closed by `close()`) and best effort: any SQLite or OS
    error reads as a miss and a skipped write."""

    def __init__(self, db_path: str | None, *, max_age_s: float | None = None) -> None:
        self._db_path = db_path
        self._max_age_s = DETAIL_MAX_AGE_S if max_age_s is None else max_age_s
        self._local = threading.local()
        self._lock = threading.Lock()
        self._connections: list[sqlite3.Connection] = []

    def _conn(self) -> sqlite3.Connection | None:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            from library import db as librarydb

            try:
                conn = librarydb.open_library(self._db_path)
            except (sqlite3.Error, OSError):
                return None
            self._local.conn = conn
            with self._lock:
                self._connections.append(conn)
        return conn

    def get(self, key: str) -> dict[str, Any] | None:
        conn = self._conn()
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT analytics, created_at FROM analytics_cache WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            stored = datetime.datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            if (datetime.datetime.now(datetime.timezone.utc) - stored).total_seconds() > self._max_age_s:
                return None
            value = json.loads(row["analytics"])
        except (sqlite3.Error, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def put(self, key: str, value: dict[str, Any]) -> None:
        conn = self._conn()
        if conn is None:
            return
        try:
            cache_put(conn, key, None, value)
        except sqlite3.Error:
            return

    def close(self) -> None:
        with self._lock:
            for conn in self._connections:
                conn.close()
            self._connections.clear()


def _skipped(reason: str) -> dict[str, Any]:
    return {"status": "skipped", "reason": reason}


def _fetch(model: Model, name: str, deadline: Deadline, tmp: Path, n: int) -> tuple[Path | None, dict[str, Any]]:
    """Download ONE listed mesh into `tmp`: `(path, {})` on success, `(None, doc)` with the deep entry to report
    otherwise. A transport failure is a `status: error` entry, never raised: one unreachable file must not cost
    the rest of the batch."""
    client = catalog.CLIENTS[model.site]
    if client.file_link is None:
        return None, _skipped(f"{model.site} serves its files only inside a login-gated archive")
    if deadline.expired():
        return None, _skipped("deep time budget spent")
    file = next(f for f in model.files if f.name == name)
    dest = tmp / f"{n}-{safe_name(name)}"
    try:
        link, headers = client.file_link(model, file)
        transport.download(model.site, link, dest, headers=headers)
    except MarketplaceError as exc:
        return None, {"status": "error", "reason": exc.reason[:200], "file": name}
    return dest, {}


def deep_measure(
    models: Sequence[Model],
    conn: sqlite3.Connection | None,
    *,
    max_bytes: int,
    budget_s: float = DEEP_BUDGET_S,
) -> dict[tuple[str, str], dict[str, Any]]:
    """`{(site, model id): deep facts}` for the smallest mesh of each model. A model measured before (same
    site/model/file/size) is answered from the library cache with no network call; only measured answers (`ok`, or
    `error` from the mesh itself) are cached, never a transport failure, a skip or a missing trimesh. The cache is
    read and written on the calling thread (sqlite connections are thread-bound); downloads run in a bounded pool
    under a time budget and every downloaded mesh is measured in ONE batch (`analytics.measure_meshes`)."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    todo: list[tuple[Model, str, int, str]] = []
    for model in models:
        key = (model.site, model.id)
        if model.paid:
            out[key] = _skipped("paid listing")
            continue
        pick = analytics.pick_mesh(((f.name, f.size) for f in model.files), max_bytes=max_bytes)
        if pick is None:
            out[key] = _skipped("no mesh file of known size within the cap")
            continue
        cache_key = f"file:{model.site}:{model.id}:{pick[0]}:{pick[1]}"
        cached = cache_get(conn, cache_key)
        if cached is not None:
            out[key] = {**cached, "cached": True}
        else:
            todo.append((model, pick[0], pick[1], cache_key))
    if not todo:
        return out
    deadline = Deadline(budget_s)
    with tempfile.TemporaryDirectory(prefix="3d-deep-") as tmp_name:
        tmp = Path(tmp_name)
        with ThreadPoolExecutor(max_workers=min(DEEP_WORKERS, pool_size(len(todo)))) as pool:
            fetched = list(pool.map(lambda it: _fetch(it[1][0], it[1][1], deadline, tmp, it[0]), enumerate(todo)))
        paths = [path for path, _ in fetched if path is not None]
        measured = analytics.measure_meshes(paths)
        for (model, name, size, cache_key), (path, failure) in zip(todo, fetched, strict=True):
            key = (model.site, model.id)
            if path is None:
                out[key] = failure
                continue
            sha = file_sha256(path)
            doc = {"file": name, "size": size, "sha256": sha, **measured[str(path)]}
            if doc.get("status") in ("ok", "error"):
                cache_put(conn, cache_key, sha, doc)
            out[key] = {**doc, "cached": False}
    return out


# --------------------------------------------------------------------------- the search itself
def _session_key(options: SearchOptions, query: str) -> str | None:
    """The explicit `idempotency_key`, namespaced; None (a fresh session per call) otherwise."""
    if options.idempotency_key is None:
        return None
    digest = hashlib.sha256(f"{options.idempotency_key}\0{query}".encode()).hexdigest()[
        :32
    ]
    return f"search:{digest}"


@dataclass(frozen=True)
class SearchRun:
    """`doc`: the `3d.models.search/2` document; `failures`: site -> the structured error that site raised (the
    document carries only its JSON form), so a caller can re-raise the one error of a single-site search."""

    doc: dict[str, Any]
    failures: dict[str, ThreeDError]


def run_search(query: str, options: SearchOptions | None = None) -> SearchRun:
    """Search, analyse, filter, persist; return the document and the per-site failures."""
    opts = options or SearchOptions()
    check_vitamin(opts.vitamin)
    if opts.kind not in SEARCH_KINDS:
        raise InvalidArgument("kind", opts.kind, list(SEARCH_KINDS), command="models")
    effective = effective_query(query, opts.vitamin)
    started = time.monotonic()
    conn, warnings = _open_cache(opts)
    store = LibraryDetailStore(opts.db_path) if conn is not None else None
    try:
        site_results = catalog.search(
            effective,
            list(opts.sites),
            opts.limit,
            details=opts.details,
            store=store,
        )
        deep: dict[tuple[str, str], dict[str, Any]] = {}
        models_by_site: dict[str, list[tuple[int, Model]]] = {}
        for r in site_results:
            ranked = list(enumerate(r.models, start=1))
            if opts.only_cad:
                ranked = [(n, m) for n, m in ranked if _has_cad(m)]
            models_by_site[r.site] = ranked
        if opts.deep or opts.vitamin:
            candidates = [
                m
                for site in models_by_site.values()
                for _, m in _deep_candidates(site, opts)
            ]
            if candidates:
                deep = deep_measure(candidates, conn, max_bytes=opts.deep_max_bytes)
        results: dict[str, list[dict[str, Any]]] = {}
        for r in site_results:
            if r.error is not None:
                continue
            records = [
                result_record(m, n, deep=deep.get((m.site, m.id)))
                for n, m in models_by_site[r.site]
            ]
            if opts.vitamin:
                records = [
                    rec
                    for rec in records
                    if rec["reference_suitable"] or not rec["analytics"]["files_known"]
                ]
            results[r.site] = records
        doc: dict[str, Any] = {
            "schema": SCHEMA,
            "query": query,
            "effective_query": effective,
            "search_id": None,
            "limit": opts.limit,
            "sites": list(opts.sites),
            "deep": bool(opts.deep or opts.vitamin),
            "vitamin": opts.vitamin,
            "elapsed_s": round(time.monotonic() - started, 3),
            "timings": {r.site: round(r.elapsed_s, 3) for r in site_results},
            "results": results,
            "errors": [
                catalog.error_json(r.site, r.error)
                for r in site_results
                if r.error is not None
            ],
            "warnings": [
                {"site": r.site, "message": w} for r in site_results for w in r.warnings
            ]
            + warnings,
        }
        if opts.save and conn is not None:
            _persist(conn, doc, opts)
        return SearchRun(doc, {r.site: r.error for r in site_results if r.error is not None})
    finally:
        if store is not None:
            store.close()
        if conn is not None:
            conn.close()


def _has_cad(model: Model) -> bool:
    return bool(
        analytics.file_facts(
            ((f.name, f.size) for f in model.files), files_known=model.files_known
        ).get("cad")
    )


def _deep_candidates(
    ranked: Iterable[tuple[int, Model]], opts: SearchOptions
) -> list[tuple[int, Model]]:
    """The models to measure: for a vitamin preset only those passing the cheap suitability tests, first
    `deep_top` (default VITAMIN_DEEP_TOP) per site; for a plain `--deep` every result (or the first `deep_top`)."""
    chosen = list(ranked)
    if opts.vitamin:
        chosen = [(n, m) for n, m in chosen if _cheap_suitable(m)]
    top = (
        opts.deep_top
        if opts.deep_top is not None
        else (VITAMIN_DEEP_TOP if opts.vitamin else None)
    )
    return chosen[:top] if top is not None else chosen


def _cheap_suitable(model: Model) -> bool:
    facts = analytics.file_facts(
        ((f.name, f.size) for f in model.files), files_known=model.files_known
    )
    return bool(
        analytics.reference_suitability(
            facts, paid=model.paid, license_name=model.license
        )["suitable"]
    )


def _open_cache(
    opts: SearchOptions,
) -> tuple[sqlite3.Connection | None, list[dict[str, str]]]:
    """The library connection (cache + persistence), or None with a warning when it cannot be opened;
    `--no-save` never opens it."""
    if not opts.save:
        return None, []
    try:
        from library import db as librarydb

        return librarydb.open_library(opts.db_path), []
    except (sqlite3.Error, OSError) as exc:
        return None, [
            {
                "site": "library",
                "message": f"library database unavailable, search not saved: {exc}",
            }
        ]


def _persist(
    conn: sqlite3.Connection, doc: dict[str, Any], opts: SearchOptions
) -> None:
    from library import db as librarydb

    try:
        parent_id = (
            _resolve_session(conn, opts.parent_session)["id"]
            if opts.parent_session
            else None
        )
        meta = {
            k: doc[k]
            for k in (
                "schema",
                "effective_query",
                "limit",
                "sites",
                "deep",
                "vitamin",
                "elapsed_s",
                "timings",
                "errors",
                "warnings",
            )
        }
        session = librarydb.insert_row(
            conn,
            "search_session",
            {
                "query": doc["query"],
                "parent_id": parent_id,
                "kind": opts.kind,
                "meta": meta,
            },
            source_key=_session_key(opts, doc["query"]),
        )
        doc["search_id"] = session.uid
        for site, records in doc["results"].items():
            for rec in records:
                librarydb.insert_row(
                    conn,
                    "search_result",
                    {
                        "session_id": session.id,
                        "site": site,
                        "model_id": rec["id"],
                        "url": rec["url"],
                        "title": rec["title"],
                        "rank": rec["rank"],
                        "analytics": rec["analytics"],
                        "image_url": rec["image"] or None,
                        "record": rec,
                    },
                    source_key=f"{session.uid}:{site}:{rec['id']}",
                )
    except sqlite3.Error as exc:
        doc["search_id"] = None
        doc["warnings"].append(
            {"site": "library", "message": f"search not saved: {exc}"}
        )


# --------------------------------------------------------------------------- reading searches back
def _resolve_session(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    """A session by full uid, integer id, or `latest`."""
    if ref in ("latest", "last"):
        row = conn.execute(
            "SELECT * FROM search_session ORDER BY id DESC LIMIT 1"
        ).fetchone()
    elif ref.isascii() and ref.isdecimal():
        row = conn.execute(
            "SELECT * FROM search_session WHERE id = ?", (int(ref),)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM search_session WHERE uid = ?", (ref,)
        ).fetchone()
    if row is None:
        raise UsageError(
            f"no saved search {ref!r}",
            command="models",
            remediation=["List saved searches with `3d models searches list`."],
        )
    return row


def session_doc(conn: sqlite3.Connection, ref: str) -> dict[str, Any]:
    """The `3d.models.search/2` document of a persisted search (same `results`, `errors`, `warnings`, `timings` the
    original run printed), plus `created_at`, `kind` and `parent`."""
    session = _resolve_session(conn, ref)
    meta = json.loads(session["meta"])
    rows = conn.execute(
        "SELECT site, record FROM search_result WHERE session_id = ? ORDER BY id",
        (session["id"],),
    ).fetchall()
    sites = list(meta.get("sites", []))
    results: dict[str, list[dict[str, Any]]] = {
        s: [] for s in sites if s not in _failed(meta)
    }
    for row in rows:
        results.setdefault(row["site"], []).append(json.loads(row["record"]))
    parent = None
    if session["parent_id"] is not None:
        parent = conn.execute(
            "SELECT uid FROM search_session WHERE id = ?", (session["parent_id"],)
        ).fetchone()["uid"]
    return {
        "schema": SCHEMA,
        "query": session["query"],
        "effective_query": meta.get("effective_query", session["query"]),
        "search_id": session["uid"],
        "limit": meta.get("limit"),
        "sites": sites,
        "deep": meta.get("deep", False),
        "vitamin": meta.get("vitamin"),
        "elapsed_s": meta.get("elapsed_s"),
        "timings": meta.get("timings", {}),
        "results": results,
        "errors": meta.get("errors", []),
        "warnings": meta.get("warnings", []),
        "created_at": session["created_at"],
        "kind": session["kind"],
        "parent": parent,
    }


def _failed(meta: dict[str, Any]) -> set[str]:
    return {str(e.get("site")) for e in meta.get("errors", [])}


def list_sessions(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict[str, Any]]:
    """Newest first: `{id, search_id, created_at, query, kind, sites, counts{site: n}}`."""
    out = []
    for row in conn.execute(
        "SELECT * FROM search_session ORDER BY id DESC LIMIT ?", (limit,)
    ):
        counts = {
            r["site"]: r["n"]
            for r in conn.execute(
                "SELECT site, COUNT(*) AS n FROM search_result WHERE session_id = ? GROUP BY site ORDER BY site",
                (row["id"],),
            )
        }
        out.append(
            {
                "id": row["id"],
                "search_id": row["uid"],
                "created_at": row["created_at"],
                "query": row["query"],
                "kind": row["kind"],
                "sites": json.loads(row["meta"]).get("sites", []),
                "counts": counts,
            }
        )
    return out
