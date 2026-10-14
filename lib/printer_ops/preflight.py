"""preflight.py — checks run before a print starts (`3d print-preflight`, `3d printer start`).

WHAT (issue https://github.com/alex-mextner/3d-cli/issues/96), one PASS/WARN/FAIL/SKIP line each:
  - thumbnails   the G-code carries the preview images its `; thumbnails =` spec asks for
                 (lib/gcode_thumbnails.py check(), the `3d gcode-thumbnails` checker);
  - printer      the target printer is free (not printing/paused/busy/unknown);
  - slot Tn      for every tool the G-code uses: the printer's head is loaded, and its colour
                 and material equal the G-code's `; filament_colour` / `; filament_type`
                 (needs per-head data: Moonraker printers exposing print_task_config, i.e. U1);
  - halves       `3d halves-check <3mf> --json` when the source 3MF is given;
  - axis travel  every move (arcs by their real extremes) inside the printer's X/Y limits,
                 start/end G-code included; SKIP when the limits are unknown (move_bounds.py);
  - print area   plate moves inside the G-code's `; printable_area`, outside `; bed_exclude_area`.

WHY: wrong colour in a head, a used head that is empty (the U1's broken T2), missing
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
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from printer_ops.base import PrinterStatus
from printer_ops.move_bounds import (
    BoundsReport,
    Limits,
    Violation,
    check_bounds,
    fmt_area,
    fmt_extent,
    fmt_limits,
)

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
_CONFIG_LINE = re.compile(r"^;\s*([a-z_ \[\]0-9]+?)\s*=\s*(.*)$")
_TOOL_LINE = re.compile(r"^T(\d+)\s*(?:;.*)?$")
_BIN_3D = Path(__file__).resolve().parents[2] / "bin" / "3d"


@dataclass
class Check:
    name: str
    status: str
    detail: str


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
        return {"file": self.file, "overall": self.overall,
                "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in self.checks]}

    def render(self) -> str:
        lines = [f"{self.title} {self.file}"]
        lines += [f"  {c.status:<4}  {c.name:<11} {c.detail}" for c in self.checks]
        lines.append(f"STATUS: {self.overall}")
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
    return "#" + v[:6].upper() if re.fullmatch(r"[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", v) else None


def read_gcode_meta(path: Path) -> GcodeMeta:
    """Filament colours/types and the tools actually used, from slicer comments + Tn lines."""
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
    used: list[int]
    used_mm = config.get("filament used [mm]")
    if used_mm:
        amounts = []
        for v in used_mm.split(","):
            try:
                amounts.append(float(v))
            except ValueError:
                amounts.append(0.0)
        used = [i for i, a in enumerate(amounts) if a > 0]
    elif tool_lines:
        used = sorted(tool_lines)
    else:
        used = [0]
    return GcodeMeta(
        colours=[_hex(c) for c in _split(config.get("filament_colour", ""))],
        types=[(t or None) for t in _split(config.get("filament_type", ""))],
        used_tools=used,
    )


def check_thumbnails(path: Path) -> Check:
    from gcode_thumbnails import check as thumb_check  # lazy: keeps `3d help` import-light

    r = thumb_check(str(path))
    if r.ok:
        sizes = ", ".join(f"{t.width}x{t.height}" for t in r.present) or "present"
        return Check("thumbnails", PASS, sizes)
    # ThumbCheck.reason already names what is missing ("missing 48x48/PNG"); the list is the fallback.
    reason = r.reason or "missing " + (", ".join(str(t) for t in r.missing) or "thumbnails")
    return Check("thumbnails", FAIL, f"{reason}; fix: 3d gcode-thumbnails add FILE")


def check_printer_free(status: PrinterStatus) -> Check:
    if status.busy:
        job = f" ({status.filename})" if status.filename else ""
        return Check("printer", FAIL, f"printer is {status.state}{job} [{status.raw_state}]")
    return Check("printer", PASS, f"{status.state} [{status.raw_state}]")


def check_slots(meta: GcodeMeta, status: PrinterStatus) -> list[Check]:
    if not status.slots:
        return [Check("slots", SKIP, "the printer reports no per-head filament data")]
    out = []
    for t in meta.used_tools:
        name = f"slot T{t}"
        if t >= len(status.slots):
            out.append(Check(name, FAIL, f"G-code uses tool {t} but the printer has {len(status.slots)} heads"))
            continue
        slot = status.slots[t]
        if not slot.present:
            out.append(Check(name, FAIL, f"head {slot.label} is used by the G-code but has no filament / is disabled"))
            continue
        want_c = meta.colours[t] if t < len(meta.colours) else None
        want_m = meta.types[t] if t < len(meta.types) else None
        problems = []
        if want_c and slot.color and want_c != slot.color:
            problems.append(f"colour: G-code {want_c}, loaded {slot.color}")
        if want_m and slot.material and want_m.upper() != slot.material.upper():
            problems.append(f"material: G-code {want_m}, loaded {slot.material}")
        if problems:
            out.append(Check(name, FAIL, f"head {slot.label}: " + "; ".join(problems)))
        elif not want_c:
            out.append(Check(name, WARN, f"head {slot.label}: G-code has no filament_colour for tool {t}; "
                             f"loaded {slot.color} {slot.material}"))
        else:
            out.append(Check(name, PASS, f"head {slot.label}: {slot.color} {slot.material or ''}".rstrip()))
    return out


def run_halves_check(source: Path) -> tuple[int, str, str]:
    r = subprocess.run([sys.executable, str(_BIN_3D), "halves-check", str(source), "--json"],
                       capture_output=True, text=True, timeout=900)
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
        bad = [str(p.get("pair")) for p in pairs if isinstance(p, dict) and p.get("status") == FAIL]
        return Check("halves", FAIL, f"halves-check FAIL: {', '.join(bad) or 'see 3d halves-check ' + str(source)}")
    tail = (err.strip().splitlines() or ["no output"])[-1]
    return Check("halves", FAIL, f"halves-check could not run (exit {rc}): {tail}")


def _first(violations: Sequence[Violation], count: int, where: str = "outside") -> str:
    shown = ", ".join(str(v) for v in violations)
    return f"{count} point(s) {where}: {shown}{', ...' if count > len(violations) else ''}"


def check_moves(rep: BoundsReport, axis_limits: Limits | None, limits_source: str) -> list[Check]:
    out: list[Check] = []
    if axis_limits is None:
        why = f"{limits_source}; " if limits_source else ""
        out.append(Check("axis travel", SKIP, f"printer axis limits unknown ({why}pass --axis-limits "
                         "X0:X1,Y0:Y1 or set axis_limits in printer-devices.yaml)"))
    elif rep.axis_out_count:
        out.append(Check("axis travel", FAIL, f"{fmt_limits(axis_limits)} ({limits_source}): "
                         + _first(rep.axis_out, rep.axis_out_count)))
    else:
        out.append(Check("axis travel", PASS, f"all moves {fmt_extent(rep.all_extent)} within "
                         f"{fmt_limits(axis_limits)} ({limits_source})"))

    if not rep.plate_found:
        out.append(Check("print area", WARN, "UNVERIFIED: no ;LAYER_CHANGE / ; CHANGE_LAYER markers, "
                         "the plate section cannot be told from start/end G-code"))
    else:
        # bed_exclude_area is evaluated on its own: a missing printable_area must not hide it
        parts = []
        if rep.area_out_count and rep.printable_area:
            parts.append(f"printable_area {fmt_area(rep.printable_area)}: "
                         + _first(rep.area_out, rep.area_out_count))
        if rep.excluded_in_count and rep.exclude_area:
            parts.append(f"bed_exclude_area {fmt_area(rep.exclude_area)}: "
                         + _first(rep.excluded_in, rep.excluded_in_count, "inside"))
        if parts:
            out.append(Check("print area", FAIL, f"plate moves {fmt_extent(rep.plate_extent)}; " + "; ".join(parts)))
        elif rep.printable_area is None:
            out.append(Check("print area", WARN, "UNVERIFIED: the G-code has no `; printable_area`"))
        else:
            out.append(Check("print area", PASS, f"plate moves {fmt_extent(rep.plate_extent)} within "
                             f"{fmt_area(rep.printable_area)}"))

    if rep.unverified_count:
        more = ", ..." if rep.unverified_count > len(rep.unverified) else ""
        out.append(Check("unverified", WARN, f"{rep.unverified_count} move(s) only partly checked: "
                         + ", ".join(rep.unverified) + more))
    return out


def bounds_report(gcode: Path, axis_limits: Limits | None, limits_source: str) -> Report:
    """Only the move-bounds checks (`3d gcode-bounds`)."""
    return Report(str(gcode), check_moves(check_bounds(gcode, axis_limits), axis_limits, limits_source), "BOUNDS")


def run_preflight(gcode: Path, *, status: PrinterStatus | None = None, source_3mf: Path | None = None,
                  axis_limits: Limits | None = None, limits_source: str = "") -> Report:
    """`limits_source` names where `axis_limits` came from, or why they are unknown."""
    report = Report(str(gcode))
    report.checks.append(check_thumbnails(gcode))
    if status is not None:
        report.checks.append(check_printer_free(status))
        report.checks.extend(check_slots(read_gcode_meta(gcode), status))
    report.checks.extend(check_moves(check_bounds(gcode, axis_limits), axis_limits, limits_source))
    if source_3mf is not None:
        report.checks.append(check_halves(source_3mf))
    return report
