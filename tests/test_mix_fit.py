"""`3d mix fit` (#417): per-filament effective colours fitted to the measured mixes of a tester."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("scipy")
from mix_fit_fixtures import PURE, TRUTH, simulated_readout  # noqa: E402

from printops.color import hex_delta_e  # noqa: E402
from slicing.mix_fit import DEFAULT_LAMBDA, FitError, collect, fit, validate_fit  # noqa: E402


def _by_slot(rec: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {f["slot"]: f for f in rec["filaments"]}


def test_fitted_colours_are_within_de15_of_the_truth_and_the_mix_error_drops(
    tmp_path: Path,
) -> None:
    readout = simulated_readout(tmp_path)
    rec = fit([readout])
    fitted = _by_slot(rec)
    for slot, true_hex in TRUTH.items():
        err = hex_delta_e(fitted[slot]["fitted_hex"], true_hex)
        assert err < 1.5, (slot, fitted[slot]["fitted_hex"], true_hex, err)
    # the pure swatches alone are off the walls, and the nominal colours more so: the fit is what helps
    assert max(hex_delta_e(PURE[s], TRUTH[s]) for s in TRUTH) > 1.5
    sm = rec["summary"]
    assert sm["mixes"] == 4 and sm["filaments"] == 3
    assert (
        sm["mean_delta_e_fitted"] < sm["mean_delta_e_pure"] < sm["mean_delta_e_nominal"]
    )
    assert sm["improved"] is True
    assert rec["notes"] == [] and all(
        f["constrained_by_mixes"] for f in rec["filaments"]
    )


def test_every_mix_is_predicted_better_after_the_fit_than_from_the_nominal_colours(
    tmp_path: Path,
) -> None:
    rec = fit([simulated_readout(tmp_path)])
    for m in rec["mixes"]:
        assert m["delta_e_fitted"] < m["delta_e_nominal"], m
        assert m["delta_e_fitted"] < 1.5, m


def test_two_testers_pool_their_mixes_by_filament(tmp_path: Path) -> None:
    a = simulated_readout(tmp_path / "a", target="#808080", count=4)
    b = simulated_readout(tmp_path / "b", target="#B5654F", count=3, sha="b" * 64)
    rec = fit([a, b])
    assert rec["summary"]["filaments"] == 3  # the same three filaments, not six
    assert rec["summary"]["mixes"] == len(a["swatches"]) + len(b["swatches"]) - 6
    assert {f["readouts"] for f in rec["filaments"]} == {2}
    assert len(rec["readouts"]) == 2


def test_a_filament_without_a_mix_keeps_its_pure_colour_and_says_so(
    tmp_path: Path,
) -> None:
    readout = simulated_readout(tmp_path)
    readout["swatches"] = [s for s in readout["swatches"] if s["kind"] == "reference"]
    rec = fit([readout])
    assert rec["mixes"] == [] and rec["summary"]["improved"] is False
    assert [f["fitted_hex"] for f in rec["filaments"]] == [
        f["pure_hex"] for f in rec["filaments"]
    ]
    assert len(rec["notes"]) == 3 and "no mix swatch" in rec["notes"][0]


def test_lambda_trades_the_pure_swatch_against_the_mixes(tmp_path: Path) -> None:
    readout = simulated_readout(tmp_path)
    default = _by_slot(fit([readout]))
    tight = _by_slot(fit([readout], 100.0))
    for slot in TRUTH:
        assert (
            hex_delta_e(tight[slot]["fitted_hex"], tight[slot]["pure_hex"]) < 0.5
        )  # the prior wins
    for slot in (
        3,
        4,
    ):  # the mixes are what move the green and the white towards the walls
        assert hex_delta_e(default[slot]["fitted_hex"], TRUTH[slot]) < hex_delta_e(
            tight[slot]["fitted_hex"], TRUTH[slot]
        )
    assert (
        fit([readout], 0.0)["summary"]["mean_delta_e_fitted"] < 0.5
    )  # no prior: the mixes alone are matched
    assert DEFAULT_LAMBDA > 0
    with pytest.raises(FitError, match="lambda"):
        fit([readout], -1.0)


def test_the_fit_is_deterministic(tmp_path: Path) -> None:
    readout = simulated_readout(tmp_path)
    assert fit([readout]) == fit([copy.deepcopy(readout)])


def test_filaments_are_keyed_by_spoolman_id_else_by_head(tmp_path: Path) -> None:
    readout = simulated_readout(tmp_path)
    assert set(collect([readout])[0]) == {"f21", "f23", "f24"}
    for s in readout["swatches"]:
        s["calibration"] = {}
    assert set(collect([readout])[0]) == {"slot1", "slot3", "slot4"}


def test_bad_input_is_refused_with_the_reason(tmp_path: Path) -> None:
    readout = simulated_readout(tmp_path)
    with pytest.raises(FitError, match="schema must be mix-readout/1"):
        fit([{"schema": "other"}])
    no_ref = copy.deepcopy(readout)
    no_ref["swatches"] = [
        s
        for s in no_ref["swatches"]
        if not (s["kind"] == "reference" and s["slot"] == 4)
    ]
    with pytest.raises(FitError, match="no reference swatch of head"):
        fit([no_ref])
    none = copy.deepcopy(readout)
    none["swatches"] = []
    with pytest.raises(FitError, match="no reference"):
        fit([none])


def test_validate_fit_has_teeth(tmp_path: Path) -> None:
    rec = fit([simulated_readout(tmp_path)])
    assert validate_fit(rec) == []
    assert validate_fit({"schema": "x"}) != []
    bad = copy.deepcopy(rec)
    bad["filaments"][0]["fitted_hex"] = "nope"
    assert validate_fit(bad) != []
    assert validate_fit({**rec, "filaments": []}) == [
        "filaments must be a non-empty list"
    ]
    assert validate_fit({**rec, "summary": None}) != []
