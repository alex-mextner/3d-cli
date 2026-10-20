"""Teeth tests for the layer-pattern rules (slicing.mix_pattern): every rule must be able to fail."""
from __future__ import annotations

import pytest

from slicing.mix_pattern import (
    LAYER_TOO_THICK,
    MINOR_ADJACENT,
    RUN_TOO_THICK,
    MixRules,
    analyse,
    analyse_pattern,
    decode_pattern,
    parse_row,
)


def rules_of(report) -> set[str]:
    return {v.rule for v in report.violations}


def test_isolated_minor_layers_pass() -> None:
    for pattern in ("WR", "GW", "KWR", "ABAC", "W"):
        report = analyse_pattern(pattern, 0.12)
        assert report.ok, (pattern, report.violations)
    # RWR: R is 2/3 — the isolated minor W is fine, the base R doubling over the seam is the band rule
    assert rules_of(analyse_pattern("RWR", 0.12)) == {RUN_TOO_THICK}


def test_gwg_repeats_as_gwggwg_and_fails() -> None:
    report = analyse_pattern("GWG", 0.12)
    assert not report.ok
    assert rules_of(report) == {RUN_TOO_THICK}
    (v,) = report.violations
    assert (v.colour, v.layers) == ("G", 2)
    assert v.thickness_mm == pytest.approx(0.24)
    assert "seam" in v.detail
    # the sequence itself is the proof: unrolled twice the G·G pair is there
    assert "GG" in "GWG" * 2


def test_gwg_passes_when_the_majority_base_may_double() -> None:
    assert analyse_pattern("GWG", 0.12, MixRules(max_run_mm=0.24)).ok
    assert not analyse_pattern("GWG", 0.12, MixRules(max_run_mm=0.20)).ok


def test_minor_on_adjacent_layers_always_fails() -> None:
    # W is the minor colour (1 of 4) and sits on two layers in a row — not relaxable by max_run_mm
    report = analyse_pattern("WWGGG", 0.12, MixRules(max_run_mm=10.0))  # W = 2/5 minor, adjacent
    assert rules_of(report) == {MINOR_ADJACENT}
    assert report.minor == ("W",)


def test_minor_adjacency_wraps_around_the_seam() -> None:
    # W is minor and printed on the LAST and the FIRST layer: a WW band at every repeat
    report = analyse_pattern("WGGGW", 0.12, MixRules(max_run_mm=10.0))
    (v,) = report.violations
    assert v.rule == MINOR_ADJACENT and v.layers == 2 and "seam" in v.detail


def test_equal_shares_make_every_colour_minor() -> None:
    assert analyse_pattern("GW", 0.12).ok
    report = analyse_pattern("GGWW", 0.12, MixRules(max_run_mm=10.0))
    assert {v.colour for v in report.violations} == {"G", "W"}


def test_thick_layer_fails_whatever_the_pattern() -> None:
    report = analyse_pattern("GW", 0.32)
    assert rules_of(report) == {LAYER_TOO_THICK}
    assert analyse_pattern("GW", 0.12).ok
    assert analyse_pattern("GW", 0.2, MixRules(layer_cap_mm=0.2)).ok


def test_one_thick_layer_in_a_variable_height_sequence() -> None:
    heights = [0.12, 0.12, 0.16, 0.12]
    report = analyse(list("GWGW"), heights, cyclic=False)
    assert rules_of(report) == {LAYER_TOO_THICK}
    assert report.max_layer_mm == pytest.approx(0.16)


def test_run_thickness_is_measured_in_mm_not_layers() -> None:
    # three G layers of 0.04 mm are a 0.12 mm band: within the limit; W (1 of 4) is isolated
    assert analyse(list("GGGW"), [0.04] * 3 + [0.04], cyclic=True).ok
    # the same pattern at 0.05 mm layers is a 0.15 mm band
    assert rules_of(analyse(list("GGGW"), 0.05, cyclic=True)) == {RUN_TOO_THICK}


def test_linear_sequence_does_not_wrap() -> None:
    assert analyse(list("WGGGW"), 0.12, MixRules(max_run_mm=10.0), cyclic=False).ok
    assert not analyse(list("WGGGW"), 0.12, MixRules(max_run_mm=10.0), cyclic=True).ok


def test_single_colour_is_not_a_mix() -> None:
    report = analyse_pattern("GGGG", 0.32)
    assert report.ok and not report.mixed


def test_rules_reject_nonsense() -> None:
    with pytest.raises(ValueError):
        MixRules(layer_cap_mm=0)
    with pytest.raises(ValueError):
        MixRules(max_run_mm=-1)
    with pytest.raises(ValueError):
        analyse([], 0.12, cyclic=True)
    with pytest.raises(ValueError):
        analyse(list("GW"), [0.12], cyclic=True)


def test_row_decoding() -> None:
    row = parse_row("2,3,1,1,50,0,g,w,m2,z0,xa0,xb0,d0,o0,u701,cm1,12")
    assert (row.component_a, row.component_b, row.pattern, row.slots) == (2, 3, "12", "23")
    assert parse_row("1,4,1,0,25,0,g,w,m2,z0,xa0,xb0,d0,o0,u702,cm0").pattern == ""
    assert decode_pattern("4312", 2, 3) == "4323"
    with pytest.raises(ValueError):
        parse_row("g,w,1,1")
