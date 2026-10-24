from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

import fits
from errors import InvalidArgument
from fits import Correction, FitTableError


def raw() -> dict[str, Any]:
    data = yaml.safe_load(fits.TABLE_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


# --------------------------------------------------------------------------- schema
def test_committed_table_validates_against_the_schema() -> None:
    table = fits.load_table()
    assert set(table.fit_types) == {"snap", "sliding", "tight", "press", "glue"}
    assert fits.SCHEMA == raw()["schema"] == "3d.fits/1"
    for ft in table.fit_types.values():
        assert ft.tol_mm > 0 and ft.applies_to and ft.features and ft.notes


def test_house_defaults_snap_sliding_press() -> None:
    t = fits.load_table().fit_types
    assert t["snap"].clearance_mm_per_face == 0.3
    assert t["sliding"].clearance_mm_per_face == 0.2
    assert t["press"].interference_mm == 0.1


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d.update(schema="3d.fits/2"), "schema"),
    (lambda d: d["fit_types"].pop("glue"), "missing"),
    (lambda d: d["fit_types"]["snap"].pop("tol_mm"), "tol_mm"),
    (lambda d: d["fit_types"]["snap"].update(applies_to=["cube"]), "not in shapes"),
    (lambda d: d["fit_types"]["snap"].update(clearance_mm_per_face=-1), "minimum"),
    (lambda d: d["aliases"].update(sliding={"fit_type": "tight"}), "shadows"),
    (lambda d: d["aliases"].update(x={"fit_type": "nope"}), "fit_type"),
    (lambda d: d["corrections"].pop(0), "default row"),
    (lambda d: d["corrections"][0].update(source="guess"), "source"),
    (lambda d: d["corrections"].append({"printer": "k1se", "material": "PLA", "nozzle_mm": 0.4,
                                         "delta_mm": 0.1, "source": "manual"}), "measured_at"),
    (lambda d: d["corrections"][0].update(bogus=1), "unknown keys"),
])
def test_invalid_tables_are_rejected(mutate: Any, message: str) -> None:
    data = copy.deepcopy(raw())
    mutate(data)
    with pytest.raises(FitTableError, match=message):
        fits.parse_table(data)


def test_load_table_reports_bad_yaml(tmp_path: Path) -> None:
    path = tmp_path / "fits.yaml"
    path.write_text("schema: [unclosed", encoding="utf-8")
    with pytest.raises(FitTableError, match="not valid YAML"):
        fits.load_table(path)


# --------------------------------------------------------------------------- resolve
def test_every_fit_type_and_shape_resolves_with_nonempty_provenance() -> None:
    table = fits.load_table()
    for name in table.names():
        for shape in table.shapes:
            res = fits.resolve(name, shape)
            assert res.tol_mm > 0
            assert res.provenance and res.provenance[0]["kind"] == "fit_type"
            assert any(p["kind"] == "correction" for p in res.provenance)
            assert res.to_dict()["schema"] == "3d.fits/1"


def test_unknown_printer_and_material_fall_back_to_the_default_row_and_say_so() -> None:
    res = fits.resolve("sliding", "planar", printer="mystery-printer", material="unobtainium", nozzle=0.4)
    assert res.clearance_mm == 0.2 and res.fallback is True
    corr = next(p for p in res.provenance if p["kind"] == "correction")
    assert corr["printer"] == "*" and corr["material"] == "*"
    assert corr["fallback"] is True and corr["fallback_reason"] == ["printer", "material"]


def test_unspecified_printer_and_material_is_not_a_fallback() -> None:
    assert fits.resolve("sliding", "planar").fallback is False


def test_material_default_row_applies_and_flags_the_wildcard_printer() -> None:
    res = fits.resolve("sliding", "round", printer="k1se", material="petg", nozzle=0.4)
    assert res.clearance_mm == 0.25
    corr = next(p for p in res.provenance if p["kind"] == "correction")
    assert corr["material"] == "PETG" and corr["fallback_reason"] == ["printer"]


def test_a_matching_correction_row_is_applied_and_explained() -> None:
    row = Correction(delta_mm=0.07, source="calibration", printer="K1 SE", material="PLA", nozzle_mm=0.4,
                     measured_at="2026-10-24T10:00:00.000Z")
    res = fits.resolve("sliding", "planar", "k1-se", "pla", 0.4, 0.2, extra_corrections=[row])
    assert res.clearance_mm == 0.27 and res.fallback is False
    kinds = [p["kind"] for p in res.provenance]
    assert kinds[:2] == ["fit_type", "correction"]
    assert res.provenance[1]["source"] == "calibration" and res.provenance[1]["delta_mm"] == 0.07


def test_a_correction_tagged_with_an_alias_applies_to_that_alias_only() -> None:
    row = Correction(delta_mm=0.05, source="manual", printer="k1se", fit_type="normal",
                     measured_at="2026-10-24T10:00:00.000Z")
    assert fits.resolve("normal", "planar", "k1se", extra_corrections=[row]).clearance_mm == 0.25
    assert fits.resolve("sliding", "planar", "k1se", extra_corrections=[row]).clearance_mm == 0.2
    assert fits.resolve("loose", "planar", "k1se", extra_corrections=[row]).clearance_mm == 0.35


def test_a_correction_for_another_nozzle_layer_or_shape_does_not_apply() -> None:
    row = Correction(delta_mm=0.5, source="manual", printer="k1se", material="PLA", nozzle_mm=0.6,
                     measured_at="2026-10-24T10:00:00.000Z")
    assert fits.resolve("sliding", "planar", "k1se", "PLA", 0.4, extra_corrections=[row]).clearance_mm == 0.2
    layer = Correction(delta_mm=0.5, source="manual", printer="k1se", layer_height_mm=0.12,
                       measured_at="2026-10-24T10:00:00.000Z")
    assert fits.resolve("sliding", "planar", "k1se", "PLA", 0.4, 0.2, extra_corrections=[layer]).clearance_mm == 0.2
    assert fits.resolve("sliding", "planar", "k1se", "PLA", 0.4, 0.12, extra_corrections=[layer]).clearance_mm == 0.7


def test_most_specific_then_calibration_then_newest_wins_and_losers_are_listed() -> None:
    generic = Correction(delta_mm=0.01, source="calibration", printer="k1se", measured_at="2026-10-01T00:00:00.000Z")
    specific = Correction(delta_mm=0.04, source="manual", printer="k1se", material="PLA",
                          measured_at="2026-09-01T00:00:00.000Z")
    res = fits.resolve("sliding", "planar", "k1se", "PLA", extra_corrections=[generic, specific])
    assert res.clearance_mm == 0.24
    assert [p["delta_mm"] for p in res.provenance if p["kind"] == "correction_superseded"] == [0.01]
    older = Correction(delta_mm=0.02, source="manual", printer="k1se", material="PLA",
                       measured_at="2026-08-01T00:00:00.000Z")
    cal = Correction(delta_mm=0.03, source="calibration", printer="k1se", material="PLA",
                     measured_at="2026-07-01T00:00:00.000Z")
    assert fits.resolve("sliding", "planar", "k1se", "PLA", extra_corrections=[specific, older, cal]).clearance_mm == 0.23


def test_press_is_a_signed_negative_clearance() -> None:
    res = fits.resolve("press", "pin")
    assert res.clearance_mm == -0.1 and res.interference_mm == 0.1


def test_aliases_resolve_through_the_table_and_loose_is_sliding_plus_015() -> None:
    assert fits.resolve("free_running", "planar").fit_type == "sliding"
    assert fits.resolve("tight_running", "planar").clearance_mm == 0.1
    loose = fits.resolve("loose", "round")
    assert loose.clearance_mm == 0.35 and loose.fit_type == "sliding" and loose.requested == "loose"
    assert {"kind": "alias", "id": "loose"}.items() <= loose.provenance[1].items()


def test_shape_outside_applies_to_is_noted_not_refused() -> None:
    res = fits.resolve("press", "hinge")
    assert any(p.get("id") == "shape_outside_applies_to" for p in res.provenance)


def test_unknown_fit_type_or_shape_is_an_invalid_argument() -> None:
    with pytest.raises(InvalidArgument):
        fits.resolve("snug-ish", "planar")
    with pytest.raises(InvalidArgument):
        fits.resolve("sliding", "cube")


# --------------------------------------------------------------------------- calibration records
def test_fit_clearance_record_validation() -> None:
    ok = {"fit_type": "sliding", "shape": "planar", "gap_mm": 0.2, "result": "free"}
    assert fits.validate_fit_clearance(ok) == ok
    for bad in ({**ok, "result": "stuck"}, {**ok, "gap_mm": -1}, {**ok, "shape": "cube"},
                {**ok, "fit_type": "snug"}, {**ok, "extra": 1}):
        with pytest.raises(FitTableError):
            fits.validate_fit_clearance(bad)


def test_non_finite_numbers_are_refused_in_records_and_tables() -> None:
    ok = {"fit_type": "sliding", "shape": "planar", "gap_mm": 0.2, "result": "free"}
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(FitTableError, match="finite"):
            fits.validate_fit_clearance({**ok, "gap_mm": bad})
    table = raw()
    table["fit_types"]["snap"]["tol_mm"] = float("nan")
    with pytest.raises(FitTableError, match="finite"):
        fits.parse_table(table)


def test_correction_rows_are_ordered_by_instant_not_by_timestamp_text() -> None:
    older = Correction(delta_mm=0.01, source="manual", printer="k1se", measured_at="2026-10-24T10:00:00Z")
    newer = Correction(delta_mm=0.09, source="manual", printer="k1se", measured_at="2026-10-24T10:00:00.500Z")
    for rows in ([older, newer], [newer, older]):
        assert fits.resolve("sliding", "planar", "k1se", extra_corrections=rows).clearance_mm == 0.29


def test_a_malformed_measured_at_is_refused_in_tables_selection_and_calibration() -> None:
    table = raw()
    table["corrections"].append({"printer": "k1se", "material": "*", "delta_mm": 0.1, "source": "manual",
                                 "measured_at": "yesterday"})
    with pytest.raises(FitTableError, match="ISO-8601"):
        fits.parse_table(table)
    bad = Correction(delta_mm=0.1, source="manual", printer="k1se", measured_at="yesterday")
    with pytest.raises(FitTableError, match="ISO-8601"):
        fits.resolve("sliding", "planar", "k1se", extra_corrections=[bad])
    with pytest.raises(FitTableError, match="ISO-8601"):
        fits.corrections_from_fit_clearance([], printer="k1se", material="PLA", nozzle_mm=0.4, measured_at="soon")


def test_coupon_readings_become_calibration_rows_that_resolve_applies() -> None:
    records = [
        {"fit_type": "sliding", "shape": "planar", "gap_mm": 0.15, "result": "fused"},
        {"fit_type": "sliding", "shape": "planar", "gap_mm": 0.2, "result": "press"},
        {"fit_type": "sliding", "shape": "planar", "gap_mm": 0.25, "result": "free"},
        {"fit_type": "sliding", "shape": "planar", "gap_mm": 0.3, "result": "free"},
        {"fit_type": "tight", "shape": "round", "gap_mm": 0.1, "result": "fused"},
        {"fit_type": "press", "shape": "pin", "gap_mm": 0.0, "result": "press"},
    ]
    rows = fits.corrections_from_fit_clearance(
        records, printer="k1se", material="PLA", nozzle_mm=0.4, measured_at="2026-10-24T10:00:00.000Z")
    assert [(r.fit_type, r.shape, r.delta_mm, r.source) for r in rows] == [("sliding", "planar", 0.05, "calibration")]
    res = fits.resolve("sliding", "planar", "k1se", "PLA", 0.4, extra_corrections=rows)
    assert res.clearance_mm == 0.25 and res.fallback is False
    # another shape of the same printer is untouched
    assert fits.resolve("sliding", "round", "k1se", "PLA", 0.4, extra_corrections=rows).clearance_mm == 0.2
    assert fits.median_delta(rows) == 0.05 and fits.median_delta([]) is None


# --------------------------------------------------------------------------- S1: provenance of the registries and rows
def test_a_correction_row_moves_the_clearance_by_exactly_delta_and_only_for_its_printer_material_nozzle() -> None:
    base = fits.resolve("sliding", "round").clearance_mm
    row = Correction(delta_mm=-0.035, source="manual", printer="k1se", material="PLA", nozzle_mm=0.4,
                     measured_at="2026-10-24T10:00:00Z")
    hit = fits.resolve("sliding", "round", "k1se", "PLA", 0.4, extra_corrections=[row])
    assert hit.clearance_mm == round(base + row.delta_mm, 4)
    for other in (("u1", "PLA", 0.4), ("k1se", "ABS", 0.4), ("k1se", "PLA", 0.6)):
        assert fits.resolve("sliding", "round", *other, extra_corrections=[row]).clearance_mm != hit.clearance_mm
    assert fits.resolve("sliding", "round", "u1", "PLA", 0.4, extra_corrections=[row]).clearance_mm == base


def test_every_applied_row_says_where_it_came_from() -> None:
    default = next(p for p in fits.resolve("sliding", "planar", "k1se", "PLA").provenance if p["kind"] == "correction")
    assert default["status"] == "default, not calibrated"
    manual = Correction(delta_mm=0.01, source="manual", printer="k1se", measured_at="2026-10-24T10:00:00Z")
    cal = Correction(delta_mm=0.02, source="calibration", printer="k1se", measured_at="2026-10-24T10:00:00Z")
    statuses = {
        c.source: next(p for p in fits.resolve("sliding", "planar", "k1se", extra_corrections=[c]).provenance
                       if p["kind"] == "correction")["status"]
        for c in (manual, cal)
    }
    assert statuses == {"manual": "manual override", "calibration": "calibrated"}


def test_provenance_keeps_fit_type_first_and_alias_or_correction_second_whatever_is_named() -> None:
    res = fits.resolve("loose", "round", "k1se", "PETG", 0.4)
    kinds = [p["kind"] for p in res.provenance]
    assert kinds[:3] == ["fit_type", "alias", "correction"]
    assert {"printer", "material", "material_correction"} <= set(kinds)


def test_material_shrink_is_reported_as_a_scale_and_never_changes_the_clearance() -> None:
    pla = fits.resolve("sliding", "planar", material="PLA")
    plain = fits.resolve("sliding", "planar")
    assert pla.clearance_mm == plain.clearance_mm and pla.xy_scale == 1.003009
    entry = next(p for p in pla.provenance if p["kind"] == "material_correction")
    assert entry["id"] == "shrink_xy" and entry["shrink_xy_pct"] == 0.3 and "not changed" in entry["applied_as"]
    assert pla.to_dict()["xy_scale"] == 1.003009


def test_a_material_without_shrink_gets_an_explicit_no_correction_entry_not_a_silent_zero() -> None:
    for material, why in ((None, "no material given"), ("unobtainium", "'unobtainium' has no shrink_xy_pct recorded")):
        res = fits.resolve("sliding", "planar", material=material)
        entries = [p for p in res.provenance if p["kind"] == "material_correction"]
        assert res.xy_scale is None and [e["id"] for e in entries] == ["none"]
        assert entries[0]["message"] == f"no material correction ({why})"


def test_unknown_printer_is_flagged_in_the_provenance_and_the_row_stays_default_not_calibrated() -> None:
    res = fits.resolve("snap", "round", printer="mystery-printer", material="PETG", nozzle=0.4)
    printer = next(p for p in res.provenance if p["kind"] == "printer")
    assert printer["known"] is False and printer["input"] == "mystery-printer"
    corr = next(p for p in res.provenance if p["kind"] == "correction")
    assert res.fallback is True and corr["status"] == "default, not calibrated"
    assert corr["fallback_reason"] == ["printer"]    # the PETG row is a material row: only the printer fell back


def test_a_caller_named_in_the_error_gets_the_accepted_values() -> None:
    with pytest.raises(InvalidArgument, match="accepted: snap") as err:
        fits.resolve("squishy", "planar", command="fit-check")
    assert err.value.command == "fit-check"
