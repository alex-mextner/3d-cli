"""Site registry and the cross-site operations behind `3d models`: search every requested
site in parallel (one site failing never hides the others), resolve a model URL to its
site, and download a model's files next to a model.json that records author and license."""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from cli.resources import pool_size
from errors import ThreeDError, UsageError
from marketplaces import cults, makerworld, printables, thingiverse, transport
from marketplaces.base import SITES, DetailStore, DownloadLinks, MarketplaceError, Model, ModelFile, safe_name


FileLink = Callable[[Model, ModelFile], tuple[str, dict[str, str]]]


@dataclass(frozen=True)
class Client:
    """One site. `enrich` (optional) completes search hits with per-hit follow-up requests
    (Thingiverse licenses and file lists); `file_link` resolves ONE file's download URL and
    headers (absent when the site only serves a login-gated archive, MakerWorld)."""

    site: str
    match: Callable[[str], str | None]
    search: Callable[[str, int], list[Model]]
    show: Callable[[str], Model]
    download_links: Callable[[Model], DownloadLinks]
    file_link: FileLink | None = None
    enrich: Callable[[list[Model], DetailStore | None], list[Model]] | None = None


CLIENTS: dict[str, Client] = {
    printables.SITE: Client(printables.SITE, printables.match, printables.search, printables.show,
                            printables.download_links, file_link=printables.download_link),
    makerworld.SITE: Client(makerworld.SITE, makerworld.match, makerworld.search, makerworld.show,
                            makerworld.download_links),
    thingiverse.SITE: Client(thingiverse.SITE, thingiverse.match, thingiverse.search, thingiverse.show,
                             thingiverse.download_links, file_link=thingiverse.download_link,
                             enrich=thingiverse.enrich),
    cults.SITE: Client(cults.SITE, cults.match, cults.search, cults.show, cults.download_links,
                       file_link=cults.download_link),
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
    elapsed_s: float = 0.0
    warnings: tuple[str, ...] = ()


def error_json(site: str, exc: ThreeDError) -> dict[str, Any]:
    if isinstance(exc, MarketplaceError):
        return exc.to_json()
    kind = "dependency" if exc.exit_code == 127 else "error"
    return {"site": site, "kind": kind, "message": exc.message, "remediation": list(exc.remediation)}


def search(query: str, sites: Sequence[str], limit: int, *, details: bool = True,
           store: DetailStore | None = None) -> list[SiteResult]:
    """Search every site in parallel. `details=False` skips the per-hit follow-up requests of
    sites that need them (Thingiverse: one request per page, but no license or file list); `store`
    is the persistent cache those follow-ups read and fill."""
    def one(site: str) -> SiteResult:
        client = CLIENTS[site]
        started = time.monotonic()
        try:
            models = client.search(query, limit)
            warnings: list[str] = []
            if details and client.enrich is not None:
                models = client.enrich(models, store)
                missing = sum(1 for m in models if not m.files_known)
                if missing:
                    warnings.append(f"{missing} of {len(models)} hits have no file list or license "
                                    "(request budget spent or the site refused the follow-up call)")
            return SiteResult(site, tuple(models), elapsed_s=time.monotonic() - started,
                              warnings=tuple(warnings))
        except ThreeDError as exc:
            return SiteResult(site, error=exc, elapsed_s=time.monotonic() - started)
        except Exception as exc:  # noqa: BLE001 — a parser bug on one site must not hide the others
            return SiteResult(site, error=MarketplaceError(
                site, "parse", f"unexpected answer ({type(exc).__name__}: {exc})"),
                elapsed_s=time.monotonic() - started)

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


_DIR_HINT = "Pass a new or empty directory with --dir."


def _check_target(target: Path) -> None:
    try:
        if target.exists() and not target.is_dir():
            raise UsageError(f"download target is not a directory: {target}", command="models",
                             remediation=[_DIR_HINT])
        busy = target.is_dir() and any(target.iterdir())
    except OSError as exc:
        raise UsageError(f"cannot use download directory {target}: {exc.strerror or exc}",
                         command="models", remediation=[_DIR_HINT]) from exc
    if busy:
        raise UsageError(f"download directory is not empty: {target}", command="models",
                         remediation=[f"{_DIR_HINT} Existing files are never overwritten."])


def _publish(src: Path, dst: Path) -> None:
    """Move `src` to `dst` without ever replacing an existing `dst`: a hard link fails on an
    existing name atomically; file systems without hard links get an exclusive-create copy.
    A `dst` this call created is removed again if the move fails part-way."""
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError:
        with src.open("rb") as reader, dst.open("xb") as writer:
            try:
                shutil.copyfileobj(reader, writer, 1 << 20)
            except BaseException:
                dst.unlink(missing_ok=True)
                raise
    try:
        src.unlink()
    except BaseException:
        dst.unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class WrittenFile:
    """One file a download wrote: its (unique, sanitised) name in the target directory."""

    name: str
    size: int
    sha256: str
    kind: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _select_files(model: Model, select: Sequence[str]) -> Model:
    """`model` restricted to the files named in `select` (exact names, case-sensitive)."""
    wanted = list(dict.fromkeys(select))
    names = {f.name for f in model.files}
    unknown = [n for n in wanted if n not in names]
    if unknown:
        raise UsageError(f"{model.site} #{model.id} has no file named {', '.join(map(repr, unknown))}",
                         command="models", remediation=["Available: " + ", ".join(sorted(names))])
    return replace(model, files=tuple(f for f in model.files if f.name in wanted))


def download(url: str, dest: Path | None, *, select: Sequence[str] | None = None
             ) -> tuple[Model, Path, list[WrittenFile]]:
    """Fetch every file of the model at `url` (or only the `select`ed names) into `dest` and
    write `dest/model.json`.
    Paid models are refused before any file request. The directory itself is never replaced
    (an existing empty one keeps its mode, owner and ACLs): files are staged in a hidden
    `.part-*` directory inside it and moved in only once all of them and model.json are
    complete; a failure removes everything this run created and never touches other files."""
    client, ref = resolve(url)
    if dest is not None:
        _check_target(dest)  # before any request: a busy --dir is a usage error, not auth/paid
    model = client.show(ref)
    if model.paid:
        raise MarketplaceError(model.site, "paid",
                               f"'{model.title}' is a paid model ({model.price or 'paid'}); "
                               "3d models only downloads free files",
                               remediation=[f"Buy it on the site and download it there: {model.url}"])
    if select:
        if client.file_link is None:
            raise UsageError(f"{model.site} serves every file in one archive; --file cannot pick members",
                             command="models", remediation=["Download without --file."])
        model = _select_files(model, select)
    target = dest or default_dir(model)
    _check_target(target)
    links = client.download_links(model)
    if not links:
        raise MarketplaceError(model.site, "not-found", f"'{model.title}' lists no downloadable files")
    created = False
    try:
        try:
            target.mkdir(parents=True)
            created = True
        except FileExistsError:
            _check_target(target)
        staging = Path(tempfile.mkdtemp(prefix=".part-", dir=target))
    except OSError as exc:
        if created:
            with contextlib.suppress(OSError):
                target.rmdir()
        raise UsageError(f"cannot create the download directory {target}: {exc.strerror or exc}",
                         command="models", remediation=["Pass a --dir inside a writable directory."]) from exc
    published: list[Path] = []
    try:
        written: list[WrittenFile] = []
        used: set[str] = {"model.json"}
        for file, link, headers in links:
            name = _unique(safe_name(file.name or Path(link.split("?", 1)[0]).name), used)
            size = transport.download(model.site, link, staging / name, headers=headers)
            written.append(WrittenFile(name, size, _sha256(staging / name), file.kind))
        meta = {
            "source": {"site": model.site, "id": model.id, "url": model.url},
            "title": model.title,
            "author": {"name": model.author, "url": model.author_url},
            "license": model.license,
            "price": model.price or "free",
            "tags": list(model.tags),
            "summary": model.summary,
            "downloaded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "files": [w.to_json() for w in written],
        }
        (staging / "model.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
                                            encoding="utf-8")
        for name in [w.name for w in written] + ["model.json"]:
            try:
                _publish(staging / name, target / name)
            except OSError as exc:  # something appeared in the target meanwhile: never overwrite
                raise UsageError(f"could not move {name} into {target}: {exc.strerror or exc}",
                                 command="models", remediation=[_DIR_HINT]) from exc
            published.append(target / name)
        staging.rmdir()
    except BaseException:
        for path in published:
            path.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
        if created:
            with contextlib.suppress(OSError):  # someone else's file landed there: leave it
                target.rmdir()
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
