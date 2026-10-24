"""`assembly.ergonomics`: finger / tool access, hook flex clearance, removal path, prying slots - each with teeth."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import assembly.ergonomics as E
import assembly.scene as SC
import assembly.spec as S
from assembly_fixtures import box, snap_tab, write_stl


def run(doc: dict[str, Any], base: Path, **kw: Any) -> dict[str, Any]:
    spec = S.parse_spec(doc, base_dir=base)
    return E.run_ergonomics(SC.load_scene(spec), **kw)


def check(rep: dict[str, Any], suffix: str) -> dict[str, Any]:
    (found,) = [c for c in rep["checks"] if c["id"].endswith(suffix)]
    return found


def test_a_hook_buried_under_the_cap_has_no_finger_or_tool_access(
    tmp_path: Path,
) -> None:
    rep = run(snap_tab(tmp_path, covered=True), tmp_path)
    access = check(rep, "h1.access")
    assert access["status"] == "fail" and access["check"] == "finger_access"
    assert (
        "no finger, nail, tool probe reaches" in access["reasons"][0]
        or "reaches" in access["reasons"][0]
    )
    assert "cap" in access["blockers"]
    assert not rep["ok"]


def test_an_exposed_hook_in_a_narrow_slot_is_reached_by_the_nail_but_not_the_finger_pad(
    tmp_path: Path,
) -> None:
    """The cap leaves a 7 mm slot around the hook: the 12 mm pad does not fit, the 6 mm nail does: WARN, with the sizes."""
    rep = run(snap_tab(tmp_path, covered=False), tmp_path)
    access = check(rep, "h1.access")
    assert access["status"] == "warn", access
    assert (
        "fingernail" in access["reasons"][0]
        and "finger pad is blocked" in access["reasons"][0]
    )
    assert access["best"]["probe"] == "nail" and access["best"][
        "radius_mm"
    ] == pytest.approx(3.0)


def test_probe_sizes_are_configurable_and_the_wider_pad_passes_when_it_fits(
    tmp_path: Path,
) -> None:
    doc = snap_tab(tmp_path, covered=False)
    doc["settings"] = {"finger_pad_mm": 6.5}  # a 6.5 mm fingertip fits the 7 mm slot
    rep = run(doc, tmp_path)
    assert check(rep, "h1.access")["status"] == "pass"
    doc["settings"] = {"finger_pad_mm": 12.0, "finger_nail_mm": 14.0, "tool_mm": 14.0}
    narrow = run(doc, tmp_path)
    assert check(narrow, "h1.access")["status"] in {"fail", "warn"}


def test_declared_access_direction_is_the_only_one_tried(tmp_path: Path) -> None:
    above = [{"name": "from_above", "target": [6.2, 0, 15.7], "from": "+Z", "probe": "finger", "purpose": "release"}]
    blocked = check(run(snap_tab(tmp_path, covered=False, access=above), tmp_path), "h1.access")
    assert blocked["status"] == "fail" and blocked["tried"] == 3, "the arm itself is in the way from +Z: finger, nail, tool tried"
    side = [{"name": "from_side", "target": [6.2, 0, 15.7], "from": "+X", "probe": "nail", "purpose": "release"}]
    ok = check(run(snap_tab(tmp_path, covered=False, access=side), tmp_path), "h1.access")
    assert ok["status"] == "pass" and ok["best"]["access"] == "from_side" and ok["best"]["direction_label"] == "+X"


def test_a_hook_that_cannot_flex_clear_of_its_neighbour_fails_and_a_free_one_passes(
    tmp_path: Path,
) -> None:
    free = check(run(snap_tab(tmp_path), tmp_path), "h1.deflection")
    assert (
        free["status"] == "pass"
        and free["free_deflection_mm"] >= free["needed_deflection_mm"] > 0.15
    )
    stuck = check(
        run(snap_tab(tmp_path, wall_behind_arm=0.1), tmp_path), "h1.deflection"
    )
    assert stuck["status"] == "fail", stuck
    assert stuck["free_deflection_mm"] == pytest.approx(0.1, abs=0.03)
    assert stuck["blocker"]["part"] == "base" or stuck["blocker"]["part"] == "cap"


def test_removal_path_is_part_of_the_ergonomic_report_and_fails_when_the_limit_is_too_low(
    tmp_path: Path,
) -> None:
    ok = check(run(snap_tab(tmp_path), tmp_path), "cap_snap.removal_path")
    assert ok["status"] == "pass"
    tight = check(
        run(snap_tab(tmp_path, flex_limit=0.1), tmp_path), "cap_snap.removal_path"
    )
    assert tight["status"] == "fail" and "flex limit" in tight["reasons"][0]


def test_force_row_says_whether_the_s4_formulas_were_available(tmp_path: Path) -> None:
    import assembly.forces as forces

    row = check(run(snap_tab(tmp_path), tmp_path), "h1.forces")
    if forces.s4_available():
        assert row["status"] in {"pass", "warn"}
    else:
        assert (
            row["status"] == "warn"
            and "S4" in row["reasons"][0]
            and row["forces"]["available"] is False
        )


def test_a_pull_released_hook_needs_a_grip_and_a_prying_point_needs_a_seam_slot(
    tmp_path: Path,
) -> None:
    doc = snap_tab(tmp_path, covered=True, release="pull")
    # no grip declared: say so
    assert check(run(doc, tmp_path), "cap_snap.grip")["status"] == "warn"
    write_stl(tmp_path / "base.stl", box((-15, -15, 0), (15, 15, 10)))
    doc["mates"][0]["access"] = [
        {
            "name": "grip_top",
            "target": [0, 0, 22],
            "from": "+Z",
            "probe": "finger",
            "purpose": "pull",
        },
    ]
    grip = check(run(doc, tmp_path), "cap_snap.grip")
    assert grip["status"] in {"pass", "warn"} and grip["points"][0]["purpose"] == "pull"
    doc["mates"][0]["access"].append(
        {
            "name": "pry",
            "target": [15.0, 0, 10.0],
            "from": "+X",
            "probe": "nail",
            "purpose": "pry",
        }
    )
    pry = check(run(doc, tmp_path), "cap_snap.grip")
    assert pry["status"] == "warn"
    assert any("prying blade" in r for r in pry["reasons"]), (
        "the butt seam has no slot for a blade"
    )


def test_bolt_without_tool_access_declared_warns_and_a_blocked_driver_fails(
    tmp_path: Path,
) -> None:
    write_stl(tmp_path / "plate.stl", box((-20, -20, 0), (20, 20, 4)))
    write_stl(
        tmp_path / "boss.stl",
        box((-6, -6, 4), (6, 6, 20)) - box((-3, -3, 6), (3, 3, 21)),
    )
    doc: dict[str, Any] = {
        "parts": {"plate": {"mesh": "plate.stl"}, "boss": {"mesh": "boss.stl"}},
        "mates": [{"name": "screw", "a": "plate", "b": "boss", "kind": "bolt"}],
    }
    assert run(doc, tmp_path)["checks"][0]["status"] == "warn"
    doc["mates"][0]["access"] = [
        {
            "name": "driver",
            "target": [0, 0, 6],
            "from": "+Z",
            "probe": "tool",
            "purpose": "drive",
        }
    ]
    deep = run(doc, tmp_path)["checks"][0]
    assert deep["status"] == "pass", deep  # a 3 mm driver fits the 6 mm well
    doc["mates"][0]["access"][0]["radius_mm"] = 3.4
    assert run(doc, tmp_path)["checks"][0]["status"] == "fail", (
        "a 6.8 mm driver does not fit the 6 mm well"
    )
