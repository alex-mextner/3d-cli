"""The project side of `3d models publish`: load `publish.yaml` and pick the files to publish.

Project layout (the garage-band convention): sources and final STLs at the project root, the
sliced project 3mf in `print/`, renders in `previews/`, third-party material in `references/`.
Only the root and `print/` are scanned for model files, never `references/` (that is somebody
else's code), `print/runs/` or G-code. Every publication must carry the 3mf and the scad
source; any STL and any other CAD file found (Fusion .f3d/.f3z, STEP, Blender .blend) goes
along.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from errors import InputNotFound, InvalidArgument, UsageError
from publishing.licenses import LICENSES, License, parse_license

SPEC_NAME = "publish.yaml"
SCAN_DIRS = (".", "print")
IMAGE_DIR = "previews"
IMAGE_EXTS = (".png", ".jpg", ".jpeg")
ROLE_BY_EXT = {
    ".3mf": "3mf",
    ".scad": "scad",
    ".stl": "stl",
    ".f3d": "cad",
    ".f3z": "cad",
    ".step": "cad",
    ".stp": "cad",
    ".blend": "cad",
}
ROLE_ORDER = ("3mf", "scad", "stl", "cad")
KEYS = (
    "title",
    "description",
    "summary",
    "tags",
    "license",
    "category",
    "remix_of",
    "remix_license",
    "permission_ref",
    "images",
    "hosted",
)


@dataclass(frozen=True)
class PublishFile:
    path: Path
    name: str  # the name it is uploaded under (the basename)
    role: str  # 3mf | scad | stl | cad
    size: int


@dataclass(frozen=True)
class FileSet:
    files: tuple[PublishFile, ...]
    clashes: tuple[str, ...]  # basenames that two different directories both provide

    def of(self, role: str) -> tuple[PublishFile, ...]:
        return tuple(f for f in self.files if f.role == role)

    def missing(self) -> tuple[str, ...]:
        """The mandatory roles nothing in the project provides."""
        return tuple(role for role in ("3mf", "scad") if not self.of(role))


@dataclass(frozen=True)
class PublishSpec:
    title: str
    description: str
    summary: str
    tags: tuple[str, ...]
    license: License
    category: str
    remix_of: str
    remix_license: str
    permission_ref: str
    images: tuple[Path, ...]
    hosted: tuple[
        tuple[str, str], ...
    ] = ()  # (file name, public https URL) pairs for Cults
    defaults_used: tuple[
        str, ...
    ] = ()  # keys the offer filled in because publish.yaml left them out


def select_files(project: Path) -> FileSet:
    found: list[PublishFile] = []
    for sub in SCAN_DIRS:
        folder = project / sub
        if not folder.is_dir() or (sub != "." and folder.is_symlink()):
            continue  # a symlinked folder must not smuggle in files from outside
        for path in sorted(folder.iterdir()):
            role = ROLE_BY_EXT.get(path.suffix.lower())
            if (
                role is None
                or path.name.startswith(".")
                or path.is_symlink()
                or not path.is_file()
            ):
                continue
            found.append(PublishFile(path, path.name, role, path.stat().st_size))
    found.sort(key=lambda f: (ROLE_ORDER.index(f.role), f.name.casefold()))
    return FileSet(tuple(found), name_clashes(found))


def name_clashes(files: Sequence[PublishFile]) -> tuple[str, ...]:
    """Basenames (case-folded) that two different files of `files` both carry."""
    seen: dict[str, Path] = {}
    clashes: list[str] = []
    for f in files:
        key = f.name.casefold()
        if key in seen and seen[key] != f.path:
            clashes.append(f.name)
        seen.setdefault(key, f.path)
    return tuple(dict.fromkeys(clashes))


def _text(data: dict[str, Any], key: str, *, required: bool = False) -> str:
    value = data.get(key)
    if isinstance(value, str):
        value = value.strip()
    if value is None or value == "":
        if required:
            raise InvalidArgument(
                f"{SPEC_NAME}: {key}",
                "(missing)",
                ["a non-empty text"],
                command="models",
            )
        return ""
    if not isinstance(value, str):
        raise InvalidArgument(
            f"{SPEC_NAME}: {key}", repr(value), ["a text"], command="models"
        )
    return value


def default_images(project: Path) -> tuple[Path, ...]:
    """The listing images of `<project>/previews/`: regular image files that stay inside the project (a
    symlinked folder or file is skipped so it cannot smuggle in a file from outside)."""
    folder = project / IMAGE_DIR
    if not folder.is_dir() or folder.is_symlink():
        return ()
    root = project.resolve()
    return tuple(
        p
        for p in sorted(folder.iterdir())
        if p.suffix.lower() in IMAGE_EXTS
        and p.is_file()
        and not p.name.startswith(".")
        and not p.is_symlink()
        and root
        in p.resolve().parents  # a symlink must not smuggle in a file from outside
    )


def _images(project: Path, data: dict[str, Any]) -> tuple[Path, ...]:
    declared = data.get("images")
    if declared is None:
        return default_images(project)
    if not isinstance(declared, list) or not all(isinstance(i, str) for i in declared):
        raise InvalidArgument(
            f"{SPEC_NAME}: images",
            repr(declared),
            ["a list of paths"],
            command="models",
        )
    root = project.resolve()
    images: list[Path] = []
    for rel in declared:
        path = (project / rel).resolve()
        if root not in path.parents or "references" in path.relative_to(root).parts:
            raise InvalidArgument(
                f"{SPEC_NAME}: images",
                rel,
                ["paths inside the project directory"],
                command="models",
            )
        if path.suffix.lower() not in IMAGE_EXTS:
            raise InvalidArgument(
                f"{SPEC_NAME}: images",
                rel,
                [f"a {'/'.join(IMAGE_EXTS)} file"],
                command="models",
            )
        if not path.is_file():
            raise InputNotFound(str(project / rel), command="models")
        images.append(path)
    return tuple(images)


def _tags(data: dict[str, Any]) -> tuple[str, ...]:
    raw_tags = data.get("tags")
    if raw_tags is None:
        return ()
    if not isinstance(raw_tags, list) or not all(
        isinstance(t, str) and t.strip() for t in raw_tags
    ):
        raise InvalidArgument(
            f"{SPEC_NAME}: tags",
            repr(raw_tags),
            ["a list of non-empty texts"],
            command="models",
        )
    return tuple(t.strip() for t in raw_tags)


def _is_public_https(url: str) -> bool:
    """A usable hosted URL: https, a host, no whitespace, no userinfo."""
    from urllib.parse import urlsplit

    if any(c.isspace() for c in url):
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    return parts.scheme == "https" and bool(host) and "@" not in parts.netloc


def _hosted(data: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """`hosted:` maps an uploaded file's name to the public HTTPS URL it is reachable at (Cults pulls its
    files and images from URLs; it takes no upload)."""
    raw = data.get("hosted")
    if raw is None:
        return ()
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and _is_public_https(v)
        for k, v in raw.items()
    ):
        raise InvalidArgument(
            f"{SPEC_NAME}: hosted",
            repr(raw),
            ["a mapping of file name to a public https:// URL"],
            command="models",
        )
    return tuple(raw.items())


def _read_mapping(project: Path, *, allow_missing: bool = False) -> dict[str, Any]:
    import yaml  # lazy: only `publish` needs it

    path = project / SPEC_NAME
    if not path.is_file():
        if allow_missing:
            return {}
        raise InputNotFound(str(path), command="models")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise UsageError(
            f"{path} is not valid YAML: {exc}",
            command="models",
            remediation=[f"Fix {SPEC_NAME}; accepted keys: {', '.join(KEYS)}."],
        ) from exc
    if not isinstance(data, dict):
        raise UsageError(
            f"{path} must be a mapping of keys to values",
            command="models",
            remediation=[f"Accepted keys: {', '.join(KEYS)}."],
        )
    unknown = sorted(str(k) for k in data if k not in KEYS)
    if unknown:
        raise UsageError(
            f"{SPEC_NAME}: unknown key(s) {', '.join(unknown)}",
            command="models",
            remediation=[f"Accepted keys: {', '.join(KEYS)}."],
        )
    return data


def listing_images(project: Path, *, allow_missing: bool = False) -> tuple[Path, ...]:
    """The listing images of an original/remix post (publish.yaml `images`, else previews/)."""
    return _images(project, _read_mapping(project, allow_missing=allow_missing))


def load_spec(
    project: Path,
    defaults: Mapping[str, str] | None = None,
    *,
    with_images: bool = True,
) -> PublishSpec:
    """Read and validate `<project>/publish.yaml`; every defect is a structured exit-2 error.

    `defaults` (the publish OFFER only) fills title / description / license that publish.yaml leaves out, or
    stands in for a missing publish.yaml; every key it supplied is listed in `PublishSpec.defaults_used` so
    the offer can say what it assumed. `3d models publish` passes none: it stays strict."""
    data = _read_mapping(project, allow_missing=bool(defaults))
    used: list[str] = []
    for key, value in (defaults or {}).items():
        if data.get(key) in (None, ""):
            data[key] = value
            used.append(key)
    license_text = _text(data, "license", required=True)
    license_ = parse_license(license_text)
    if license_ is None:
        raise InvalidArgument(
            f"{SPEC_NAME}: license", license_text, list(LICENSES), command="models"
        )
    remix_of = _text(data, "remix_of")
    if remix_of and not remix_of.startswith(("http://", "https://")):
        raise InvalidArgument(
            f"{SPEC_NAME}: remix_of",
            remix_of,
            ["an http(s) URL of the original"],
            command="models",
        )
    return PublishSpec(
        title=_text(data, "title", required=True),
        description=_text(data, "description", required=True),
        summary=_text(data, "summary"),
        tags=_tags(data),
        license=license_,
        category=_text(data, "category"),
        remix_of=remix_of,
        remix_license=_text(data, "remix_license"),
        permission_ref=_text(data, "permission_ref"),
        images=_images(project, data) if with_images else (),
        hosted=_hosted(data),
        defaults_used=tuple(used),
    )
