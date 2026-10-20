"""mix_gcode.py — measure the per-layer tool sequence of a sliced G-code against the layer rules.

WHAT: for every object printed in a mixed filament, reads which head printed it on each layer and
  the real layer Z (so variable layer heights are measured, not assumed), and applies the rules of
  slicing/mix_pattern.py to the sequence that was actually printed:
    - the additional (minor) colour on two or more consecutive layers: FAIL, with the layer
      numbers, the head and the band thickness in mm;
    - a colour band thicker than the run limit, a layer thicker than the cap (default 0.12 mm):
      FAIL, with the layer and its mm.
  With the project 3MF (`--project`, or NAME.3mf next to NAME.gcode in print-preflight) the mixed
  rows say which heads an object's mix uses; the mix head of a layer is the head printed on it
  that is not one of the object's fixed (non-mixed) heads — or, when only fixed heads printed,
  the mix's own head among them. The check covers the layers from the first to the last one on
  which a head used ONLY by the mix printed: a mix part that shares a head with the solid colour
  of its object cannot be told from it outside that window, so the edge layers go unchecked
  rather than guessed (a missed edge layer, never a false FAIL).
  Without a project, only objects that print exactly one head per layer and alternate (a run
  between two head changes) are measured: the runs inside the alternation, never the first or
  last run (those may be solid colour).
WHY: a recipe that passes the validator can still print wrong — the slicer indexes the pattern by
  layer number, the wrap-around of GWG is GWGGWG, a variable layer height thickens a stripe.
  Only the G-code says what was printed (2026-10-20, Chekhov on the U1: "the additional colour
  must never print several layers in a row as thick stripes").
INVARIANTS: stdlib only (print-preflight imports it in-process); the first layer is exempt from the
  layer cap (it is the bed layer, usually 0.2 mm); raft layers are skipped; ratio (Local-Z) rows
  are reported SKIP — they print both components inside one layer.
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass, field

from slicing.head_sequence import (
    ProjectError,
    _match_object,
    _read_project,
    scan_gcode,
)
from slicing.mix_pattern import (
    LAYER_TOO_THICK,
    MINOR_ADJACENT,
    MixReport,
    MixRules,
    Violation,
    analyse,
)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


@dataclass
class MixLayers:
    """One mixed filament of one object, as printed."""

    object: str
    filament: int  # project filament id of the mixed row (0 = unknown, no project)
    recipe: str  # the planned pattern as heads ('' = unknown or ratio row)
    status: str
    detail: str
    layers: int = 0  # layers measured
    first_layer: int = 0
    sequence: str = ""  # one head digit per measured layer
    report: MixReport | None = None

    def to_dict(self) -> dict[str, builtins.object]:
        return {
            "object": self.object,
            "filament": self.filament,
            "recipe": self.recipe,
            "status": self.status,
            "detail": self.detail,
            "layers": self.layers,
            "first_layer": self.first_layer,
            "sequence": self.sequence,
            "report": self.report.to_dict() if self.report else None,
        }


@dataclass
class MixGcodeReport:
    results: list[MixLayers] = field(default_factory=list)
    rules: MixRules = MixRules()  # noqa: B008 — frozen dataclass

    @property
    def status(self) -> str:
        states = {r.status for r in self.results}
        if FAIL in states:
            return FAIL
        return PASS if PASS in states else SKIP

    @property
    def failures(self) -> int:
        return sum(r.status == FAIL for r in self.results)

    def to_dict(self) -> dict[str, object]:
        return {
            "check": "mix-layers",
            "status": self.status,
            "failures": self.failures,
            "layer_cap_mm": self.rules.layer_cap_mm,
            "max_run_mm": self.rules.run_limit_mm,
            "results": [r.to_dict() for r in self.results],
        }


def layer_thickness(layer_z: dict[int, float]) -> dict[int, float]:
    """Layer index -> thickness: the Z step from the layer below (the first layer: its own Z)."""
    out: dict[int, float] = {}
    for k in sorted(layer_z):
        below = layer_z.get(k - 1)
        out[k] = layer_z[k] - (below if below is not None else 0.0)
    return out


def _segments(seq: dict[int, int]) -> list[list[int]]:
    """Layer numbers of `seq` in runs of consecutive layers: a gap (a layer with no
    attributable mix head, a second mix part higher up) ends a segment, so two runs of one
    head on either side of it are never joined."""
    out: list[list[int]] = []
    for k in sorted(seq):
        if out and out[-1][-1] == k - 1:
            out[-1].append(k)
        else:
            out.append([k])
    return out


def _measure(
    name: str,
    filament: int,
    recipe: str,
    seq: dict[int, int],
    thick: dict[int, float],
    rules: MixRules,
) -> MixLayers:
    """Apply the rules to {layer: mix head}, segment by segment (see `_segments`)."""
    graded: list[tuple[list[int], MixReport]] = []
    for layers in _segments(seq):
        # the bed layer is one fixed 0.2 mm layer: judged at the cap, never a violation
        heights = [
            min(thick[k], rules.layer_cap_mm) if k == 0 else thick[k] for k in layers
        ]
        report = analyse([str(seq[k]) for k in layers], heights, rules, cyclic=False)
        if report.mixed:
            graded.append((layers, report))
    measured = sum(len(ls) for ls, _ in graded)
    text = "|".join("".join(str(seq[k]) for k in ls) for ls, _ in graded)
    if not graded:
        return MixLayers(
            name,
            filament,
            recipe,
            SKIP,
            "no stretch of layers on which the mix alternates between heads",
        )
    failed = [(ls, r) for ls, r in graded if not r.ok]
    first_layer = graded[0][0][0]
    if failed:
        detail = "; ".join(
            _describe(v, ls, rules) for ls, r in failed for v in r.violations
        )
        return MixLayers(
            name,
            filament,
            recipe,
            FAIL,
            detail,
            measured,
            first_layer,
            text,
            failed[0][1],
        )
    worst = max((r for _, r in graded), key=lambda r: r.max_run_mm)
    detail = (
        f"{measured} layers, thickest layer {max(r.max_layer_mm for _, r in graded):.3f} mm, "
        f"thickest band {worst.max_run_mm:.3f} mm (minor colour {worst.max_minor_run_mm:.3f} mm)"
    )
    return MixLayers(
        name, filament, recipe, PASS, detail, measured, first_layer, text, worst
    )


def _describe(v: Violation, layers: list[int], rules: MixRules) -> str:
    """A violation indexes its segment; a user looks a layer number up in the G-code."""
    first = layers[v.first]
    if v.rule == LAYER_TOO_THICK:
        return (
            f"{v.layers} mixed layer(s) thicker than the {rules.layer_cap_mm:g} mm cap, "
            f"first at layer {first} (thickest {v.thickness_mm:.3f} mm): stripes that thick read as bands"
        )
    last = layers[min(v.first + v.layers - 1, len(layers) - 1)]
    what = "minor colour" if v.rule == MINOR_ADJACENT else "colour"
    return (
        f"{v.rule}: {what} on head {v.colour} printed on layers {first}-{last}, "
        f"a {v.thickness_mm:.3f} mm band"
    )


def check(
    gcode: str,
    project_3mf: str | None = None,
    rules: MixRules = MixRules(),  # noqa: B008 — frozen dataclass
) -> MixGcodeReport:
    """Raises ProjectError for a project input that is not a readable Orca-family project."""
    with open(gcode, encoding="utf-8", errors="replace") as fh:
        scan = scan_gcode(fh)
    report = MixGcodeReport(rules=rules)
    thick = layer_thickness(scan.layer_z)
    if not scan.heads:
        report.results.append(
            MixLayers("(G-code)", 0, "", SKIP, "no extrusion inside object labels")
        )
        return report
    if not thick:
        report.results.append(
            MixLayers(
                "(G-code)",
                0,
                "",
                SKIP,
                "no layer Z comments (;Z: / ; Z_HEIGHT:): the layer thickness cannot be measured",
            )
        )
        return report
    if project_3mf is None:
        report.results.extend(_without_project(scan.heads, thick, rules))
    else:
        report.results.extend(_with_project(scan.heads, thick, project_3mf, rules))
    return report


def _without_project(
    heads: dict[str, dict[int, set[int]]], thick: dict[int, float], rules: MixRules
) -> list[MixLayers]:
    out: list[MixLayers] = []
    for name, layers in heads.items():
        ks = sorted(k for k in layers if k in thick)
        used = {h for k in ks for h in layers[k]}
        if len(used) < 2:
            continue
        if any(len(layers[k]) != 1 for k in ks):
            out.append(
                MixLayers(
                    name,
                    0,
                    "",
                    SKIP,
                    "several heads per layer: pass the project 3MF to isolate the mixed part",
                )
            )
            continue
        seq = {k: next(iter(layers[k])) for k in ks}
        inner = _interior(seq)
        if inner is None:
            out.append(
                MixLayers(
                    name, 0, "", SKIP, "no alternation (one colour change at most)"
                )
            )
            continue
        out.append(_measure(name, 0, "", inner, thick, rules))
    return out


def _interior(seq: dict[int, int]) -> dict[int, int] | None:
    """The layers between the first and the last head change — the alternating stretch. A solid
    first/last colour is a run touching the edge of the object, so it is cut off; a stretch with
    fewer than two changes is a colour change, not a mix."""
    ks = sorted(seq)
    changes = [k for a, k in zip(ks, ks[1:], strict=False) if seq[a] != seq[k]]
    if len(changes) < 2:
        return None
    first, last = changes[0], changes[-1]
    return {k: seq[k] for k in ks if first <= k < last}


def _with_project(
    heads: dict[str, dict[int, set[int]]],
    thick: dict[int, float],
    project_3mf: str,
    rules: MixRules,
) -> list[MixLayers]:
    patterns, raft, objs, _plates = _read_project(project_3mf)
    out: list[MixLayers] = []
    for gname, layers in heads.items():
        key = _match_object(gname, objs)
        if key is None:
            continue  # head-sequence reports objects that are not in the project
        extruders = objs[key]
        mixed = [e for e in extruders if e in patterns]
        if not mixed:
            continue
        fixed = {e for e in extruders if e not in patterns}
        ks = sorted(k for k in layers if k in thick and k >= raft)
        for e in mixed:
            recipe = patterns[e]
            if not recipe:
                out.append(
                    MixLayers(
                        key,
                        e,
                        "",
                        SKIP,
                        "ratio row (Local-Z): both components in one layer, not measured",
                    )
                )
                continue
            own = {int(d) for d in recipe}
            others = fixed | {int(d) for o in mixed if o != e for d in patterns[o] if d}
            only = own - others  # heads that only this mix prints
            if not only:
                out.append(
                    MixLayers(
                        key,
                        e,
                        recipe,
                        SKIP,
                        "every head of this mix also prints the object's solid colour: the mix layers cannot be told apart",
                    )
                )
                continue
            seq = _mix_heads(layers, ks, own, only)
            if not seq:
                out.append(
                    MixLayers(
                        key, e, recipe, FAIL, "no layer printed a head of this mix"
                    )
                )
                continue
            out.append(_measure(key, e, recipe, seq, thick, rules))
    return out


def _mix_heads(
    layers: dict[int, set[int]], ks: list[int], own: set[int], only: set[int]
) -> dict[int, int]:
    """{layer: head the mix printed} from the first to the last layer a mix-only head appears on.
    On a layer where only shared heads printed, the mix's own head among them. A layer where
    the mix's head cannot be told (none of its heads, or two mix-only heads at once) is left
    out: it ends a segment (see `_segments`)."""
    hit = [k for k in ks if layers[k] & only]
    if not hit:
        return {}
    lo, hi = hit[0], hit[-1]
    seq: dict[int, int] = {}
    for k in ks:
        if not lo <= k <= hi:
            continue
        exclusive = layers[k] & only
        if len(exclusive) == 1:
            seq[k] = next(iter(exclusive))
            continue
        mine = layers[k] & own
        if len(mine) == 1 and not exclusive:
            seq[k] = next(iter(mine))
    return seq


def format_lines(report: MixGcodeReport) -> list[str]:
    return [
        f"{r.status:<4} {r.object}"
        + (f" / filament {r.filament} ({r.recipe})" if r.filament else "")
        + f": {r.detail}"
        for r in report.results
    ]


def preflight_check(
    gcode: str,
    project_3mf: str | None,
    rules: MixRules = MixRules(),  # noqa: B008
) -> dict[str, object]:
    """print-preflight entry: {status PASS/FAIL/SKIP, detail, lines}."""
    try:
        report = check(gcode, project_3mf, rules)
    except ProjectError as exc:
        return {"status": SKIP, "detail": str(exc), "lines": []}
    measured = [r for r in report.results if r.status in (PASS, FAIL)]
    lines = [
        line
        for r, line in zip(report.results, format_lines(report), strict=True)
        if r.status == FAIL
    ]
    if report.status == FAIL:
        detail = (
            f"{report.failures} of {len(measured)} mixed object(s) print the additional "
            "colour on adjacent layers or in thick bands"
        )
    elif report.status == PASS:
        worst = max((r.report.max_layer_mm for r in measured if r.report), default=0.0)
        detail = (
            f"{len(measured)} mixed object(s) alternate in thin layers "
            f"(thickest {worst:.3f} mm, cap {rules.layer_cap_mm:g} mm)"
        )
    else:
        why = (
            "; ".join(f"{r.object}: {r.detail}" for r in report.results)
            or "no mixed filament"
        )
        detail = f"nothing measured: {why}"
    return {"status": report.status, "detail": detail, "lines": lines}
