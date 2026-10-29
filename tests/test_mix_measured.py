"""Measured colours in recipes and testers (#417): filament_color replaces Spoolman's hex, a mix_color
record replaces the model's prediction of that mix."""

from __future__ import annotations

from printops.color import hex_delta_e
from slicing.mix_measured import measured_mixes, measured_slots
from slicing.mix_model import Slot, rank
from slicing.mix_pattern import MixRules
from slicing.mix_tester import LoadedSlot

SLOTS = [
    LoadedSlot(1, "#000000", "black", 11, 21),
    LoadedSlot(3, "#1B5E20", "green", 13, 23),
    LoadedSlot(4, "#FFFFFF", "white", 14, 24),
]


def _fil(fid: int, hex_: str) -> dict:
    return {"filament": {"filament_id": fid}, "values": {"hex": hex_}}


def test_a_filament_record_replaces_the_hex_and_keeps_the_nominal_one() -> None:
    out = measured_slots(
        SLOTS, [_fil(24, "#E8E4D8"), _fil(24, "#111111"), _fil(99, "#222222")]
    )
    assert [s.hex for s in out] == [
        "#000000",
        "#1B5E20",
        "#E8E4D8",
    ]  # the newest (first) record wins
    assert [s.nominal_hex for s in out] == [None, None, "#FFFFFF"]
    assert (
        out[2].to_dict()["measured"] is True
        and out[2].to_dict()["nominal_hex"] == "#FFFFFF"
    )
    assert "measured" not in out[0].to_dict()


def test_a_fit_without_filament_ids_replaces_by_head_number() -> None:
    fit = {
        "filaments": [
            {"slot": 3, "fitted_hex": "#2E7D32"},
            {"slot": 7, "fitted_hex": "#123456"},
        ]
    }
    out = measured_slots(SLOTS, [_fil(23, "#999999")], fit)
    assert out[1].hex == "#2E7D32" and out[1].nominal_hex == "#1B5E20"
    assert out[0].hex == "#000000" and out[2].hex == "#FFFFFF"


def test_a_fit_matches_by_filament_id_not_by_the_head_a_spool_sat_on() -> None:
    """The fit was made with green PLA (filament 23) on head 3; a red spool on head 3 is not green."""
    fit = {"filaments": [{"slot": 3, "filament_id": 23, "fitted_hex": "#2E7D32"}]}
    red_on_3 = [LoadedSlot(3, "#FF0000", "red", 15, 99)]
    assert measured_slots(red_on_3, [], fit) == red_on_3
    green_on_1 = [LoadedSlot(1, "#1B5E20", "green", 13, 23)]
    out = measured_slots(green_on_1, [], fit)
    assert out[0].hex == "#2E7D32" and out[0].nominal_hex == "#1B5E20"


def test_a_slot_without_a_filament_id_takes_the_fit_by_head_number() -> None:
    fit = {"filaments": [{"slot": 3, "filament_id": 23, "fitted_hex": "#2E7D32"}]}
    unnamed = [LoadedSlot(3, "#1B5E20")]
    out = measured_slots(unnamed, [], fit)
    assert out[0].hex == "#2E7D32" and out[0].nominal_hex == "#1B5E20"


def test_nothing_measured_returns_the_slots_unchanged() -> None:
    assert measured_slots(SLOTS) == SLOTS
    assert measured_slots(SLOTS, [_fil(21, "not a colour")]) == SLOTS


def test_a_mix_record_replaces_the_prediction_and_reranks() -> None:
    ranked = rank(
        "#808080",
        [Slot(s.slot, s.hex, s.name) for s in SLOTS],
        max_cycle=3,
        layer_h=0.12,
        rules=MixRules(0.12),
    )
    mix_c = next(c for c in ranked if c.pattern == "14")
    from slicing.mix_tester import mix_key_of

    key = mix_key_of(mix_c, SLOTS, 0.12)
    assert key is not None
    out = measured_mixes(
        ranked, SLOTS, 0.12, "#808080", [{"mix": key, "values": {"hex": "#808081"}}]
    )
    hit = next(c for c in out if c.pattern == "14")
    assert (
        hit.measured
        and hit.hex == "#808081"
        and hit.delta_e == hex_delta_e("#808081", "#808080")
    )
    assert hit.to_dict()["measured"] is True
    assert out[0].pattern == "14"  # it is now the closest to the target
    assert sum(1 for c in out if c.measured) == 1 and len(out) == len(ranked)
    assert all("measured" not in c.to_dict() for c in out if not c.measured)


def test_pure_filaments_have_no_mix_key_and_are_never_replaced() -> None:
    ranked = rank(
        "#808080",
        [Slot(s.slot, s.hex, s.name) for s in SLOTS],
        max_cycle=1,
        layer_h=0.12,
        rules=MixRules(0.12),
    )
    out = measured_mixes(
        ranked,
        SLOTS,
        0.12,
        "#808080",
        [{"mix": "h0.12:f21", "values": {"hex": "#808081"}}],
    )
    assert not any(c.measured for c in out)
