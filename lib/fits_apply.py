"""fits_apply.py — put fit-table clearances into OpenSCAD text: emit a snippet, or correct declared values.

WHAT: two pure text operations behind `3d fits apply`.
  - `scad_snippet(resolution, var)`: the lines a designer pastes into a mate (`clearance = 0.35; // @fit snap
    shape=round`, plus the tolerance as `clearance_tol`).
  - `correct_text(text, resolve)`: finds every DECLARED clearance - a line `NAME = NUMBER; // @fit TYPE
    [shape=SHAPE] ...` - and rewrites NUMBER to what `resolve(TYPE, SHAPE)` returns for the chosen printer x
    material. `3d fit-niche` writes its `clearance` line in this form, so an existing insert is re-corrected
    for another printer without regenerating it.

INVARIANTS:
  - The marker is the only thing that makes a number a fit: a bare `clearance = 0.2;` is never touched, so
    the correction cannot rewrite a number that merely looks like a clearance.
  - The value is the ABSOLUTE `resolve` answer, not a delta on the old number: re-applying the same
    printer/material is a no-op (idempotent) and a hand-edited number under a marker is replaced.
  - Only the number changes; the indentation, spacing and the rest of the comment are kept byte for byte.
  - Pure functions, no I/O: the command decides whether anything is written (`--yes`).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fits import Resolution

DEFAULT_SHAPE = "planar"
_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_DECLARATION = re.compile(
    rf"^(?P<head>\s*(?P<name>[A-Za-z_]\w*)\s*=\s*)(?P<value>{_NUMBER})(?P<mid>\s*;\s*//\s*@fit\s+"
    r"(?P<fit>\w+)(?P<opts>(?:\s+\w+=[\w.+-]+)*))(?P<rest>.*)$"
)


@dataclass(frozen=True)
class Change:
    line: int
    name: str
    fit: str
    shape: str
    before: float
    after: float
    resolution: Resolution

    @property
    def changed(self) -> bool:
        return abs(self.after - self.before) > 1e-9

    def to_dict(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "name": self.name,
            "fit": self.fit,
            "shape": self.shape,
            "before_mm": self.before,
            "after_mm": self.after,
            "changed": self.changed,
            "tol_mm": self.resolution.tol_mm,
            "fallback": self.resolution.fallback,
            "xy_scale": self.resolution.xy_scale,
            "provenance": [dict(p) for p in self.resolution.provenance],
        }


def format_mm(value: float) -> str:
    """A compact OpenSCAD literal: 0.35, -0.1, 0.2 (no trailing zeros, no exponent for these magnitudes)."""
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def scad_snippet(res: Resolution, var: str = "clearance") -> str:
    """Lines for a mate: the per-face clearance (with the `@fit` marker) and its tolerance, plus provenance."""
    who = " ".join(x for x in (res.printer, res.material) if x) or "table defaults"
    corr = next(p for p in res.provenance if p["kind"] == "correction")
    lines = [
        f"// 3d fits apply: {res.fit_type}/{res.shape} for {who} ({corr['status']})",
        f"{var} = {format_mm(res.clearance_mm)};  // @fit {res.requested} shape={res.shape}"
        " mm per mating face (radial for round); negative = interference",
        f"{var}_tol = {format_mm(res.tol_mm)};  // +/- mm scatter of the printed gap",
    ]
    if res.xy_scale is not None:
        lines.append(
            f"// slicer: scale the model XY by {res.xy_scale:g} (material shrink compensation)"
        )
    return "\n".join(lines) + "\n"


def correct_text(
    text: str, resolve: Callable[[str, str], Resolution]
) -> tuple[str, list[Change]]:
    """Rewrite every `@fit`-marked number of `text`; returns the new text and one `Change` per marker."""
    out: list[str] = []
    changes: list[Change] = []
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        m = _DECLARATION.match(body)
        if m is None:
            out.append(line)
            continue
        shape = _shape_of(m["opts"])
        res = resolve(m["fit"], shape)
        before = float(m["value"])
        changes.append(
            Change(number, m["name"], m["fit"], shape, before, res.clearance_mm, res)
        )
        value = format_mm(res.clearance_mm) if changes[-1].changed else m["value"]
        out.append(f"{m['head']}{value}{m['mid']}{m['rest']}{ending}")
    return "".join(out), changes


def _shape_of(opts: str) -> str:
    for token in opts.split():
        key, _, value = token.partition("=")
        if key == "shape":
            return value
    return DEFAULT_SHAPE
