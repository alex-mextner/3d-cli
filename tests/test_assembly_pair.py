"""`assembly.pair` / `assembly.helical` / `assembly.printed`: do two parts mate? Each test has a teeth case that must FAIL.

The fixtures (tests/pair_fixtures.py) are exact threads and ribs: the bore profile is the boss profile plus `gap` at the same
phase, so the radial gap at the best phase is `gap` and the free fraction of a turn is `gap / amplitude`. The numbers asserted
below are read off those dimensions, not off a previous run of the code under test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pair_fixtures as PF
import pytest
from manifold3d import Manifold
from assembly import helical
from assembly import pair as P
from assembly.scene import pose_matrix
from errors import InputNotFound, InvalidArgument

GAP = 0.5
AMPLITUDE = 0.8
PITCH = 3.0
FACET_MM = (
    0.02  # the triangle mesh of the fixture is a faceted surface: radii agree to ~ this
)


def thread_feature(**kw: Any) -> P.Feature:
    return P.Feature(
        "thread", P.FeatureSide("inner"), P.FeatureSide("outer"), pitch_mm=PITCH, **kw
    )


def pose(text: str) -> Any:
    return P.pose_from_text(text, "pose")


def run(a: Path, b: Path, features: list[P.Feature], **kw: Any) -> dict[str, Any]:
    pose_b = kw.pop("pose_b", ())
    gcode_a, gcode_b = kw.pop("gcode_a", None), kw.pop("gcode_b", None)
    part_a = P.PartInput(a, gcode=gcode_a, gcode_object="bore" if gcode_a else None)
    part_b = P.PartInput(
        b, pose=pose_b, gcode=gcode_b, gcode_object="boss" if gcode_b else None
    )
    return P.run_pair(
        P.PairOptions(part_a, part_b, features, axis_point=(0.0, 0.0, 0.0), **kw)
    )


@pytest.fixture(scope="module")
def threads(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    return PF.thread_pair(
        tmp_path_factory.mktemp("threads"), gap=GAP, amplitude=AMPLITUDE, pitch=PITCH
    )


# ---- the radial clearance of a thread ------------------------------------------------------------------------------


def test_thread_radial_gap_free_play_and_handedness_match_the_construction(
    threads: tuple[Path, Path],
) -> None:
    doc = run(*threads, [thread_feature()])
    feat = doc["features"][0]
    model = feat["model"]
    cl = model["clearance"]
    assert cl["at_pose_min_gap_mm"] == pytest.approx(GAP, abs=FACET_MM)
    assert cl["best_phase_min_gap_mm"] == pytest.approx(GAP, abs=FACET_MM)
    # slack of the construction: |d| < gap / (2 * amplitude) turns either side -> gap / amplitude of a turn
    assert cl["free_play"]["fraction_of_turn"] == pytest.approx(
        GAP / AMPLITUDE, abs=0.03
    )
    assert cl["free_play"]["value"] == pytest.approx(GAP / AMPLITUDE * PITCH, abs=0.1)
    assert cl["free_play"]["unit"] == "mm axial"
    for side in ("a", "b"):
        assert model["profiles"][side]["handedness"]["hand"] == "right"
        assert model["profiles"][side]["coverage"] == 1.0
    # crest / root: the bore is the boss + gap everywhere
    assert cl["male_crest_to_female_root_mm"] == pytest.approx(GAP, abs=FACET_MM)
    assert cl["female_crest_to_male_root_mm"] == pytest.approx(GAP, abs=FACET_MM)
    # the crest is a sharp ridge the mesh grid samples every pitch/12: its tip is cut by up to a few hundredths
    assert model["profiles"]["b"]["r_max_mm"] == pytest.approx(8.0 + AMPLITUDE, abs=0.06)
    assert doc["ok"] and doc["interpenetration"]["volume_mm3"] == 0.0


def test_a_bore_window_is_found_without_being_told(threads: tuple[Path, Path]) -> None:
    """No radius window on the tube: its own outer wall (r 14) must not be read as part of its bore (r ~ 8.5-9.3)."""
    doc = run(*threads, [thread_feature()])
    a = doc["features"][0]["model"]["profiles"]["a"]
    assert a["r_max_mm"] < 10.0, a


def test_thread_too_tight_for_the_required_gap_fails(tmp_path: Path) -> None:
    (tmp_path / "tight").mkdir()
    (tmp_path / "ok").mkdir()
    bore, boss = PF.thread_pair(tmp_path / "tight", gap=0.05, amplitude=AMPLITUDE, pitch=PITCH)
    doc = run(bore, boss, [thread_feature()], min_radial_gap_mm=0.2)
    assert not doc["ok"]
    feat = doc["features"][0]
    assert feat["status"] == "fail"
    assert any("best-phase radial gap" in r for r in feat["model"]["reasons"])
    # the same parts with a 0.5 mm gap pass the same requirement
    bore2, boss2 = PF.thread_pair(tmp_path / "ok", gap=GAP, pitch=PITCH)
    assert run(bore2, boss2, [thread_feature()], min_radial_gap_mm=0.2)["ok"]



def test_a_pose_in_the_wrong_phase_interferes_and_screw_to_best_clears_it(
    threads: tuple[Path, Path],
) -> None:
    half_turn = pose("{rotate: [0, 0, 180]}")
    blocked = run(*threads, [thread_feature()], pose_b=half_turn)
    assert (
        blocked["interpenetration"]["volume_mm3"] > 1.0
    )  # the flanks occupy the same space
    cl = blocked["features"][0]["model"]["clearance"]
    assert cl["at_pose_min_gap_mm"] < 0 < cl["best_phase_min_gap_mm"]
    assert any(
        "posed phase interferes" in r
        for r in blocked["features"][0]["model"]["reasons"]
    )
    assert abs(abs(cl["best_phase_move_b"]["rotate_deg"]) - 180.0) < 3.0
    # the advance equivalent to that turn: half a pitch
    assert abs(cl["best_phase_move_b"]["axial_mm"]) == pytest.approx(
        PITCH / 2, abs=0.05
    )

    screwed = run(*threads, [thread_feature()], pose_b=half_turn, screw_to_best=True)
    assert screwed["interpenetration"]["volume_mm3"] == 0.0
    assert screwed["features"][0]["model"]["clearance"][
        "at_pose_min_gap_mm"
    ] == pytest.approx(GAP, abs=FACET_MM)
    assert screwed["ok"]


def test_screw_to_best_turns_about_the_axis_and_keeps_the_axial_position(
    threads: tuple[Path, Path],
) -> None:
    doc = run(
        *threads,
        [thread_feature()],
        pose_b=pose("{rotate: [0, 0, 90]}"),
        screw_to_best=True,
    )
    bbox = doc["parts"]["b"]["bbox_assembled"]
    assert bbox["min"][2] == pytest.approx(0.0, abs=1e-3) and bbox["max"][
        2
    ] == pytest.approx(9.0, abs=1e-3)
    # a quarter turn off is neither its own mirror image nor a symmetry of the thread: only the RIGHT turn clears it
    assert doc["screw_to_best"]["applied_to_b"]["rotate_deg"] == pytest.approx(-90.0, abs=3.0)
    assert doc["interpenetration"]["volume_mm3"] == 0.0
    assert doc["features"][0]["model"]["clearance"]["at_pose_min_gap_mm"] == pytest.approx(GAP, abs=FACET_MM)
    assert doc["screw_to_best"]["applied_to_b"]["rotate_deg"] != 0.0


def test_opposite_handedness_cannot_screw_together(tmp_path: Path) -> None:
    bore, boss = PF.thread_pair(
        tmp_path, gap=GAP, pitch=PITCH, boss_hand=1, bore_hand=-1
    )
    doc = run(bore, boss, [thread_feature()])
    assert not doc["ok"]
    reasons = doc["features"][0]["model"]["reasons"]
    assert any("handedness mismatch" in r for r in reasons), reasons
    assert (
        "clearance" not in doc["features"][0]["model"]
    )  # no phase to slide: they never mate


def test_a_wrong_pitch_is_reported_not_trusted(threads: tuple[Path, Path]) -> None:
    wrong = P.Feature(
        "thread", P.FeatureSide("inner"), P.FeatureSide("outer"), pitch_mm=2.0
    )
    doc = run(*threads, [wrong])
    model = doc["features"][0]["model"]
    assert model["status"] != "pass", (
        "a pitch that does not describe the thread must not pass silently"
    )
    assert any("not decisive" in r or "measured" in r for r in model["reasons"]), model[
        "reasons"
    ]


def test_a_window_without_surface_is_refused(threads: tuple[Path, Path]) -> None:
    feat = P.Feature(
        "thread",
        P.FeatureSide("inner", z=(50.0, 60.0)),
        P.FeatureSide("outer"),
        pitch_mm=PITCH,
    )
    with pytest.raises(InvalidArgument, match="surface points"):
        run(*threads, [feat])


def test_two_bores_do_not_make_a_feature(threads: tuple[Path, Path]) -> None:
    feat = P.Feature(
        "thread", P.FeatureSide("inner"), P.FeatureSide("inner"), pitch_mm=PITCH
    )
    with pytest.raises(InvalidArgument, match="inner"):
        run(*threads, [feat])


# ---- ribs and keys ------------------------------------------------------------------------------------------------


def test_ribs_in_wider_grooves_have_a_small_free_turn(tmp_path: Path) -> None:
    bore, boss = PF.ribbed_pair(tmp_path, rib_width=2.0, groove_width=2.6)
    doc = run(
        bore, boss, [P.Feature("keys", P.FeatureSide("inner"), P.FeatureSide("outer"))]
    )
    cl = doc["features"][0]["model"]["clearance"]
    assert doc["features"][0]["kind"] == "ribs"
    assert cl["free_play"]["unit"] == "deg"
    assert (
        2.0 < cl["free_play"]["value"] < 8.0
    )  # 0.6 mm of slack on a radius of ~11 mm is ~3 deg each way
    assert cl["best_phase_min_gap_mm"] > 0.5
    assert doc["ok"]


def test_ribs_wider_than_their_grooves_never_clear(tmp_path: Path) -> None:
    bore, boss = PF.ribbed_pair(tmp_path, rib_width=2.0, groove_width=1.8)
    doc = run(
        bore, boss, [P.Feature("keys", P.FeatureSide("inner"), P.FeatureSide("outer"))]
    )
    assert not doc["ok"]
    assert doc["features"][0]["model"]["clearance"]["best_phase_min_gap_mm"] < 0
    assert doc["features"][0]["model"]["clearance"]["free_play"]["value"] == 0.0


def test_rib_clocking_is_recovered_by_screw_to_best(tmp_path: Path) -> None:
    bore, boss = PF.ribbed_pair(tmp_path)
    feat = P.Feature("keys", P.FeatureSide("inner"), P.FeatureSide("outer"))
    off = run(
        bore, boss, [feat], pose_b=pose("{rotate: [0, 0, 20]}")
    )  # a third of the rib pitch: the ribs hit the bore wall
    assert off["interpenetration"]["volume_mm3"] > 1.0
    on = run(
        bore, boss, [feat], pose_b=pose("{rotate: [0, 0, 20]}"), screw_to_best=True
    )
    assert on["interpenetration"]["volume_mm3"] == 0.0 and on["ok"]
    assert on["screw_to_best"]["applied_to_b"]["rotate_deg"] == pytest.approx(-20.0, abs=1.5)


# ---- interpenetration and minimum clearance -------------------------------------------------------------------------


def test_overlap_volume_and_minimum_gap_of_two_boxes(tmp_path: Path) -> None:
    a = PF.write_stl(tmp_path / "a.stl", Manifold.cube((10, 10, 10), False))
    b = PF.write_stl(tmp_path / "b.stl", Manifold.cube((10, 10, 10), False))
    gap = P.run_pair(
        P.PairOptions(
            P.PartInput(a), P.PartInput(b, pose=pose("{translate: [12.5, 0, 0]}"))
        )
    )
    assert gap["interpenetration"]["volume_mm3"] == 0.0
    assert gap["clearance"]["min_gap_mm"] == pytest.approx(2.5, abs=1e-3)
    lap = P.run_pair(
        P.PairOptions(
            P.PartInput(a), P.PartInput(b, pose=pose("{translate: [9, 0, 0]}"))
        )
    )
    assert lap["interpenetration"]["volume_mm3"] == pytest.approx(
        100.0, abs=0.01
    )  # 1 x 10 x 10
    assert lap["interpenetration"]["regions"][0]["depth_mm"] == pytest.approx(
        1.0, abs=0.01
    )
    assert not lap["ok"]
    assert lap["clearance"]["min_gap_mm"] == 0.0
    allowed = P.run_pair(
        P.PairOptions(
            P.PartInput(a),
            P.PartInput(b, pose=pose("{translate: [9, 0, 0]}")),
            max_overlap_mm3=150.0,
        )
    )
    assert allowed["ok"], (
        "a designed interference is allowed only when the caller says how much"
    )


def test_a_seat_face_is_a_contact_not_an_interpenetration(tmp_path: Path) -> None:
    a = PF.write_stl(tmp_path / "a.stl", Manifold.cube((10, 10, 10), False))
    seated = P.run_pair(
        P.PairOptions(
            P.PartInput(a), P.PartInput(a, pose=pose("{translate: [9.995, 0, 0]}"))
        )
    )
    assert seated["ok"] and seated["interpenetration"]["contacts"]["count"] == 1
    assert seated["interpenetration"]["contacts"]["max_depth_mm"] < 0.02
    sunk = P.run_pair(
        P.PairOptions(
            P.PartInput(a), P.PartInput(a, pose=pose("{translate: [9.9, 0, 0]}"))
        )
    )
    assert not sunk["ok"], "0.1 mm deep is a collision, not a seat"


def test_required_minimum_clearance_is_enforced(tmp_path: Path) -> None:
    a = PF.write_stl(tmp_path / "a.stl", Manifold.cube((10, 10, 10), False))
    far = P.PairOptions(
        P.PartInput(a),
        P.PartInput(a, pose=pose("{translate: [10.25, 0, 0]}")),
        min_clearance_mm=0.3,
    )
    assert P.run_pair(far)["clearance"]["status"] == "fail"
    ok = P.PairOptions(
        P.PartInput(a),
        P.PartInput(a, pose=pose("{translate: [10.4, 0, 0]}")),
        min_clearance_mm=0.3,
    )
    assert P.run_pair(ok)["ok"]


def test_a_zone_measures_the_surfaces_inside_it_only(tmp_path: Path) -> None:
    """Two boxes touch on a seat at z=0..10 but are 1 mm apart higher up: the zone above the seat sees the 1 mm."""
    base = Manifold.cube((10, 10, 10), False)
    stepped = Manifold.cube((10, 10, 10), False) - Manifold.cube(
        (1, 10, 5), False
    ).translate((0, 0, 5))
    a = PF.write_stl(tmp_path / "a.stl", base)
    b = PF.write_stl(tmp_path / "b.stl", stepped)
    pose_b = pose("{translate: [10, 0, 0]}")
    whole = P.run_pair(P.PairOptions(P.PartInput(a), P.PartInput(b, pose=pose_b)))
    assert whole["clearance"]["min_gap_mm"] == 0.0
    upper = P.run_pair(
        P.PairOptions(
            P.PartInput(a), P.PartInput(b, pose=pose_b), zone=(0, 0, 6, 12, 10, 10)
        )
    )
    assert upper["clearance"]["zone"]["min_gap_mm"] == pytest.approx(1.0, abs=0.05)


# ---- the fit table ----------------------------------------------------------------------------------------------------


def test_the_required_radial_gap_comes_from_the_fit_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("THREED_FITS_CORRECTIONS", str(tmp_path / "none.yaml"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    import fits_sources

    sliding = P.fit_requirement("sliding", "round", None, None, None, None)
    press = P.fit_requirement("press", "round", None, None, None, None)
    res = fits_sources.resolve("sliding", "round", command="assembly")
    assert sliding["required_gap_mm"] == round(res.clearance_mm - res.tol_mm, 4)
    assert sliding["required_gap_mm"] > press["required_gap_mm"], (
        "a sliding fit asks for more gap than a press fit"
    )


# ---- the as-printed side ----------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def printed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    tmp = tmp_path_factory.mktemp("printed")
    bore, boss = PF.thread_pair(tmp, gap=GAP, amplitude=AMPLITUDE, pitch=PITCH)
    bore_m, boss_m = PF.load(bore), PF.load(boss)
    faithful = PF.write_wall_gcode(
        tmp / "faithful.gcode",
        [("bore", bore_m, (110.0, 90.0)), ("boss", boss_m, (60.0, 40.0))],
    )
    oversize = PF.write_wall_gcode(
        tmp / "oversize.gcode",
        [("bore", bore_m, (110.0, 90.0)), ("boss", boss_m, (60.0, 40.0))],
        grow=0.15,
    )
    return {"bore": bore, "boss": boss, "faithful": faithful, "oversize": oversize}


def test_a_faithful_print_reproduces_the_model_and_is_registered_on_its_bed_position(
    printed: dict[str, Path],
) -> None:
    doc = run(
        printed["bore"],
        printed["boss"],
        [thread_feature()],
        gcode_a=printed["faithful"],
        gcode_b=printed["faithful"],
    )
    info = doc["printed"]
    assert info["a"]["placement"]["shift_applied_mm"] == pytest.approx(
        [110.0, 90.0], abs=0.02
    )
    assert info["b"]["placement"]["shift_applied_mm"] == pytest.approx(
        [60.0, 40.0], abs=0.02
    )
    assert (
        info["a"]["deviation"]["max_mm"] < 0.03
        and info["b"]["deviation"]["max_mm"] < 0.03
    )
    feat = doc["features"][0]
    printed_cl = feat["as_printed"]["clearance"]
    assert printed_cl["best_phase_min_gap_mm"] == pytest.approx(GAP, abs=0.03)
    # the printed radius sits on the model's: the half line width is added back the right way round
    for side in ("a", "b"):
        diff = feat["printed_vs_model"][side]["radius_printed_minus_model_mm"]
        assert abs(diff["mean"]) < 0.02, (side, diff)


def test_an_oversize_print_closes_the_gap_the_model_has(
    printed: dict[str, Path],
) -> None:
    """The slicer prints every wall 0.15 mm into the bore: the model's 0.5 mm gap is 0.35 mm on the plastic."""
    doc = run(
        printed["bore"],
        printed["boss"],
        [
            P.Feature(
                "thread",
                P.FeatureSide("inner"),
                P.FeatureSide("outer"),
                pitch_mm=PITCH,
                min_gap_mm=0.4,
            )
        ],
        gcode_a=printed["oversize"],
        gcode_b=printed["oversize"],
    )
    feat = doc["features"][0]
    assert feat["model"]["status"] == "pass", (
        "the model alone clears the 0.4 mm requirement"
    )
    printed_cl = feat["as_printed"]["clearance"]
    assert printed_cl["best_phase_min_gap_mm"] == pytest.approx(
        GAP - 2 * 0.15, abs=0.04
    )  # both walls grow 0.15
    assert feat["as_printed"]["status"] == "fail" and not doc["ok"]
    assert doc["printed"]["a"]["deviation"]["max_mm"] == pytest.approx(0.15, abs=0.03)


def test_one_printed_part_is_judged_against_the_model_of_the_other(
    printed: dict[str, Path],
) -> None:
    doc = run(
        printed["bore"],
        printed["boss"],
        [thread_feature()],
        gcode_a=printed["oversize"],
    )
    feat = doc["features"][0]
    assert feat["as_printed_parts"] == ["a"]
    assert feat["as_printed"]["clearance"]["best_phase_min_gap_mm"] == pytest.approx(
        GAP - 0.15, abs=0.04
    )


def test_an_object_name_that_the_gcode_does_not_have_names_the_ones_it_has(
    printed: dict[str, Path],
) -> None:
    part = P.PartInput(printed["bore"], gcode=printed["faithful"], gcode_object="nope")
    with pytest.raises(InvalidArgument, match="bore"):
        P.run_pair(
            P.PairOptions(
                part,
                P.PartInput(printed["boss"]),
                [thread_feature()],
                axis_point=(0.0, 0.0, 0.0),
            )
        )


def test_a_mirrored_model_does_not_register_and_the_deviation_says_so(
    printed: dict[str, Path], tmp_path: Path
) -> None:
    """A model flipped against its G-code (as when the wrong STL is paired) leaves a large residual, not a quiet pass."""
    mirrored = PF.load(printed["boss"])
    mirrored.apply_transform(
        np.diag([1.0, 1.0, -1.0, 1.0])
    )  # z flip: the thread phase runs the other way
    mirrored.invert()
    path = tmp_path / "boss_flipped.stl"
    mirrored.export(path)
    doc = run(
        printed["bore"],
        path,
        [
            P.Feature(
                "thread", P.FeatureSide("inner"), P.FeatureSide("outer"), pitch_mm=PITCH
            )
        ],
        gcode_b=printed["faithful"],
    )
    assert doc["printed"]["b"]["deviation"]["max_mm"] > 0.1


# ---- helpers --------------------------------------------------------------------------------------------------------------


def test_features_file_round_trip_and_errors(tmp_path: Path) -> None:
    good = tmp_path / "f.yaml"
    good.write_text(
        "features:\n"
        "  - name: thread\n    pitch: 3.0\n    hand: right\n    min_gap_mm: 0.2\n"
        "    a: {side: inner, z: [1, 9], r: [0, 12]}\n    b: {side: outer, z: [0, 8]}\n"
    )
    feats = P.features_from_file(good)
    assert (
        feats[0].pitch_mm == 3.0
        and feats[0].a.z == (1.0, 9.0)
        and feats[0].b.r is None
        and feats[0].min_gap_mm == 0.2
    )
    for body, needle in (
        ("features:\n  - {name: x, a: {side: inner}, b: {side: sideways}}\n", "side"),
        (
            "features:\n  - {name: x, bogus: 1, a: {side: inner}, b: {side: outer}}\n",
            "bogus",
        ),
        (
            "features:\n  - {name: x, hand: up, a: {side: inner}, b: {side: outer}}\n",
            "hand",
        ),
        ("features: []\n", "at least one"),
    ):
        bad = tmp_path / "bad.yaml"
        bad.write_text(body)
        with pytest.raises(InvalidArgument, match=needle):
            P.features_from_file(bad)
    with pytest.raises(InputNotFound):
        P.features_from_file(tmp_path / "missing.yaml")


def test_ranges_and_inline_poses_are_validated() -> None:
    assert P.parse_range("-36.78:-28.28", "--a-z") == (-36.78, -28.28)
    for bad in ("1", "2:1", "a:b"):
        with pytest.raises(InvalidArgument):
            P.parse_range(bad, "--a-z")
    m = pose_matrix(P.pose_from_text("{scale: 2, translate: [1, 2, 3]}", "--pose-b"))
    assert m[:3, 3].tolist() == [1.0, 2.0, 3.0] and m[0, 0] == 2.0
    with pytest.raises(InvalidArgument):
        P.pose_from_text("{translate: [1, 2", "--pose-b")


def test_the_helical_profile_recovers_a_known_thread_and_its_handedness() -> None:
    theta = np.random.default_rng(0).uniform(-np.pi, np.pi, 60000)
    z = np.random.default_rng(1).uniform(0, 12, 60000)
    r = PF.triangle_thread(10.0, 0.5, 3.0, hand=-1)(theta, z)
    pts = np.column_stack([r * np.cos(theta), r * np.sin(theta), z])
    prof, info = helical.pick_handedness(pts, 3.0, "auto")
    assert info["hand"] == "left" and info["decided"]
    assert prof.coherence_mm < 0.01 and prof.coverage == 1.0
    wrong, _ = helical.pick_handedness(pts, 3.0, "right")
    assert wrong.coherence_mm > 10 * prof.coherence_mm, (
        "the wrong handedness must look incoherent"
    )


def test_sliding_gap_peaks_at_the_aligned_phase_and_shift_to_motion_has_the_right_sign() -> (
    None
):
    n = helical.BINS
    u = np.arange(n) / n
    male = 8.0 + 0.8 * (1 - np.abs(2 * u - 1))
    female = male + 0.5
    by = helical.sliding(female, male)
    assert int(np.argmax(by)) == 0 and by[0] == pytest.approx(0.5)
    shifted = np.roll(male, 40)  # the male profile moved to higher phase by 40 bins
    cl = helical.clearance(
        helical.Profile(female, np.ones(n), 0.0, 3.0, 1),
        helical.Profile(shifted, np.ones(n), 0.0, 3.0, 1),
    )
    assert cl.best_shift_bins == n - 40  # rolling it back by 40 (= -40) realigns it
    motion = helical.shift_to_motion(cl.best_shift_bins, 3.0, +1)
    assert motion["axial_mm"] == pytest.approx(
        -40 / n * 3.0, abs=1e-3
    )  # move it back by 40/360 of a pitch
    assert motion["rotate_deg"] == pytest.approx(
        +40.0, abs=1e-3
    )  # right-handed: +turn = -advance


def test_a_part_printed_upside_down_is_mapped_back_through_its_print_pose(
    printed: dict[str, Path], tmp_path: Path
) -> None:
    """The boss was printed flipped and turned (rotate 180 about x, 40 about z; lowest point on the bed); `--print-b` says so
    and the printed surface still lands on the model's: the bed -> model -> assembly chain is inverted correctly, not
    merely identity. (A flip alone would not do: the triangle thread is symmetric under it.)"""
    flipped = PF.load(printed["boss"])
    flipped.apply_transform(pose_matrix(P.pose_from_text("{rotate: [180, 0, 40]}", "pose")))
    flipped.apply_translation([0.0, 0.0, -float(flipped.bounds[0][2])])
    gcode = PF.write_wall_gcode(tmp_path / "flipped.gcode", [("boss", flipped, (60.0, 40.0))])
    part_b = P.PartInput(
        printed["boss"],
        print_pose=P.pose_from_text("{rotate: [180, 0, 40]}", "--print-b"),
        gcode=gcode,
        gcode_object="boss",
    )
    doc = P.run_pair(
        P.PairOptions(
            P.PartInput(printed["bore"]), part_b, [thread_feature()], axis_point=(0.0, 0.0, 0.0)
        )
    )
    feat = doc["features"][0]
    assert doc["printed"]["b"]["deviation"]["max_mm"] < 0.03
    diff = feat["printed_vs_model"]["b"]["radius_printed_minus_model_mm"]
    assert diff is not None and abs(diff["mean"]) < 0.02 and abs(diff["max"]) < 0.08, diff
    assert feat["as_printed"]["clearance"]["best_phase_min_gap_mm"] == pytest.approx(GAP, abs=0.04)
    # a print pose that misses the 40 degree turn does not describe these walls: the deviation says so
    wrong = P.PartInput(
        printed["boss"],
        print_pose=P.pose_from_text("{rotate: [180, 0, 0]}", "--print-b"),
        gcode=gcode,
        gcode_object="boss",
    )
    bad = P.run_pair(
        P.PairOptions(P.PartInput(printed["bore"]), wrong, [thread_feature()], axis_point=(0.0, 0.0, 0.0))
    )
    assert bad["printed"]["b"]["deviation"]["max_mm"] > 0.1


def test_an_explicit_print_translate_is_used_as_given(printed: dict[str, Path]) -> None:
    part_b = P.PartInput(
        printed["boss"],
        print_pose=P.pose_from_text("{translate: [60, 40, 0]}", "--print-b"),
        gcode=printed["faithful"],
        gcode_object="boss",
    )
    doc = P.run_pair(
        P.PairOptions(P.PartInput(printed["bore"]), part_b, [thread_feature()], axis_point=(0.0, 0.0, 0.0))
    )
    placement = doc["printed"]["b"]["placement"]
    assert placement["registered"] is False and placement["residual_rms_mm"] is None
    assert doc["printed"]["b"]["deviation"]["max_mm"] < 0.03
    # a wrong explicit position is a large deviation, not a silent registration
    off = P.PartInput(
        printed["boss"],
        print_pose=P.pose_from_text("{translate: [65, 40, 0]}", "--print-b"),
        gcode=printed["faithful"],
        gcode_object="boss",
    )
    bad = P.run_pair(
        P.PairOptions(P.PartInput(printed["bore"]), off, [thread_feature()], axis_point=(0.0, 0.0, 0.0))
    )
    assert bad["printed"]["b"]["deviation"]["max_mm"] > 1.0


def test_a_3mf_part_is_read_without_lxml_one_object_by_id(threads: tuple[Path, Path], tmp_path: Path) -> None:
    bore, boss = (PF.load(p) for p in threads)
    plate = PF.write_3mf(tmp_path / "plate.3mf", [(1, bore), (2, boss)])
    path, oid = P.split_part_path(f"{plate}#2")
    assert path == plate and oid == 2
    assert P.split_part_path("a#b.stl") == (Path("a#b.stl"), None)
    doc = P.run_pair(
        P.PairOptions(
            P.PartInput(threads[0]), P.PartInput(path, object_id=oid), [thread_feature()], axis_point=(0.0, 0.0, 0.0)
        )
    )
    assert doc["features"][0]["model"]["clearance"]["best_phase_min_gap_mm"] == pytest.approx(GAP, abs=FACET_MM)
    # two objects and no id: refused, with the ids to pick from
    with pytest.raises(InvalidArgument, match="2 objects") as err:
        P.run_pair(P.PairOptions(P.PartInput(threads[0]), P.PartInput(plate)))
    rendered = err.value.render()
    assert "#1:" in rendered and "#2:" in rendered, rendered
    with pytest.raises(InvalidArgument, match="#9"):
        P.run_pair(P.PairOptions(P.PartInput(threads[0]), P.PartInput(plate, object_id=9)))
