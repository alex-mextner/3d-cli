"""mix_measured.py — measured colours replace nominal ones in recipes and testers (#417).

WHAT: `measured_slots()` swaps the hex of a loaded slot for its measured colour: the current
  `filament_color` record of its Spoolman filament (3d-api), or the fitted colour of a `mix-fit/1`
  file (by head number). The slot keeps Spoolman's colour as `nominal_hex`, so every listing can
  say which colours are measured and which nominal. `measured_mixes()` then replaces the model's
  prediction of a mix by its current `mix_color` record (plan §20.3, first in the order of
  trust): the candidate takes the measured hex, its ΔE to the target is recomputed and the list
  is re-ranked.
WHY: epic #410 / issue #417: a measured colour that does not feed the next recipe is thrown away.
INVARIANTS: stdlib only; a record or fit entry that is not a colour is ignored (never guessed);
  the pure functions take records the caller fetched (no network here).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from printops.color import hex_delta_e, parse_hex, srgb_to_lab
from slicing.mix_model import Candidate, with_stripes
from slicing.mix_tester import LoadedSlot, mix_key_of


def _record_hexes(filament_records: Sequence[dict[str, Any]]) -> dict[int, str]:
    """Current measured hex per Spoolman filament id (records are newest first)."""
    by_filament: dict[int, str] = {}
    for rec in filament_records:  # newest first: the first of a filament is the current one
        fid = (rec.get("filament") or {}).get("filament_id")
        hex_ = parse_hex((rec.get("values") or {}).get("hex"))
        if isinstance(fid, int) and hex_ and fid not in by_filament:
            by_filament[fid] = hex_
    return by_filament


def _fit_hexes(
    fit: dict[str, Any] | None,
) -> tuple[dict[int, str], dict[int, str], dict[int, str]]:
    """Fitted hex by filament id, by head number (every entry) and by head number for the
    entries of a tester that did not know its filaments."""
    by_filament: dict[int, str] = {}
    by_slot: dict[int, str] = {}
    by_slot_unkeyed: dict[int, str] = {}
    for f in (fit or {}).get("filaments", []):
        hex_ = parse_hex(f.get("fitted_hex"))
        if not hex_:
            continue
        keyed = isinstance(f.get("filament_id"), int)
        if keyed:
            by_filament[f["filament_id"]] = hex_
        if isinstance(f.get("slot"), int):
            by_slot[f["slot"]] = hex_
            if not keyed:
                by_slot_unkeyed[f["slot"]] = hex_
    return by_filament, by_slot, by_slot_unkeyed


def measured_slots(
    slots: Sequence[LoadedSlot],
    filament_records: Sequence[dict[str, Any]] = (),
    fit: dict[str, Any] | None = None,
) -> list[LoadedSlot]:
    """`slots` with the measured colour where one is known. A fit entry of the filament (matched by
    Spoolman id; by head number only for a tester that did not know its filaments) wins over a
    record of the filament: the fit is the newer, fitted answer."""
    by_filament = _record_hexes(filament_records)
    fit_by_filament, fit_by_slot, fit_by_slot_unkeyed = _fit_hexes(fit)
    out = []
    for s in slots:
        fid = s.filament_id
        if fid is None:  # a spool the printer could not name: the head is all there is to go by
            hex_ = fit_by_slot.get(s.slot)
        else:
            hex_ = fit_by_filament.get(fid) or fit_by_slot_unkeyed.get(s.slot) or by_filament.get(fid)
        out.append(s if hex_ is None else replace(s, hex=hex_, nominal_hex=s.nominal_hex or s.hex))
    return out


def _measured_hexes(mix_records: Sequence[dict[str, Any]]) -> dict[str, str]:
    """mix key -> measured hex of the newest record (`mix_records` come newest first)."""
    by_key: dict[str, str] = {}
    for rec in mix_records:
        hex_ = parse_hex((rec.get("values") or {}).get("hex"))
        if isinstance(rec.get("mix"), str) and hex_ and rec["mix"] not in by_key:
            by_key[rec["mix"]] = hex_
    return by_key


def _with_measurement(
    c: Candidate, hex_: str, target: str, layers_hex: dict[int, str], layer: float
) -> Candidate:
    """`c` showing the measured `hex_`. A measured mix keeps the stripe penalty every scored candidate
    carries: rank() scores stripes only near the best colour, so one outside that window is scored here."""
    m = replace(
        c,
        hex=hex_,
        lab=srgb_to_lab(hex_),
        delta_e=hex_delta_e(hex_, target),
        measured=True,
        model_hex=c.model_hex or c.hex,
    )
    return m if m.stripe_near is not None else with_stripes(m, layers_hex, layer)


def measured_mixes(
    ranked: Sequence[Candidate],
    slots: Sequence[LoadedSlot],
    layer: float,
    target: str,
    mix_records: Sequence[dict[str, Any]],
) -> list[Candidate]:
    """`ranked` with the measured hex of every mix that has a current `mix_color` record, re-sorted
    by score (the order `rank()` returns). Pure filaments (one-layer cycles) have no mix key."""
    by_key = _measured_hexes(mix_records)
    layers_hex = {s.slot: parse_hex(s.hex) or s.hex for s in slots}
    out = []
    for c in ranked:
        key = mix_key_of(c, slots, layer) if c.cycle > 1 else None
        hex_ = by_key.get(key) if key else None
        out.append(c if hex_ is None else _with_measurement(c, hex_, target, layers_hex, layer))
    return sorted(
        out,
        key=lambda c: (c.stripe_near is None, round(c.score, 6), c.cycle, c.weights),
    )
