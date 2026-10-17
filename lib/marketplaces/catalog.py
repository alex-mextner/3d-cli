"""Site registry and the cross-site operations behind `3d models`: search every requested
site in parallel (one site failing never hides the others), resolve a model URL to its
site, and download a model's files next to a model.json that records author and license."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cli.resources import pool_size
from errors import ThreeDError, UsageError
from marketplaces import cults, makerworld, printables, thingiverse, transport
from marketplaces.base import SITES, DownloadLinks, MarketplaceError, Model, safe_name


@dataclass(frozen=True)
class Client:
    site: str
    match: Callable[[str], str | None]
    search: Callable[[str, int], list[Model]]
    show: Callable[[str], Model]
    download_links: Callable[[Model], DownloadLinks]


CLIENTS: dict[str, Client] = {
    module.SITE: Client(module.SITE, module.match, module.search, module.show, module.download_links)
    for module in (printables, makerworld, thingiverse, cults)
}
assert tuple(CLIENTS) == SITES, "SITES and CLIENTS must list the same sites in the same order"

URL_FORMS = (
    "https://www.printables.com/model/<id>-<slug>",
    "https://makerworld.com/en/models/<id>-<slug>",
    "https://www.thingiverse.com/thing:<id>",
    "https://cults3d.com/en/3d-model/<category>/<slug>",
)


@dataclass(frozen=True)
class SiteResult:
    site: str
    models: tuple[Model, ...] = ()
    error: ThreeDError | None = None


def error_json(site: str, exc: ThreeDError) -> dict[str, Any]:
    if isinstance(exc, MarketplaceError):
        return exc.to_json()
    kind = "dependency" if exc.exit_code == 127 else "error"
    return {"site": site, "kind": kind, "message": exc.message, "remediation": list(exc.remediation)}


def search(query: str, sites: Sequence[str], limit: int) -> list[SiteResult]:
    def one(site: str) -> SiteResult:
        try:
            return SiteResult(site, tuple(CLIENTS[site].search(query, limit)))
        except ThreeDError as exc:
            return SiteResult(site, error=exc)
        except Exception as exc:  # noqa: BLE001 — a parser bug on one site must not hide the others
            return SiteResult(site, error=MarketplaceError(
                site, "parse", f"unexpected answer ({type(exc).__name__}: {exc})"))

    with ThreadPoolExecutor(max_workers=pool_size(len(sites))) as pool:
        return list(pool.map(one, sites))


def resolve(url: str) -> tuple[Client, str]:
    for client in CLIENTS.values():
        ref = client.match(url)
        if ref:
            return client, ref
    raise UsageError(f"not a model URL of a supported site: {url}", command="models",
                     remediation=["Accepted forms: " + "; ".join(URL_FORMS)])


def show(url: str) -> Model:
    client, ref = resolve(url)
    return client.show(ref)


def default_dir(model: Model) -> Path:
    return Path(safe_name(f"{model.site}-{model.id}-{_slug(model.title)}", model.site))


def _slug(text: str) -> str:
    return "-".join("".join(c.lower() if c.isalnum() else " " for c in text).split())[:60]


def _check_target(target: Path) -> None:
    try:
        if target.exists() and not target.is_dir():
            raise UsageError(f"download target is not a directory: {target}", command="models",
                             remediation=["Pass a new or empty directory with --dir."])
        busy = target.is_dir() and any(target.iterdir())
    except OSError as exc:
        raise UsageError(f"cannot use download directory {target}: {exc.strerror or exc}",
                         command="models", remediation=["Pass a new or empty directory with --dir."]) from exc
    if busy:
        raise UsageError(f"download directory is not empty: {target}", command="models",
                         remediation=["Pass a new or empty directory with --dir; existing files "
                                      "are never overwritten."])


def download(url: str, dest: Path | None) -> tuple[Model, Path, list[dict[str, Any]]]:
    """Fetch every file of the model at `url` into `dest` and write `dest/model.json`.
    Paid models are refused before any file request. Files are staged in a hidden sibling
    directory that becomes `dest` only once every file and model.json are written, so a
    failure leaves nothing behind and existing files are never touched."""
    client, ref = resolve(url)
    if dest is not None:
        dest = dest.resolve()  # `""`/symlinks: rmdir+rename need the real path
        if dest == Path.cwd().resolve():
            # Replacing the current directory would leave the shell in a deleted one.
            raise UsageError("--dir cannot be the current directory", command="models",
                             remediation=["Pass a new subdirectory, e.g. --dir ./model."])
        _check_target(dest)  # before any request: a busy --dir is a usage error, not auth/paid
    model = client.show(ref)
    if model.paid:
        raise MarketplaceError(model.site, "paid",
                               f"'{model.title}' is a paid model ({model.price or 'paid'}); "
                               "3d models only downloads free files",
                               remediation=[f"Buy it on the site and download it there: {model.url}"])
    target = dest or default_dir(model).resolve()
    _check_target(target)
    links = client.download_links(model)
    if not links:
        raise MarketplaceError(model.site, "not-found", f"'{model.title}' lists no downloadable files")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", suffix=".part", dir=target.parent))
    except OSError as exc:
        raise UsageError(f"cannot create the download directory next to {target}: "
                         f"{exc.strerror or exc}", command="models",
                         remediation=["Pass a --dir inside a writable directory."]) from exc
    os.chmod(staging, 0o755)  # mkdtemp's 0700 would otherwise become the model dir's mode
    try:
        written: list[dict[str, Any]] = []
        used: set[str] = {"model.json"}
        for file, link, headers in links:
            name = _unique(safe_name(file.name or Path(link.split("?", 1)[0]).name), used)
            size = transport.download(model.site, link, staging / name, headers=headers)
            written.append({"name": name, "size": size, "sha256": _sha256(staging / name),
                            "kind": file.kind})
        meta = {
            "source": {"site": model.site, "id": model.id, "url": model.url},
            "title": model.title,
            "author": {"name": model.author, "url": model.author_url},
            "license": model.license,
            "price": model.price or "free",
            "tags": list(model.tags),
            "summary": model.summary,
            "downloaded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "files": written,
        }
        (staging / "model.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
                                            encoding="utf-8")
        try:
            if target.is_dir():
                target.rmdir()  # empty (checked above); rename needs the name free
            staging.rename(target)
        except OSError as exc:  # something appeared in the target meanwhile: never overwrite
            raise UsageError(f"could not move the download into {target}: {exc.strerror or exc}",
                             command="models", remediation=["Pass a new or empty directory with --dir."]) from exc
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return model, target, written


def _unique(name: str, used: set[str]) -> str:
    """`name`, or `name-2`, `name-3`… — compared case-insensitively, since macOS and Windows
    file systems would let `Part.stl` overwrite `part.stl` (or a site's `Model.json`)."""
    stem, dot, ext = name.rpartition(".")
    base, suffix = (stem, f".{ext}") if dot and stem else (name, "")
    candidate, n = name, 2
    while candidate.casefold() in used:
        candidate = f"{base}-{n}{suffix}"
        n += 1
    used.add(candidate.casefold())
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()
