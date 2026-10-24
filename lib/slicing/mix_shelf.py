"""mix_shelf.py — which spools from the shelf to load on a printer to reach a target colour by layer mixing.

WHAT: `plan()` takes the target, the spools loaded now (one per head), the spools in stock but not
  loaded (Spoolman through 3d-api, read by commands/mix.py) and the printer's usable heads, and ranks
  LOADOUTS — the loaded spools, plus up to `max_swaps` shelf spools put on a free head or on the head of
  a loaded spool taken out. Each loadout is scored by its best layer mix (slicing.mix_model.rank: ΔE
  to the target + the stripe penalty, cycles up to `max_cycle`, the layer rules of slicing.mix_pattern).
  Order: best score first; equal scores (to 0.01) prefer fewer swaps, then the lower head numbers. The
  loaded-only loadout is the baseline: a loadout is listed only when it beats it by `MIN_GAIN`.
  Excluded before ranking, with the reason kept: spools of another material than the loaded ones (or
  `material`), and spools whose remaining weight is under `min_g` (empty, or too little for a tester).
WHY: epic #410 / issue #420 (plan §20.2) — the best mix often needs a spool that is on the shelf, and
  Alex should see which spool to load on which head, and how close it gets, before printing anything.
  `3d printops spool load ID PRINTER/Tn` performs a load.
INVARIANTS: stdlib only; pure (no I/O); deterministic; heads are 1-based like slicing.mix_model slots
  (U1 head T3 is slot 3).
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field

from slicing.mix_model import Candidate, Slot, rank
from slicing.mix_pattern import MixRules

MIN_GAIN = (
    0.05  # a loadout must beat the loaded-only best score by this much to be offered
)
MIN_SPOOL_G = (
    30.0  # a spool under this weight is "empty": too little for a tester or a print
)
_SHELF_POOL = 12  # shelf spools kept for 2-swap combinations: the best single additions


@dataclass(frozen=True)
class Spool:
    """A spool as the planner sees it. `head` is its 1-based head when loaded, else None."""

    spool_id: int | None
    hex: str
    name: str = ""
    material: str = ""
    filament_id: int | None = None
    remaining_g: float | None = None
    head: int | None = None
    hex_source: str = (
        "spoolman"  # "spoolman" | "measured" (a filament_color record, #417) | "given"
    )


@dataclass(frozen=True)
class Loadout:
    placed: tuple[Spool, ...]  # every spool of the loadout with the head it ends up on
    loads: tuple[
        Spool, ...
    ]  # the shelf spools to load (their `head` is the head to load on)
    unloads: tuple[Spool, ...]  # the loaded spools that make room for them
    best: Candidate

    @property
    def swaps(self) -> int:
        return len(self.loads)

    def slots(self) -> list[Slot]:
        return [Slot(s.head or 0, s.hex, s.name) for s in self.placed]

    def to_dict(self) -> dict[str, object]:
        def one(s: Spool) -> dict[str, object]:
            return {
                "head": s.head,
                "spool_id": s.spool_id,
                "filament_id": s.filament_id,
                "name": s.name,
                "hex": s.hex,
                "hex_source": s.hex_source,
            }

        return {
            "swaps": self.swaps,
            "load": [one(s) for s in self.loads],
            "unload": [one(s) for s in self.unloads],
            "keep": [one(s) for s in self.placed if s not in self.loads],
            "best": self.best.to_dict(),
        }


@dataclass
class ShelfPlan:
    baseline: Loadout  # the loaded spools alone
    options: list[Loadout] = field(
        default_factory=list
    )  # better loadouts, best first (may be empty)
    material: str = ""
    excluded: list[tuple[Spool, str]] = field(default_factory=list)

    @property
    def chosen(self) -> Loadout:
        """The loadout to build for: the best option, else the loaded spools as they are."""
        return self.options[0] if self.options else self.baseline

    def to_dict(self) -> dict[str, object]:
        return {
            "material": self.material,
            "baseline": self.baseline.to_dict(),
            "options": [o.to_dict() for o in self.options],
            "excluded": [
                {"spool_id": s.spool_id, "name": s.name, "reason": why}
                for s, why in self.excluded
            ],
        }


def _score(c: Candidate) -> float:
    return round(c.score, 2)


def _best(target: str, spools: Sequence[Spool], **kw: object) -> Candidate:
    ranked = rank(target, [Slot(s.head or 0, s.hex, s.name) for s in spools], **kw)  # type: ignore[arg-type]
    return ranked[0]


def common_material(loaded: Sequence[Spool]) -> str:
    """The most common material of the loaded spools (ties: the first loaded), upper-case; '' when
    none states one."""
    counts: dict[str, int] = {}
    for s in loaded:
        if s.material:
            counts[s.material.upper()] = counts.get(s.material.upper(), 0) + 1
    return max(counts, key=lambda m: counts[m]) if counts else ""


def plan(
    target: str,
    loaded: Sequence[Spool],
    shelf: Sequence[Spool],
    heads: Sequence[int],
    *,
    max_swaps: int = 2,
    max_cycle: int = 3,
    layer_h: float = 0.12,
    rules: MixRules | None = None,
    material: str | None = None,
    min_g: float = MIN_SPOOL_G,
    top: int = 5,
) -> ShelfPlan:
    """Rank the loadouts reachable with at most `max_swaps` shelf spools. `loaded` spools carry their
    head; `heads` are the printer's usable heads (loaded or free)."""
    kw: dict[str, object] = {
        "max_cycle": max_cycle,
        "layer_h": layer_h,
        "rules": rules or MixRules(),
    }
    mat = (material or common_material(loaded)).upper()
    baseline = Loadout(tuple(loaded), (), (), _best(target, loaded, **kw))
    out = ShelfPlan(baseline, material=mat)
    usable: list[Spool] = []
    seen: set[tuple[str, int | None]] = set()
    for s in shelf:
        if s.remaining_g is not None and s.remaining_g < min_g:
            out.excluded.append(
                (s, f"empty ({s.remaining_g:.0f} g left, need {min_g:g} g)")
            )
        elif mat and s.material and s.material.upper() != mat:
            out.excluded.append((s, f"{s.material} is not {mat}"))
        elif mat and not s.material:
            out.excluded.append((s, f"material unknown, loaded spools are {mat}"))
        elif (s.hex.upper(), s.filament_id) in seen:
            out.excluded.append((s, "same filament as a spool already offered"))
        else:
            seen.add((s.hex.upper(), s.filament_id))
            usable.append(s)
    free = sorted(set(heads) - {s.head for s in loaded if s.head is not None})
    k_max = min(max_swaps, len(usable), len(heads))
    if k_max < 1 or not heads:
        return out

    def placed(extra: Sequence[Spool], evict: Sequence[Spool]) -> Loadout | None:
        homes = sorted(free + [e.head for e in evict if e.head is not None])
        if len(homes) < len(extra):
            return None
        moved = tuple(
            Spool(
                s.spool_id,
                s.hex,
                s.name,
                s.material,
                s.filament_id,
                s.remaining_g,
                h,
                s.hex_source,
            )
            for s, h in zip(extra, homes, strict=False)
        )
        kept = tuple(s for s in loaded if s not in evict)
        every = tuple(sorted((*kept, *moved), key=lambda s: s.head or 0))
        return Loadout(every, moved, tuple(evict), _best(target, every, **kw))

    def loadouts(pool: Sequence[Spool], k: int) -> list[Loadout]:
        found = []
        for extra in itertools.combinations(pool, k):
            need = max(0, k - len(free))
            for evict in itertools.combinations(loaded, need):
                lo = placed(extra, evict)
                if lo is not None:
                    found.append(lo)
        return found

    singles = loadouts(usable, 1)
    found = list(singles)
    if k_max >= 2:
        # two-spool loadouts: only from the shelf spools that help most alone (the rest cannot pair up
        # into something better than their own best single, except in rare complementary cases)
        best_alone = sorted(
            {lo.loads[0].spool_id: lo for lo in singles}.values(),
            key=lambda lo: (_score(lo.best), lo.swaps),
        )[:_SHELF_POOL]
        pool = [lo.loads[0] for lo in best_alone]
        for k in range(2, k_max + 1):
            found += loadouts(pool, k)
    floor = _score(baseline.best)
    better = [lo for lo in found if floor - _score(lo.best) >= MIN_GAIN]
    better.sort(
        key=lambda lo: (
            _score(lo.best),
            lo.swaps,
            tuple(s.head or 0 for s in lo.loads),
            tuple(s.spool_id or 0 for s in lo.loads),
        )
    )
    # one option per distinct best result: a loadout that only adds an unused spool to a better one is noise
    kept: list[Loadout] = []
    for lo in better:
        if not any(
            _score(o.best) == _score(lo.best) and o.best.pattern == lo.best.pattern
            for o in kept
        ):
            kept.append(lo)
    out.options = kept[:top]
    return out
