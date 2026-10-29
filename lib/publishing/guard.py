"""The print/publish guardrail of `3d models publish` (umbrella #570, S0 contract `parts.py`).

A bought or imaginary part (PCB, connector, wire, screw, stand-in for a real board) must never be published
as a printable model by mistake. The roles live in the project's `3d.yaml`:

    parts:
      mole_back:  {file: parts/mole_back.scad}                       # role defaults to `printed`
      probe_pcb:  {file: vitamins/REF_probe.scad, role: vitamin,
                   reference_source: {url: https://www.printables.com/model/1-probe, license: CC BY 4.0}}
      board_ghost: {file: vitamins/REF_board.scad, role: stand_in}

`load_parts` reads them through `parts.part_from_dict` (so a role/`printable` contradiction and a `vitamin`
without `reference_source` are exit-2 errors, not silently ignored). A file of the publish set belongs to a
part when its stem equals the part name or the stem of the part's `file`.

What `evaluate_parts` decides (the publish set is what `publishing.spec.select_files` found):
  - a published file of a non-printable part is a BLOCKER (`NonPrintableParts`, exit 1) — unless
    `drop=True` (`--drop-nonprintable`: the file leaves the set, listed as a warning) or
    `allow=[PART]` with a `reason` (`--allow-nonprintable PART --reason TEXT`: logged by
    `parts.assert_printable`, returned in `overridden`, written as a library event by the caller);
  - every part with a `reference_source` is attributed in the description, and its licence is judged by the
    same rules as `remix_of` (`remix.check_remix`: ND refused without permission_ref, NC/SA/GPL conditions
    inherited by the published licence).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from errors import InvalidArgument, UsageError
from parts import NonPrintableParts, Part, assert_printable, part_from_dict
from publishing.remix import RemixCheck, check_remix
from publishing.spec import FileSet, PublishFile, PublishSpec, name_clashes

PROJECT_FILE = "3d.yaml"


class NonPrintablePublish(NonPrintableParts):
    """`NonPrintableParts` with the publish flags in its remediation (the base names the print override)."""

    def __init__(self, parts: Sequence[Part]) -> None:
        super().__init__(parts, command="models publish")
        self.message = self.message.replace("print", "publish", 1)
        self.remediation = [
            "Drop them from the publish set: `--drop-nonprintable` (listed as a warning), or move their files "
            "out of the project root and print/.",
            "Publish them on purpose only with `--allow-nonprintable PART --reason TEXT` (logged as a library event).",
        ]


@dataclass(frozen=True)
class PartsReport:
    parts: tuple[Part, ...]  # every part of 3d.yaml
    matched: tuple[
        tuple[Part, PublishFile], ...
    ]  # non-printable part -> the file of the publish set it owns
    dropped: tuple[str, ...]  # file names removed from the set (--drop-nonprintable)
    overridden: tuple[Part, ...]  # published on purpose (--allow-nonprintable)
    reason: str  # why, as typed with --reason
    blocked: tuple[
        Part, ...
    ]  # non-printable parts whose files are in the set and nothing allowed them
    references: tuple[
        Part, ...
    ]  # parts that carry a reference_source (attributed in the description)
    checks: tuple[RemixCheck, ...]  # licence verdict per reference source

    def gate_error(self) -> NonPrintableParts | None:
        """The refusal (exit 1) when a non-printable part's file is in the publish set, else None."""
        return NonPrintablePublish(self.blocked) if self.blocked else None

    def problems(self) -> tuple[str, ...]:
        out = [
            f"reference model {c.source_url}: {p}"
            for c in self.checks
            for p in c.problems
        ]
        return tuple(out)

    def warnings(self) -> tuple[str, ...]:
        out = [
            f"dropped {name}: its part is not printable (--drop-nonprintable)"
            for name in self.dropped
        ]
        out += [
            f"published on purpose: {p.name} ({p.role}) — reason: {self.reason}"
            for p in self.overridden
        ]
        return tuple(out)

    def to_json(self) -> dict[str, Any]:
        return {
            "parts": [p.to_dict() for p in self.parts],
            "blocked": [p.name for p in self.blocked],
            "dropped": list(self.dropped),
            "overridden": [p.to_dict() for p in self.overridden],
            "reason": self.reason,
            "references": [
                {
                    "name": p.name,
                    "role": p.role,
                    **(p.reference_source.to_dict() if p.reference_source else {}),
                }
                for p in self.references
            ],
        }


def load_parts(project: Path) -> tuple[Part, ...]:
    """The parts declared in `<project>/3d.yaml` (none when the file or its `parts` is absent)."""
    path = project / PROJECT_FILE
    if not path.is_file():
        return ()
    import yaml  # lazy: only publish needs it

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise UsageError(
            f"{path} is not valid YAML: {exc}",
            command="models",
            remediation=["Fix 3d.yaml."],
        ) from exc
    declared = data.get("parts") if isinstance(data, dict) else None
    if not declared:
        return ()
    if not isinstance(declared, dict):
        raise InvalidArgument(
            "3d.yaml: parts",
            repr(declared),
            ["a mapping of part names to settings"],
            command="models",
        )
    found: list[Part] = []
    for name, raw in declared.items():
        if not isinstance(raw, dict):
            raise InvalidArgument(
                f"3d.yaml: parts.{name}", repr(raw), ["a mapping"], command="models"
            )
        found.append(part_from_dict({**raw, "name": str(name)}))
    return tuple(found)


def _part_files(project: Path) -> dict[str, str]:
    """part name -> the stem of its `file:` (the project root is where `file` is relative to)."""
    import yaml

    path = project / PROJECT_FILE
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    declared = data.get("parts") if isinstance(data, dict) else None
    out: dict[str, str] = {}
    for name, raw in (declared or {}).items():
        if isinstance(raw, dict) and isinstance(raw.get("file"), str):
            out[str(name)] = Path(raw["file"]).stem.casefold()
    return out


def match_files(
    parts: Sequence[Part], stems: dict[str, str], files: FileSet
) -> tuple[tuple[Part, PublishFile], ...]:
    """Each file of the publish set that belongs to a NON-printable part."""
    found: list[tuple[Part, PublishFile]] = []
    for part in parts:
        if part.printable:
            continue
        names = {part.name.casefold(), stems.get(part.name, "")} - {""}
        found.extend(
            (part, f) for f in files.files if Path(f.name).stem.casefold() in names
        )
    return tuple(found)


def _check_allow(parts: Sequence[Part], allow: Sequence[str], reason: str) -> None:
    """`--allow-nonprintable` must name declared parts and carry a reason (InvalidArgument/UsageError, exit 2)."""
    names = {p.name for p in parts}
    unknown = sorted(set(allow) - names)
    if unknown:
        raise InvalidArgument(
            "--allow-nonprintable",
            ", ".join(unknown),
            sorted(p.name for p in parts if not p.printable)
            or ["(no non-printable part in 3d.yaml)"],
            command="models",
        )
    if allow and not reason.strip():
        raise UsageError(
            "--allow-nonprintable needs --reason TEXT: an override is logged with its reason",
            command="models",
            remediation=[
                '3d models publish DIR --platform all --allow-nonprintable probe_pcb --reason "assembly kit, reviewed"'
            ],
        )


def _without(files: FileSet, matched: Sequence[tuple[Part, PublishFile]]) -> FileSet:
    """`files` minus the matched ones, with the name clashes recomputed from what is left."""
    gone = {f.path for _, f in matched}
    retained = tuple(f for f in files.files if f.path not in gone)
    return replace(files, files=retained, clashes=name_clashes(retained))


def _resolve_matches(
    matched: Sequence[tuple[Part, PublishFile]],
    files: FileSet,
    *,
    allow: Sequence[str],
    drop: bool,
) -> tuple[FileSet, tuple[str, ...], tuple[Part, ...], tuple[Part, ...]]:
    """(file set to publish, dropped names, overridden parts, still-blocked parts) for the matched files."""
    blocked = tuple(dict.fromkeys(p for p, _ in matched))
    if not matched:
        return files, (), (), blocked
    if drop:
        return _without(files, matched), tuple(f.name for _, f in matched), (), ()
    allowed = [p for p in blocked if p.name in allow]
    overridden = assert_printable(
        allowed, include_non_printable=True, command="models publish"
    ).overridden
    return files, (), overridden, tuple(p for p in blocked if p.name not in allow)


def evaluate_parts(
    project: Path,
    spec: PublishSpec,
    files: FileSet,
    *,
    allow: Sequence[str] = (),
    reason: str = "",
    drop: bool = False,
) -> tuple[PartsReport, FileSet]:
    """Apply the guardrail. -> (report, the file set to publish). Raises NonPrintableParts (exit 1) when a
    non-printable part's file is in the set and neither `allow` nor `drop` says what to do with it."""
    parts = load_parts(project)
    stems = _part_files(project) if parts else {}
    _check_allow(parts, allow, reason)
    matched = match_files(parts, stems, files)
    kept, dropped, overridden, blocked_parts = _resolve_matches(
        matched, files, allow=allow, drop=drop
    )
    references = tuple(p for p in parts if p.reference_source is not None)
    checks = tuple(_check_reference(spec, p) for p in references)
    report = PartsReport(
        parts,
        matched,
        dropped,
        overridden,
        reason.strip(),
        blocked_parts,
        references,
        tuple(c for c in checks if c is not None),
    )
    return report, kept


def _check_reference(spec: PublishSpec, part: Part) -> RemixCheck | None:
    """The `remix_of` licence rules applied to one reference part: its declared licence is the evidence
    (no network: the 3d.yaml is the record of what the model was found under)."""
    src = part.reference_source
    if src is None or not src.url.startswith(("http://", "https://")):
        return (
            None  # "unknown" from 3MF metadata: nothing to judge, nothing to attribute
        )
    return check_remix(
        replace(spec, remix_of=src.url, remix_license=src.license),
        lambda _url: "",
    )


def attribution(parts: Sequence[Part]) -> str:
    """The credit block appended to the description for every part that carries a reference_source."""
    lines = [
        f"- {p.name}: {p.reference_source.url} ({p.reference_source.license})"
        for p in parts
        if p.reference_source is not None
        and p.reference_source.url.startswith(("http://", "https://"))
    ]
    return (
        "Reference models used (not part of the printable files):\n" + "\n".join(lines)
        if lines
        else ""
    )
