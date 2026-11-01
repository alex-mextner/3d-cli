"""standalone_script.py — generate the self-contained installer for Spoolman + 3d-api.

ACCESSED VIA: `3d init --env --spoolman script [--script-out FILE]` (printops.env_setup).

WHAT: fills TEMPLATE (printops.standalone_template: the bash text) with the version stamp and the
  docker build context — the very paths
  `3d printops deploy` rsyncs (printops.deploy.SYNC_PATHS: bin/3d, lib, fleet, pyproject.toml,
  services/3d-api) — as a gzipped tar, base64 inside a heredoc. The result runs on any Linux server
  with docker compose and needs nothing from 3d-cli: it unpacks the context, runs the SAME
  services/3d-api/compose.yaml as the Odroid deploy, waits for health, mints a token.
WHY: a server the user reaches by hand (no ssh from this machine, a NAS, a VPS) must be able to
  host PrintOps without 3d-cli installed there; embedding the compose file and the 3d-api sources
  keeps ONE deployment definition instead of a second hand-written compose.
INVARIANTS:
  - Deterministic: sorted members, zeroed mtime/uid/gid, gzip mtime 0 — the same checkout yields a
    byte-identical script, so re-generating never shows a spurious diff and the script's payload
    sha256 only changes when the sources do.
  - __pycache__/*.pyc are never embedded (deploy's rsync excludes them too).
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import tarfile
from pathlib import Path

from printops.deploy import SYNC_PATHS
from printops.standalone_template import TEMPLATE

WRAP = 76


def _payload(repo_root: Path) -> bytes:
    """The build context as a reproducible .tar.gz."""
    files: list[Path] = []
    for rel in SYNC_PATHS:
        base = repo_root / rel
        if base.is_file():
            files.append(base)
        elif base.is_dir():
            files += [
                p
                for p in base.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
            ]
    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, compresslevel=9) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for path in sorted(files, key=lambda p: p.relative_to(repo_root).as_posix()):
            info = tar.gettarinfo(
                str(path), arcname=path.relative_to(repo_root).as_posix()
            )
            info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
            info.pax_headers = {}
            with path.open("rb") as fh:
                tar.addfile(info, fh)
    return buf.getvalue()


def render(repo_root: Path, version: str) -> str:
    """The complete script text for the checkout at `repo_root`."""
    payload = _payload(repo_root)
    b64 = base64.b64encode(payload).decode("ascii")
    wrapped = "\n".join(b64[i:i + WRAP] for i in range(0, len(b64), WRAP))
    for marker in ("@PAYLOAD_SHA256@", "@PAYLOAD@"):
        if TEMPLATE.count(marker) != 1:
            raise ValueError(f"the script template must contain {marker} exactly once")
    return (TEMPLATE.replace("@GENERATED_BY@", f"3d-cli {version}")
            .replace("@PAYLOAD_SHA256@", hashlib.sha256(payload).hexdigest())
            .replace("@PAYLOAD@", wrapped))


def write(repo_root: Path, version: str, out: Path) -> Path:
    """Write the script to `out` (mode 0755, atomically); returns the path."""
    text = render(repo_root, version)
    out = out.expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o755)
    tmp.replace(out)
    return out
