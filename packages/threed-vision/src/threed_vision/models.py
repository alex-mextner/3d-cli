"""Model registry: pinned files + sha256, offline-first lookup, explicit atomic `fetch`.

Rules (the contract every stage relies on):
  * Models are NEVER downloaded implicitly. `model_path()` only looks at the cache; a missing, truncated or
    corrupt file raises `MissingDependency` whose remediation is `3d vision-models pull <name>`.
  * `fetch()` is the only function that touches the network. It is idempotent (verified files are skipped),
    atomic (download to a `.part` file, verify size + sha256, `os.replace`) and prefers importing an existing
    local copy (hard link, else copy) over downloading.
  * Verification is a sha256 pass the first time a file is seen; the result is cached in
    `<models_dir>/.verified.json` keyed by size + mtime_ns, so later lookups are a `stat()`.
  * Cache dir: env `THREED_VISION_MODELS_DIR`, default `$XDG_CACHE_HOME|~/.cache` + `/3d-cli/vision/models`.

Importable legacy copies (spike / earlier 3d-cli research caches) are listed in `legacy_roots()`; the env
`THREED_VISION_IMPORT_DIRS` (os.pathsep separated) replaces that list (set it empty to disable importing).

The ONNX encoders are the public Xenova exports (revision pinned below); the sha256 of the files that were already
in the 3d-cli research cache equals the Hugging Face LFS oid of those exports, so no local export step exists.
`clipb32_vision_fp16.onnx` is intentionally not registered: it does not load with default ORT graph optimisation
and the fp32 tower already runs at ~4 ms/crop.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import shutil
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .errors import BadInput, MissingDependency, VisionError

Progress = Callable[
    [str, str, int, int], None
]  # (model, file, done_bytes, total_bytes)
FileState = Literal["ok", "unverified", "missing", "corrupt"]

OWLV2_REV = "cfd3195ba4ea9592eec887ded089f4c08eff231d"
CLIP_REV = "d15189d7028b43f1d3e65039190477f6af591c2a"
DINO_REV = "c2bb04a51fab207c420665f1946016107bffc701"
_OWL_URL = (
    f"https://huggingface.co/google/owlv2-base-patch16-ensemble/resolve/{OWLV2_REV}/"
)
_CLIP_URL = f"https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/{CLIP_REV}/"
_DINO_URL = f"https://huggingface.co/Xenova/dinov2-small/resolve/{DINO_REV}/"
_REMBG_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/"
_OWL_DIR = "owlv2-base-patch16-ensemble"


@dataclass(frozen=True)
class ModelFile:
    rel: str  # path relative to models_dir()
    sha256: str
    size: int
    url: str


@dataclass(frozen=True)
class ModelSpec:
    name: str
    description: str
    licence: str
    primary: (
        str  # what model_path(name) returns: a file, or the directory for HF snapshots
    )
    files: tuple[ModelFile, ...]

    @property
    def size_bytes(self) -> int:
        return sum(f.size for f in self.files)


def _owl(name: str, size: int, sha: str) -> ModelFile:
    return ModelFile(f"{_OWL_DIR}/{name}", sha, size, _OWL_URL + name)


MODELS: dict[str, ModelSpec] = {
    "sam2-tiny": ModelSpec(
        "sam2-tiny",
        "SAM 2.1 Hiera-Tiny checkpoint: automatic masks + point/box prompts",
        "Apache-2.0 (Meta SAM 2)",
        "sam2.1_hiera_tiny.pt",
        (
            ModelFile(
                "sam2.1_hiera_tiny.pt",
                "7402e0d864fa82708a20fbd15bc84245c2f26dff0eb43a4b5b93452deb34be69",
                156008466,
                "https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_tiny.pt",
            ),
        ),
    ),
    "owlv2-base": ModelSpec(
        "owlv2-base",
        "OWLv2 base patch16 ensemble (open-vocabulary boxes); HF snapshot loaded with local_files_only",
        "Apache-2.0 (Google OWLv2)",
        _OWL_DIR,
        (
            _owl(
                "added_tokens.json",
                67,
                "e5dc0da35d20111e8ff3fdfc03682beca23d5f94ed74331bce81786b2636a24f",
            ),
            _owl(
                "config.json",
                414,
                "ba9df8c25a4b8461887dd0a93d9252c9cd84697fe8d49a9d8794ce409af9acb2",
            ),
            _owl(
                "merges.txt",
                524619,
                "9fd691f7c8039210e0fced15865466c65820d09b63988b0174bfe25de299051a",
            ),
            _owl(
                "preprocessor_config.json",
                425,
                "cf3e396635b797ee1a464e1b2836e98748f8edac19e89aaa2c93b55ac15b0064",
            ),
            _owl(
                "special_tokens_map.json",
                121,
                "d6e2b9cf664efbad2d22998b8d3da986abcbeed3e0825ad33605c9401f9cf73e",
            ),
            _owl(
                "tokenizer_config.json",
                1100,
                "b55cda6198e152ded427c8a9b3faf1cccf27a7fa080697a62f6ff143f511f44f",
            ),
            _owl(
                "vocab.json",
                1059962,
                "e089ad92ba36837a0d31433e555c8f45fe601ab5c221d4f607ded32d9f7a4349",
            ),
            _owl(
                "model.safetensors",
                619918824,
                "e1e130b9e404cf91a75ad45644c1da9d7fa5284085eecc864266a6923efb99e7",
            ),
        ),
    ),
    "clip-b32": ModelSpec(
        "clip-b32",
        "CLIP ViT-B/32 ONNX (fp32 vision tower, text tower, tokenizer; Xenova export)",
        "MIT (OpenAI CLIP; ONNX export by Xenova)",
        "clipb32_vision.onnx",
        (
            ModelFile(
                "clipb32_vision.onnx",
                "fd6e1402a588279d1723c7534d4bcba5bc0b14b47dfab0e46f8c47b8270d7d40",
                351685709,
                _CLIP_URL + "onnx/vision_model.onnx",
            ),
            ModelFile(
                "clip_text.onnx",
                "3f6571f5bad13a97c469c1622e1cfc4d9aef78b79fdbfcff804ca357bfada8cc",
                254058553,
                _CLIP_URL + "onnx/text_model.onnx",
            ),
            ModelFile(
                "clip_tokenizer.json",
                "f7f3b7af117d467b58374797691a6438d3e6b9e9cef800dfd5dced7f697a90cd",
                2224119,
                _CLIP_URL + "tokenizer.json",
            ),
        ),
    ),
    "dinov2-s": ModelSpec(
        "dinov2-s",
        "DINOv2-small ONNX image encoder (Xenova export)",
        "Apache-2.0 (Meta DINOv2; ONNX export by Xenova)",
        "dinov2s.onnx",
        (
            ModelFile(
                "dinov2s.onnx",
                "83141175ec78b4ff9a2bb58a4c7c264ba0054d1c2e122e5a8114b79a8d4179ea",
                88459888,
                _DINO_URL + "onnx/model.onnx",
            ),
        ),
    ),
    "u2net": ModelSpec(
        "u2net",
        "U2-Net salient-object matte (rembg ONNX) for the single-object fast path; u2netp is the 4.6 MB variant",
        "Apache-2.0 (U2-Net), MIT (rembg release)",
        "u2net.onnx",
        (
            ModelFile(
                "u2net.onnx",
                "8d10d2f3bb75ae3b6d527c77944fc5e7dcd94b29809d47a739a7a728a912b491",
                175997641,
                _REMBG_URL + "u2net.onnx",
            ),
            ModelFile(
                "u2netp.onnx",
                "309c8469258dda742793dce0ebea8e6dd393174f89934733ecc8b14c76f4ddd8",
                4574861,
                _REMBG_URL + "u2netp.onnx",
            ),
        ),
    ),
}


def _cache_home() -> Path:
    env = os.environ.get("XDG_CACHE_HOME")
    return Path(env).expanduser() if env else Path.home() / ".cache"


def models_dir() -> Path:
    """`$THREED_VISION_MODELS_DIR`, else `$XDG_CACHE_HOME|~/.cache` + `/3d-cli/vision/models` (same base as the vision venv)."""
    env = os.environ.get("THREED_VISION_MODELS_DIR")
    return (
        Path(env).expanduser()
        if env
        else _cache_home() / "3d-cli" / "vision" / "models"
    )


# Copies made before this registry existed (spike weights, the hsw-editor HF/u2net caches). Migration shims: they are only
# ever READ, as import sources for `fetch`; relative to the cache home.
_LEGACY_SUBPATHS = (
    "hsw-editor/spikes/vision/weights",
    "hsw-editor/u2net",
    f"hsw-editor/hf/hub/models--google--owlv2-base-patch16-ensemble/snapshots/{OWLV2_REV}",
)


def legacy_roots() -> list[Path]:
    """Directories searched for an importable copy (`<root>/<rel>`, then `<root>/<basename>`).

    `$THREED_VISION_IMPORT_DIRS` (os.pathsep separated) REPLACES the built-in locations when set, even to the empty
    string, which disables importing altogether.
    """
    env = os.environ.get("THREED_VISION_IMPORT_DIRS")
    if env is not None:
        return [Path(p).expanduser() for p in env.split(os.pathsep) if p]
    return [
        models_dir() / "research",
        *(_cache_home() / sub for sub in _LEGACY_SUBPATHS),
    ]


def _spec(name: str) -> ModelSpec:
    try:
        return MODELS[name]
    except KeyError:
        raise BadInput(
            f"unknown vision model '{name}'", f"one of: {', '.join(sorted(MODELS))}"
        ) from None


def _pull_hint(name: str) -> str:
    return f"3d vision-models pull {name}"


# --- locking ------------------------------------------------------------------------------------------------


@contextlib.contextmanager
def _flock(name: str) -> Iterator[None]:
    """Exclusive advisory lock on `<models_dir>/<name>`; a no-op when the cache dir is not writable (nothing to protect)."""
    import fcntl  # POSIX only; imported here so merely importing the registry works everywhere

    try:
        fh = open(models_dir() / name, "a")  # noqa: SIM115 - closed by the with below
    except OSError:
        yield
        return
    with fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# --- verification -------------------------------------------------------------------------------------------

_MARKER = ".verified.json"
_MARKER_LOCK = ".verified.lock"
# (abs path, size, mtime_ns) -> sha256 verified by this process
_session: dict[tuple[str, int, int], str] = {}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_marker() -> dict[str, Any]:
    try:
        data = json.loads((models_dir() / _MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _remember(f: ModelFile, st: os.stat_result) -> None:
    """Record that `f` (as stat'ed) has its pinned sha256: in memory and, best effort, in the on-disk marker."""
    _session[(str(models_dir() / f.rel), st.st_size, st.st_mtime_ns)] = f.sha256
    root = models_dir()
    tmp = root / f"{_MARKER}.{os.getpid()}.tmp"
    with _flock(
        _MARKER_LOCK
    ):  # read-modify-write: without the lock a concurrent verifier's entry could be dropped
        marker = _read_marker()
        marker[f.rel] = {
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "sha256": f.sha256,
        }
        try:
            tmp.write_text(
                json.dumps(marker, indent=1, sort_keys=True) + "\n", encoding="utf-8"
            )
            os.replace(tmp, root / _MARKER)
        except OSError:  # read-only cache: verification just is not cached
            with contextlib.suppress(OSError):
                tmp.unlink()


def _file_state(f: ModelFile, *, verify: bool) -> FileState:
    """'ok' (sha256 verified, cached), 'unverified' (present, size right, not hashed), 'missing', 'corrupt'."""
    path = models_dir() / f.rel
    try:
        st = path.stat()
    except OSError:
        return "missing"
    if st.st_size != f.size:
        return "corrupt"
    if _session.get((str(path), st.st_size, st.st_mtime_ns)) == f.sha256:
        return "ok"
    m = _read_marker().get(f.rel)
    if (
        isinstance(m, dict)
        and m.get("size") == st.st_size
        and m.get("mtime_ns") == st.st_mtime_ns
        and m.get("sha256") == f.sha256
    ):
        _session[(str(path), st.st_size, st.st_mtime_ns)] = f.sha256
        return "ok"
    if not verify:
        return "unverified"
    if _sha256(path) != f.sha256:
        return "corrupt"
    _remember(f, st)
    return "ok"


def _select_files(spec: ModelSpec, rel: str | None) -> tuple[ModelFile, ...]:
    if rel is None:
        return spec.files
    exact = tuple(f for f in spec.files if f.rel == rel)
    if exact:
        return exact
    by_name = tuple(f for f in spec.files if Path(f.rel).name == rel)
    if len(by_name) == 1:
        return by_name
    known = ", ".join(f.rel for f in spec.files)
    raise BadInput(
        f"model '{spec.name}' has no unambiguous file '{rel}'", f"one of: {known}"
    )


def model_path(name: str, rel: str | None = None) -> Path:
    """Verified local path of a model (`rel` selects one file of a multi-file model; default = the primary path).

    Raises MissingDependency naming `3d vision-models pull <name>` when any required file is absent or fails its sha256.
    Never touches the network.
    """
    spec = _spec(name)
    wanted = _select_files(spec, rel)
    for f in wanted:
        state = _file_state(f, verify=True)
        if state != "ok":
            what = (
                "is not installed"
                if state == "missing"
                else "is corrupt or truncated (sha256/size mismatch)"
            )
            raise MissingDependency(
                f"vision model '{name}': {f.rel} {what} in {models_dir()}",
                _pull_hint(name),
            )
    return models_dir() / (wanted[0].rel if rel is not None else spec.primary)


def status(*, verify: bool = False) -> dict[str, Any]:
    """Per-model install state. `verify=True` hashes unverified files (first pass is slow, then cached)."""
    out: dict[str, Any] = {}
    for name, spec in MODELS.items():
        files = [
            {
                "rel": f.rel,
                "size": f.size,
                "sha256": f.sha256,
                "state": _file_state(f, verify=verify),
            }
            for f in spec.files
        ]
        out[name] = {
            "installed": all(x["state"] in ("ok", "unverified") for x in files),
            "verified": all(x["state"] == "ok" for x in files),
            "path": str(models_dir() / spec.primary),
            "size_bytes": spec.size_bytes,
            "licence": spec.licence,
            "description": spec.description,
            "files": files,
            "pull": _pull_hint(name),
        }
    return {"models_dir": str(models_dir()), "models": out}


# --- fetch --------------------------------------------------------------------------------------------------


def _verified_legacy(f: ModelFile) -> Path | None:
    """First importable copy whose size AND sha256 match (a bad copy never hides a good one in a later root)."""
    seen: set[Path] = set()
    for root in legacy_roots():
        for cand in (root / f.rel, root / Path(f.rel).name):
            try:
                real = cand.resolve()
                if real in seen or not real.is_file() or real.stat().st_size != f.size:
                    continue
                seen.add(real)
                if _sha256(real) == f.sha256:
                    return real
            except OSError:
                continue
    return None


def _part_path(dest: Path) -> Path:
    return dest.with_name(f"{dest.name}.part{os.getpid()}")


def _sweep_parts(root: Path) -> None:
    """Remove `*.part<pid>` leftovers of killed pulls. Only called under the fetch lock: no pull is running."""
    for p in root.rglob("*.part*"):
        if p.suffix.startswith(".part") and p.suffix[5:].isdigit():
            with contextlib.suppress(OSError):
                p.unlink()


def _local_error(action: str, dest: Path, exc: OSError) -> VisionError:
    return VisionError(
        f"cannot {action} {dest.name} in {dest.parent}: {exc}",
        "free disk space / fix permissions of the models directory, then re-run `3d vision-models pull`",
    )


def _place_atomic(src: Path, dest: Path) -> str:
    """Hard link (same filesystem) else copy `src` to a temp sibling of `dest`, then rename over `dest`."""
    tmp = _part_path(dest)
    try:
        try:
            os.link(src, tmp)
            how = "linked"
        except OSError:
            shutil.copyfile(src, tmp)
            how = "copied"
        os.replace(tmp, dest)
    except OSError as exc:
        raise _local_error("import", dest, exc) from exc
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
    return how


_NETWORK_ERRORS = (
    urllib.error.URLError,
    http.client.HTTPException,
    ConnectionError,
    TimeoutError,
    ssl.SSLError,
)


def _download(f: ModelFile, name: str, dest: Path, progress: Progress | None) -> None:
    """Stream `f.url` to a temp file, verify size + sha256, rename into place. Network and disk failures are reported as such."""
    tmp = _part_path(dest)
    req = urllib.request.Request(
        f.url, headers={"User-Agent": "threed-vision-models/1"}
    )
    h = hashlib.sha256()
    done = 0
    try:
        try:
            with (
                urllib.request.urlopen(req, timeout=60) as resp,
                open(tmp, "wb") as out,
            ):  # pinned https URLs
                while chunk := resp.read(1 << 20):
                    out.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(name, f.rel, done, f.size)
        except _NETWORK_ERRORS as exc:
            raise MissingDependency(
                f"cannot download {f.rel} from {f.url}: {exc}",
                f"check the network, or copy the file into {dest.parent} yourself and re-run `3d vision-models pull` "
                "(or set THREED_VISION_IMPORT_DIRS to a directory that already holds it)",
            ) from exc
        except (
            OSError
        ) as exc:  # opening / writing the temp file: the disk, not the network
            raise _local_error("write", dest, exc) from exc
        if done < f.size:
            raise MissingDependency(
                f"download of {f.rel} from {f.url} was cut short ({done} of {f.size} bytes)",
                "re-run `3d vision-models pull` (the connection dropped)",
            )
        if done != f.size or h.hexdigest() != f.sha256:
            raise VisionError(
                f"downloaded {f.rel} from {f.url} fails verification (got {done} bytes sha256 {h.hexdigest()[:16]}..., "
                f"expected {f.size} bytes sha256 {f.sha256[:16]}...)",
                "the pinned upstream file changed or a proxy rewrote it; retry, or import a known-good copy via THREED_VISION_IMPORT_DIRS",
            )
        try:
            os.replace(tmp, dest)
        except OSError as exc:
            raise _local_error("install", dest, exc) from exc
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def fetch(
    names: list[str] | None = None, progress: Progress | None = None
) -> dict[str, Any]:
    """Make the named models (default: all) present and verified. The ONLY function that may use the network.

    Per file: already verified -> 'present'; a verified local copy found in `legacy_roots()` -> 'imported-linked' (hard
    link) or 'imported-copied'; otherwise 'downloaded' from the pinned URL. Idempotent and atomic; stale `.part<pid>`
    leftovers of killed pulls are removed first.
    """
    specs = [_spec(n) for n in (names if names else list(MODELS))]
    root = models_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _local_error("create", root / ".", exc) from exc
    results: dict[str, dict[str, str]] = {}
    with _flock(".fetch.lock"):
        _sweep_parts(root)
        for spec in specs:
            res = results.setdefault(spec.name, {})
            for f in spec.files:
                dest = root / f.rel
                if _file_state(f, verify=True) == "ok":
                    res[f.rel] = "present"
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                legacy = _verified_legacy(f)
                if legacy is not None:
                    res[f.rel] = "imported-" + _place_atomic(legacy, dest)
                else:
                    _download(f, spec.name, dest, progress)
                    res[f.rel] = "downloaded"
                _remember(
                    f, dest.stat()
                )  # content was verified above (copy source hashed / download hashed while streaming)
    return {"models_dir": str(root), "results": results}
