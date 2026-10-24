"""`assembly.sweep`: insertion / removal paths along ANY axis (#553), snap flex vs limit, assembly order (#61)."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
from assembly import scene as SC
from assembly import spec as S
from assembly import sweep as W
from assembly_fixtures import box, drawer, peg_pocket, snap_tab, write_stl

STEP = 0.1
TOL = 0.03  # the refined first-blocking distance is good to the bisection tolerance (0.005) + float32


def scene_of(doc: dict[str, Any], base: Path) -> tuple[SC.Scene, S.AssemblySpec]:
    spec = S.parse_spec(doc, base_dir=base)
    return SC.load_scene(spec), spec


@pytest.mark.parametrize("axis", ["+Y", "-X", "oblique"])
def test_a_free_drawer_slides_out_and_in_along_any_axis(
    tmp_path: Path, axis: str
) -> None:
    """+Y, -X and an oblique axis: the case #553 asks for. The same mechanism gives the same verdicts."""
    scene, spec = scene_of(drawer(tmp_path, axis=axis), tmp_path)
    for direction in ("remove", "insert"):
        rep = W.sweep_mate(scene, spec.mate("slide"), direction=direction, step=STEP)
        assert rep["ok"] and rep["path_clear"] and rep["first_blocking"] is None, (
            axis,
            direction,
            rep["first_blocking"],
        )
        assert rep["resolution_note"].startswith("sampled")
        assert rep["motion"]["axis_label"] in {"-Y", "+X", "[-0.7071, -0.7071, 0]"}


@pytest.mark.parametrize("axis", ["+Y", "-X", "oblique"])
def test_a_lip_stops_the_drawer_at_the_distance_it_is_away(
    tmp_path: Path, axis: str
) -> None:
    """First-blocking distance within a stated tolerance, for remove and for insert, on every axis."""
    scene, spec = scene_of(drawer(tmp_path, lip=True, axis=axis), tmp_path)
    remove = W.sweep_mate(scene, spec.mate("slide"), direction="remove", step=STEP)
    assert not remove["ok"]
    assert remove["first_blocking"]["travelled"] == pytest.approx(
        2.0, abs=TOL
    )  # the drawer's face is 2 mm from the lip
    assert set(remove["first_blocking"]["pair"]) == {"body", "drawer"}
    insert = W.sweep_mate(
        scene, spec.mate("slide"), direction="insert", step=STEP, distance=30
    )
    assert not insert["ok"]
    # starting 30 mm out, the leading face (y = s - 10) meets the lip outer face (y = 13.2) at s = 23.2
    assert insert["first_blocking"]["travelled"] == pytest.approx(6.8, abs=TOL)
    assert insert["first_blocking"]["distance_from_assembled"] == pytest.approx(
        23.2, abs=TOL
    )


def test_the_sweep_resolution_is_reported_and_a_finer_step_finds_the_same_distance(
    tmp_path: Path,
) -> None:
    scene, spec = scene_of(drawer(tmp_path, lip=True), tmp_path)
    coarse = W.sweep_mate(scene, spec.mate("slide"), direction="remove", step=1.0)
    fine = W.sweep_mate(scene, spec.mate("slide"), direction="remove", step=0.05)
    assert (
        coarse["step"] == 1.0
        and fine["step"] == 0.05
        and coarse["samples"] < fine["samples"]
    )
    assert coarse["first_blocking"]["travelled"] == pytest.approx(
        fine["first_blocking"]["travelled"], abs=TOL
    )


def test_a_part_seated_a_hair_into_the_floor_does_not_block_its_own_removal(
    tmp_path: Path,
) -> None:
    """#140 in a sweep: the touching floor is contact, not a collision; a real 0.05 mm sink is."""
    clean, spec = scene_of(peg_pocket(tmp_path, peg_z0=5.0 - 0.006), tmp_path)
    rep = W.sweep_mate(clean, spec.mate("peg_in_pocket"), direction="remove", step=STEP)
    assert rep["ok"], rep["first_blocking"]
    pushed, spec2 = scene_of(peg_pocket(tmp_path, peg_z0=5.0 - 0.05), tmp_path)
    bad = W.sweep_mate(
        pushed, spec2.mate("peg_in_pocket"), direction="remove", step=STEP
    )
    assert not bad["ok"] and bad["first_blocking"]["travelled"] == pytest.approx(
        0.0, abs=TOL
    )


def test_hinge_sweeps_report_the_angle_of_first_contact(tmp_path: Path) -> None:
    """#61: a revolute joint is swept in degrees; the lid stops where it meets the stop block."""
    write_stl(
        tmp_path / "base.stl",
        box((-10, -10, -5), (10, 10, 0)) + box((-10, -4, 4), (10, 6, 8)),
    )
    write_stl(tmp_path / "lid.stl", box((-10, -10, 0.2), (10, 6, 2.2)))
    doc = {
        "parts": {"base": {"mesh": "base.stl"}, "lid": {"mesh": "lid.stl"}},
        "mates": [
            {
                "name": "hinge",
                "a": "base",
                "b": "lid",
                "kind": "sliding",
                "hinge": {
                    "part": "lid",
                    "axis": "+X",
                    "origin": [0, -10, 0.2],
                    "range_deg": [0, 90],
                },
            }
        ],
    }
    scene, spec = scene_of(doc, tmp_path)
    rep = W.sweep_mate(scene, spec.mate("hinge"), direction="remove", step=1.0)
    assert rep["motion"]["unit"] == "deg" and not rep["ok"]
    # the lid's far top corner (16 mm from the hinge, 2 mm above it) reaches the block's underside (z = 4) when
    # 0.2 + 16 sin(t) + 2 cos(t) = 4: solved by bisection here, found by the sweep
    lo, hi = 0.0, math.radians(40)
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if 0.2 + 16 * math.sin(mid) + 2 * math.cos(mid) < 4 else (lo, mid)
    assert rep["first_blocking"]["travelled"] == pytest.approx(math.degrees(hi), abs=0.6)
    assert set(rep["first_blocking"]["pair"]) == {"lid", "base"}
    write_stl(tmp_path / "base.stl", box((-10, -10, -5), (10, 10, 0)))  # teeth: no stop block, the lid swings free
    free_scene, free_spec = scene_of(doc, tmp_path)
    assert W.sweep_mate(free_scene, free_spec.mate("hinge"), direction="remove", step=1.0)["ok"]


# --- snap hooks -------------------------------------------------------------------------------------------------
def test_a_snap_arm_flexes_within_its_limit_on_the_way_in_and_out(
    tmp_path: Path,
) -> None:
    scene, spec = scene_of(snap_tab(tmp_path), tmp_path)
    mate = spec.mate("cap_snap")
    for direction in ("insert", "remove"):
        rep = W.sweep_mate(scene, mate, direction=direction, step=0.05)
        hook = rep["snap"]["hooks"][0]
        assert rep["ok"] and rep["snap"]["verdict"]["status"] == "pass", rep["snap"][
            "verdict"
        ]
        assert hook["implied_deflection_mm"] == pytest.approx(
            0.2, abs=0.01
        )  # barb 0.5 - clearance 0.3
        assert hook["flex_limit_mm"] == pytest.approx(0.3) and hook["engaged"]
        assert rep["first_contact"]["classification"] == "flex"
        assert rep["first_blocking"] is None


def test_teeth_a_flex_limit_below_the_needed_deflection_blocks_the_path(
    tmp_path: Path,
) -> None:
    scene, spec = scene_of(snap_tab(tmp_path, flex_limit=0.15), tmp_path)
    rep = W.sweep_mate(scene, spec.mate("cap_snap"), direction="insert", step=0.05)
    assert not rep["ok"] and rep["snap"]["verdict"]["status"] == "fail"
    assert "flex limit" in rep["first_blocking"]["reason"]
    assert rep["first_blocking"]["classification"] == "blocking"


def test_the_force_hook_says_why_it_has_no_number_without_the_s4_formulas(
    tmp_path: Path,
) -> None:
    from assembly import forces

    scene, spec = scene_of(snap_tab(tmp_path), tmp_path)
    forces_doc = W.sweep_mate(
        scene, spec.mate("cap_snap"), direction="remove", step=0.1
    )["snap"]["hooks"][0]["forces"]
    if forces.s4_available():
        assert forces_doc["available"] or "needs" in forces_doc["reason"]
    else:
        assert forces_doc["available"] is False and "S4" in forces_doc["reason"]


# --- assembly order ---------------------------------------------------------------------------------------------
def test_order_finds_the_cover_before_the_peg_and_names_the_straight_moves(
    tmp_path: Path,
) -> None:
    doc = peg_pocket(tmp_path)
    write_stl(tmp_path / "cover.stl", box((-8, -8, 20.5), (8, 8, 22.5)))
    doc["parts"]["cover"] = {"mesh": "cover.stl"}
    doc["parts"]["peg"]["pose"] = {"translate": [0, 0, 0]}
    scene, _ = scene_of(doc, tmp_path)
    rep = W.plan_order(scene, base="socket", step=0.5)
    assert rep["feasible"] and rep["ok"]
    assert rep["order"][0]["action"] == "place" and rep["order"][0]["parts"] == ["socket"]
    moved = [s["parts"][0] for s in rep["order"] if s["action"] == "assemble"]
    assert moved == ["peg", "cover"] or moved == ["cover", "peg"]
    assert "sampled" in rep["resolution_note"]
    disassembly = [d["parts"][0] for d in rep["disassembly"]]
    assert disassembly[-1] in {"peg", "cover"} and set(disassembly) == {"peg", "cover"}


def test_teeth_a_peg_sealed_inside_a_closed_box_has_no_straight_disassembly(
    tmp_path: Path,
) -> None:
    write_stl(
        tmp_path / "shell.stl",
        box((-15, -15, 0), (15, 15, 20)) - box((-5.3, -5.3, 5), (5.3, 5.3, 19)),
    )
    write_stl(tmp_path / "peg.stl", box((-5, -5, 5), (5, 5, 15)))
    doc = {"parts": {"shell": {"mesh": "shell.stl"}, "peg": {"mesh": "peg.stl"}}}
    scene, _ = scene_of(doc, tmp_path)
    rep = W.plan_order(scene, base="shell", step=0.5)
    assert not rep["feasible"] and not rep["ok"]
    assert rep["stuck"]["remaining"] == ["peg", "shell"]
    blockers = {
        b["blocker"] for blocked in rep["stuck"]["blocked"] for b in blocked["blockers"]
    }
    assert blockers == {"shell"}


def test_order_refuses_a_scene_too_big_for_the_search(tmp_path: Path) -> None:
    doc = {"parts": {f"p{i}": {"mesh": "x.stl"} for i in range(3)}}
    write_stl(tmp_path / "x.stl", box((0, 0, 0), (1, 1, 1)))
    scene, _ = scene_of(doc, tmp_path)
    with pytest.raises(Exception, match="at most 2"):
        W.plan_order(scene, max_parts=2)
