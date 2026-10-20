"""mix_pattern.py — the hard rules for a Full Spectrum layer pattern, one validator for every producer and checker.

WHAT: a mixed filament prints ONE filament per layer and alternates them layer by layer, so a mix
  reads as the average only when the stripes are thin. This module is the single place that says
  what a legal layer sequence is, for a cyclic recipe pattern and for the measured tool sequence
  of a sliced G-code alike:
    layer-too-thick   a layer of the mixed region is thicker than `layer_cap_mm` (default 0.12 mm);
    minor-adjacent    a MINOR colour (every colour that is not a strict majority of the cycle) is
                      printed on two or more consecutive layers — the additional colour is always
                      one thin layer between the others; cyclic patterns wrap (GW·GW, so `WGGW`
                      hides a GG at the seam);
    run-too-thick     any colour forms a band thicker than `max_run_mm` (default = one capped
                      layer, so by default no colour repeats on adjacent layers; raise it to let
                      a majority base colour repeat, e.g. 0.24 for GWG, 0.36 for WWWG).
  A single-colour sequence is not a mix and has no violations. Thicknesses are real layer heights
  in mm, so variable layer heights are measured, not assumed.
  Also here: the manual pattern token decoding of a `mixed_filament_definitions` row
  (`decode_pattern`): token 1 = the row's component_a, 2 = component_b, 3-9 = that filament slot.
WHY: 2026-10-20, Chekhov on the U1: the eye recipe GWG repeats as GWGGWG — two G layers in a row, a
  band twice as thick as its neighbours — and "the additional colour must never print several
  layers in a row as thick stripes" (project owner). Before this module each producer (recipe
  ranking, 3MF lint, G-code gates) would have had to re-implement the rule; a gate that reads a=2,
  b=3 pattern digits as slots had already mis-checked a real project.
INVARIANTS: stdlib only (print-preflight imports it in-process); pure functions, no I/O; the
  rules are data (`MixRules`), never module state.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

LAYER_CAP_MM = 0.12
# layer_heights_profile.txt rounds to 0.0001 mm and slicers write layer Z with 3 decimals
TOLERANCE_MM = 5e-4

LAYER_TOO_THICK = "layer-too-thick"
MINOR_ADJACENT = "minor-adjacent"
RUN_TOO_THICK = "run-too-thick"


@dataclass(frozen=True)
class MixRules:
    """The limits of one check. `max_run_mm=None` means one capped layer (`layer_cap_mm`)."""

    layer_cap_mm: float = LAYER_CAP_MM
    max_run_mm: float | None = None

    def __post_init__(self) -> None:
        if not self.layer_cap_mm > 0:
            raise ValueError(f"layer cap {self.layer_cap_mm} mm must be positive")
        if self.max_run_mm is not None and not self.max_run_mm > 0:
            raise ValueError(f"max run {self.max_run_mm} mm must be positive")

    @property
    def run_limit_mm(self) -> float:
        return self.layer_cap_mm if self.max_run_mm is None else self.max_run_mm


@dataclass(frozen=True)
class Run:
    colour: str
    first: int  # index of its first layer (a wrapped run starts in the cycle's tail)
    layers: int
    thickness_mm: float


@dataclass(frozen=True)
class Violation:
    rule: str
    colour: str
    first: int
    layers: int
    thickness_mm: float
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "rule": self.rule,
            "colour": self.colour,
            "first_layer": self.first,
            "layers": self.layers,
            "thickness_mm": round(self.thickness_mm, 4),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class MixReport:
    sequence: str
    cyclic: bool
    mixed: bool  # False: one colour only — not a mix, nothing to check
    minor: tuple[str, ...] = ()
    runs: tuple[Run, ...] = ()
    max_layer_mm: float = 0.0
    max_run_mm: float = 0.0  # thickest band of any colour
    max_minor_run_mm: float = 0.0  # thickest band of a minor colour
    violations: tuple[Violation, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "cyclic": self.cyclic,
            "mixed": self.mixed,
            "ok": self.ok,
            "minor": list(self.minor),
            "max_layer_mm": round(self.max_layer_mm, 4),
            "max_run_mm": round(self.max_run_mm, 4),
            "max_minor_run_mm": round(self.max_minor_run_mm, 4),
            "violations": [v.to_dict() for v in self.violations],
        }


def _runs(
    colours: Sequence[str], thickness: Sequence[float], cyclic: bool
) -> list[Run]:
    runs: list[Run] = []
    for i, (c, t) in enumerate(zip(colours, thickness, strict=True)):
        if runs and runs[-1].colour == c:
            r = runs[-1]
            runs[-1] = Run(c, r.first, r.layers + 1, r.thickness_mm + t)
        else:
            runs.append(Run(c, i, 1, t))
    if cyclic and len(runs) > 1 and runs[0].colour == runs[-1].colour:
        head, tail = runs[0], runs[-1]
        runs = [
            Run(
                tail.colour,
                tail.first,
                tail.layers + head.layers,
                tail.thickness_mm + head.thickness_mm,
            ),
            *runs[1:-1],
        ]
    return runs


def _thicknesses(thickness: float | Sequence[float], n: int) -> list[float]:
    if isinstance(thickness, int | float):
        return [float(thickness)] * n
    out = [float(t) for t in thickness]
    if len(out) != n:
        raise ValueError(f"{n} layers but {len(out)} thicknesses")
    return out


def analyse(
    colours: Sequence[str],
    thickness: float | Sequence[float],
    rules: MixRules = MixRules(),  # noqa: B008 — frozen dataclass, safe as a default
    *,
    cyclic: bool,
) -> MixReport:
    """Check a layer sequence (one colour symbol per layer, bottom to top) against `rules`.

    `thickness` is one layer height in mm for every layer, or one per layer. `cyclic=True` for a
    recipe pattern that repeats (the last layer is followed by the first); `False` for a measured
    sequence of a real print.
    """
    if not colours:
        raise ValueError("an empty layer sequence")
    layers = _thicknesses(thickness, len(colours))
    if any(not t > 0 for t in layers):
        raise ValueError("layer thicknesses must be positive")
    text = "".join(colours) if all(len(c) == 1 for c in colours) else ",".join(colours)
    if len(set(colours)) == 1:
        return MixReport(text, cyclic, False, max_layer_mm=max(layers))
    total = sum(layers)
    share: dict[str, float] = {}
    for c, t in zip(colours, layers, strict=True):
        share[c] = share.get(c, 0.0) + t / total
    major = {c for c, s in share.items() if s > 0.5}
    minor = tuple(sorted(c for c in share if c not in major))
    runs = _runs(colours, layers, cyclic)
    limit = rules.run_limit_mm
    violations: list[Violation] = []
    thick = [i for i, t in enumerate(layers) if t > rules.layer_cap_mm + TOLERANCE_MM]
    if thick:
        worst = max(thick, key=lambda i: layers[i])
        violations.append(
            Violation(
                LAYER_TOO_THICK,
                colours[worst],
                thick[0],
                len(thick),
                layers[worst],
                f"{len(thick)} mixed layer(s) thicker than the {rules.layer_cap_mm:g} mm cap "
                f"(first: layer {thick[0]}, thickest {layers[worst]:.3f} mm at layer {worst}): "
                "stripes that thick read as bands; cap the layer height over the mixed part",
            )
        )
    for r in runs:
        if r.layers < 2:
            continue
        where = _where(r, len(colours))
        if r.colour in minor:
            violations.append(
                Violation(
                    MINOR_ADJACENT,
                    r.colour,
                    r.first,
                    r.layers,
                    r.thickness_mm,
                    f"minor colour {r.colour} on {r.layers} consecutive layers ({where}), a "
                    f"{r.thickness_mm:.3f} mm band: the additional colour must be one thin layer "
                    "between the others",
                )
            )
        elif r.thickness_mm > limit + TOLERANCE_MM:
            violations.append(
                Violation(
                    RUN_TOO_THICK,
                    r.colour,
                    r.first,
                    r.layers,
                    r.thickness_mm,
                    f"{r.colour} forms a {r.thickness_mm:.3f} mm band over {r.layers} consecutive "
                    f"layers ({where}), above the {limit:g} mm limit",
                )
            )
    return MixReport(
        text,
        cyclic,
        True,
        minor,
        tuple(runs),
        max(layers),
        max(r.thickness_mm for r in runs),
        max((r.thickness_mm for r in runs if r.colour in minor), default=0.0),
        tuple(violations),
    )


def _span(first: int, last: int) -> str:
    return f"layer {first}" if first == last else f"layers {first}-{last}"


def _where(run: Run, n: int) -> str:
    last = run.first + run.layers - 1
    if last >= n:  # wrapped run of a cyclic pattern
        return (
            f"{_span(run.first, n - 1)} and {_span(0, last - n)} across the repeat seam"
        )
    return _span(run.first, last)


def analyse_pattern(
    pattern: str,
    layer_mm: float | Sequence[float],
    rules: MixRules = MixRules(),  # noqa: B008 — frozen dataclass, safe as a default
) -> MixReport:
    """A repeating recipe pattern, one character per layer (`GWG`, `431`), at `layer_mm`."""
    if not pattern.strip():
        raise ValueError("an empty pattern")
    return analyse(list(pattern.strip()), layer_mm, rules, cyclic=True)


# -- mixed_filament_definitions rows ----------------------------------------------------------


@dataclass(frozen=True)
class MixedRow:
    component_a: int
    component_b: int
    pattern: str  # the row's tokens as written; '' = a ratio (Local-Z) row

    @property
    def slots(self) -> str:
        """One physical-filament digit per layer."""
        return decode_pattern(self.pattern, self.component_a, self.component_b)


def decode_pattern(tokens: str, component_a: int, component_b: int) -> str:
    """Manual pattern tokens -> one physical filament digit per layer.

    Snapmaker Orca (MixedFilament.cpp): token `1` is the row's component_a, `2` its component_b,
    `3`-`9` the filament of that number. With a=1, b=2 the tokens ARE the slots.
    """
    out = []
    for tok in tokens:
        if tok == "1":
            out.append(component_a)
        elif tok == "2":
            out.append(component_b)
        else:
            out.append(int(tok))
    return "".join(str(s) for s in out)


def parse_row(row: str) -> MixedRow:
    """`a,b,enabled,custom,mix%,…,cm1,<pattern>` -> MixedRow. Raises ValueError on a row whose
    first two fields are not filament numbers."""
    fields = [f.strip() for f in row.split(",")]
    if len(fields) < 2 or not (fields[0].isdigit() and fields[1].isdigit()):
        raise ValueError(
            f"mixed filament row {row!r} must start with two filament numbers"
        )
    last = fields[-1]
    pattern = last if len(fields) > 2 and last.isdigit() and last.isascii() else ""
    return MixedRow(int(fields[0]), int(fields[1]), pattern)
