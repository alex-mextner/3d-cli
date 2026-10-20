"""Teeth tests for the project-level layer lint (slicing.mix_project) on synthetic project 3MFs."""

from __future__ import annotations

from pathlib import Path

import pytest
from mix_fixtures import Obj, Part, box, orca_project, tile

from slicing import layer_heights as lh
from slicing.head_sequence import ProjectError
from slicing.mix_pattern import MixRules
from slicing.mix_project import FAIL, PASS, SKIP, check, plan_profiles

# the layer_heights_profile.txt key is the object's 1-based position; the standing tile is 20 mm tall
PTA = 1
GWG_ROWS = "1,4,1,1,50,0,g,w,m2,z0,xa0,xb0,d0,o0,u701,cm1,141"


def project(tmp_path: Path, name: str = "p.3mf", **kw: object) -> Path:
    objects = kw.pop("objects", None) or [tile("PTA", 5, standing=True)]
    return orca_project(tmp_path / name, objects, **kw)  # type: ignore[arg-type]


def by_scope(report, scope: str):
    return [f for f in report.findings if f.scope == scope]


def test_thin_process_layers_pass_the_cap(tmp_path: Path) -> None:
    report = check(str(project(tmp_path, settings={"layer_height": "0.12"})))
    (layers,) = by_scope(report, "layers")
    assert layers.status == PASS and "thickest 0.120 mm" in layers.detail
    assert report.status == PASS


def test_a_thick_process_layer_over_a_mixed_part_fails(tmp_path: Path) -> None:
    report = check(str(project(tmp_path, settings={"layer_height": "0.2"})))
    (layers,) = by_scope(report, "layers")
    assert layers.status == FAIL
    assert (
        "thicker than the 0.12 mm cap" in layers.detail and "0.200 mm" in layers.detail
    )
    assert "3d mix profile" in layers.detail
    assert report.failures == 1
    # a wider configured cap accepts the same project
    ok = check(
        str(project(tmp_path, "q.3mf", settings={"layer_height": "0.2"})),
        MixRules(layer_cap_mm=0.2),
    )
    assert ok.status == PASS


def test_a_capped_variable_profile_passes_and_is_measured_in_mm(tmp_path: Path) -> None:
    profile = lh.cap_profile(20.0, [(0.0, 20.0)], cap=0.12)
    text = lh.profile_line(PTA, profile)
    report = check(
        str(
            project(
                tmp_path,
                settings={"layer_height": "0.32"},
                files={lh.PROFILE_ENTRY: text},
            )
        )
    )
    (layers,) = by_scope(report, "layers")
    assert layers.status == PASS and "its layer height profile" in layers.detail


def test_a_profile_capped_over_only_part_of_the_part_fails(tmp_path: Path) -> None:
    profile = lh.cap_profile(
        20.0, [(0.0, 8.0)], cap=0.12
    )  # the mixed part reaches 20 mm
    text = lh.profile_line(PTA, profile)
    report = check(
        str(
            project(
                tmp_path,
                settings={"layer_height": "0.32"},
                files={lh.PROFILE_ENTRY: text},
            )
        )
    )
    (layers,) = by_scope(report, "layers")
    assert layers.status == FAIL and "0.3" in layers.detail


def test_a_profile_orca_would_drop_is_a_fail(tmp_path: Path) -> None:
    profile = lh.cap_profile(20.0, [(0.0, 20.0)], cap=0.12)
    profile[-2] = (
        19.0  # the last z is not the object height: Orca drops the whole profile silently
    )
    report = check(
        str(
            project(
                tmp_path,
                settings={"layer_height": "0.12"},
                files={lh.PROFILE_ENTRY: lh.profile_line(PTA, profile)},
            )
        )
    )
    dropped = [f for f in by_scope(report, "layers") if "dropped by Orca" in f.detail]
    assert dropped and dropped[0].status == FAIL


def test_a_recipe_that_doubles_a_colour_fails_unless_the_run_limit_allows_it(
    tmp_path: Path,
) -> None:
    p = project(tmp_path, settings={"layer_height": "0.12"}, mixed=GWG_ROWS)
    (recipe,) = by_scope(check(str(p)), "recipe")
    assert (
        recipe.status == FAIL
        and "run-too-thick" in recipe.detail
        and "0.240 mm band" in recipe.detail
    )
    (ok,) = by_scope(check(str(p), MixRules(max_run_mm=0.24)), "recipe")
    assert ok.status == PASS


def test_the_recipe_is_judged_at_the_layers_it_prints_at(tmp_path: Path) -> None:
    """GWG at 0.08 mm layers: the doubled G is a 0.16 mm band; at 0.05 mm (0.10 mm) it fits the 0.12 mm limit."""
    profile = lh.cap_profile(20.0, [(0.0, 20.0)], cap=0.08, hmin=0.05)
    p = project(
        tmp_path,
        mixed=GWG_ROWS,
        settings={"layer_height": "0.32"},
        files={lh.PROFILE_ENTRY: lh.profile_line(PTA, profile)},
    )
    (recipe,) = by_scope(check(str(p)), "recipe")
    assert recipe.status == FAIL


def test_a_prime_tower_over_objects_with_different_layer_heights_is_a_fail(
    tmp_path: Path,
) -> None:
    profile = lh.cap_profile(20.0, [(0.0, 20.0)], cap=0.12)
    p = project(
        tmp_path,
        objects=[tile("PTA", 5, standing=True), tile("REF", 1, standing=True)],
        settings={"layer_height": "0.12", "enable_prime_tower": "1"},
        files={lh.PROFILE_ENTRY: lh.profile_line(PTA, profile)},
    )
    plate = by_scope(check(str(p)), "plate")
    assert [f.status for f in plate] == [FAIL] and "ONE multi-part object" in plate[
        0
    ].detail
    off = project(
        tmp_path,
        "off.3mf",
        objects=[tile("PTA", 5, standing=True), tile("REF", 1, standing=True)],
        settings={"layer_height": "0.12", "enable_prime_tower": "0"},
        files={lh.PROFILE_ENTRY: lh.profile_line(PTA, profile)},
    )
    assert by_scope(check(str(off)), "plate") == []


def test_a_raft_shifts_the_stack_so_the_layers_are_not_guessed(tmp_path: Path) -> None:
    report = check(
        str(project(tmp_path, settings={"layer_height": "0.2", "raft_layers": "2"}))
    )
    assert [f.status for f in by_scope(report, "layers")] == [SKIP]


def test_no_part_in_a_mixed_filament_has_no_layer_finding(tmp_path: Path) -> None:
    p = orca_project(tmp_path / "p.3mf", [tile("REF", 1)])
    report = check(str(p))
    assert by_scope(report, "layers") == [] and by_scope(report, "plate") == []
    bare = orca_project(tmp_path / "bare.3mf", [tile("REF", 1)], mixed="")
    assert check(str(bare)).status == SKIP


def test_plan_profiles_caps_the_zone_and_keeps_the_rest_at_the_process_height(
    tmp_path: Path,
) -> None:
    two = [tile("PTA", 5, standing=True)]
    p = project(tmp_path, objects=two, settings={"layer_height": "0.2"})
    (plan,) = plan_profiles(str(p), cap=0.12, hmin=0.08, hmax=0.32)
    assert plan.object_id == PTA and plan.height == pytest.approx(20.0)
    assert plan.profile[-2] == pytest.approx(20.0)
    assert lh.check_profile(plan.profile, plan.height) is None
    tops = lh.simulate_layers(plan.profile, plan.height)
    th = lh.thicknesses(tops)
    zone = [th[i] for i in lh.layers_in_zone(tops, 0.0, 20.0) if i > 0]
    assert max(zone) <= 0.12 + 1e-6


def test_plan_profiles_refuses_what_orca_cannot_slice(tmp_path: Path) -> None:
    towers = project(
        tmp_path,
        objects=[tile("PTA", 5, standing=True), tile("REF", 1, standing=True)],
        settings={"enable_prime_tower": "1"},
    )
    with pytest.raises(ProjectError, match="ONE multi-part object"):
        plan_profiles(str(towers), cap=0.12, hmin=0.08, hmax=0.32)
    raft = project(tmp_path, "raft.3mf", settings={"raft_layers": "2"})
    with pytest.raises(ProjectError, match="raft"):
        plan_profiles(str(raft), cap=0.12, hmin=0.08, hmax=0.32)
    none = orca_project(tmp_path / "none.3mf", [tile("REF", 1)])
    with pytest.raises(ProjectError, match="nothing to cap"):
        plan_profiles(str(none), cap=0.12, hmin=0.08, hmax=0.32)


def test_an_objects_own_layer_height_overrides_the_projects(tmp_path: Path) -> None:
    """Orca slices an object with a layer_height of its own at that height: 0.28 mm here, whatever the
    project says (0.12 mm), and the other way round."""
    thick = tile("PTA", 5, standing=True)
    thick.settings = {"layer_height": "0.28"}
    report = check(str(project(tmp_path, objects=[thick], settings={"layer_height": "0.12"})))
    (layers,) = by_scope(report, "layers")
    assert layers.status == FAIL and "0.280 mm" in layers.detail
    thin = tile("PTA", 5, standing=True)
    thin.settings = {"layer_height": "0.1"}
    ok = check(str(project(tmp_path, "thin.3mf", objects=[thin], settings={"layer_height": "0.32"})))
    assert [f.status for f in by_scope(ok, "layers")] == [PASS]


def test_blockers_and_modifiers_do_not_make_the_object_taller(tmp_path: Path) -> None:
    """The profile's last z must be the height of the MODEL parts: a tall support blocker or modifier
    in the object does not count (Orca would drop a profile that ends at the wrong z)."""
    obj = Obj(
        "PTA",
        [
            Part("body", box(20, 3, 20), 5, offset=(0.0, 0.0, 10.0)),
            Part("blocker", box(5, 5, 60), 1, offset=(0.0, 0.0, 30.0), subtype="support_blocker"),
            Part("mod", box(5, 5, 50), 1, offset=(0.0, 0.0, 25.0), subtype="modifier_part"),
        ],
        extruder=5,
    )
    (plan,) = plan_profiles(str(project(tmp_path, objects=[obj])), cap=0.12, hmin=0.08, hmax=0.32)
    assert plan.height == pytest.approx(20.0)
    assert plan.profile[-2] == pytest.approx(20.0)
    assert plan.zones == [(0.0, 20.0)]
