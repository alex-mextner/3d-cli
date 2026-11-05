"""preflight.py — checks run before a print starts (`3d print-preflight`, `3d printer start`).

WHAT (issue https://github.com/alex-mextner/3d-cli/issues/96), one PASS/WARN/FAIL/SKIP line each:
  - thumbnails   the G-code carries the preview images its `; thumbnails =` spec asks for
                 (lib/gcode_thumbnails.py check(), the `3d gcode-thumbnails` checker);
  - printer      the target printer is free (not printing/paused/busy/unknown);
  - bed          (printers with `watch.bed.enabled`) the camera saw the bed clear with the plate
                 on, from the best view with the printer's light on (printer_ops/bed.py, issue
                 #303/#723): a missing plate or a CONFIDENT occupied FAIL, unknown or an occupied
                 seen at a poor view WARN with the command to run;
  - slot Tn      for every tool the G-code uses: the printer's head is loaded, its material
                 equals the G-code's `; filament_type` and its colour is within a CIEDE2000
                 tolerance of `; filament_colour` (default 10, the distance is shown; a beige
                 reported as #FEE5A5 for a #E8D8B0 slice passes) — printer_ops/colour_fix.py,
                 which `print-preflight --fix` uses to rewrite the file to the loaded colour
                 (needs per-head data: Moonraker printers exposing print_task_config, i.e. U1);
  - halves       `3d halves-check <3mf> --json` when the source 3MF is given;
  - axis travel  every move (arcs by their real extremes) inside the printer's X/Y limits,
                 start/end G-code included; SKIP when the limits are unknown (move_bounds.py);
  - print area   plate moves inside the G-code's `; printable_area`, outside `; bed_exclude_area`;
  - overhangs    no object prints a layer on nothing: per layer, extrusion the layer below
                 does not carry under the 45-degree rule and no short bridge reaches
                 (lib/gcode_overhangs.py, `3d gcode-overhangs`, run as a subprocess: numpy);
                 FAIL blocks the start, WARN (small ceilings, edge step-outs) is shown;
  - slicer warn  every warning the slicer printed for this G-code is recorded in
                 NAME.slice-warnings.json (same SHA-256) and resolved or accepted with a reason
                 (slicing/slice_warnings.py, `3d slice-warnings`);
  - head seq     every object prints each layer on its recipe's heads (slicing/head_sequence.py,
                 issue #103) — needs the project 3MF: --source-3mf or NAME.3mf next to the G-code;
  - mix face     every part in a mixed filament shows its colour on walls: FAIL when its top
                 outweighs half its walls (slicing/mix_faces.py, issue #411; same project 3MF; without one
                 the G-code alone, objects printed by several heads: WARN, slicing/mix_gcode_faces.py, #419);
  - mix layers   the layer rules on what was printed: the additional colour never on consecutive
                 layers, no colour band or mixed layer thicker than 0.12 mm, measured on the
                 real per-layer heads and Z (slicing/mix_gcode.py, issue #116; with the project
                 3MF, else only objects that print one head per layer);
  - visible      `3d visible-faces --gcode` when --visible specs are given;
  - printops     the PrintOps material gates (printops_checks.py, issue #164) when a
                 PrintopsContext is given: filament temperatures, loaded spool, remaining
                 weight, drying, nozzle, maintenance, enclosure, plate, printer profile.
  A gate whose input is absent (no project 3MF, no --visible spec) is a SKIP that says what
  is missing; it never passes silently. A PrintOps gate without PrintOps data is UNVERIFIED
  (shown, counted under STATUS, never PASS; it does not block on its own).

WHY: wrong colour in a head, a used head that is empty, missing
  previews and halves that no longer fit were all caught by hand on recent jobs — after
  they would have cost hours of printing.

TOOL INDEXING: G-code `Tn` / the n-th entry of `; filament_colour` is head index n, which
  the U1 shows as T(n+1). Used tools come from `; filament used [mm] = a, b, ...` (non-zero
  entries); without that line, from `Tn` tool-change lines; without either, tool 0.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from printer_ops.base import PrinterStatus, Slot
from printer_ops.colour_fix import DEFAULT_SLOT_DELTA_E, colour_delta
from printer_ops.move_bounds import (
    BoundsReport,
    Limits,
    Violation,
    check_bounds,
    fmt_area,
    fmt_extent,
    fmt_limits,
)

if TYPE_CHECKING:
    from printer_ops.devices import Device
    from printer_ops.printops_checks import PrintopsContext

PASS, WARN, FAIL, SKIP, UNVERIFIED = "PASS", "WARN", "FAIL", "SKIP", "UNVERIFIED"
# Where a printer's axis_limits / trusted_macros live (named in the hints below).
PROFILE_FILES = "fleet/printers.yaml or printer-devices.yaml"
_CONFIG_LINE = re.compile(r"^;\s*([a-z_ \[\]0-9]+?)\s*=\s*(.*)$")
_TOOL_LINE = re.compile(r"^T(\d+)\s*(?:;.*)?$")
_BIN_3D = Path(__file__).resolve().parents[2] / "bin" / "3d"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    lines: list[str] = field(
        default_factory=list
    )  # per-object breakdown under the check line


@dataclass
class Report:
    file: str
    checks: list[Check] = field(default_factory=list)
    title: str = "PREFLIGHT"

    @property
    def overall(self) -> str:
        states = {c.status for c in self.checks}
        return FAIL if FAIL in states else WARN if WARN in states else PASS

    def to_json(self) -> dict[str, object]:
        return {
            "file": self.file,
            "overall": self.overall,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status,
                    "detail": c.detail,
                    "lines": c.lines,
                }
                for c in self.checks
            ],
        }

    def render(self) -> str:
        lines = [f"{self.title} {self.file}"]
        for c in self.checks:
            lines.append(f"  {c.status:<4}  {c.name:<11} {c.detail}")
            lines += [f"{'':19}{line}" for line in c.lines]
        unverified = sum(c.status == UNVERIFIED for c in self.checks)
        lines.append(
            f"STATUS: {self.overall}"
            + (f" ({unverified} unverified)" if unverified else "")
        )
        return "\n".join(lines)


@dataclass
class GcodeMeta:
    colours: list[str | None]
    types: list[str | None]
    used_tools: list[int]


def _split(value: str) -> list[str | None]:
    return [v.strip().strip('"') or None for v in value.split(";")]


def _hex(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lstrip("#")
    return (
        "#" + v[:6].upper()
        if re.fullmatch(r"[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", v)
        else None
    )


def _scan_gcode_config(path: Path) -> tuple[dict[str, str], set[int]]:
    """(first value of every `; key = value` slicer comment, tool numbers of the `Tn` lines)."""
    config: dict[str, str] = {}
    tool_lines: set[int] = set()
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith(";"):
                m = _CONFIG_LINE.match(line.rstrip("\n"))
                if m:
                    config.setdefault(m.group(1).strip(), m.group(2).strip())
            elif line[:1] == "T":
                t = _TOOL_LINE.match(line.strip())
                if t:
                    tool_lines.add(int(t.group(1)))
    return config, tool_lines


def _amount(value: str) -> float:
    """One entry of `; filament used [mm]`; an unparsable amount counts as unused."""
    try:
        return float(value)
    except ValueError:
        return 0.0


def _used_tools(config: dict[str, str], tool_lines: set[int]) -> list[int]:
    """Non-zero entries of `; filament used [mm]`, else the `Tn` lines, else tool 0."""
    used_mm = config.get("filament used [mm]")
    if used_mm:
        return [
            i for i, a in enumerate(_amount(v) for v in used_mm.split(",")) if a > 0
        ]
    return sorted(tool_lines) or [0]


def read_gcode_meta(path: Path) -> GcodeMeta:
    """Filament colours/types and the tools actually used, from slicer comments + Tn lines."""
    config, tool_lines = _scan_gcode_config(path)
    return GcodeMeta(
        colours=[_hex(c) for c in _split(config.get("filament_colour", ""))],
        types=[(t or None) for t in _split(config.get("filament_type", ""))],
        used_tools=_used_tools(config, tool_lines),
    )


def check_thumbnails(path: Path, shown: Path | None = None) -> Check:
    """`shown` is the file the fix command names (the original when `path` is a snapshot copy)."""
    from gcode_thumbnails import (
        check as thumb_check,
    )  # lazy: keeps `3d help` import-light
    from slicing.thumbnails import fix_command

    r = thumb_check(str(path))
    if r.ok:
        sizes = ", ".join(f"{t.width}x{t.height}" for t in r.present) or "present"
        return Check("thumbnails", PASS, sizes)
    # ThumbCheck.reason already names what is missing ("missing 48x48/PNG"); the list is the fallback.
    reason = r.reason or "missing " + (
        ", ".join(str(t) for t in r.missing) or "thumbnails"
    )
    fix = fix_command(shown or path)
    return Check("thumbnails", FAIL, f"{reason}; fix: {fix}  (or print-preflight --fix; upload/start add them themselves)")


def check_printer_free(status: PrinterStatus) -> Check:
    if status.busy:
        job = f" ({status.filename})" if status.filename else ""
        return Check(
            "printer", FAIL, f"printer is {status.state}{job} [{status.raw_state}]"
        )
    return Check("printer", PASS, f"{status.state} [{status.raw_state}]")


def _slot_problems(
    want_c: str | None, want_m: str | None, slot: Slot, colour_delta_e: float
) -> tuple[list[str], str]:
    """(problems, colour note): the mismatches of one loaded slot against the G-code's colour and
    material, and the ΔE note of a colour that differs but is within tolerance."""
    problems: list[str] = []
    note = ""
    if want_c and slot.color and want_c != slot.color:
        de = colour_delta(want_c, slot.color)
        if de is None or de > colour_delta_e:
            shown = "" if de is None else f" (ΔE {de:.1f} > {colour_delta_e:g})"
            problems.append(
                f"colour: G-code {want_c}, loaded {slot.color}{shown}; "
                "fix: print-preflight --fix / printer start --fix-colour"
            )
        else:
            note = (
                f" (colour ΔE {de:.1f} from G-code {want_c}, within {colour_delta_e:g})"
            )
    if want_m and slot.material and want_m.upper() != slot.material.upper():
        problems.append(f"material: G-code {want_m}, loaded {slot.material}")
    return problems, note


def _slot_check(
    tool: int, meta: GcodeMeta, status: PrinterStatus, colour_delta_e: float
) -> Check:
    name = f"slot T{tool}"
    if tool >= len(status.slots):
        return Check(
            name,
            FAIL,
            f"G-code uses tool {tool} but the printer has {len(status.slots)} heads",
        )
    slot = status.slots[tool]
    if not slot.present:
        return Check(
            name,
            FAIL,
            f"head {slot.label} is used by the G-code but has no filament / is disabled",
        )
    want_c = meta.colours[tool] if tool < len(meta.colours) else None
    want_m = meta.types[tool] if tool < len(meta.types) else None
    problems, note = _slot_problems(want_c, want_m, slot, colour_delta_e)
    if problems:
        return Check(name, FAIL, f"head {slot.label}: " + "; ".join(problems))
    if not want_c:
        return Check(
            name,
            WARN,
            f"head {slot.label}: G-code has no filament_colour for tool {tool}; "
            f"loaded {slot.color} {slot.material}",
        )
    return Check(
        name,
        PASS,
        f"head {slot.label}: {slot.color} {slot.material or ''}".rstrip() + note,
    )


def check_slots(
    meta: GcodeMeta, status: PrinterStatus, colour_delta_e: float = DEFAULT_SLOT_DELTA_E
) -> list[Check]:
    """One line per used tool. The colour matches within `colour_delta_e` (CIEDE2000, the distance is
    in the line); a material mismatch is always a FAIL."""
    if not status.slots:
        return [Check("slots", SKIP, "the printer reports no per-head filament data")]
    return [_slot_check(t, meta, status, colour_delta_e) for t in meta.used_tools]


def run_halves_check(source: Path) -> tuple[int, str, str]:
    r = subprocess.run(
        [sys.executable, str(_BIN_3D), "halves-check", str(source), "--json"],
        capture_output=True,
        text=True,
        timeout=900,
    )
    return r.returncode, r.stdout, r.stderr


def check_halves(source: Path) -> Check:
    rc, out, err = run_halves_check(source)
    try:
        payload = json.loads(out)
    except ValueError:
        payload = {}
    pairs = payload.get("pairs", []) if isinstance(payload, dict) else []
    if rc == 0:
        return Check("halves", PASS, f"{len(pairs)} pair(s) fit ({source.name})")
    if rc == 1:
        bad = [
            str(p.get("pair"))
            for p in pairs
            if isinstance(p, dict) and p.get("status") == FAIL
        ]
        return Check(
            "halves",
            FAIL,
            f"halves-check FAIL: {', '.join(bad) or 'see 3d halves-check ' + str(source)}",
        )
    tail = (err.strip().splitlines() or ["no output"])[-1]
    return Check("halves", FAIL, f"halves-check could not run (exit {rc}): {tail}")


def _tail(text: str) -> str:
    return (text.strip().splitlines() or ["no output"])[-1]


def check_overhangs(
    gcode: Path, bridge_max: float | None = None, bridge_source: str = "default"
) -> Check:
    """`3d gcode-overhangs`: no object prints more than the limit on nothing (FAIL); small
    ceilings and edge step-outs are WARN, listed per object. `bridge_source` says where
    `bridge_max` came from (--bridge-max, the printer's bridge_max_mm, default)."""
    from cli.pyrun import tool_argv
    from errors import MissingDependency
    from gcode_overhangs import format_lines

    try:
        r = subprocess.run(
            tool_argv(
                "numpy,scipy",
                "gcode_overhangs.py",
                [str(gcode), "--json"]
                + ([] if bridge_max is None else ["--bridge-max", f"{bridge_max:g}"]),
            ),
            capture_output=True,
            text=True,
            timeout=900,
        )
    except MissingDependency as exc:
        return Check("overhangs", FAIL, f"could not run: {exc}")
    try:
        payload = json.loads(r.stdout)
    except ValueError:
        payload = None
    if r.returncode not in (0, 1) or not isinstance(payload, dict):
        return Check(
            "overhangs",
            FAIL,
            f"gcode-overhangs could not run (exit {r.returncode}): {_tail(r.stderr)}",
        )
    detail = f"{payload.get('detail', '')} (bridge limit from {bridge_source})"
    return Check(
        "overhangs", str(payload.get("status", FAIL)), detail, format_lines(payload)
    )


def check_slice_warnings(origin: Path, gcode: Path) -> Check:
    """Every slicer warning for this exact G-code recorded and accepted (`3d slice-warnings`)."""
    from slicing.slice_warnings import evaluate

    status, detail, lines = evaluate(origin, gcode)
    return Check("slicer warn", status, detail, lines)


def find_project_3mf(gcode: Path, source: Path | None) -> tuple[Path | None, str]:
    """The plate project 3MF: --source-3mf, else NAME.3mf next to NAME.gcode."""
    if source is not None:
        return source, f"--source-3mf {source.name}"
    sibling = gcode.with_suffix(".3mf")
    if sibling.is_file():
        return sibling, f"{sibling.name} next to the G-code"
    return None, ""


def check_head_sequence(
    gcode: Path, project: Path | None, project_source: str
) -> Check:
    """Issue #103: every object prints each layer on the heads its filaments call for."""
    if project is None:
        return Check(
            "head seq",
            SKIP,
            "no project 3MF: pass --source-3mf FILE.3mf or keep NAME.3mf next to NAME.gcode",
        )
    from slicing.head_sequence import ProjectError, preflight_check

    try:
        result = preflight_check(str(gcode), str(project))
    except ProjectError as exc:
        return Check("head seq", SKIP, f"{project_source}: {exc}")
    return _head_sequence_verdict(result, project_source)


def _head_sequence_verdict(result: dict[str, Any], project_source: str) -> Check:
    """The `head seq` line of one `slicing.head_sequence.preflight_check` result."""
    objects = result.get("objects")
    objs = (
        [o for o in objects if isinstance(o, dict)] if isinstance(objects, list) else []
    )
    status = str(result.get("status", FAIL))
    if status == SKIP:
        return Check(
            "head seq",
            SKIP,
            f"nothing to sequence: no mixed filaments and every object on one head "
            f"({len(objs)} object(s), {project_source})",
        )
    if status == FAIL:
        return _head_seq_failed(result, objs, project_source)
    return Check(
        "head seq",
        PASS,
        f"{len(objs)} object(s) on their recipes, {result.get('mixed_rows')} mixed row(s) "
        f"({project_source})",
    )


def _head_seq_failed(
    result: dict[str, Any], objs: list[dict[str, Any]], project_source: str
) -> Check:
    lines = [
        f"{o.get('status')} {o.get('name')}: {o.get('detail') or 'heads ' + str(o.get('heads'))}"
        for o in objs
        if o.get("status") != "ok"
    ]
    return Check(
        "head seq",
        FAIL,
        f"{result.get('failures')} object(s) off their recipes ({project_source})",
        lines,
    )


def check_mix_faces(project: Path | None, project_source: str, gcode: Path) -> Check:
    """Issues #411 / #419: a part in a mixed filament shows its colour on walls, not on a flat top.
    By the project 3MF when there is one (exact: it knows which parts are mixed, FAIL); else by the
    G-code alone (objects printed on more than one head; WARN, a multi-colour part matches too)."""
    if project is None:
        from slicing.mix_gcode_faces import preflight_check as gcode_check

        got = gcode_check(str(gcode))
        lines = got.get("lines")
        return Check(
            "mix face",
            str(got.get("status", FAIL)),
            f"{got.get('detail')} (G-code only: no project 3MF; --source-3mf FILE.3mf or NAME.3mf next to "
            "NAME.gcode judges by the project)",
            [str(x) for x in lines] if isinstance(lines, list) else [],
        )
    from slicing.mix_faces import preflight_check

    result = preflight_check(str(project))
    lines = result.get("lines")
    return Check(
        "mix face",
        str(result.get("status", FAIL)),
        f"{result.get('detail')} ({project_source})",
        [str(x) for x in lines] if isinstance(lines, list) else [],
    )


def check_mix_layers(gcode: Path, project: Path | None, project_source: str) -> Check:
    """Layer rules on what was printed: the additional colour never on consecutive layers, no
    band or layer thicker than the cap, measured on the real per-layer heads and Z."""
    from slicing.mix_gcode import preflight_check

    result = preflight_check(str(gcode), str(project) if project else None)
    lines = result.get("lines")
    where = (
        project_source
        if project is not None
        else "no project 3MF: alternating objects only"
    )
    return Check(
        "mix layers",
        str(result.get("status", FAIL)),
        f"{result.get('detail')} ({where})",
        [str(x) for x in lines] if isinstance(lines, list) else [],
    )


def check_visible(gcode: Path, specs: Sequence[str]) -> Check:
    """`3d visible-faces --gcode`: no support / sag on the declared visible sides."""
    if not specs:
        return Check(
            "visible",
            SKIP,
            "no --visible spec: the visible side is a design input "
            "(e.g. --visible 'WALL*:y<104.8')",
        )
    argv = [
        sys.executable,
        str(_BIN_3D),
        "visible-faces",
        "--gcode",
        str(gcode),
        "--json",
    ]
    for spec in specs:
        argv += ["--visible", spec]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=900)
    if r.returncode == 0:
        return Check("visible", PASS, f"visible sides clean ({', '.join(specs)})")
    if r.returncode == 1:
        return Check(
            "visible",
            FAIL,
            f"visible-faces FAIL ({', '.join(specs)}); see 3d visible-faces --gcode {gcode}",
        )
    return Check(
        "visible",
        FAIL,
        f"visible-faces could not run (exit {r.returncode}): {_tail(r.stderr)}",
    )


def _first(violations: Sequence[Violation], count: int, where: str = "outside") -> str:
    shown = ", ".join(str(v) for v in violations)
    return (
        f"{count} point(s) {where}: {shown}{', ...' if count > len(violations) else ''}"
    )


def check_macros(rep: BoundsReport, trusted: frozenset[str]) -> list[Check]:
    """Macros hide their moves. Untrusted ones FAIL (start is refused); the printer's
    `trusted_macros` (PROFILE_FILES) names the ones known to stay on the bed."""
    if not rep.macros:
        return []

    def fmt(names: list[str]) -> str:
        return ", ".join(
            f"{k}×{rep.macros[k][0]} (line {rep.macros[k][1]})" for k in names
        )

    untrusted = sorted(k for k in rep.macros if k not in trusted)
    if untrusted:
        return [
            Check(
                "macros",
                FAIL,
                f"moves inside these commands are not checked: {fmt(untrusted)}; "
                "if they stay on the bed add them to trusted_macros for this printer in "
                f"{PROFILE_FILES}",
            )
        ]
    return [
        Check(
            "macros",
            PASS,
            f"all in the printer's trusted_macros ({PROFILE_FILES}): "
            + fmt(sorted(rep.macros)),
        )
    ]


def _axis_travel_check(
    rep: BoundsReport, axis_limits: Limits | None, limits_source: str
) -> Check:
    if axis_limits is None:
        why = f"{limits_source}; " if limits_source else ""
        return Check(
            "axis travel",
            SKIP,
            f"printer axis limits unknown ({why}pass --axis-limits "
            f"X0:X1,Y0:Y1 or set axis_limits in {PROFILE_FILES})",
        )
    if rep.axis_out_count:
        return Check(
            "axis travel",
            FAIL,
            f"{fmt_limits(axis_limits)} ({limits_source}): "
            + _first(rep.axis_out, rep.axis_out_count),
        )
    return Check(
        "axis travel",
        PASS,
        f"all moves {fmt_extent(rep.all_extent)} within "
        f"{fmt_limits(axis_limits)} ({limits_source})",
    )


def _print_area_violations(rep: BoundsReport) -> list[str]:
    """printable_area / bed_exclude_area breaches; each is evaluated on its own, so a missing
    printable_area must not hide an exclusion."""
    parts = []
    if rep.area_out_count and rep.printable_area:
        parts.append(
            f"printable_area {fmt_area(rep.printable_area)}: "
            + _first(rep.area_out, rep.area_out_count)
        )
    if rep.excluded_in_count and rep.exclude_area:
        parts.append(
            f"bed_exclude_area {fmt_area(rep.exclude_area)}: "
            + _first(rep.excluded_in, rep.excluded_in_count, "inside")
        )
    return parts


def _print_area_check(rep: BoundsReport) -> Check:
    if not rep.plate_found:
        return Check(
            "print area",
            WARN,
            "UNVERIFIED: no ;LAYER_CHANGE / ; CHANGE_LAYER markers, "
            "the plate section cannot be told from start/end G-code",
        )
    parts = _print_area_violations(rep)
    if parts:
        return Check(
            "print area",
            FAIL,
            f"plate moves {fmt_extent(rep.plate_extent)}; " + "; ".join(parts),
        )
    if rep.printable_area is None:
        return Check(
            "print area", WARN, "UNVERIFIED: the G-code has no `; printable_area`"
        )
    return Check(
        "print area",
        PASS,
        f"plate moves {fmt_extent(rep.plate_extent)} within "
        f"{fmt_area(rep.printable_area)}",
    )


def check_moves(
    rep: BoundsReport,
    axis_limits: Limits | None,
    limits_source: str,
    trusted_macros: frozenset[str] = frozenset(),
) -> list[Check]:
    out = [
        _axis_travel_check(rep, axis_limits, limits_source),
        _print_area_check(rep),
    ]
    if rep.unverified_count:
        more = ", ..." if rep.unverified_count > len(rep.unverified) else ""
        out.append(
            Check(
                "unverified",
                WARN,
                f"{rep.unverified_count} move(s) only partly checked: "
                + ", ".join(rep.unverified)
                + more,
            )
        )
    out.extend(check_macros(rep, trusted_macros))
    return out


def bounds_report(
    gcode: Path,
    axis_limits: Limits | None,
    limits_source: str,
    trusted_macros: frozenset[str] = frozenset(),
) -> Report:
    """Only the move-bounds checks (`3d gcode-bounds`)."""
    rep = check_bounds(gcode, axis_limits)
    return Report(
        str(gcode),
        check_moves(rep, axis_limits, limits_source, trusted_macros),
        "BOUNDS",
    )


def _bed_when(v: dict[str, Any], view: dict[str, Any]) -> str:
    """Where the verdict came from, and how the bed got to the Z it was judged at."""
    age = v.get("age_s")
    when = (
        f"{v.get('source', '?')}, {age:.0f} s ago"
        if isinstance(age, (int, float))
        else str(v.get("source", "?"))
    )
    if view.get("action") == "moved":
        when += (
            f", judged at Z {view.get('z_used', v.get('z'))} after moving the bed from Z "
            f"{view.get('moved_from')}" + (" and back" if view.get("restored") else "")
        )
    return when


def _bed_evidence(v: dict[str, Any], view: dict[str, Any]) -> list[str]:
    """The frame, a bed left away from where it was found, and what the printer's light did."""
    lines = [f"frame: {v['frame']}"] if v.get("frame") else []
    if view.get("action") == "moved" and not view.get("restored"):
        lines.append(
            f"BED NOT PUT BACK to Z {view.get('moved_from')}: {view.get('restore_error')}"
        )
    light = v.get("light")
    if isinstance(light, dict) and light.get("text"):
        lines.append(f"light: {light['text']}")
    return lines


def _bed_check_command(v: dict[str, Any]) -> str:
    return f"3d printer bed check {v.get('printer') or 'NAME'}"


def _assume_hint(v: dict[str, Any]) -> str:
    """How the owner says the bed is empty when they looked and the camera is wrong (bed_override.py)."""
    return (
        f'; if you looked and the bed is empty: `{_bed_check_command(v)} --assume-empty "why"` '
        "(the line then passes for 60 min while no job starts)"
    )


def _check_bed_occupied(
    v: dict[str, Any], when: str, lines: list[str], view_min: float
) -> Check:
    """FAIL only on a CONFIDENT `occupied` (bed_state.occupied_confident: a region of the normal
    occupied size with at least `view_min` of the plate in the frame). The same pixels at a poor view
    (the U1 at Z 200 sees 15 % of the plate: a window light read as 16 % changed) or in a verdict
    that does not say it is confident are a WARN with the way to look properly."""
    why = str(v.get("reason") or "")
    if v.get("occupied_confident") is True:
        return Check(
            "bed", FAIL, f"objects on the bed ({when}): {why}{_assume_hint(v)}", lines
        )
    in_view = (v.get("metrics") or {}).get("bed_in_view")
    low = (
        f"only {in_view:.0%} of the plate is in the camera's view at this Z (needs {view_min:.0%})"
        if isinstance(in_view, (int, float)) and in_view < view_min
        else "the verdict does not mark it as a confident find"
    )
    return Check(
        "bed",
        WARN,
        f"objects suspected on the bed but not confirmed: {low} ({when}): {why}; "
        f"run `{_bed_check_command(v)}` (best view, light on) and look at the frame"
        f"{_assume_hint(v)}",
        lines,
    )


def check_bed_verdict(v: dict[str, Any], *, view_min: float | None = None) -> Check:
    """`bed_state.py status --json` (issue #303) -> the `bed` line. A verdict judged after the auto view
    moved the bed names the Z it was judged at; a bed left away from where it was found is a WARN.
    FAIL on a missing plate or a CONFIDENT `occupied` only; `unknown` and an occupied that is not
    confident (low view of the plate, `view_min` = watch.bed.auto_view_min) are a WARN naming the command."""
    from printer_ops.bed import AUTO_VIEW_MIN

    bed, plate = v.get("bed", "unknown"), v.get("plate", "unknown")
    raw_view = v.get("view")
    view: dict[str, Any] = raw_view if isinstance(raw_view, dict) else {}
    when, lines = _bed_when(v, view), _bed_evidence(v, view)
    why = str(v.get("reason") or "")
    left_away = view.get("action") == "moved" and not view.get("restored")
    if left_away and plate != "missing" and bed != "occupied":
        return Check("bed", WARN, f"bed state {bed} ({when}): {why}", lines)
    if plate == "missing":
        return Check("bed", FAIL, f"build plate missing ({when}): {why}", lines)
    if bed == "occupied":
        return _check_bed_occupied(
            v, when, lines, AUTO_VIEW_MIN if view_min is None else view_min
        )
    if bed == "clear" and plate == "present":
        return Check("bed", PASS, f"clear, plate present ({when})", lines)
    if bed == "clear":
        return Check("bed", WARN, f"clear, plate not judged ({when}): {why}", lines)
    return Check(
        "bed",
        WARN,
        f"bed state {bed} ({when}): {why}; "
        f"run `{_bed_check_command(v)}` for the best view with the light on"
        f"{_assume_hint(v)}",
        lines,
    )


def check_bed(
    device: Device,
    *,
    move: bool = True,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float | None = None,
    now: float | None = None,
    adapter_for: Callable[[Device], Any] | None = None,
) -> Check:
    """The camera `bed` line (printer_ops/bed.py): SKIP when the printer's bed monitor is off, else
    the FULL bed check path of an idle printer (`bed_state.py status --best-view`, what `3d printer
    bed check` does): a fresh frame judged from the best view (the bed moved in Z only, put back),
    with the printer's light on and restored, instead of the watcher's verdict at whatever Z the
    bed rests at. `move=False` (--no-bed-move) judges at the current Z, light still on. FAIL only
    on a confident `occupied` (check_bed_verdict). A check that cannot run is a WARN, never a crash."""
    from printer_ops.bed import bed_config

    cfg = bed_config(device)
    if not cfg.enabled:
        return Check(
            "bed",
            SKIP,
            f"bed camera check off for {device.name} (watch.bed.enabled in {device.source})",
        )
    confirmed, why_not = _owner_confirmation(device, now, adapter_for)
    if confirmed is not None:  # the owner looked: no camera, no move
        return Check("bed", PASS, confirmed)
    check = _bed_camera_check(device, cfg, move, runner, timeout)
    if why_not:
        check.lines.append(why_not)
    return check


def _owner_confirmation(
    device: Device, now: float | None, adapter_for: Callable[[Device], Any] | None
) -> tuple[str | None, str | None]:
    """(the PASS note, or why a recorded `--assume-empty` does not count): bed_override.py. No record: no I/O."""
    import time

    from printer_ops import bed_override, make_adapter

    if bed_override.read_record(device.name) is None:
        return None, None
    now = time.time() if now is None else now
    adapter = adapter_for(device) if adapter_for else make_adapter(device, timeout=10.0)
    confirmation, why = bed_override.honoured(device.name, adapter, now)
    return (None if confirmation is None else confirmation.note(now)), why


def _bed_camera_check(
    device: Device,
    cfg: Any,
    move: bool,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    timeout: float | None,
) -> Check:
    from printer_ops.bed import STATUS_MOVE_TIMEOUT_S, STATUS_TIMEOUT_S, status_argv

    if (
        timeout is None
    ):  # a move is two Z moves, a settle wait and a frame wait; never kill it half-way
        timeout = STATUS_MOVE_TIMEOUT_S if move else STATUS_TIMEOUT_S
    try:
        argv = status_argv(device.name, move="force" if move else "never")
        r = runner(argv, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:  # no runtime / timeout
        return Check("bed", WARN, f"bed check could not run: {exc}")
    try:
        verdict = json.loads(r.stdout)
    except ValueError:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or ["no output"]
        return Check(
            "bed", WARN, f"bed check failed (exit {r.returncode}): {tail[0][:300]}"
        )
    if not isinstance(verdict, dict):
        return Check("bed", WARN, f"bed check returned {str(verdict)[:200]}")
    return check_bed_verdict(verdict, view_min=cfg.auto_view_min)


def run_preflight(
    gcode: Path,
    *,
    status: PrinterStatus | None = None,
    source_3mf: Path | None = None,
    axis_limits: Limits | None = None,
    limits_source: str = "",
    trusted_macros: frozenset[str] = frozenset(),
    origin: Path | None = None,
    visible: Sequence[str] = (),
    bridge_max: float | None = None,
    bridge_source: str = "default",
    bed: Check | None = None,
    printops: PrintopsContext | None = None,
    colour_delta_e: float = DEFAULT_SLOT_DELTA_E,
) -> Report:
    """`colour_delta_e` is the CIEDE2000 tolerance of the `slot Tn` colour (default 10; the line
    shows the distance). `limits_source` names where `axis_limits` came from, or why they are unknown.
    `origin` is where the G-code lives when `gcode` is a snapshot copy (`printer start`): the
    sibling NAME.3mf and NAME.slice-warnings.json are looked up there. `bed` is the camera
    bed line (`check_bed` above), computed by the caller that knows the device."""
    report = Report(str(gcode))
    report.checks.append(check_thumbnails(gcode, origin))
    if status is not None:
        report.checks.append(check_printer_free(status))
        report.checks.extend(
            check_slots(read_gcode_meta(gcode), status, colour_delta_e)
        )
    if bed is not None:
        report.checks.append(bed)
    report.checks.extend(
        check_moves(
            check_bounds(gcode, axis_limits), axis_limits, limits_source, trusted_macros
        )
    )
    report.checks.append(check_overhangs(gcode, bridge_max, bridge_source))
    report.checks.append(check_slice_warnings(origin or gcode, gcode))
    project, project_source = find_project_3mf(origin or gcode, source_3mf)
    report.checks.append(check_head_sequence(gcode, project, project_source))
    report.checks.append(check_mix_faces(project, project_source, gcode))
    report.checks.append(check_mix_layers(gcode, project, project_source))
    report.checks.append(check_visible(gcode, visible))
    if source_3mf is not None:
        report.checks.append(check_halves(source_3mf))
    if printops is not None:
        from printer_ops.printops_checks import run_checks

        report.checks.extend(
            run_checks(gcode, printops, origin=origin or gcode, project=project).checks
        )
    return report
