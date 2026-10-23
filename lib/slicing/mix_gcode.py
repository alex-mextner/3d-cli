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
  Without a project, only objects that print exactly one head per layer are measured, and only
  where they alternate: stretches of at least five consecutive runs of at most three layers each,
  minus the first and last run of the stretch (solid colour regions are long runs, never measured).
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
    """Layer index -> thickness: the Z step from the layer below (the first layer: its own Z).
    A layer whose predecessor has no Z, or whose Z does not rise (a by-object print restarts Z for
    every object), has no measurable thickness and is left out: it ends a stretch of layers."""
    out: dict[int, float] = {}
    for k in sorted(layer_z):
        z = layer_z[k]
        if k == 0:
            if z > 0:
                out[k] = z
            continue
        below = layer_z.get(k - 1)
        if below is not None and z > below:
            out[k] = z - below
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
    reference: str = "",
) -> MixLayers:
    """Apply the rules to {layer: mix head}, segment by segment (see `_segments`). `reference`
    is the recipe's cycle (one head digit per layer): it says which colour is the additional one."""
    graded: list[tuple[list[int], MixReport]] = []
    for layers in _segments(seq):
        # the bed layer is one fixed 0.2 mm layer: judged at the cap, never a violation
        heights = [
            min(thick[k], rules.layer_cap_mm) if k == 0 else thick[k] for k in layers
        ]
        report = analyse(
            [str(seq[k]) for k in layers],
            heights,
            rules,
            cyclic=False,
            reference=list(reference) or None,
        )
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
        detail = _describe_all(failed, rules)
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


def _describe_all(failed: list[tuple[list[int], MixReport]], rules: MixRules) -> str:
    """One sentence per rule and colour, however many layers repeat the fault. A violation
    indexes its segment; a user looks a layer number up in the G-code."""
    groups: dict[tuple[str, str], list[tuple[int, int, Violation]]] = {}
    for layers, report in failed:
        for v in report.violations:
            last = layers[min(v.first + v.layers - 1, len(layers) - 1)]
            key = (v.rule, "" if v.rule == LAYER_TOO_THICK else v.colour)
            groups.setdefault(key, []).append((layers[v.first], last, v))
    out = []
    for (rule, colour), items in groups.items():
        first, last, _ = items[0]
        worst = max(v.thickness_mm for _, _, v in items)
        if rule == LAYER_TOO_THICK:
            n = sum(v.layers for _, _, v in items)
            out.append(
                f"{n} mixed layer(s) thicker than the {rules.layer_cap_mm:g} mm cap, first at "
                f"layer {first} (thickest {worst:.3f} mm): stripes that thick read as bands"
            )
            continue
        what = "minor colour" if rule == MINOR_ADJACENT else "colour"
        times = "" if len(items) == 1 else f" {len(items)} times"
        out.append(
            f"{rule}: {what} on head {colour} printed on consecutive layers{times}, first on "
            f"layers {first}-{last}, up to a {worst:.3f} mm band"
        )
    return "; ".join(out)


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
        inner = _alternating(seq)
        if inner is None:
            out.append(
                MixLayers(
                    name,
                    0,
                    "",
                    SKIP,
                    "no stretch of short alternating runs: solid colours, or pass the project 3MF",
                )
            )
            continue
        out.append(_measure(name, 0, "", inner, thick, rules))
    return out


_MIX_RUN_LAYERS = 3  # a mix alternates in runs this short; a longer run is a solid colour region
_MIN_ALTERNATING_RUNS = 5  # three interior runs after the edge runs are cut off


def _alternating(seq: dict[int, int]) -> dict[int, int] | None:
    """Without a project: the layers where the object plausibly prints a mix — stretches of at least
    `_MIN_ALTERNATING_RUNS` consecutive runs of the same head, none longer than `_MIX_RUN_LAYERS`
    layers, minus the first and last run of each stretch (they may be solid colour). Solid colour
    bands stacked on one another (a run of 5 layers, then another colour) never qualify."""
    runs: list[tuple[int, list[int]]] = []
    for k in sorted(seq):
        if runs and runs[-1][0] == seq[k] and runs[-1][1][-1] == k - 1:
            runs[-1][1].append(k)
        else:
            runs.append((seq[k], [k]))
    out: dict[int, int] = {}
    stretch: list[tuple[int, list[int]]] = []

    def flush() -> None:
        if len(stretch) >= _MIN_ALTERNATING_RUNS:
            for head, ks in stretch[1:-1]:
                out.update(dict.fromkeys(ks, head))
        stretch.clear()

    for head, ks in runs:
        short = len(ks) <= _MIX_RUN_LAYERS
        contiguous = not stretch or stretch[-1][1][-1] == ks[0] - 1
        if short and contiguous:
            stretch.append((head, ks))
            continue
        flush()
        if short:
            stretch.append((head, ks))
    flush()
    return out or None


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
            seq = _mix_heads(
                layers, ks, own, only, _shared_run(recipe, own & others)
            )
            if not seq:
                out.append(
                    MixLayers(
                        key, e, recipe, FAIL, "no layer printed a head of this mix"
                    )
                )
                continue
            out.append(_measure(key, e, recipe, seq, thick, rules, recipe))
    return out


def _shared_run(recipe: str, shared: set[int]) -> int:
    """The longest stretch of consecutive layers of the (repeating) recipe printed in a head that
    the object's solid parts also use: so long a run of shared-head-only layers can still be the
    mix; a longer one is solid colour between two mixed regions."""
    flags = [int(d) in shared for d in recipe]
    if all(flags):
        return len(flags)
    best = run = 0
    for f in flags + flags:  # the cycle repeats
        run = run + 1 if f else 0
        best = max(best, run)
    return min(best, len(flags))


def _mix_heads(
    layers: dict[int, set[int]],
    ks: list[int],
    own: set[int],
    only: set[int],
    shared_run: int,
) -> dict[int, int]:
    """{layer: head the mix printed} from the first to the last layer a mix-only head appears on.
    On a layer where only shared heads printed, the mix's own head among them — unless more than
    `shared_run` such layers follow one another: that is solid colour between two mixed regions,
    and its layers are left out. A layer where the mix's head cannot be told (none of its heads,
    or two mix-only heads at once) is left out too: it ends a segment (see `_segments`)."""
    hit = [k for k in ks if layers[k] & only]
    if not hit:
        return {}
    lo, hi = hit[0], hit[-1]
    seq: dict[int, int] = {}
    pending: dict[int, int] = {}  # shared-head-only layers since the last mix-only layer

    def settle() -> None:
        if len(pending) <= shared_run:
            seq.update(pending)
        pending.clear()

    for k in ks:
        if not lo <= k <= hi:
            continue
        exclusive = layers[k] & only
        mine = layers[k] & own
        if not exclusive and len(mine) == 1:
            if pending and k - 1 not in pending:
                settle()
            pending[k] = next(iter(mine))
            continue
        settle()
        if len(exclusive) == 1:
            seq[k] = next(iter(exclusive))
    settle()
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
    except (ProjectError, ValueError) as exc:
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
