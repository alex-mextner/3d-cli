"""Colour-mix acceptance (#116): CIEDE2000 against the published Sharma reference pairs, the
black/white fixture, and stripe-aware scoring of long high-contrast cycles."""

from __future__ import annotations

import pytest

from printops.color import delta_e2000, hex_from_linear, linear_rgb, linear_to_lab
from slicing.mix_model import Candidate, Slot, rank, stripe_delta_e
from slicing.mix_pattern import MixRules

Lab = tuple[float, float, float]

# Sharma, Wu, Dalal, "The CIEDE2000 Color-Difference Formula: Implementation Notes,
# Supplementary Test Data, and Mathematical Observations", Color Res. Appl. 30(1), 2005, Table 1:
# (L1, a1, b1), (L2, a2, b2), published dE00. Source: the authors' ciede2000testdata.txt
# (hajim.rochester.edu/ece/sites/gsharma/ciede2000/).
SHARMA_PAIRS: list[tuple[Lab, Lab, float]] = [
    ((50.0000, 2.6772, -79.7751), (50.0000, 0.0000, -82.7485), 2.0425),
    ((50.0000, 3.1571, -77.2803), (50.0000, 0.0000, -82.7485), 2.8615),
    ((50.0000, 2.8361, -74.0200), (50.0000, 0.0000, -82.7485), 3.4412),
    ((50.0000, -1.3802, -84.2814), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -1.1848, -84.8006), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -0.9009, -85.5211), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, 0.0000, 0.0000), (50.0000, -1.0000, 2.0000), 2.3669),
    ((50.0000, -1.0000, 2.0000), (50.0000, 0.0000, 0.0000), 2.3669),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0009), 7.1792),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0010), 7.1792),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0011), 7.2195),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0012), 7.2195),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0009, -2.4900), 4.8045),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0010, -2.4900), 4.8045),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0011, -2.4900), 4.7461),
    ((50.0000, 2.5000, 0.0000), (50.0000, 0.0000, -2.5000), 4.3065),
    ((50.0000, 2.5000, 0.0000), (73.0000, 25.0000, -18.0000), 27.1492),
    ((50.0000, 2.5000, 0.0000), (61.0000, -5.0000, 29.0000), 22.8977),
    ((50.0000, 2.5000, 0.0000), (56.0000, -27.0000, -3.0000), 31.9030),
    ((50.0000, 2.5000, 0.0000), (58.0000, 24.0000, 15.0000), 19.4535),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.1736, 0.5854), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.2972, 0.0000), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 1.8634, 0.5757), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.2592, 0.3350), 1.0000),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((63.0109, -31.0961, -5.8663), (62.8187, -29.7946, -4.0864), 1.2630),
    ((61.2901, 3.7196, -5.3901), (61.4292, 2.2480, -4.9620), 1.8731),
    ((35.0831, -44.1164, 3.7933), (35.0232, -40.0716, 1.5901), 1.8645),
    ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
    ((36.4612, 47.8580, 18.3852), (36.2715, 50.5065, 21.2231), 1.4146),
    ((90.8027, -2.0831, 1.4410), (91.1528, -1.6435, 0.0447), 1.4441),
    ((90.9257, -0.5406, -0.9208), (88.6381, -0.8985, -0.7239), 1.5381),
    ((6.7747, -0.2908, -2.4247), (5.8714, -0.0985, -2.2286), 0.6377),
    ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
]

BW = [Slot(1, "#000000"), Slot(4, "#FFFFFF")]
_HEX = {"1": "#000000", "4": "#FFFFFF"}


def test_sharma_table_has_all_34_published_pairs() -> None:
    assert len(SHARMA_PAIRS) == 34


@pytest.mark.parametrize(("lab1", "lab2", "expected"), SHARMA_PAIRS)
def test_de2000_matches_the_published_sharma_pairs_within_1e_4(
    lab1: Lab, lab2: Lab, expected: float
) -> None:
    assert delta_e2000(lab1, lab2) == pytest.approx(expected, abs=1e-4)
    assert delta_e2000(lab2, lab1) == pytest.approx(expected, abs=1e-4)  # symmetric


def _best(target: str, rules: MixRules, max_cycle: int) -> Candidate:
    return rank(target, BW, max_cycle=max_cycle, rules=rules)[0]


def test_black_white_one_to_one_in_linear_light_alternates() -> None:
    """The 1:1 stripe of black and white is #BCBCBC (L* 76) in linear light; asked for exactly
    that, the best recipe is weights 1:1 with L* within 2 of the target, alternating."""
    target = hex_from_linear((0.5, 0.5, 0.5))
    assert target == "#BCBCBC"
    best = _best(target, MixRules(), 12)
    assert best.weights == (1, 1) and best.pattern in ("14", "41")
    assert abs(best.lab[0] - linear_to_lab(linear_rgb(target))[0]) < 2


def test_black_white_grey_808080_alternates_under_default_rules() -> None:
    """#808080 (L* 54) is only 22 % white in linear light, so 1:1 overshoots to L* 76; the default
    rules forbid the 2:1 cycle (black on adjacent layers), so the best is the alternating 1:1."""
    best = _best("#808080", MixRules(), 12)
    assert best.weights == (1, 1) and best.pattern in ("14", "41")
    assert all(a != b for a, b in zip(best.pattern, best.pattern[1:], strict=False))


def test_black_white_grey_808080_reaches_target_lightness_with_dispersed_layers() -> (
    None
):
    """With thicker runs allowed the best cycle lands within 2 L* of #808080 and keeps the
    minority white layers apart instead of grouping them."""
    best = _best("#808080", MixRules(max_run_mm=0.48), 12)
    assert abs(best.lab[0] - linear_to_lab(linear_rgb("#808080"))[0]) < 2
    assert "44" not in best.pattern * 2
    assert best.pattern.count("4") < best.pattern.count("1")


def _cand(pat: str, delta_e: float, near: float) -> Candidate:
    weights = (pat.count("1"), pat.count("4"))
    return Candidate(weights, (1, 4), pat, "#808080", (50.0, 0.0, 0.0), delta_e, near)


def test_twelve_layer_high_contrast_cycle_scores_worse_than_three_layer_with_higher_de() -> (
    None
):
    long_cycle, short_cycle = "111111444444", "141"
    near_long = stripe_delta_e([_HEX[d] for d in long_cycle], 0.12)
    near_short = stripe_delta_e([_HEX[d] for d in short_cycle], 0.12)
    assert near_long > near_short
    long_c = _cand(long_cycle, 1.0, near_long)
    short_c = _cand(short_cycle, 3.0, near_short)  # ΔE 2 higher
    assert long_c.cycle == 12 and short_c.cycle == 3
    assert long_c.score > short_c.score
