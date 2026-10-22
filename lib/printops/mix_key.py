"""mix_key.py — the canonical key of one colour mix, the `mix` scope of the `mix_color` calibration.

WHAT: a mix is what the eye sees on a wall printed with Full Spectrum / Snapmaker Orca mixed
  filaments: the layer height and either the cycle of filaments layer by layer (a manual pattern)
  or the share of each filament in every layer (a Local-Z ratio row). The key names filaments by
  their Spoolman filament id, not by slot, so the same mix on other heads is the same record:
    cycle   h0.12:f3.f12.f7          one fNN per layer of the cycle, rotated to its smallest form
                                     (431 and 314 on the same filaments are one mix)
    ratio   h0.12:lz:f1=25,f4=75     percent per filament, sorted by id, summing to 100
WHY: plan §20.4 (issue #412): a measured mix must be found again by the tester, the readout and
  the pre-print check, whichever heads the filaments sit on that day.
INVARIANTS: stdlib only; `canonical(key(...)) == key(...)`; layer height printed with `:g`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

# ids and percents are bounded so int() never sees an oversized run of digits
_KEY = re.compile(r"^h(?P<h>\d{1,2}(?:\.\d{1,8})?):(?P<body>.+)$")
_CYCLE = re.compile(r"^f\d{1,9}(?:\.f\d{1,9})*$")
_RATIO = re.compile(r"^lz:f\d{1,9}=\d{1,3}(?:,f\d{1,9}=\d{1,3})+$")
_FORMS = "h<layer>:f<id>.f<id>… or h<layer>:lz:f<id>=<pct>,…"
MAX_CYCLE = 64


class MixKeyError(ValueError):
    """Not a mix key, or a mix that cannot be keyed."""


def _height(layer_h: float) -> str:
    """Layer height to at most 4 decimals; finer heights are refused, not rounded onto another
    height's key."""
    rounded = round(layer_h, 4)
    if not 0 < rounded <= 1 or abs(rounded - layer_h) > 1e-9:
        raise MixKeyError(
            f"layer height {layer_h!r} must be in (0, 1] mm with at most 4 decimals"
        )
    return f"{rounded:g}"


def _smallest_rotation(ids: Sequence[int]) -> tuple[int, ...]:
    n = len(ids)
    return min(tuple(ids[i:]) + tuple(ids[:i]) for i in range(n))


def _reduce(ids: tuple[int, ...]) -> tuple[int, ...]:
    """The shortest repeating unit: 1313 prints exactly like 13."""
    n = len(ids)
    for p in range(1, n + 1):
        if n % p == 0 and ids == ids[:p] * (n // p):
            return ids[:p]
    return ids


def cycle_key(layer_h: float, filament_ids: Sequence[int]) -> str:
    """Key of a manual layer pattern given as the filament id printed on each layer of one cycle."""
    if not filament_ids or len(filament_ids) > MAX_CYCLE:
        raise MixKeyError(f"a cycle has 1..{MAX_CYCLE} layers, got {len(filament_ids)}")
    if any(
        isinstance(f, bool) or not isinstance(f, int) or f <= 0 for f in filament_ids
    ):
        raise MixKeyError(
            f"filament ids must be positive integers, got {list(filament_ids)}"
        )
    ids = _smallest_rotation(_reduce(tuple(filament_ids)))
    return f"h{_height(layer_h)}:" + ".".join(f"f{i}" for i in ids)


def ratio_key(layer_h: float, shares: Mapping[int, int]) -> str:
    """Key of a Local-Z ratio row: whole percent per filament id, at least two, summing to 100."""
    if len(shares) < 2 or any(
        isinstance(f, bool) or not isinstance(f, int) or f <= 0 for f in shares
    ):
        raise MixKeyError(
            f"a ratio mix has two or more positive filament ids, got {dict(shares)}"
        )
    if any(
        isinstance(p, bool) or not isinstance(p, int) or p <= 0 for p in shares.values()
    ):
        raise MixKeyError(
            f"ratio shares are positive whole percents, got {dict(shares)}"
        )
    if sum(shares.values()) != 100:
        raise MixKeyError(f"ratio shares must sum to 100, got {sum(shares.values())}")
    return f"h{_height(layer_h)}:lz:" + ",".join(
        f"f{f}={shares[f]}" for f in sorted(shares)
    )


def canonical(text: str) -> str:
    """Validate a key and return its canonical form (MixKeyError when it is not one)."""
    m = _KEY.match(text.strip()) if isinstance(text, str) else None
    if m is None:
        raise MixKeyError(f"{text!r} is not a mix key ({_FORMS})")
    h, body = float(m["h"]), m["body"]
    if _CYCLE.match(body):
        return cycle_key(h, [int(t[1:]) for t in body.split(".")])
    if _RATIO.match(body):
        pairs = [p.split("=") for p in body.removeprefix("lz:").split(",")]
        shares = {int(f[1:]): int(pct) for f, pct in pairs}
        if len(shares) != len(pairs):
            raise MixKeyError(f"{text!r} names a filament twice")
        return ratio_key(h, shares)
    raise MixKeyError(f"{text!r} is not a mix key ({_FORMS})")
