"""backup.py — read-only backups of what a firmware runbook says to save, and the freshness gate.

WHAT: `take_backup` pulls every item of the profile's `backups:` list that applies in the printer's
  state — an HTTP GET (`http_get`), one remote file over ssh (`ssh_file`: `cat`), a directory
  tree over ssh (`ssh_tar`: `tar czf -`, verified by listing the archive), or a note for the user
  (`manual`) — into `DEST/<printer>-<profile>-<UTC stamp>/` and writes `manifest.json` (profile id,
  printer, model, state, time, and per item the source, file, size and SHA-256) plus a
  `SHA256SUMS`. `find_fresh_backup` is the gate `fw apply` passes through: the newest manifest of
  THIS printer and profile that is younger than the allowed age, has no failed item, still matches
  its files' hashes and holds every backup id the plan's steps require.

WHY: every runbook says "back up first, and do not start without a good backup" (the K1 runbook
  stops unless the backup script prints `backup OK`). Making that a machine-checked precondition
  of `apply` — freshness, integrity and coverage — is the point of a backup tool.

ACCESSED VIA: `printers_cli/fw.py` (`fw backup`), `printer_fw.engine` (the gate).

INVARIANTS: read-only on the printer (GET, `cat`, `tar czf -`). Backup files are written 0600
  inside a 0700 directory (configs carry Wi-Fi and printer secrets). A failed item makes the
  manifest `ok: false` and the command exit 1; an optional item that is absent (HTTP 404, no such
  file) is recorded `missing` and does not. `manual` items never satisfy a step's `backup:` (the
  profile validator forbids requiring them). Remote names never become local paths: files are
  named `<item id>` plus a sanitised suffix.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from errors import GateFailure
from printer_fw.probes import Backends, Endpoint
from printer_fw.profiles import BackupItem, Profile

MANIFEST = "manifest.json"
SCHEMA = 1
DEFAULT_MAX_AGE_S = 24 * 3600.0
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class ItemResult:
    id: str
    via: str
    status: str  # ok | failed | missing | manual | skipped
    source: str = ""
    file: str = ""
    size: int = 0
    sha256: str = ""
    detail: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "via": self.via,
            "status": self.status,
            "source": self.source,
            "file": self.file,
            "size": self.size,
            "sha256": self.sha256,
            "detail": self.detail,
        }


@dataclass
class Manifest:
    printer: str
    profile_id: str
    model: str | None
    state: str | None
    created_at: str
    created_epoch: float
    directory: Path
    items: list[ItemResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.status == "failed" for i in self.items)

    def ok_ids(self) -> set[str]:
        return {i.id for i in self.items if i.status == "ok"}

    def to_json(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "printer": self.printer,
            "profile": self.profile_id,
            "model": self.model,
            "state": self.state,
            "created_at": self.created_at,
            "created_epoch": self.created_epoch,
            "directory": str(self.directory),
            "ok": self.ok,
            "items": [i.to_json() for i in self.items],
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _filename(item: BackupItem, suffix_from: str) -> str:
    base = _SAFE.sub("_", Path(suffix_from).name or "data")[-60:]
    return f"{_SAFE.sub('_', item.id)}__{base}"


def _fetch_http(
    item: BackupItem, ep: Endpoint, be: Backends
) -> tuple[ItemResult, bytes]:
    url = ep.url(item.service, item.path)
    try:
        status, body = be.http("GET", url, timeout=item.timeout_s)
    except (OSError, ValueError) as exc:
        return ItemResult(item.id, item.via, "failed", url, detail=str(exc)), b""
    if status == 404 and item.optional:
        return ItemResult(
            item.id, item.via, "missing", url, detail="HTTP 404 (optional)"
        ), b""
    if status != 200:
        return ItemResult(
            item.id, item.via, "failed", url, detail=f"HTTP {status}"
        ), b""
    return ItemResult(item.id, item.via, "ok", url), body


def _fetch_file(
    item: BackupItem, ep: Endpoint, be: Backends
) -> tuple[ItemResult, bytes]:
    if ep.ssh is None or not be.use_ssh:
        why = "ssh disabled (--no-ssh)" if ep.ssh else "the profile has no ssh settings"
        return ItemResult(item.id, item.via, "failed", item.path, detail=why), b""
    result = be.runner.run(
        ep.host,
        ep.ssh.user,
        ["cat", item.path],
        timeout=max(item.timeout_s, ep.ssh.connect_timeout_s),
        port=ep.ssh_port,
    )
    if result.rc != 0:
        gone = "No such file" in result.err
        if item.optional and gone:
            return ItemResult(
                item.id,
                item.via,
                "missing",
                item.path,
                detail="no such file (optional)",
            ), b""
        return ItemResult(
            item.id,
            item.via,
            "failed",
            item.path,
            detail=f"cat exited {result.rc}: {result.err.strip()[:200]}",
        ), b""
    if item.expect_regex and not re.search(item.expect_regex, result.out):
        return ItemResult(
            item.id,
            item.via,
            "failed",
            item.path,
            detail=f"content does not match /{item.expect_regex}/ — look at the file before going on",
        ), b""
    return ItemResult(item.id, item.via, "ok", item.path), result.stdout


def _fetch_tar(
    item: BackupItem, ep: Endpoint, be: Backends
) -> tuple[ItemResult, bytes]:
    source = f"{item.base}: {' '.join(item.paths)}"
    if ep.ssh is None or not be.use_ssh:
        why = "ssh disabled (--no-ssh)" if ep.ssh else "the profile has no ssh settings"
        return ItemResult(item.id, item.via, "failed", source, detail=why), b""
    result = be.runner.run(
        ep.host,
        ep.ssh.user,
        ["tar", "czf", "-", "-C", item.base, *item.paths],
        timeout=max(item.timeout_s, ep.ssh.connect_timeout_s),
        port=ep.ssh_port,
    )
    if result.rc != 0:
        gone = "No such file" in result.err or "not found" in result.err
        if item.optional and gone:
            return ItemResult(
                item.id, item.via, "missing", source, detail="path absent (optional)"
            ), b""
        return ItemResult(
            item.id,
            item.via,
            "failed",
            source,
            detail=f"tar exited {result.rc}: {result.err.strip()[:200]}",
        ), b""
    try:
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:gz") as tar:
            names = set(tar.getnames())
    except (tarfile.TarError, OSError, EOFError) as exc:
        return ItemResult(
            item.id, item.via, "failed", source, detail=f"archive is broken: {exc}"
        ), b""
    absent = [m for m in item.expect_tar_members if m not in names]
    if absent:
        return ItemResult(
            item.id,
            item.via,
            "failed",
            source,
            detail=f"archive lacks {', '.join(absent)}",
        ), b""
    return ItemResult(item.id, item.via, "ok", source), result.stdout


def _write_private(path: Path, data: bytes) -> None:
    with path.open("xb") as fh:
        fh.write(data)
    path.chmod(0o600)


def take_backup(
    profile: Profile,
    ep: Endpoint,
    be: Backends,
    *,
    printer: str,
    model: str | None,
    state: str | None,
    dest_root: Path,
    now: float | None = None,
) -> Manifest:
    """Pull the profile's backup items that apply in `state` into a new timestamped directory."""
    stamp_epoch = time.time() if now is None else now
    moment = datetime.fromtimestamp(stamp_epoch, tz=timezone.utc)
    dest_root.mkdir(parents=True, exist_ok=True)
    base = f"{_SAFE.sub('_', printer)}-{profile.id}-{moment.strftime('%Y%m%dT%H%M%SZ')}"
    directory = dest_root / base
    n = 1
    while directory.exists():
        n += 1
        directory = dest_root / f"{base}-{n}"
    directory.mkdir(mode=0o700)
    manifest = Manifest(
        printer, profile.id, model, state, moment.isoformat(), stamp_epoch, directory
    )
    for item in profile.backups:
        if item.states and state not in item.states:
            manifest.items.append(
                ItemResult(
                    item.id,
                    item.via,
                    "skipped",
                    detail=f"not taken in state {state or 'unknown'} (only {', '.join(item.states)})",
                )
            )
            continue
        if item.via == "manual":
            manifest.items.append(
                ItemResult(item.id, item.via, "manual", detail=item.instruction)
            )
            continue
        fetch = {
            "http_get": _fetch_http,
            "ssh_file": _fetch_file,
            "ssh_tar": _fetch_tar,
        }[item.via]
        res, data = fetch(item, ep, be)
        if res.status == "ok":
            name = _filename(
                item,
                item.path or item.paths[0]
                if item.via != "ssh_tar"
                else f"{item.id}.tgz",
            )
            _write_private(directory / name, data)
            res = ItemResult(
                res.id, res.via, "ok", res.source, name, len(data), _sha256(data)
            )
        manifest.items.append(res)
    sums = "".join(
        f"{i.sha256}  {i.file}\n" for i in manifest.items if i.status == "ok"
    )
    _write_private(directory / "SHA256SUMS", sums.encode())
    _write_private(
        directory / MANIFEST, (json.dumps(manifest.to_json(), indent=2) + "\n").encode()
    )
    return manifest


def _load(path: Path) -> Manifest | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = [ItemResult(**i) for i in raw["items"]]
        return Manifest(
            raw["printer"],
            raw["profile"],
            raw.get("model"),
            raw.get("state"),
            raw["created_at"],
            float(raw["created_epoch"]),
            path.parent,
            items,
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _intact(manifest: Manifest) -> str | None:
    """Why the manifest's files no longer match it, or None."""
    for i in manifest.items:
        if i.status != "ok":
            continue
        f = manifest.directory / i.file
        try:
            data = f.read_bytes()
        except OSError:
            return f"{i.file} is missing"
        if _sha256(data) != i.sha256:
            return f"{i.file} no longer matches its recorded SHA-256"
    return None


def list_manifests(root: Path, printer: str) -> list[Manifest]:
    """Every readable manifest of `printer` under `root`, newest first."""
    found = [
        m
        for p in root.glob(f"*/{MANIFEST}")
        if (m := _load(p)) is not None and m.printer == printer
    ]
    return sorted(found, key=lambda m: m.created_epoch, reverse=True)


def find_fresh_backup(
    root: Path,
    printer: str,
    profile_id: str,
    *,
    max_age_s: float,
    required: set[str],
    now: float | None = None,
) -> tuple[Manifest | None, str]:
    """(manifest, "") when a backup satisfies the gate, else (None, why). Only the NEWEST
    manifest of the printer counts: an older good one must not mask a newer failed backup."""
    current = time.time() if now is None else now
    manifests = [m for m in list_manifests(root, printer) if m.profile_id == profile_id]
    if not manifests:
        return None, f"no backup of {printer} found under {root}"
    newest = manifests[0]
    age = current - newest.created_epoch
    if age > max_age_s:
        return (
            None,
            f"the newest backup ({newest.directory.name}) is {age / 3600:.1f} h old; the limit is {max_age_s / 3600:g} h",
        )
    if age < -60:
        return (
            None,
            f"the newest backup ({newest.directory.name}) is dated in the future",
        )
    if not newest.ok:
        failed = ", ".join(i.id for i in newest.items if i.status == "failed")
        return (
            None,
            f"the newest backup ({newest.directory.name}) has failed items: {failed}",
        )
    broken = _intact(newest)
    if broken:
        return None, f"the newest backup ({newest.directory.name}) is damaged: {broken}"
    lacking = sorted(required - newest.ok_ids())
    if lacking:
        return (
            None,
            f"the newest backup ({newest.directory.name}) lacks required items: {', '.join(lacking)}",
        )
    return newest, ""


def refuse_without_backup(why: str, printer: str) -> GateFailure:
    return GateFailure(
        f"refusing to change {printer}: {why}",
        command="printers fw",
        remediation=[
            f"Run `3d printers fw backup {quote(printer)}` first (read-only), then re-run the same command."
        ],
    )
