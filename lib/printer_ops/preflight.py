"""preflight.py — checks run before a print starts (`3d print-preflight`, `3d printer start`).

WHAT (issue https://github.com/alex-mextner/3d-cli/issues/96), one PASS/WARN/FAIL/SKIP line each:
  - thumbnails   the G-code carries the preview images its `; thumbnails =` spec asks for
                 (lib/gcode_thumbnails.py check(); WARN "UNVERIFIED" when that module is absent);
  - printer      the target printer is free (not printing/paused/busy/unknown);
  - slot Tn      for every tool the G-code uses: the printer's head is loaded, and its colour
                 and material equal the G-code's `; filament_colour` / `; filament_type`
                 (needs per-head data: Moonraker printers exposing print_task_config, i.e. U1);
  - halves       `3d halves-check <3mf> --json` when the source 3MF is given.

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
from dataclasses import dataclass, field
from pathlib import Path

from printer_ops.base import PrinterStatus

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

    @property
    def overall(self) -> str:
        states = {c.status for c in self.checks}
        return FAIL if FAIL in states else WARN if WARN in states else PASS

    def to_json(self) -> dict[str, object]:
        return {"file": self.file, "overall": self.overall,
                "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in self.checks]}

    def render(self) -> str:
        lines = [f"PREFLIGHT {self.file}"]
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
    try:
        # lib/gcode_thumbnails.py ships separately (`3d gcode-thumbnails`); absent -> UNVERIFIED.
        from gcode_thumbnails import check as thumb_check  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        return Check("thumbnails", WARN, "UNVERIFIED: the thumbnail checker (lib/gcode_thumbnails.py) is not installed")
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


def run_preflight(gcode: Path, *, status: PrinterStatus | None = None, source_3mf: Path | None = None) -> Report:
    report = Report(str(gcode))
    report.checks.append(check_thumbnails(gcode))
    if status is not None:
        report.checks.append(check_printer_free(status))
        report.checks.extend(check_slots(read_gcode_meta(gcode), status))
    if source_3mf is not None:
        report.checks.append(check_halves(source_3mf))
    return report
