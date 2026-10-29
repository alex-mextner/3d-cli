"""calibrate_fit: plan, the result reader (free / press / fused, ties, non-monotonic, all fused), staleness check."""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from fleet_fixtures import REPO_FLEET

import calibrate_fit as cf
import fit_coupon
import fits
from calibrate_fit import Reading
from calibrate_fit_store import StoredRecord
from errors import InvalidArgument, UsageError


@pytest.fixture(autouse=True)
def fleet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THREED_FLEET", str(REPO_FLEET))


def ladder(fit: str, shape: str) -> fit_coupon.Ladder:
    return fit_coupon.ladders((fit,), (shape,))[0]


def readings(fit: str, shape: str, results: str) -> list[Reading]:
    return cf.parse_result(results, ladder(fit, shape))


# --------------------------------------------------------------------------- plan
def test_plan_lists_coupons_gaps_estimate_and_the_readout_per_coupon() -> None:
    info = cf.printer_info("k1se")
    doc = cf.plan_doc(info, "PLA", cf.select_ladders(None, None))
    assert doc["schema"] == "3d.calibrate_fit/1" and doc["printer"] == "k1se" and doc["model"] == "Creality K1 SE"
    assert doc["bed_mm"] == [220.0, 220.0, 250.0] and doc["nozzle_mm"] == 0.4 and doc["head"] == "T1"
    assert doc["estimate"]["minutes"] <= 10 and doc["estimate"]["grams"] > 0 and "visits" in doc["estimate"]["basis"]
    assert doc["coupon"]["fits_bed"] is True
    assert [c["id"] for c in doc["coupons"]] == [
        "sliding_round", "tight_round", "press_round", "sliding_slot", "snap_slot", "press_slot"
    ]
    first = doc["coupons"][0]
    assert [s["gap_mm"] for s in first["steps"]] == [0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35]
    assert "--result step=N" in first["readout"] and "--measured D1,...,D7" in first["readout"]
    assert set(first["results"]) == {"free", "press", "fused"}
    assert first["record"].startswith("3d calibrate fit record --printer k1se --material PLA --fit sliding")
    press = doc["coupons"][2]
    assert "LAST step that still grips" in press["readout"]


def test_plan_for_the_u1_uses_its_bed_and_its_four_heads_first_usable_one() -> None:
    doc = cf.plan_doc(cf.printer_info("u1"), "PETG", cf.select_ladders(("sliding",), None))
    assert doc["bed_mm"][:2] == [270.0, 270.0] and doc["coupon"]["fits_bed"] is True
    assert [c["id"] for c in doc["coupons"]] == ["sliding_round", "sliding_slot"] and doc["head"] == "T1"


def test_an_unknown_printer_lists_what_is_accepted() -> None:
    with pytest.raises(InvalidArgument) as e:
        cf.printer_info("ender9")
    assert "k1se" in e.value.accepted and "Creality K1 SE" in e.value.accepted


def test_csv_validates_each_name() -> None:
    assert cf.csv("snap,sliding", ["snap", "sliding", "tight"], "--fit") == ("snap", "sliding")
    assert cf.csv(None, ["a"], "--fit") is None
    with pytest.raises(InvalidArgument):
        cf.csv("snap,glue", ["snap", "sliding"], "--fit")


def test_material_names_resolve_through_the_registry() -> None:
    assert cf.material_name("Elegoo Rapid PLA+") == "PLA" and cf.material_name("pla") == "PLA"
    assert cf.material_name("mystery") == "MYSTERY"


# --------------------------------------------------------------------------- --result
def test_step_n_marks_the_first_free_step_and_the_smaller_ones_press() -> None:
    got = readings("sliding", "round", "step=3")
    assert [(r.gap_mm, r.result) for r in got] == [
        (0.05, "press"), (0.1, "press"), (0.15, "free"), (0.2, "free"), (0.25, "free"), (0.3, "free"), (0.35, "free")
    ]


def test_step_n_of_a_press_ladder_is_the_last_gripping_step() -> None:
    got = readings("press", "round", "step=4")
    assert [r.result for r in got] == ["press"] * 4 + ["free"] * 3
    assert got[3].gap_mm == -0.075


def test_one_word_per_step_or_k_equals_word_pairs() -> None:
    words = "fused,fused,press,free,free,free,free"
    assert [r.result for r in readings("tight", "round", words)] == words.split(",")
    pairs = readings("tight", "round", "3=press,4=free")
    assert [(r.gap_mm, r.result) for r in pairs] == [(0.1, "press"), (0.15, "free")]


@pytest.mark.parametrize(
    "bad",
    ["", "step=0", "step=8", "step=x", "free,free", "1=free,1=press", "9=free", "step=2,3=free", "1=stuck", "free,free,free,free,free,free,loose"],
)
def test_bad_results_are_usage_errors(bad: str) -> None:
    with pytest.raises((UsageError, InvalidArgument)):
        readings("sliding", "round", bad)


# --------------------------------------------------------------------------- --measured
def test_caliper_numbers_pick_the_step_whose_real_gap_reaches_the_table_value() -> None:
    lad = ladder("sliding", "round")
    undersize = 0.07  # the printed holes come out 0.07 mm per side too small
    measured = [round(4.0 + 2 * (g - undersize), 3) for g in lad.gaps]
    got = cf.from_measured(measured, lad)
    assert [r.result for r in got] == ["fused", "press", "press", "press", "press", "free", "free"]
    pick = cf.pick(got, "sliding", "round")
    assert pick.ok and pick.gap_mm == 0.3 and pick.delta_mm == 0.1  # 0.07 rounded up to the next 0.05 step


def test_a_measured_pin_changes_the_real_gap() -> None:
    lad = ladder("sliding", "round")
    measured = [round(4.0 + 2 * g, 3) for g in lad.gaps]  # holes exactly as designed...
    assert cf.pick(cf.from_measured(measured, lad), "sliding", "round").gap_mm == 0.2
    assert cf.pick(cf.from_measured(measured, lad, 4.1), "sliding", "round").gap_mm == 0.25  # a pin 0.05/side fat


def test_a_press_ladder_by_caliper_picks_the_loosest_step_with_enough_interference() -> None:
    lad = ladder("press", "round")
    measured = [round(4.0 + 2 * (g + 0.02), 3) for g in lad.gaps]  # holes come out 0.02 per side too big
    got = cf.from_measured(measured, lad)
    pick = cf.pick(got, "press", "round")
    assert [r.result for r in got] == ["press", "press"] + ["free"] * 5  # real gaps -0.13 -0.105 | -0.08 ...
    assert pick.ok and pick.gap_mm == -0.125 and pick.delta_mm == -0.025


def test_measured_needs_one_number_per_step() -> None:
    with pytest.raises(UsageError, match="needs 7 numbers"):
        cf.from_measured([4.1, 4.2], ladder("sliding", "round"))


# --------------------------------------------------------------------------- pick
def test_pick_takes_the_smallest_free_gap_and_computes_delta_against_the_table() -> None:
    pick = cf.pick(readings("sliding", "round", "step=3"), "sliding", "round")
    assert (pick.ok, pick.gap_mm, pick.delta_mm, pick.table_mm, pick.warnings) == (True, 0.15, -0.05, 0.2, [])
    snap = cf.pick(readings("snap", "slot", "step=5"), "snap", "slot")
    assert snap.gap_mm == 0.35 and snap.delta_mm == pytest.approx(0.05)


def test_delta_feeds_the_s0_conversion_to_the_same_clearance() -> None:
    pick = cf.pick(readings("sliding", "round", "step=3"), "sliding", "round")
    rows = fits.corrections_from_fit_clearance(
        [r.to_dict() for r in pick.readings], printer="k1se", material="PLA", nozzle_mm=0.4,
        measured_at="2026-10-24T10:00:00.000Z",
    )
    assert [(r.fit_type, r.shape, r.delta_mm) for r in rows] == [("sliding", "round", pick.delta_mm)]
    assert fits.resolve("sliding", "round", "k1se", "PLA", 0.4, extra_corrections=rows).clearance_mm == pick.gap_mm


def test_press_picks_the_largest_gap_that_still_grips() -> None:
    pick = cf.pick(readings("press", "round", "step=3"), "press", "round")
    assert pick.ok and pick.gap_mm == -0.1 and pick.delta_mm == 0.0  # exactly the table's 0.10 interference
    rows = fits.corrections_from_fit_clearance(
        [r.to_dict() for r in pick.readings], printer="k1se", material="PLA", nozzle_mm=0.4, measured_at="2026-10-24T10:00:00Z"
    )
    assert fits.resolve("press", "round", "k1se", "PLA", 0.4, extra_corrections=rows).clearance_mm == -0.1
    looser = cf.pick(readings("press", "round", "step=5"), "press", "round")
    assert looser.gap_mm == -0.05 and looser.delta_mm == 0.05


def test_two_readings_of_one_gap_keep_the_tighter_one_and_say_so() -> None:
    twice = readings("sliding", "round", "step=3") + [Reading("sliding", "round", 0.15, "press")]
    pick = cf.pick(twice, "sliding", "round")
    assert pick.gap_mm == 0.2  # 0.15 is now `press`, the next free gap wins
    assert any("read twice" in w and "kept the tighter `press`" in w for w in pick.warnings)
    same = cf.pick(readings("sliding", "round", "step=3") * 2, "sliding", "round")
    assert same.warnings == [] and len(same.readings) == 7  # identical repeats are not a tie


def test_a_non_monotonic_readout_is_recorded_with_a_warning() -> None:
    pick = cf.pick(readings("sliding", "round", "press,free,press,free,free,free,free"), "sliding", "round")
    assert pick.ok and pick.gap_mm == 0.1
    assert any("non-monotonic" in w and "0.1" in w and "0.15" in w for w in pick.warnings)


def test_all_fused_is_not_usable_and_says_to_widen_the_range() -> None:
    pick = cf.pick(readings("sliding", "round", ",".join(["fused"] * 7)), "sliding", "round")
    assert not pick.ok and pick.gap_mm is None and pick.delta_mm is None
    assert pick.problem is not None and "widen the range" in pick.problem and "--gap-offset" in pick.problem
    assert "every step is fused" in pick.problem


def test_no_free_step_but_some_press_is_not_usable_either() -> None:
    pick = cf.pick(readings("tight", "round", "fused,fused,press,press,press,press,press"), "tight", "round")
    assert not pick.ok and pick.problem is not None and "widen the range" in pick.problem


def test_a_press_ladder_that_never_grips_or_always_cracks_says_which_way_to_widen() -> None:
    loose = cf.pick(readings("press", "round", ",".join(["free"] * 7)), "press", "round")
    assert not loose.ok and loose.problem is not None and "more interference" in loose.problem
    cracked = cf.pick(readings("press", "round", ",".join(["fused"] * 7)), "press", "round")
    assert not cracked.ok and cracked.problem is not None and "larger gaps" in cracked.problem


def test_a_ladder_that_is_free_from_the_first_step_warns_but_records() -> None:
    pick = cf.pick(readings("sliding", "round", "step=1"), "sliding", "round")
    assert pick.ok and pick.gap_mm == 0.05 and any("every step is free" in w for w in pick.warnings)


def test_a_press_ladder_that_grips_everywhere_warns() -> None:
    pick = cf.pick(readings("press", "round", ",".join(["press"] * 7)), "press", "round")
    assert pick.ok and pick.gap_mm == 0.0 and any("every step grips" in w for w in pick.warnings)


# --------------------------------------------------------------------------- merge / records
def test_merge_readings_replaces_only_the_ladders_it_re_reads() -> None:
    old = [r.to_dict() for r in readings("snap", "slot", "step=4")] + [
        r.to_dict() for r in readings("sliding", "round", "step=2")
    ]
    new = readings("sliding", "round", "step=5")
    merged = cf.merge_readings(old, new)
    assert {(r["fit_type"], r["shape"]) for r in merged} == {("snap", "slot"), ("sliding", "round")}
    assert [r["result"] for r in merged if r["fit_type"] == "sliding"] == [r.result for r in new]


def test_readings_json_is_sorted_and_compact() -> None:
    text = cf.readings_json(readings("tight", "round", "step=2") + readings("snap", "slot", "step=2"))
    assert text.startswith('[{"fit_type":"snap"') and " " not in text


# --------------------------------------------------------------------------- check
NOW = datetime.datetime(2026, 10, 24, tzinfo=datetime.timezone.utc)


def record(**kw: object) -> StoredRecord:
    base: dict[str, object] = {
        "id": "#1", "origin": "3d-api", "printer": "k1se", "head": "T1", "material": "PLA", "nozzle_mm": 0.4,
        "measured_at": "2026-10-20T10:00:00.000Z",
        "readings": [r.to_dict() for fit, shape in fit_coupon.LADDER_SPECS for r in readings(fit, shape, "step=3")],
    }
    base.update(kw)
    return StoredRecord(**base)  # type: ignore[arg-type]


def test_check_flags_missing_stale_partial_and_ok() -> None:
    info = cf.printer_info("k1se")
    assert cf.check_pair(info, "PLA", [], now=NOW).level == "MISSING"
    assert cf.check_pair(info, "PETG", [record()], now=NOW).level == "MISSING"  # a material change
    ok = cf.check_pair(info, "pla", [record()], now=NOW)
    assert ok.level == "OK" and ok.reasons == [] and ok.records == ["#1"] and len(ok.covered) == 6
    old = cf.check_pair(info, "PLA", [record(measured_at="2026-01-01T00:00:00.000Z")], now=NOW)
    assert old.level == "STALE" and "days old" in old.reasons[0]
    assert cf.check_pair(info, "PLA", [record(measured_at="2026-01-01T00:00:00.000Z")], now=NOW, max_age_days=400).level == "OK"
    wide = cf.check_pair(cf.printer_info("k1se", nozzle_mm=0.6), "PLA", [record()], now=NOW)
    assert wide.level == "STALE" and "0.4 mm nozzle" in wide.reasons[0] and "0.6 mm now" in wide.reasons[0]
    some = [r.to_dict() for r in readings("sliding", "round", "step=3")]
    partial = cf.check_pair(info, "PLA", [record(readings=some)], now=NOW)
    assert partial.level == "PARTIAL" and "snap/slot" in partial.missing and partial.to_dict()["fix"] is None


def test_check_fix_names_the_quick_command_and_other_printers_do_not_count() -> None:
    info = cf.printer_info("u1")
    status = cf.check_pair(info, "PLA", [record()], now=NOW)  # the record is for the k1se
    assert status.level == "MISSING"
    assert status.to_dict()["fix"] == "3d calibrate fit quick --printer u1 --material PLA"


def test_the_buffer_record_ids_survive_into_the_status(tmp_path: Path) -> None:
    status = cf.check_pair(cf.printer_info("k1se"), "PLA", [record(id="buffer:0198abcd", origin="buffer")], now=NOW)
    assert status.records == ["buffer:0198abcd"]
