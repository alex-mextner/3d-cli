"""project.py — the `3d.yaml` project model + loader (ROADMAP §5/§15: the project spine).

ACCESSED VIA: `3d init` (writes one), `3d pack`/`3d slice`/`3d check`/`3d strength` (read
parts + material/printer by name), `3d projects` (registers the path), and the AI tools
(scope by part). This is the headless-core data structure (§20) every higher-level command
sits on; it has NO argv/printing — callers raise/format.

INVARIANTS:
  - A project is the directory containing a `3d.yaml`. The CLI finds the NEAREST one walking
    up from cwd (§15) — like git finds `.git` — so commands work from any subdir.
  - Every path in `3d.yaml` (a part's `file`) is resolved RELATIVE TO THE 3d.yaml's DIRECTORY
    (§15), never to cwd. `Part.path` is the resolved absolute path; `Part.file` keeps the raw
    string for round-tripping.
  - Materials and printers are referenced BY NAME only (§2a); this loader does not resolve
    them — it stores the names. The registries (materials.yaml/printers.yaml) resolve them.
  - `Project.raw` keeps the full parsed dict so forward-compatible keys (anchors, loads,
    stylesheet, lint, …) survive a load→use cycle even before this model grows fields for them.

SCOPE (deliberately the spine, not the whole §5): project + parts + the common per-part
fields. The object-model selector engine, stylesheet cascade and op-DAG (§5/§18/§19) are a
separate, larger build and are NOT here.
"""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field
from typing import Any

from errors import MissingDependency, ThreeDError

PROJECT_FILE = "3d.yaml"

# The standard, combinable part tags (§5). Not a closed enum — unknown tags are allowed
# (a project may define its own), but these are the documented vocabulary.
STANDARD_TAGS = (
    "structural", "shell", "cosmetic", "functional", "flexible",
    "engineering", "artistic", "press-fit", "removable", "bought",
)


class ProjectError(ThreeDError):
    """A `3d.yaml` is missing, malformed, or references a missing part file. Exit 2."""

    exit_code = 2


@dataclass(slots=True)
class Part:
    """One entry under `parts.<name>` in 3d.yaml."""

    name: str
    file: str                       # raw `file:` string as written in 3d.yaml
    path: pathlib.Path              # `file` resolved relative to the project root
    module: str | None = None       # OpenSCAD module to instantiate (None = whole file)
    tags: list[str] = field(default_factory=list)
    material: str | None = None     # name into materials.yaml (§2a); None = project default
    color: str | None = None
    copies: int = 1
    orientation: str | list[float] | None = None  # auto|flat-bottom|[rx,ry,rz]
    supports: str | None = None     # minimize|none|tree
    infill: str | int | None = None
    gates: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)  # the untouched part dict


@dataclass(slots=True)
class ProjectAnchor:
    """Named semantic point/feature in the project object model."""

    name: str
    pos: list[float]
    direction: list[float] | None = None
    area: float | None = None
    note: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProjectSection:
    """Named section/cut declaration, resolved by render tooling later."""

    name: str
    preset: str | None = None
    through: str | None = None
    plane: str | None = None
    at: float | None = None
    offset: float | None = None
    keep: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProjectLoad:
    """Named load declaration anchored to the object model."""

    name: str
    anchor: str
    vector: list[float] | None = None
    note: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProjectGate:
    """Project-level gate selection/config declaration."""

    name: str
    parts: list[str] = field(default_factory=list)
    config: str | None = None
    hard: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Project:
    """A loaded `3d.yaml`. `root` is its directory; resolve relative paths against it."""

    path: pathlib.Path              # absolute path to the 3d.yaml file
    root: pathlib.Path              # the project directory (path.parent)
    name: str
    units: str = "mm"
    copies: int = 1
    printer: str | None = None      # name into printers.yaml (§2a)
    material: str | None = None     # default material name into materials.yaml (§2a)
    bed: list[float] | None = None  # [x, y, z] build volume, optional
    parts: dict[str, Part] = field(default_factory=dict)
    anchors: dict[str, ProjectAnchor] = field(default_factory=dict)
    sections: dict[str, ProjectSection] = field(default_factory=dict)
    loads: dict[str, ProjectLoad] = field(default_factory=dict)
    gates: list[ProjectGate] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)  # the untouched full document


def find_project(start: str | os.PathLike[str] | None = None) -> pathlib.Path | None:
    """Walk UP from `start` (default cwd) and return the nearest `3d.yaml`, or None.

    Mirrors git's discovery: a command run in any subdirectory still finds the project."""
    cur = pathlib.Path(start).resolve() if start is not None else pathlib.Path.cwd()
    if cur.is_file():
        cur = cur.parent
    for d in (cur, *cur.parents):
        candidate = d / PROJECT_FILE
        if candidate.is_file():
            return candidate
    return None


def _require_yaml() -> Any:
    try:
        import yaml  # lazy: pyyaml is a real dependency of project handling, not import-time
    except ImportError as exc:  # pragma: no cover - exercised via load() error path
        raise MissingDependency(
            "pyyaml",
            install="uv sync  (pyyaml is a core dependency)  # or: pip install pyyaml",
            degrades="every project command (3d.yaml cannot be parsed)",
        ) from exc
    return yaml


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _mapping(value: Any, label: str, *, command: str | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProjectError(
            f"`{label}` must be a mapping, got {type(value).__name__}",
            command=command,
            remediation=[f"In {PROJECT_FILE}, write `{label}:` as key/value entries."],
        )
    return {str(k): v for k, v in value.items()}


def _float_value(value: Any, label: str, *, command: str | None) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ProjectError(
            f"{label} must be a number, got {value!r}",
            command=command,
        ) from None


def _float_triplet(value: Any, label: str, *, command: str | None) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ProjectError(
            f"{label} must be a 3-number list, got {value!r}",
            command=command,
            remediation=[f"Example: `{label}: [0, 0, 1]`"],
        )
    return [_float_value(v, label, command=command) for v in value]


def _optional_float(value: Any, label: str, *, command: str | None) -> float | None:
    if value is None:
        return None
    return _float_value(value, label, command=command)


def _optional_triplet(value: Any, label: str, *, command: str | None) -> list[float] | None:
    if value is None:
        return None
    return _float_triplet(value, label, command=command)


def _build_anchor(name: str, spec: Any, *, command: str | None) -> ProjectAnchor:
    data = _mapping(spec, f"anchors.{name}", command=command)
    if "pos" not in data:
        raise ProjectError(
            f"anchors.{name} is missing `pos:`",
            command=command,
            remediation=[f"Add `pos: [x, y, z]` under anchors.{name} in {PROJECT_FILE}."],
        )
    area = _optional_float(data.get("area"), f"anchors.{name}.area", command=command)
    return ProjectAnchor(
        name=name,
        pos=_float_triplet(data["pos"], f"anchors.{name}.pos", command=command),
        direction=_optional_triplet(data.get("dir"), f"anchors.{name}.dir", command=command),
        area=area,
        note=str(data["note"]) if data.get("note") is not None else None,
        raw=data,
    )


def _build_section(name: str, spec: Any, *, command: str | None) -> ProjectSection:
    if isinstance(spec, str):
        return ProjectSection(name=name, preset=spec, raw={"preset": spec})
    data = _mapping(spec, f"sections.{name}", command=command)
    plane = str(data["plane"]) if data.get("plane") is not None else None
    if plane is not None and plane not in ("YZ", "XZ", "XY"):
        raise ProjectError(
            f"sections.{name}.plane must be one of YZ, XZ, XY; got {plane!r}",
            command=command,
        )
    keep = str(data["keep"]) if data.get("keep") is not None else None
    if keep is not None and keep not in ("pos", "neg"):
        raise ProjectError(
            f"sections.{name}.keep must be one of pos, neg; got {keep!r}",
            command=command,
        )
    return ProjectSection(
        name=name,
        preset=str(data["preset"]) if data.get("preset") is not None else None,
        through=str(data["through"]) if data.get("through") is not None else None,
        plane=plane,
        at=_optional_float(data.get("at"), f"sections.{name}.at", command=command),
        offset=_optional_float(data.get("offset"), f"sections.{name}.offset", command=command),
        keep=keep,
        raw=data,
    )


def _build_load(name: str, spec: Any, *, command: str | None) -> ProjectLoad:
    data = _mapping(spec, f"loads.{name}", command=command)
    anchor = data.get("anchor")
    if not isinstance(anchor, str) or not anchor:
        raise ProjectError(
            f"loads.{name} is missing `anchor:`",
            command=command,
            remediation=[f"Add `anchor: <name>` under loads.{name} in {PROJECT_FILE}."],
        )
    return ProjectLoad(
        name=name,
        anchor=anchor,
        vector=_optional_triplet(data.get("vector"), f"loads.{name}.vector", command=command),
        note=str(data["note"]) if data.get("note") is not None else None,
        raw=data,
    )


def _build_gate(idx: int, spec: Any, *, command: str | None) -> ProjectGate:
    if isinstance(spec, str):
        return ProjectGate(name=spec, raw={"name": spec})
    data = _mapping(spec, f"gates[{idx}]", command=command)
    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise ProjectError(
            f"gates[{idx}] must be a string or a mapping with `name:`",
            command=command,
            remediation=["Example: `gates: [manifold, printability]`"],
        )
    hard = data.get("hard")
    if hard is not None and not isinstance(hard, bool):
        raise ProjectError(f"gates[{idx}].hard must be true or false", command=command)
    return ProjectGate(
        name=name,
        parts=_as_str_list(data.get("parts")),
        config=str(data["config"]) if data.get("config") is not None else None,
        hard=hard,
        raw=data,
    )


def _build_gate_list(spec: Any, *, command: str | None) -> list[ProjectGate]:
    if spec is None:
        return []
    if isinstance(spec, dict):
        gates: list[ProjectGate] = []
        for i, (name, value) in enumerate(spec.items()):
            if value is None:
                data: dict[str, Any] = {"name": str(name)}
            elif isinstance(value, dict):
                data = {**_mapping(value, f"gates.{name}", command=command), "name": str(name)}
            else:
                raise ProjectError(
                    f"`gates.{name}` must be a mapping or null, got {type(value).__name__}",
                    command=command,
                    remediation=[f"Example: `gates:\n  {name}: {{config: verify/{name}.json}}`"],
                )
            gates.append(_build_gate(i, data, command=command))
        return gates
    if not isinstance(spec, (list, tuple)):
        raise ProjectError(
            f"`gates:` must be a list or mapping, got {type(spec).__name__}",
            command=command,
            remediation=["Example: `gates: [manifold, printability]`"],
        )
    return [_build_gate(i, item, command=command) for i, item in enumerate(spec)]


def _build_named_mapping(
    spec: Any,
    label: str,
    builder: Any,
    *,
    command: str | None,
) -> dict[str, Any]:
    if spec is None:
        return {}
    entries = _mapping(spec, label, command=command)
    return {name: builder(name, value, command=command) for name, value in entries.items()}


def _build_part(name: str, spec: Any, root: pathlib.Path, *, command: str | None) -> Part:
    if not isinstance(spec, dict):
        raise ProjectError(
            f"part {name!r} must be a mapping, got {type(spec).__name__}",
            command=command,
            remediation=[f"In {PROJECT_FILE}, write:  parts:\n    {name}:\n      file: parts/{name}.scad"],
        )
    file = spec.get("file")
    if not file or not isinstance(file, str):
        raise ProjectError(
            f"part {name!r} is missing a `file:` (the .scad/.stl/.3mf path)",
            command=command,
            remediation=[f"Add `file: parts/{name}.scad` under parts.{name} in {PROJECT_FILE}."],
        )
    copies = spec.get("copies", 1)
    try:
        copies = int(copies)
    except (TypeError, ValueError):
        raise ProjectError(
            f"part {name!r}: copies must be an integer, got {copies!r}", command=command
        ) from None
    return Part(
        name=name,
        file=file,
        path=(root / file).resolve(),
        module=spec.get("module"),
        tags=_as_str_list(spec.get("tags")),
        material=spec.get("material"),
        color=spec.get("color"),
        copies=copies,
        orientation=spec.get("orientation"),
        supports=spec.get("supports"),
        infill=spec.get("infill"),
        gates=_as_str_list(spec.get("gates")),
        raw=dict(spec),
    )


def load_project(
    path_or_dir: str | os.PathLike[str],
    *,
    command: str | None = None,
    check_files: bool = True,
) -> Project:
    """Load a `3d.yaml` from a file path or a directory containing one.

    `check_files=True` (default) verifies each part's `file` exists relative to the project
    root and raises a structured ProjectError naming the missing file. Pass False when only
    the metadata is needed (e.g. listing) and the parts may not be authored yet."""
    p = pathlib.Path(path_or_dir)
    if p.is_dir():
        p = p / PROJECT_FILE
    if not p.is_file():
        raise ProjectError(
            f"no {PROJECT_FILE} at {p}",
            command=command,
            remediation=[
                f"Run `3d init` to scaffold a project here, or `cd` into a directory that has a {PROJECT_FILE}.",
            ],
        )
    p = p.resolve()
    root = p.parent

    yaml = _require_yaml()
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:  # type: ignore[attr-defined]
        raise ProjectError(
            f"could not parse {p}: {exc}",
            command=command,
            remediation=[f"Fix the YAML syntax in {p} (check indentation and `key: value` pairs)."],
        ) from exc
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise ProjectError(
            f"{p} must be a YAML mapping at the top level, got {type(doc).__name__}",
            command=command,
            remediation=["The file should start with `project:` and `parts:` keys."],
        )

    proj_spec = doc.get("project") or {}
    if not isinstance(proj_spec, dict):
        raise ProjectError(f"`project:` must be a mapping in {p}", command=command)
    name = str(proj_spec.get("name") or root.name)

    bed = proj_spec.get("bed")
    bed_list: list[float] | None = None
    if isinstance(bed, (list, tuple)):
        try:
            bed_list = [float(v) for v in bed]
        except (TypeError, ValueError):
            bed_list = None

    copies = proj_spec.get("copies", 1)
    try:
        copies = int(copies)
    except (TypeError, ValueError):
        copies = 1

    parts_spec = doc.get("parts") or {}
    if not isinstance(parts_spec, dict):
        raise ProjectError(
            f"`parts:` must be a mapping of name -> part in {p}",
            command=command,
            remediation=["Each part is `parts.<name>:` with at least a `file:` key."],
        )
    parts: dict[str, Part] = {}
    for pname, spec in parts_spec.items():
        part = _build_part(str(pname), spec, root, command=command)
        if check_files and not part.path.is_file():
            raise ProjectError(
                f"part {pname!r}: file not found: {part.file} (resolved to {part.path})",
                command=command,
                remediation=[
                    f"Create {part.file} under {root}, or fix the `file:` path in {PROJECT_FILE}.",
                ],
            )
        parts[str(pname)] = part

    anchors = _build_named_mapping(doc.get("anchors"), "anchors", _build_anchor, command=command)
    sections = _build_named_mapping(doc.get("sections"), "sections", _build_section, command=command)
    loads = _build_named_mapping(doc.get("loads"), "loads", _build_load, command=command)
    gates = _build_gate_list(doc.get("gates"), command=command)

    return Project(
        path=p,
        root=root,
        name=name,
        units=str(proj_spec.get("units", "mm")),
        copies=copies,
        printer=proj_spec.get("printer"),
        material=proj_spec.get("material"),
        bed=bed_list,
        parts=parts,
        anchors=anchors,
        sections=sections,
        loads=loads,
        gates=gates,
        raw=doc,
    )
