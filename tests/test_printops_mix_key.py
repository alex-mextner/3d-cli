"""Unit tests for the canonical colour-mix key (printops.mix_key, #412, plan §20.4)."""

from __future__ import annotations

import pytest

from printops.mix_key import MixKeyError, canonical, cycle_key, ratio_key


def test_rotations_of_one_cycle_are_one_key_compared_as_numbers() -> None:
    keys = {cycle_key(0.12, ids) for ids in ([3, 12, 7], [12, 7, 3], [7, 3, 12])}
    assert keys == {"h0.12:f3.f12.f7"}  # numeric order: as strings f12 < f3 would win
    assert (
        cycle_key(0.12, [3, 7, 12]) == "h0.12:f3.f7.f12"
    )  # another order is another mix


def test_repeated_cycles_collapse_to_their_shortest_unit() -> None:
    assert cycle_key(0.12, [4, 3, 4, 3]) == cycle_key(0.12, [3, 4]) == "h0.12:f3.f4"
    assert cycle_key(0.12, [5, 5, 5]) == "h0.12:f5"
    assert cycle_key(0.12, [1, 4, 1, 1]) == "h0.12:f1.f1.f1.f4"


def test_layer_height_is_part_of_the_key() -> None:
    assert cycle_key(0.2, [1, 4]) == "h0.2:f1.f4" != cycle_key(0.12, [1, 4])


def test_ratio_key_sorts_by_id_and_needs_whole_percents_summing_to_100() -> None:
    assert ratio_key(0.12, {4: 75, 1: 25}) == "h0.12:lz:f1=25,f4=75"
    for bad in ({1: 100}, {1: 50, 4: 40}, {1: 0, 4: 100}):
        with pytest.raises(MixKeyError):
            ratio_key(0.12, bad)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("h0.12:f12.f7.f3", "h0.12:f3.f12.f7"),
        (" h0.120:f4.f3.f4.f3 ", "h0.12:f3.f4"),
        ("h0.12:lz:f4=75,f1=25", "h0.12:lz:f1=25,f4=75"),
    ],
)
def test_canonical_normalises_any_spelling(text: str, key: str) -> None:
    assert canonical(text) == key
    assert canonical(key) == key


@pytest.mark.parametrize(
    "text",
    [
        "431",
        "h0.12:",
        "h0.12:f1.x4",
        "h2:f1.f4",
        "h0:f1",
        "h0.12:f0.f4",
        "h0.12:lz:f1=100",
        "h0.12:lz:f1=50,f1=50",
        "0.12:f1.f4",
    ],
)
def test_canonical_rejects_what_is_not_a_mix(text: str) -> None:
    with pytest.raises(MixKeyError):
        canonical(text)


@pytest.mark.parametrize("text", ["h0.00004:f1.f4", "h0.12004:f1.f4", "h0.12:f" + "9" * 5000 + ".f4"])
def test_heights_finer_than_the_key_and_oversized_ids_are_refused(text: str) -> None:
    """h0.12004 would round onto h0.12 (another height's records); a 5000-digit id would make
    int() raise a plain ValueError (a 500 in 3d-api instead of a 422)."""
    with pytest.raises(MixKeyError):
        canonical(text)


def test_every_built_key_reads_back() -> None:
    for h in (0.0001, 0.08, 0.12, 0.9999, 1.0):
        key = cycle_key(h, [1, 4])
        assert canonical(key) == key
    with pytest.raises(MixKeyError):
        ratio_key(0.12, {1.5: 50, 4: 50})  # type: ignore[dict-item]
