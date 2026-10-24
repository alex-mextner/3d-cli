"""`assembly.check`: blocking vs allowed intersections and per-mate clearance, each with a teeth test that must FAIL.

Fixtures are synthetic (tests/assembly_fixtures.py); the numbers below are read off their dimensions.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from assembly import check as C
from assembly import scene as SC
from assembly import spec as S
from assembly_fixtures import board_in_case, peg_pocket
from fits import Correction
from fits_sources import manual


@pytest.fixture(autouse=True)
def _isolated_corrections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "THREED_FITS_CORRECTIONS", str(tmp_path / "fits-corrections.yaml")
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def run(doc: dict[str, Any], base: Path, **kw: Any) -> dict[str, Any]:
    spec = S.parse_spec(doc, base_dir=base)
    return C.run_check(SC.load_scene(spec), **kw)


def test_clean_peg_in_pocket_passes_with_the_gap_of_the_fit(tmp_path: Path) -> None:
    rep = run(peg_pocket(tmp_path), tmp_path)
    assert rep["ok"] and rep["summary"]["blocking"] == 0
    mate = rep["mates"][0]
    assert mate["verdict"]["status"] == "pass"
    assert mate["clearance"]["measured_min_gap_mm"] == pytest.approx(
        0.3, abs=0.01
    )  # (10.6 - 10) / 2 per side
    assert (
        mate["clearance"]["method"] == "lateral samples"
    )  # the seat face on the floor is not a zero gap
    assert mate["fit"]["provenance"], "the verdict carries the fits.resolve provenance"


def test_a_blocking_boss_is_reported_with_pair_depth_location_and_a_fix(
    tmp_path: Path,
) -> None:
    rep = run(peg_pocket(tmp_path, boss=True), tmp_path)
    assert not rep["ok"]
    (hit,) = rep["blocking"]
    assert {hit["a"], hit["b"]} == {"socket", "peg"}
    assert hit["depth_mm"] == pytest.approx(
        1.7, abs=0.02
    )  # boss spans x 3.3..5.3, the peg reaches x = 5
    assert hit["center"] == pytest.approx((4.15, 0.0, 10.0), abs=0.1)
    assert hit["volume_mm3"] == pytest.approx(1.7 * 4 * 4, rel=0.02)
    assert hit["classification"] == "blocking"
    assert "has no interference" in hit["reason"]
    assert hit["suggested_clearance_change_mm"] >= hit["depth_mm"]
    assert any(abs(f["normal"][0]) > 0.9 for f in hit["faces"].values()), (
        "the offending faces face the X axis"
    )
    assert rep["mates"][0]["verdict"]["status"] == "fail"


def test_teeth_declaring_the_boss_as_designed_interference_allows_it_only_within_its_caps(
    tmp_path: Path,
) -> None:
    doc = peg_pocket(tmp_path, boss=True)
    mate = doc["mates"][0]
    mate["zone"] = {"min": [3, -3, 7], "max": [5.5, 3, 13]}
    mate["allow"] = {"max_depth_mm": 2.0, "max_volume_mm3": 30}
    assert run(doc, tmp_path)["ok"], "a declared, capped interference is allowed"
    mate["allow"]["max_depth_mm"] = 1.0
    assert not run(doc, tmp_path)["ok"], (
        "deeper than the declared cap is a defect again"
    )
    mate["allow"] = {"max_depth_mm": 2.0, "max_volume_mm3": 10}
    assert not run(doc, tmp_path)["ok"], (
        "bigger than the declared volume cap is a defect"
    )
    mate["allow"] = {"max_depth_mm": 2.0, "max_volume_mm3": 30}
    mate["zone"] = {"min": [-5, -3, 0], "max": [0, 3, 4]}
    assert not run(doc, tmp_path)["ok"], (
        "outside the declared zone it is still blocking"
    )


@pytest.mark.parametrize(("sink", "ok"), [(0.006, True), (0.05, False)])
def test_a_part_seated_a_hair_into_the_floor_is_contact_not_a_collision(
    tmp_path: Path, sink: float, ok: bool
) -> None:
    """#140: 6 um into the floor over the whole footprint passes; 0.05 mm still fails."""
    rep = run(peg_pocket(tmp_path, peg_z0=5.0 - sink), tmp_path)
    assert rep["ok"] is ok
    if ok:
        assert rep["summary"]["contacts"] == 1 and rep["summary"]["blocking"] == 0
    else:
        assert rep["blocking"][0]["depth_mm"] == pytest.approx(sink, abs=0.005)


def test_one_millimetre_into_the_floor_blocks_with_the_location(tmp_path: Path) -> None:
    rep = run(peg_pocket(tmp_path, peg_z0=4.0), tmp_path)
    (hit,) = rep["blocking"]
    assert hit["depth_mm"] == pytest.approx(1.0, abs=0.01)
    assert hit["center"][2] == pytest.approx(4.5, abs=0.05)


def test_crush_ribs_are_allowed_designed_interference_and_the_broken_board_is_blocking(
    tmp_path: Path,
) -> None:
    """The mole's pattern: only the declared ribs overlap; the same board moved 1 mm into the wall is a defect."""
    rep = run(board_in_case(tmp_path), tmp_path)
    assert (
        rep["ok"] and rep["summary"]["blocking"] == 0 and rep["summary"]["allowed"] == 2
    )
    for region in rep["allowed"]:
        assert region["depth_mm"] == pytest.approx(0.15, abs=0.005)
        assert (
            region["mate"] == "board_ribs" and "material_note" not in region
        )  # no material in the context
    assert rep["mates"][0]["verdict"]["status"] == "pass"

    broken = run(board_in_case(tmp_path, board_dx=1.0), tmp_path)
    assert not broken["ok"]
    assert broken["blocking"], "board +1 mm is reported BLOCKING"
    assert max(r["depth_mm"] for r in broken["blocking"]) > 0.5


def test_without_ribs_declared_the_same_overlap_is_blocking(tmp_path: Path) -> None:
    """teeth: the allowance comes only from the declaration."""
    doc = board_in_case(tmp_path)
    doc["mates"] = []
    rep = run(doc, tmp_path)
    assert not rep["ok"] and rep["summary"]["blocking"] == 2


def test_the_pla_flexibility_note_rides_with_a_designed_interference(
    tmp_path: Path,
) -> None:
    doc = board_in_case(tmp_path)
    doc["context"] = {"material": "Elegoo Rapid PLA+"}
    note = run(doc, tmp_path)["allowed"][0]["material_note"]
    assert "PLA" in note and "strain" in note


def test_press_fit_with_no_overlap_is_a_loose_warning_not_a_pass(
    tmp_path: Path,
) -> None:
    rep = run(board_in_case(tmp_path, ribs=False), tmp_path)
    assert (
        rep["mates"][0]["verdict"]["status"] == "warn"
        and "loose" in rep["mates"][0]["verdict"]["reasons"][0]
    )


def test_changing_printer_and_material_changes_the_clearance_verdict(
    tmp_path: Path,
) -> None:
    """A manual correction row for one printer x material makes the same geometry fail the fit there only."""
    doc = peg_pocket(tmp_path)
    doc["mates"][0].update(fit="snap")  # nominal 0.30 +/- 0.10 per face: the 0.30 gap passes
    base = run(doc, tmp_path)
    assert base["mates"][0]["verdict"]["status"] == "pass"
    manual.add(
        Correction(
            printer="k1se",
            material="PETG",
            delta_mm=0.25,
            source="manual",
            measured_at="2026-10-24T10:00:00Z",
        )
    )
    petg = run(
        doc,
        tmp_path,
        ctx=C.with_context(S.parse_spec(doc, base_dir=tmp_path), "k1se", "PETG"),
    )
    fit = petg["mates"][0]["fit"]
    assert (
        fit["clearance_mm"] == pytest.approx(0.55)
        and petg["mates"][0]["verdict"]["status"] == "fail"
    )
    assert any(p.get("kind") == "correction" for p in fit["provenance"])
    pla = run(
        doc,
        tmp_path,
        ctx=C.with_context(S.parse_spec(doc, base_dir=tmp_path), "k1se", "PLA"),
    )
    assert pla["mates"][0]["verdict"]["status"] == "pass", (
        "another material is untouched"
    )


def test_explicit_numbers_override_the_table_and_say_so(tmp_path: Path) -> None:
    doc = peg_pocket(tmp_path)
    doc["mates"][0].update(clearance_mm=0.5, tol_mm=0.05)
    mate = run(doc, tmp_path)["mates"][0]
    assert (
        mate["fit"]["source"] == "explicit"
        and mate["fit"]["provenance"][0]["kind"] == "explicit"
    )
    assert mate["verdict"]["status"] == "fail"  # 0.3 gap < 0.5 - 0.05


def test_mate_filter_limits_the_check_and_an_unknown_mate_names_the_choices(
    tmp_path: Path,
) -> None:
    doc = peg_pocket(tmp_path, boss=True)
    spec = S.parse_spec(copy.deepcopy(doc), base_dir=tmp_path)
    scene = SC.load_scene(spec)
    assert (
        C.run_check(scene, mate_filter=["peg_in_pocket"])["summary"]["pairs_checked"]
        == 1
    )
    with pytest.raises(Exception, match="peg_in_pocket"):
        C.run_check(scene, mate_filter=["nope"])


def test_the_report_names_every_part_in_a_distinct_palette(tmp_path: Path) -> None:
    rep = run(peg_pocket(tmp_path), tmp_path)
    colours = [row["color"] for row in rep["legend"]]
    assert len(colours) == len(set(colours)) == 2
    assert run(peg_pocket(tmp_path), tmp_path)["legend"] == rep["legend"], (
        "stable across runs"
    )
