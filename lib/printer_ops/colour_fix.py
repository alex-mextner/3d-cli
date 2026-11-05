"""colour_fix.py — slot colour tolerance and the rewrite that aligns a G-code with the loaded filament.

WHAT: two things behind the preflight `slot Tn` line.
  - `colour_delta(a, b)`: CIEDE2000 between two `#RRGGBB` colours (CIELAB, D65; printops/color.py).
    `DEFAULT_SLOT_DELTA_E` (10) is the tolerance of the slot check: two beiges of the same PETG
    (#E8D8B0 sliced, #FEE5A5 loaded: ΔE 6.4) are the same filament; white vs beige (16.7) or red vs
    blue (52.9) are not. A material mismatch is never tolerated (preflight.py).
  - `plan_colour_fix` + `apply_colour_fix` (`3d print-preflight --fix`, `3d printer start
    --fix-colour`): for every tool the G-code uses, write the colour the PRINTER reports into the
    `; filament_colour` / `; extruder_colour` entries of the G-code and into the 3MF project it was
    sliced from (`Metadata/project_settings.config`, `Metadata/plate_N.json filament_colors`), so the
    files record what is really loaded. Sidecars follow: `NAME.slice-warnings.json` gets the new
    G-code SHA-256 (`slice_warnings.follow_edit`), `NAME.manifest.json` the new slot colour. Old and
    new SHA-256 of every rewritten file are returned for the operator to print.

WHY: on 2026-11-04 a SIMBOL plate sliced with #E8D8B0 met a printer that showed the same beige PETG
  as #FEE5A5: the preflight FAILed on string inequality and the only way out was `--force`, whose
  Telegram approval then failed. The cause is metadata, and the right fix is at the source, in
  seconds, with the old and new hashes on record.

INVARIANTS: stdlib only (print-preflight imports it in-process). The toolpath bytes are never
  touched: only the colour entries of `; filament_colour =` / `; extruder_colour =` lines change;
  mode and mtime are kept (a staleness check G-code vs project must not be fooled), and both files
  are staged to temp files first, so a 3MF that cannot be rewritten leaves the G-code untouched. A
  tool whose loaded colour is further than `max_delta_e` from the sliced one is REFUSED, not
  rewritten: that is a different filament, and relabelling it would hide a wrong-colour print.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from printops.color import hex_delta_e, parse_hex

from printer_ops.base import PrinterRefused

if TYPE_CHECKING:
    from printer_ops.base import PrinterStatus
    from printer_ops.preflight import GcodeMeta

DEFAULT_SLOT_DELTA_E = 10.0  # CIEDE2000; the `colour.warn_delta_e` of fleet/policy.yaml
DEFAULT_FIX_MAX_DELTA_E = (
    30.0  # above this the loaded filament is another colour: no rewrite
)

_COLOUR_LINE = re.compile(
    rb"^(;[ \t]*(?:filament_colour|extruder_colour)[ \t]*=[ \t]*)(.*?)(\r?\n?)$"
)
_COLOUR_ENTRY = re.compile(r"#?[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?")
_PROJECT_SETTINGS = "Metadata/project_settings.config"
_PLATE_JSON = re.compile(r"Metadata/plate_\d+\.json")
_CHUNK = 1 << 22


def colour_delta(a: str | None, b: str | None) -> float | None:
    """CIEDE2000 between two colours (`#RRGGBB`, RGBA and bare forms accepted); None when either is not one."""
    pa, pb = parse_hex(a), parse_hex(b)
    if pa is None or pb is None:
        return None
    return hex_delta_e(pa, pb)


def parse_delta_e(
    value: str | None, option: str, default: float, *, command: str
) -> float:
    """`--colour-delta-e` / `--fix-max-delta-e` value: a positive number (CIEDE2000), else `default`."""
    from errors import InvalidArgument

    if value is None:
        return default
    try:
        number = float(value)
    except ValueError:
        number = -1.0
    if not number > 0:
        raise InvalidArgument(
            option, value, ["a positive number (CIEDE2000), e.g. 10"], command=command
        )
    return number


@dataclass(frozen=True)
class ColourEdit:
    """One tool's sliced colour replaced by the loaded one."""

    tool: int  # G-code tool number (0-based)
    label: str  # the printer's name of that head (U1: T1..T4)
    old: str
    new: str
    delta_e: float


@dataclass(frozen=True)
class FileChange:
    path: Path
    old_sha256: str
    new_sha256: str


@dataclass
class FixResult:
    edits: list[ColourEdit]
    gcode: FileChange
    project: FileChange | None = None
    manifest: Path | None = None  # NAME.manifest.json whose slot colours were updated
    sidecar: Path | None = (
        None  # NAME.slice-warnings.json re-bound to the new G-code hash
    )
    notes: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"colour fix: tool T{e.tool} ({e.label}) {e.old} -> {e.new} (ΔE {e.delta_e:.1f})"
            for e in self.edits
        ]
        out.extend(
            f"  {c.path}\n    sha256 old {c.old_sha256}\n    sha256 new {c.new_sha256}"
            for c in (self.gcode, self.project)
            if c is not None
        )
        if self.manifest is not None:
            out.append(f"  {self.manifest}: slot colours updated")
        if self.sidecar is not None:
            out.append(f"  {self.sidecar}: re-bound to the new G-code sha256")
        out.extend(f"  note: {n}" for n in self.notes)
        return out


def plan_colour_fix(
    meta: GcodeMeta, status: PrinterStatus, max_delta_e: float = DEFAULT_FIX_MAX_DELTA_E
) -> tuple[list[ColourEdit], list[str]]:
    """(edits, refusals): the used tools whose sliced colour differs from the loaded one, and the
    reasons a tool is not rewritten (empty head, a different colour beyond `max_delta_e`)."""
    edits: list[ColourEdit] = []
    refusals: list[str] = []
    for tool in meta.used_tools:
        slot = status.slots[tool] if tool < len(status.slots) else None
        old = meta.colours[tool] if tool < len(meta.colours) else None
        if (
            slot is None
            or not slot.present
            or not slot.color
            or not old
            or old == slot.color
        ):
            continue
        de = colour_delta(old, slot.color)
        if de is None:
            continue
        if de > max_delta_e:
            refusals.append(
                f"tool T{tool} ({slot.label}): G-code {old}, loaded {slot.color}: ΔE {de:.1f} > {max_delta_e:g}, "
                "another colour, not the same filament reported differently"
            )
        else:
            edits.append(ColourEdit(tool, slot.label, old, slot.color, de))
    return edits, refusals


def _ident(st: os.stat_result) -> tuple[int, int, int]:
    return st.st_ino, st.st_size, st.st_mtime_ns


def _entries(value: str, wanted: dict[int, str]) -> str:
    """`a;b;c` with entry `t` of `wanted` replaced by its colour (quotes and spacing kept)."""
    parts = value.split(";")
    for tool, new in wanted.items():
        if tool < len(parts) and _COLOUR_ENTRY.search(parts[tool]):
            parts[tool] = _COLOUR_ENTRY.sub(new, parts[tool], count=1)
    return ";".join(parts)


def _patch_line(line: bytes, wanted: dict[int, str]) -> bytes:
    m = _COLOUR_LINE.match(line)
    if m is None:
        return line
    value = m.group(2).decode("utf-8", errors="surrogateescape")
    return (
        m.group(1)
        + _entries(value, wanted).encode("utf-8", errors="surrogateescape")
        + m.group(3)
    )


def _stage_gcode(
    path: Path, wanted: dict[int, str]
) -> tuple[Path, FileChange, os.stat_result]:
    """Copy `path` with the colour lines patched to a temp file beside it; (temp, hashes, source stat)."""
    old, new = hashlib.sha256(), hashlib.sha256()
    fd, tmp = tempfile.mkstemp(prefix=".colour-fix-", dir=path.resolve().parent)
    changed = 0
    try:
        with open(path, "rb") as src, os.fdopen(fd, "wb") as dst:
            st = os.fstat(src.fileno())
            for line in src:
                old.update(line)
                out = (
                    _patch_line(line, wanted)
                    if line[:1] == b";" and b"colour" in line
                    else line
                )
                changed += out != line
                new.update(out)
                dst.write(out)
        if not changed:
            raise PrinterRefused(
                f"{path} has no `; filament_colour` / `; extruder_colour` entry to rewrite for tools "
                f"{', '.join(f'T{t}' for t in sorted(wanted))}",
                command="print-preflight",
                remediation=[
                    "The G-code carries no slicer config block: re-slice it, or load the colour that was sliced."
                ],
            )
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return Path(tmp), FileChange(path, old.hexdigest(), new.hexdigest()), st


def _indent_of(text: str) -> int:
    for line in text.splitlines()[1:3]:
        stripped = line.lstrip(" ")
        if stripped and stripped != line:
            return len(line) - len(stripped)
    return 4


def _patch_json(
    raw: bytes, keys: tuple[str, ...], wanted: dict[int, str]
) -> bytes | None:
    """JSON with entry `t` of each list in `keys` set to its colour; None when nothing changes."""
    text = raw.decode("utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        return None
    changed = False
    for key in keys:
        arr = data.get(key)
        if not isinstance(arr, list):
            continue
        for tool, new in wanted.items():
            if (
                tool < len(arr)
                and isinstance(arr[tool], str)
                and arr[tool].upper() != new.upper()
            ):
                arr[tool] = new
                changed = True
    if not changed:
        return None
    out = json.dumps(data, indent=_indent_of(text), ensure_ascii=False)
    return (out + "\n" if text.endswith("\n") else out).encode("utf-8")


class _NotAProject(ValueError):
    """The 3MF is not a slicer project: there is no colour list in it to rewrite."""


def _sha256_and_stat(path: Path) -> tuple[str, os.stat_result]:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        st = os.fstat(fh.fileno())
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest(), st


def _patched_members(zin: zipfile.ZipFile, wanted: dict[int, str]) -> dict[str, bytes]:
    """The members of the project whose colour lists change, as their new bytes."""
    patched: dict[str, bytes] = {}
    for info in zin.infolist():
        if info.filename == _PROJECT_SETTINGS:
            keys: tuple[str, ...] = ("filament_colour", "extruder_colour")
        elif _PLATE_JSON.fullmatch(info.filename):
            keys = ("filament_colors",)
        else:
            continue
        if (new := _patch_json(zin.read(info), keys, wanted)) is not None:
            patched[info.filename] = new
    return patched


def _write_patched(zin: zipfile.ZipFile, patched: dict[str, bytes], tmp: Path) -> None:
    """Copy every member of `zin` to the zip `tmp`, the `patched` ones with their new bytes."""
    with tmp.open("wb") as raw, zipfile.ZipFile(raw, "w") as zout:
        for info in zin.infolist():
            zout.writestr(
                info,
                patched.get(info.filename) or zin.read(info),
                compress_type=info.compress_type,
            )


def _stage_project(
    path: Path, wanted: dict[int, str]
) -> tuple[Path, FileChange, os.stat_result] | None:
    """The 3MF with its colour lists patched, as a temp file beside it; None when it already agrees."""
    old_sha, st = _sha256_and_stat(path)
    try:
        zin = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise _NotAProject(f"{path} is not a 3MF (zip): {exc}") from exc
    with zin:
        if _PROJECT_SETTINGS not in zin.namelist():
            raise _NotAProject(
                f"{path} has no {_PROJECT_SETTINGS}: not a slicer project 3MF"
            )
        patched = _patched_members(zin, wanted)
        if _PROJECT_SETTINGS not in patched:
            return None
        fd, name = tempfile.mkstemp(prefix=".colour-fix-", dir=path.resolve().parent)
        os.close(fd)
        tmp = Path(name)
        try:
            _write_patched(zin, patched, tmp)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    new_sha = hashlib.sha256(tmp.read_bytes()).hexdigest()
    return tmp, FileChange(path, old_sha, new_sha), st


def _install(tmp: Path, dst: Path, st: os.stat_result) -> None:
    """Replace `dst` by the staged `tmp`, refusing when `dst` changed meanwhile; mode and mtime kept."""
    os.chmod(tmp, st.st_mode & 0o7777)
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    if _ident(os.stat(dst)) != _ident(st):
        raise PrinterRefused(
            f"{dst} changed while its colours were being rewritten; run again",
            command="print-preflight",
        )
    os.replace(tmp, dst)


def _update_manifest(project: Path, wanted: dict[int, str]) -> Path | None:
    """NAME.manifest.json beside NAME.3mf: the slot colours (`filaments[].hex`, tool = slot - 1)."""
    path = project.with_name(project.stem + ".manifest.json")
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    changed = False
    for fil in data.get("filaments", []) if isinstance(data, dict) else []:
        tool = fil.get("tool") if isinstance(fil, dict) else None
        new = wanted.get(tool) if isinstance(tool, int) else None
        if new is not None and str(fil.get("hex", "")).upper() != new.upper():
            fil["hex"] = new
            changed = True
    if not changed:
        return None
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=_indent_of(text)), encoding="utf-8"
    )
    return path


def apply_colour_fix(
    gcode: Path, project: Path | None, edits: list[ColourEdit]
) -> FixResult:
    """Rewrite the colours of `edits` in `gcode` (and `project`, a 3MF) in place; see the module doc."""
    wanted = {e.tool: e.new for e in edits}
    staged_g, change_g, st_g = _stage_gcode(gcode, wanted)
    staged_p = None
    result = FixResult(edits, change_g)
    try:
        staged_p = _stage_project(project, wanted) if project is not None else None
    except _NotAProject as exc:
        project = None
        result.notes.append(f"{exc}; left untouched")
    except BaseException:
        staged_g.unlink(missing_ok=True)
        raise
    try:
        _install(staged_g, gcode, st_g)
        if staged_p is not None and project is not None:
            _install(staged_p[0], project, staged_p[2])
            result.project = staged_p[1]
    finally:
        staged_g.unlink(missing_ok=True)
        if staged_p is not None:
            staged_p[0].unlink(missing_ok=True)
    from slicing.slice_warnings import follow_edit

    result.sidecar = follow_edit(
        gcode, gcode, change_g.old_sha256, change_g.new_sha256, "colour fix"
    )
    if project is not None:
        result.manifest = _update_manifest(project, wanted)
        if staged_p is None:
            result.notes.append(f"{project} already carries the loaded colours")
    elif not result.notes:
        result.notes.append(
            "no project 3MF found (--source-3mf / NAME.3mf): only the G-code was rewritten"
        )
    return result


def fix_to_loaded(
    gcode: Path,
    status: PrinterStatus,
    source_3mf: Path | None,
    max_delta_e: float = DEFAULT_FIX_MAX_DELTA_E,
    *,
    command: str,
) -> FixResult | None:
    """Rewrite `gcode` (+ its project 3MF: `source_3mf`, else NAME.3mf beside it) to the colours the
    printer reports for the tools it uses. None when nothing differs; PrinterRefused (nothing written)
    when the printer reports no per-head data or a tool's loaded colour is another colour."""
    from printer_ops.preflight import find_project_3mf, read_gcode_meta

    if not status.slots:
        raise PrinterRefused(
            "the printer reports no per-head filament data: there is no loaded colour to rewrite the file to",
            command=command,
        )
    edits, refusals = plan_colour_fix(read_gcode_meta(gcode), status, max_delta_e)
    if refusals:
        raise PrinterRefused(
            "colour fix refused, nothing written: " + refusals[0],
            command=command,
            remediation=[
                *refusals[1:],
                "Load the filament that was sliced (`3d printer slot set` fixes the printer's record), or, if "
                "this really is the same filament, raise the limit: --fix-max-delta-e N.",
            ],
        )
    if not edits:
        return None
    project, _ = find_project_3mf(gcode, source_3mf)
    return apply_colour_fix(gcode, project, edits)
