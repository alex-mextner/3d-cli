"""library/sources.py — download a model and record where it came from (`model_source`).

WHAT: `download_model()` runs `marketplaces.catalog.download` (free models only, never overwrites), then
computes deterministic analytics of the files it wrote (`library.analytics`: file facts, archive members, and with
`deep` a trimesh measurement of every mesh within the size cap, cached by content sha256 in the library DB) and
records one `model_source` row: site, model id, URL, licence, author, directory, the file list with sha256, the
part `role` (`printed|vitamin|reference|stand_in`, docs/contracts/parts.md) and the vitamin kind. `analyze_paths()`
is the same analytics for files already on disk (`3d models analyze`).

ACCESSED VIA: `commands/models.py` (`download`, `analyze`) and `library.actions.download`.

INVARIANTS:
  - A `vitamin` or `reference` download is a REFERENCE: it must carry its URL and licence (the part contract's
    `reference_source`). A listing without a licence is recorded with `license: null` and a `license_missing`
    warning; the part contract refuses such a part later, it is never silently invented.
  - `model_source` has one row per (site, model id): downloading the same model again updates the row's role,
    directory, files and analytics instead of adding a second one.
  - Recording failures (read-only home, locked DB) are warnings; the downloaded files stay.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from errors import InvalidArgument
from library import analytics
from library.search import DEEP_MAX_BYTES, cache_by_sha, cache_put, file_sha256
from marketplaces import catalog

ROLES = ("printed", "vitamin", "reference", "stand_in")
REFERENCE_ROLES = ("vitamin", "reference")
VITAMIN_KINDS = ("pcb", "connector", "screw", "wire")


def check_role(role: str | None, vitamin_kind: str | None) -> None:
    if role is not None and role not in ROLES:
        raise InvalidArgument("--role", role, list(ROLES), command="models")
    if vitamin_kind is not None:
        if vitamin_kind not in VITAMIN_KINDS:
            raise InvalidArgument(
                "--vitamin", vitamin_kind, list(VITAMIN_KINDS), command="models"
            )
        if role not in REFERENCE_ROLES:
            raise InvalidArgument(
                "--vitamin",
                vitamin_kind,
                ["only with --role vitamin or --role reference"],
                command="models",
            )


def measure_many(paths: Sequence[Path], conn: sqlite3.Connection | None) -> dict[str, dict[str, Any]]:
    """`{str(path): mesh facts}` (`analytics.measure_meshes`), each answered from the library cache when the same
    content (sha256) was measured before; the uncached ones are measured in ONE batch and cached when the answer is
    `ok` or `error` (a missing trimesh is not cached)."""
    out: dict[str, dict[str, Any]] = {}
    pending: list[tuple[Path, str]] = []
    for path in paths:
        sha = file_sha256(path)
        cached = cache_by_sha(conn, sha)
        if cached is not None:
            out[str(path)] = {**cached, "sha256": sha, "cached": True}
        else:
            pending.append((path, sha))
    measured = analytics.measure_meshes([path for path, _ in pending])
    for path, sha in pending:
        facts = measured[str(path)]
        if facts.get("status") in ("ok", "error"):
            cache_put(conn, f"sha:{sha}", sha, facts)
        out[str(path)] = {**facts, "sha256": sha, "cached": False}
    return out


def analyze_paths(
    paths: Sequence[Path],
    conn: sqlite3.Connection | None,
    *,
    deep: bool,
    max_bytes: int = DEEP_MAX_BYTES,
) -> dict[str, Any]:
    """`{facts, files[]}`: `facts` = file_facts of the names (archives expanded to their members), `files[i]` =
    the per-file analytics (`mesh{}` for a mesh within `max_bytes` when `deep`, `members` for an archive)."""
    listed = {p.name: analytics.list_archive(p) for p in paths if analytics.file_class(p.name) == "archive"}
    sized = [(p.name, p.stat().st_size) for p in paths]
    facts = analytics.file_facts(analytics.expand_archives(sized, listed))
    files: list[dict[str, Any]] = [
        {"name": p.name, "class": analytics.file_class(p.name), "size": p.stat().st_size} for p in paths
    ]
    meshes = [p for p, f in zip(paths, files, strict=True) if deep and f["class"] == "mesh" and f["size"] <= max_bytes]
    measured = measure_many(meshes, conn)
    for path, doc in zip(paths, files, strict=True):
        if str(path) in measured:
            doc["mesh"] = measured[str(path)]
        elif doc["class"] == "archive" and listed.get(path.name):
            doc["members"] = len(listed[path.name])
    return {"facts": facts, "files": files}


def _open(
    db_path: str | None, save: bool
) -> tuple[sqlite3.Connection | None, list[dict[str, str]]]:
    if not save:
        return None, []
    try:
        from library import db as librarydb

        return librarydb.open_library(db_path), []
    except (sqlite3.Error, OSError) as exc:
        return None, [
            {
                "code": "library_unavailable",
                "message": f"library database unavailable: {exc}",
            }
        ]


def record_source(
    conn: sqlite3.Connection,
    model: Any,
    target: Path,
    files: list[dict[str, Any]],
    *,
    role: str | None,
    vitamin_kind: str | None,
    analytics_doc: dict[str, Any],
) -> dict[str, Any]:
    """Insert or refresh the `model_source` row of a downloaded model; returns `{uid, created}`."""
    from library import db as librarydb
    from printops import db

    values = {
        "site": model.site,
        "model_id": model.id,
        "url": model.url,
        "license": model.license or None,
        "author": model.author or None,
        "dir": str(target.resolve()),
        "files": files,
        "sha256": _digest_of(files),
        "role": role,
        "vitamin_kind": vitamin_kind,
        "analytics": analytics_doc,
    }
    inserted = librarydb.insert_row(
        conn, "model_source", values, source_key=f"source:{model.site}:{model.id}"
    )
    if not inserted.created:
        with db.transaction(conn):
            conn.execute(
                "UPDATE model_source SET url = ?, license = ?, author = ?, dir = ?, files = ?, sha256 = ?, "
                "role = ?, vitamin_kind = ?, analytics = ? WHERE id = ?",
                (
                    values["url"],
                    values["license"],
                    values["author"],
                    values["dir"],
                    json.dumps(files, sort_keys=True, separators=(",", ":")),
                    values["sha256"],
                    role,
                    vitamin_kind,
                    json.dumps(analytics_doc, sort_keys=True, separators=(",", ":")),
                    inserted.id,
                ),
            )
    return {"uid": inserted.uid, "created": inserted.created}


def _digest_of(files: list[dict[str, Any]]) -> str | None:
    """One digest for the whole download when it is a single file, else None (per-file sha256 are in `files`)."""
    return files[0]["sha256"] if len(files) == 1 else None


def download_model(
    url: str,
    dest: Path | None,
    *,
    select: Sequence[str] | None = None,
    role: str | None = None,
    vitamin_kind: str | None = None,
    deep: bool = False,
    save: bool = True,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Download, analyse, record. Returns `{model, dir, files, metadata, analytics, source, warnings}`; `source` is
    `{uid, created, role, license}` or None when nothing was recorded (`save=False`, or the DB was unavailable)."""
    check_role(role, vitamin_kind)
    model, target, written = catalog.download(url, dest, select=select)
    conn, warn = _open(db_path, save)
    warnings: list[dict[str, str]] = list(warn)
    files = [w.to_json() for w in written]
    measure = deep or role in REFERENCE_ROLES
    try:
        report = analyze_paths([target / w.name for w in written], conn, deep=measure)
        source: dict[str, Any] | None = None
        if conn is not None:
            try:
                source = record_source(
                    conn,
                    model,
                    target,
                    files,
                    role=role,
                    vitamin_kind=vitamin_kind,
                    analytics_doc=report,
                )
                source.update({"role": role, "license": model.license or None})
            except sqlite3.Error as exc:
                warnings.append(
                    {
                        "code": "source_not_recorded",
                        "message": f"model_source not recorded: {exc}",
                    }
                )
    finally:
        if conn is not None:
            conn.close()
    if role in REFERENCE_ROLES and not model.license:
        warnings.append(
            {
                "code": "license_missing",
                "message": f"{model.site} lists no licence for '{model.title}'; a {role} part needs "
                "reference_source.license (docs/contracts/parts.md)",
            }
        )
    return {
        "model": model.to_json(),
        "dir": str(target),
        "files": files,
        "metadata": str(target / "model.json"),
        "analytics": report,
        "source": source,
        "warnings": warnings,
    }
