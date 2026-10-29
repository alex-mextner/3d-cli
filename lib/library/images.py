"""library/images.py — thumbnails of search results for `3d models search --images`.

WHAT: `render_images()` returns `{image URL: terminal escape sequence}` for the covers of a result list: it
fetches each image (bounded parallelism, a wall-clock budget, a per-image size cap), shrinks it to a terminal
thumbnail and encodes it for the chosen protocol (`cli.termimage`). Covers are often multi-megabyte PNGs, so the
originals live only in a temp dir; what is cached on disk (`<cache dir>/model-images/<key>`) is the finished escape
sequence, keyed by URL + protocol + width. Pillow does the resizing; when the `3d` interpreter (the system Python)
has none, the same encoding runs in `lib/image_thumbs.py` through `cli.pyrun`.

ACCESSED VIA: `commands/models.py` (`search --images`, `searches show --images`).

INVARIANTS: only http(s) URLs (`marketplaces.transport` refuses anything else); a cache hit makes no request; no
failure raises: an unreachable or undecodable image is simply absent from the result and the caller prints a text
marker instead.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cli import paths, termimage
from cli.resources import pool_size
from errors import ThreeDError
from marketplaces import transport
from marketplaces.base import Deadline

MAX_IMAGE_BYTES = 12 * 1024 * 1024
BUDGET_S = 30.0
WORKERS = 4
TOOL_DEPS = "pillow"
TOOL_TIMEOUT_S = 120.0


def cache_path(url: str, protocol: str, cols: int) -> Path:
    key = hashlib.sha1(f"{protocol}:{cols}:{url}".encode()).hexdigest()
    return paths.cache_dir() / "model-images" / f"{key}.seq"


def _fetch(site: str, url: str, dest: Path, deadline: Deadline) -> bool:
    if deadline.expired():
        return False
    try:
        response = transport.fetch(site, url, headers={"Accept": "image/*"})
    except ThreeDError:
        return False
    if (
        not 200 <= response.status < 300
        or not response.body
        or len(response.body) > MAX_IMAGE_BYTES
    ):
        return False
    dest.write_bytes(response.body)
    return True


def _pillow_here() -> bool:
    import importlib.util

    return importlib.util.find_spec("PIL") is not None


def _encode(files: dict[str, Path], protocol: str, cols: int) -> dict[str, str]:
    """`{url: sequence}` for the downloaded `files`; in-process with Pillow, else through the thumbnail tool."""
    if not files:
        return {}
    if _pillow_here():
        out: dict[str, str] = {}
        for url, path in files.items():
            sequence = termimage.encode(path.read_bytes(), protocol, cols=cols)
            if sequence:
                out[url] = sequence
        return out
    from cli.pyrun import tool_argv

    try:
        argv = tool_argv(
            TOOL_DEPS,
            "image_thumbs.py",
            [protocol, str(cols), *map(str, files.values())],
        )
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=TOOL_TIMEOUT_S, check=False
        )
        by_path = (
            json.loads(proc.stdout.strip().splitlines()[-1])
            if proc.returncode == 0
            else {}
        )
    except (ThreeDError, OSError, subprocess.SubprocessError, ValueError, IndexError):
        return {}
    return {
        url: by_path[str(path)] for url, path in files.items() if by_path.get(str(path))
    }


def render_images(
    wanted: Sequence[tuple[str, str]],
    protocol: str,
    *,
    cols: int = 20,
    budget_s: float = BUDGET_S,
) -> dict[str, str]:
    """`{url: escape sequence}` for the `(site, url)` pairs that could be fetched and encoded within the budget."""
    unique = list(dict.fromkeys((site, url) for site, url in wanted if url))
    result: dict[str, str] = {}
    missing: list[tuple[str, str]] = []
    for site, url in unique:
        cached = cache_path(url, protocol, cols)
        try:
            result[url] = cached.read_text(encoding="utf-8")
        except OSError:
            missing.append((site, url))
    if not missing:
        return result
    deadline = Deadline(budget_s)
    with tempfile.TemporaryDirectory(prefix="3d-images-") as tmp:
        targets = {url: Path(tmp) / f"{n}.img" for n, (_, url) in enumerate(missing)}
        with ThreadPoolExecutor(
            max_workers=min(WORKERS, pool_size(len(missing)))
        ) as pool:
            fetched = list(
                pool.map(
                    lambda pair: _fetch(pair[0], pair[1], targets[pair[1]], deadline),
                    missing,
                )
            )
        files = {
            url: targets[url]
            for (_, url), ok in zip(missing, fetched, strict=True)
            if ok
        }
        for url, sequence in _encode(files, protocol, cols).items():
            result[url] = sequence
            target = cache_path(url, protocol, cols)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(sequence, encoding="utf-8")
            except OSError:
                continue  # a read-only cache dir only costs the next run a re-fetch
    return result
