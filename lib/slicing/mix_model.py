"""mix_model.py — what colour a layer mix shows on a wall, and which mixes come closest to a target.

WHAT: the recipe model of Full Spectrum / Snapmaker Orca mixed filaments (issue #116, plan §20.3),
  ported from the babushkin-dom research tools (docs/full-spectrum/tools/colour.py, recipes.py):
  - mix(): fine stripes of opaque filament mix in the eye as the area-weighted mean in LINEAR
    light; compared with the target in CIELAB with CIEDE2000 (printops.color);
  - pattern(): the weights spread over the cycle as evenly as possible (Snapmaker Orca's
    build_weighted_gradient_sequence), one slot digit per layer, or the arrangement with fewer
    equal neighbours across the repeat seam when the spread order has avoidable ones;
  - stripe_delta_e(): how visible the stripes are — the wall profile (one filament per layer,
    repeating) blurred by the eye's line spread (Gaussian, σ = 1 arcmin at the viewing distance)
    in linear light; the worst ΔE of the blurred profile against its mean (< 2 invisible, > 5 seen);
  - rank(): every integer weight vector over the slots with cycle length ≤ max_cycle and gcd 1
    whose pattern passes the layer rules of slicing/mix_pattern.py (the additional colour is one
    thin layer, never on adjacent layers; no colour band thicker than the run limit; layers no
    thicker than the cap), scored ΔE + 0.2 × stripe ΔE at 30 cm (a visibly striped wall is worse
    than a slightly off hue);
    pick(): the best plus variants around it (lighter, darker, warmer, greener, pure, short), then
    the next best, up to `count`.
RULES: a mix whose pattern breaks the layer rules is REJECTED, not ranked low: 2026-10-20 the
  eye recipe GWG repeated as GWGGWG and printed a double-thick G band ("the additional colour
  must never print several layers in a row as thick stripes"). With the default rules only
  patterns where no colour touches itself qualify (1:1 pairs, triples, 2:1:1 …); pure filaments
  always qualify. `rules=MixRules(max_run_mm=0.24)` lets a majority colour double.
WHY: Snapmaker Orca's own preview predicts mixes with a pigment model that is off by ΔE 14–21 for
  opaque PLA stripes; SPEC 58 (babushkin-dom): short even cycles (1–3 layers) read best, longer
  ones band even when their mean is closer. On 2026-10-17 a mix chosen without a printed test came
  out ΔE 16.6 from the mockup (tg#22816, epic #410).
INVARIANTS: stdlib only (`3d mix` imports it in-process); deterministic: ties break on cycle
  length, then the weight vector.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from functools import lru_cache

from printops.color import (
    delta_e2000,
    hex_from_linear,
    linear_rgb,
    linear_to_lab,
    parse_hex,
)
from slicing.mix_pattern import TOLERANCE_MM, MixRules, analyse_pattern

Lab = tuple[float, float, float]
RGB = tuple[float, float, float]

STRIPE_WEIGHT = 0.2  # score = ΔE + STRIPE_WEIGHT × stripe ΔE at 30 cm
VIEW_NEAR_MM = 300.0
VIEW_FAR_MM = 1000.0
_ARCMIN = 1.0
_STEP_MM = 0.004  # sampling of the wall profile
_STRIPE_WINDOW = 4.0  # stripes are scored only within best ΔE + this (they cost time)
_STRIPE_MAX = 300  # … and for at most this many candidates
_MAX_CANDIDATES = 200_000


class MixModelError(ValueError):
    """A request the model cannot answer (no slots, a bad colour, a cycle too long)."""


@dataclass(frozen=True)
class Slot:
    slot: int  # project filament / U1 head number, 1..9 (one pattern digit)
    hex: str
    name: str = ""


@dataclass(frozen=True)
class Candidate:
    weights: tuple[int, ...]  # layers per slot in one cycle, in `slots` order
    slots: tuple[int, ...]
    pattern: str  # one slot digit per layer
    hex: str  # predicted wall colour
    lab: Lab
    delta_e: float  # to the target
    stripe_near: float | None = (
        None  # stripe ΔE at 30 cm (None: not scored, far from the best)
    )
    stripe_far: float | None = None
    role: str = ""

    @property
    def cycle(self) -> int:
        return sum(self.weights)

    @property
    def score(self) -> float:
        return self.delta_e + STRIPE_WEIGHT * (self.stripe_near or 0.0)

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "pattern": self.pattern,
            "weights": {
                str(s): w for s, w in zip(self.slots, self.weights, strict=True) if w
            },
            "hex": self.hex,
            "delta_e": round(self.delta_e, 2),
            "stripe_delta_e_30cm": None
            if self.stripe_near is None
            else round(self.stripe_near, 2),
            "stripe_delta_e_1m": None
            if self.stripe_far is None
            else round(self.stripe_far, 2),
            "score": round(self.score, 2),
        }


def _hex(value: str) -> str:
    h = parse_hex(value)
    if h is None:
        raise MixModelError(f"{value!r} is not a colour (#RRGGBB)")
    return h


def mix_linear(linears: Sequence[RGB], weights: Sequence[int | float]) -> RGB:
    """Area-weighted mean of linear-light colours (what fine stripes show)."""
    total = float(sum(weights))
    if total <= 0:
        raise MixModelError("weights must not all be zero")
    acc = [0.0, 0.0, 0.0]
    for lin, w in zip(linears, weights, strict=True):
        for k in range(3):
            acc[k] += w * lin[k]
    return acc[0] / total, acc[1] / total, acc[2] / total


def mix(hexes: Sequence[str], weights: Sequence[int | float]) -> str:
    """Apparent colour of fine stripes of these filaments with these area weights."""
    return hex_from_linear(mix_linear([linear_rgb(_hex(h)) for h in hexes], weights))


def dispersed_sequence(ids: Sequence[int], weights: Sequence[int]) -> list[int]:
    """Order a multiset so each id is spread as evenly as possible (Snapmaker Orca
    build_weighted_gradient_sequence, MixedFilament.cpp)."""
    pairs = [(i, w) for i, w in zip(ids, weights, strict=True) if w > 0]
    total = sum(w for _, w in pairs)
    emitted = [0] * len(pairs)
    seq: list[int] = []
    for pos in range(total):
        k = max(
            range(len(pairs)),
            key=lambda j: (pos + 1) * pairs[j][1] / total - emitted[j],
        )
        emitted[k] += 1
        seq.append(pairs[k][0])
    return seq


def _equal_neighbours(seq: Sequence[int]) -> int:
    """Pairs of equal layers next to each other in the repeating cycle (the seam included)."""
    return (
        sum(seq[i] == seq[i - 1] for i in range(len(seq))) if len(set(seq)) > 1 else 0
    )


def _spread_out(ids: Sequence[int], weights: Sequence[int]) -> list[int]:
    """Most frequent colour first on every other position, the rest in the gaps: no two equal
    neighbours whenever the largest share is at most half of the cycle."""
    pairs = sorted(
        ((i, w) for i, w in zip(ids, weights, strict=True) if w > 0),
        key=lambda p: -p[1],
    )
    flat = [i for i, w in pairs for _ in range(w)]
    out = [0] * len(flat)
    out[0::2] = flat[: (len(flat) + 1) // 2]
    out[1::2] = flat[(len(flat) + 1) // 2 :]
    return out


def arrange(ids: Sequence[int], weights: Sequence[int]) -> list[int]:
    """One cycle of layers for these weights: the evenly spread order of Snapmaker Orca, unless
    it leaves equal layers next to each other across the repeat seam that another order avoids
    (`A B C A` repeats as `…A A…`; `A B A C` does not). Where a share above half makes equal
    neighbours unavoidable (`GWG` repeats as `GWGGWG`) the spread order stays."""
    spread = dispersed_sequence(ids, weights)
    alt = _spread_out(ids, weights)
    return alt if _equal_neighbours(alt) < _equal_neighbours(spread) else spread


def pattern(slots: Sequence[int], weights: Sequence[int]) -> str:
    return "".join(str(s) for s in arrange(slots, weights))


@lru_cache(maxsize=256)
def _layer_weights(
    cycle: int, layer_h: float, view_mm: float
) -> tuple[tuple[float, ...], ...]:
    """W[i][L]: share of layer L (of one cycle) in the eye's blur at sample i of one period.
    Depends only on the cycle length, so every candidate of that length reuses it."""
    sigma = view_mm * math.tan(math.radians(_ARCMIN / 60))
    n = max(1, round(cycle * layer_h / _STEP_MM))
    step = cycle * layer_h / n
    reach = int(math.ceil(4 * sigma / step))
    kernel = [
        math.exp(-0.5 * (k * step / sigma) ** 2) for k in range(-reach, reach + 1)
    ]
    norm = sum(kernel)
    layer_of = [min(cycle - 1, int((i + 0.5) * step / layer_h)) for i in range(n)]
    rows = []
    for i in range(n):
        w = [0.0] * cycle
        for k, g in enumerate(kernel):
            w[layer_of[(i + k - reach) % n]] += g / norm
        rows.append(tuple(w))
    return tuple(rows)


def stripe_delta_e(
    layer_hexes: Sequence[str], layer_h: float, view_mm: float = VIEW_NEAR_MM
) -> float:
    """Worst CIEDE2000 of the blurred wall profile against its mean, seen from view_mm."""
    if len(set(layer_hexes)) <= 1:
        return 0.0
    cols = [linear_rgb(_hex(h)) for h in layer_hexes]
    rows = _layer_weights(len(cols), round(layer_h, 4), view_mm)
    mean = tuple(sum(c[k] for c in cols) / len(cols) for k in range(3))
    mean_lab = linear_to_lab(mean)  # type: ignore[arg-type]
    worst = 0.0
    for w in rows[:: max(1, len(rows) // 200)]:
        blurred = (
            sum(wl * c[0] for wl, c in zip(w, cols, strict=True)),
            sum(wl * c[1] for wl, c in zip(w, cols, strict=True)),
            sum(wl * c[2] for wl, c in zip(w, cols, strict=True)),
        )
        worst = max(worst, delta_e2000(linear_to_lab(blurred), mean_lab))
    return worst


def _compositions(n: int, k: int) -> Iterator[tuple[int, ...]]:
    """Every way to write n as k ordered non-negative parts."""
    if k == 1:
        yield (n,)
        return
    for first in range(n + 1):
        for rest in _compositions(n - first, k - 1):
            yield (first, *rest)


def _weight_vectors(n_slots: int, max_cycle: int) -> list[tuple[int, ...]]:
    # count first (stars and bars): the guard must fire before any enumeration
    total = sum(
        math.comb(n + n_slots - 1, n_slots - 1) for n in range(1, max_cycle + 1)
    )
    if total > _MAX_CANDIDATES:
        raise MixModelError(f"{total} candidates: lower --max-cycle or use fewer slots")
    return [
        w
        for n in range(1, max_cycle + 1)
        for w in _compositions(n, n_slots)
        if math.gcd(*[x for x in w if x]) == 1
    ]


def rank(
    target: str,
    slots: Sequence[Slot],
    *,
    max_cycle: int = 3,
    layer_h: float = 0.12,
    rules: MixRules = MixRules(),  # noqa: B008 — frozen dataclass, safe as a default
) -> list[Candidate]:
    """Every candidate whose pattern passes `rules` at `layer_h`, best score first (stripes
    scored only near the best ΔE). Pure filaments (one-layer cycles) always qualify."""
    if not slots:
        raise MixModelError("no slots: pass the loaded filaments")
    if len({s.slot for s in slots}) != len(slots) or any(
        not 1 <= s.slot <= 9 for s in slots
    ):
        raise MixModelError(
            f"slots must be distinct 1..9 (one pattern digit each): {[s.slot for s in slots]}"
        )
    if not 1 <= max_cycle <= 24:
        raise MixModelError(f"max cycle {max_cycle} must be 1..24 layers")
    if max_cycle > 1 and layer_h > rules.layer_cap_mm + TOLERANCE_MM:
        raise MixModelError(
            f"layer {layer_h:g} mm is thicker than the {rules.layer_cap_mm:g} mm cap for mixed "
            "layers: stripes that thick read as bands. Use a thinner layer, raise --layer-cap, "
            "or --max-cycle 1 for pure filaments only"
        )
    tl = linear_to_lab(linear_rgb(_hex(target)))
    hexes = [_hex(s.hex) for s in slots]
    linears = [linear_rgb(h) for h in hexes]
    ids = tuple(s.slot for s in slots)
    by_slot = dict(zip(ids, hexes, strict=True))
    cands = []
    for w in _weight_vectors(len(slots), max_cycle):
        pat = pattern(ids, w)
        if not analyse_pattern(pat, layer_h, rules).ok:
            continue
        lin = mix_linear(linears, w)
        lab = linear_to_lab(lin)
        cands.append(
            Candidate(w, ids, pat, hex_from_linear(lin), lab, delta_e2000(lab, tl))
        )
    floor = min(c.delta_e for c in cands)
    # stripes cost ~ms each: score only the closest few near the best ΔE
    window = sorted(
        (c for c in cands if c.delta_e <= floor + _STRIPE_WINDOW),
        key=lambda c: (c.delta_e, c.cycle, c.weights),
    )[:_STRIPE_MAX]
    near = {c.weights for c in window}
    scored = []
    for c in cands:
        if c.weights in near:
            layers = [by_slot[int(d)] for d in c.pattern]
            c = Candidate(
                c.weights,
                c.slots,
                c.pattern,
                c.hex,
                c.lab,
                c.delta_e,
                stripe_delta_e(layers, layer_h, VIEW_NEAR_MM),
                stripe_delta_e(layers, layer_h, VIEW_FAR_MM),
            )
        scored.append(c)
    # unscored candidates (far from the best) rank after every scored one
    return sorted(
        scored,
        key=lambda c: (c.stripe_near is None, round(c.score, 6), c.cycle, c.weights),
    )


_VARIANTS = (
    ("lighter", lambda c, b: 2.5 <= c.lab[0] - b.lab[0] <= 12),
    ("darker", lambda c, b: 2.5 <= b.lab[0] - c.lab[0] <= 12),
    ("warmer", lambda c, b: c.lab[2] >= b.lab[2] + 3 and abs(c.lab[0] - b.lab[0]) <= 6),
    (
        "greener",
        lambda c, b: c.lab[1] <= b.lab[1] - 3 and abs(c.lab[0] - b.lab[0]) <= 6,
    ),
    ("pure", lambda c, b: c.cycle == 1),
    ("short", lambda c, b: c.cycle <= 2),
)
_VARIANT_REACH = 8.0  # a variant stays within the best ΔE + this


def pick(ranked: Sequence[Candidate], count: int) -> list[Candidate]:
    """The best candidate, then one per variant rule (closest ΔE first), then the next best."""
    if not ranked:
        return []
    best = ranked[0]
    out = [_role(best, "best")]
    taken = {best.weights}
    for name, ok in _VARIANTS:
        if len(out) >= count:
            break
        pool = [
            c
            for c in ranked
            if c.weights not in taken
            and c.delta_e <= best.delta_e + _VARIANT_REACH
            and ok(c, best)
        ]
        if pool:
            c = min(pool, key=lambda c: (round(c.delta_e, 6), c.cycle, c.weights))
            out.append(_role(c, name))
            taken.add(c.weights)
    for c in ranked:
        if len(out) >= count:
            break
        if c.weights not in taken:
            out.append(_role(c, "next"))
            taken.add(c.weights)
    return out


def _role(c: Candidate, role: str) -> Candidate:
    return Candidate(
        c.weights,
        c.slots,
        c.pattern,
        c.hex,
        c.lab,
        c.delta_e,
        c.stripe_near,
        c.stripe_far,
        role,
    )
