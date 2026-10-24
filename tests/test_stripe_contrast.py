"""stripe_contrast: the perceptual stripe model against the reference numbers of the babushkin-dom `stripe_view`
(0.08 mm layers, 30 cm, Chekhov palette G #1B5E20 / K #080A0D / W #FFFFFF: GKGKWK 22.9, GWGWGK 15.1, GWGK 10.7,
GKW 2.6, GW 0.2, GK 0.1). The port samples layer centres instead of layer starts, so it agrees within ~1 dE."""

from __future__ import annotations

import pytest

from stripe_contrast import layer_stripe_de

PALETTE = {"G": "#1B5E20", "K": "#080A0D", "W": "#FFFFFF"}


def _worst(pattern: str, h: float = 0.08, layers: int = 60, view_mm: float = 300.0) -> float:
    cols = [PALETTE[pattern[i % len(pattern)]] for i in range(layers)]
    got = [d for d in layer_stripe_de(cols, [h] * layers, period_layers=len(pattern), view_mm=view_mm) if d is not None]
    assert got
    return max(got)


@pytest.mark.parametrize(
    ("pattern", "reference"),
    [("GKGKWK", 22.9), ("GWGWGK", 15.1), ("GWGK", 10.7), ("GKW", 2.6), ("GW", 0.2), ("GK", 0.1)],
)
def test_matches_the_reference_model(pattern: str, reference: float) -> None:
    assert _worst(pattern) == pytest.approx(reference, abs=1.2)


def test_the_ordering_that_matters_holds() -> None:
    assert _worst("GKGKWK") > 3 > _worst("GKW") > 2 > _worst("GW")


def test_a_far_viewer_does_not_resolve_the_bands() -> None:
    assert _worst("GKGKWK", view_mm=1000.0) < 1.0


def test_a_short_stack_is_read_as_one_repeating_period() -> None:
    cols = [PALETTE[c] for c in "GKGKWK"]
    got = layer_stripe_de(cols, [0.08] * 6, period_layers=6)
    assert all(d is not None and d > 15 for d in got)


def test_edge_layers_of_a_tall_stack_are_not_measured_rather_than_zero() -> None:
    cols = [PALETTE[c] for c in ("GKGKWK" * 10)]
    got = layer_stripe_de(cols, [0.08] * 60, period_layers=6)
    assert got[0] is None and got[-1] is None and got[30] is not None


def test_real_layer_heights_change_the_verdict() -> None:
    cols = [PALETTE[c] for c in ("GKGKWK" * 10)]
    thin = max(d for d in layer_stripe_de(cols, [0.04] * 60, period_layers=6) if d is not None)
    thick = max(d for d in layer_stripe_de(cols, [0.12] * 60, period_layers=6) if d is not None)
    assert thick > thin


def test_bad_input_is_refused() -> None:
    with pytest.raises(ValueError):
        layer_stripe_de(["#FFFFFF"], [0.1, 0.1], period_layers=1)
    with pytest.raises(ValueError):
        layer_stripe_de(["green"], [0.1], period_layers=1)
