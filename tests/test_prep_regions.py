"""Behaviour of prep.regions: landmark mirroring, priority, face labels, disjoint parts, mix rules, the step result.

All geometry is a 40 mm cube standing on the bed (z 0..40, volume 64000 mm^3), so every expected number is
analytic: a box covering z >= 30 holds 10 x 40 x 40 = 16000 mm^3 and, on the surface, the 1600 mm^2 top plus four
10 x 40 mm bands = 3200 mm^2.
"""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import trimesh

from errors import InputNotFound, InvalidArgument, MissingDependency
from prep.contracts import Assign, Filament, StepResult
from prep.regions import (
    ColourPlan,
    ColourSpec,
    RegionSpec,
    BoxShape,
    check_mixes,
    label_faces,
    load_colour_spec,
    mix_digits,
    parse_colour_spec,
    plan_colours,
    resolve_assign,
    split_parts,
)

FILAMENTS: list[dict[str, Any]] = [
    {"slot": 1, "key": "K", "name": "Black", "hex": "#111111"},
    {"slot": 2, "key": "W", "name": "White", "hex": "#f0f0f0"},
    {"slot": 3, "key": "R", "name": "Red", "hex": "#c12e1f"},
    {"slot": 4, "key": "G", "name": "Green", "hex": "#1b5e20"},
]
TOP30 = {"type": "box", "min": [-30, -30, 30], "max": [30, 30, 50]}
TOP20 = {"type": "box", "min": [-30, -30, 20], "max": [30, 30, 50]}


def make_spec(
    regions: list[dict[str, Any]],
    landmarks: dict[str, Any] | None = None,
    default: dict[str, Any] | None = None,
) -> ColourSpec:
    return parse_colour_spec(
        {
            "filaments": FILAMENTS,
            "default": default or {"key": "K"},
            "landmarks": landmarks or {},
            "regions": regions,
        }
    )


@pytest.fixture(scope="module")
def cube() -> trimesh.Trimesh:
    m = trimesh.creation.box(extents=(40, 40, 40))
    m.apply_translation((0, 0, 20))
    return m


@pytest.fixture(scope="module")
def fine_cube(cube: trimesh.Trimesh) -> trimesh.Trimesh:
    """Same cube with 2.5 mm triangles, so region boundaries at z = 30 / 20 fall between facets."""
    fine = cube.subdivide_to_size(2.5)
    assert isinstance(fine, trimesh.Trimesh)
    return fine


# ------------------------------------------------------------------------------------------ landmarks
def test_mirror_generates_left_on_the_minus_x_side() -> None:
    spec = make_spec(
        [],
        {
            "eye": {"box": [5, 14, -27, -15, 65, 76], "mirror": "x"},
            "nose": {"box": [-5, 5, -28, -20, 56, 64]},
        },
    )
    assert spec.landmarks["eye_R"] == (5, 14, -27, -15, 65, 76)
    assert spec.landmarks["eye_L"] == (-14, -5, -27, -15, 65, 76)
    assert spec.landmarks["nose"] == (-5, 5, -28, -20, 56, 64)
    assert "eye" not in spec.landmarks


def test_mirror_of_a_box_written_on_the_minus_x_side_still_names_it_left() -> None:
    spec = make_spec([], {"eye": {"box": [-14, -5, -27, -15, 65, 76], "mirror": "x"}})
    assert spec.landmarks["eye_L"][:2] == (-14, -5)
    assert spec.landmarks["eye_R"][:2] == (5, 14)


def test_mirror_of_a_box_straddling_the_symmetry_plane_is_rejected() -> None:
    with pytest.raises(InvalidArgument) as exc:
        make_spec([], {"nose": {"box": [-5, 5, 0, 1, 0, 1], "mirror": "x"}})
    assert "X = 0" in exc.value.accepted[0]


def test_mirror_axis_other_than_x_lists_the_accepted_axes() -> None:
    with pytest.raises(InvalidArgument) as exc:
        make_spec([], {"eye": {"box": [5, 14, 0, 1, 0, 1], "mirror": "y"}})
    assert exc.value.accepted == ["x"]


def test_left_landmark_selects_only_the_minus_x_faces(
    fine_cube: trimesh.Trimesh,
) -> None:
    spec = make_spec(
        [{"name": "ear", "landmark": "blob_L", "assign": {"key": "R"}}],
        {"blob": {"box": [10, 30, -30, 30, -1, 50], "mirror": "x"}},
    )
    lab = label_faces(fine_cube, spec)
    owned = lab.labels == lab.names.index("ear")
    assert owned.any()
    assert (fine_cube.triangles_center[owned][:, 0] <= -10).all()
    # the -X side wall (40 x 40) plus the 10 mm wide strips of the four faces that reach x <= -10
    assert lab.stats[0].area_mm2 == pytest.approx(
        40 * 40 + 2 * 10 * 40 + 2 * 10 * 40, rel=1e-9
    )


# ------------------------------------------------------------------------------------------ labels
def test_label_stats_match_analytic_box_areas_and_volume_shares(
    fine_cube: trimesh.Trimesh,
) -> None:
    spec = make_spec([{"name": "cap", "shape": TOP30, "assign": {"key": "W"}}])
    lab = label_faces(fine_cube, spec)
    cap, base = lab.stats
    assert (cap.name, base.name) == ("cap", "base")
    assert cap.area_mm2 == pytest.approx(3200.0, rel=1e-9)
    assert base.area_mm2 == pytest.approx(6 * 1600 - 3200.0, rel=1e-9)
    assert cap.faces + base.faces == len(fine_cube.faces)
    assert cap.volume_share == pytest.approx(0.25, abs=1e-6)
    assert base.volume_share == pytest.approx(0.75, abs=1e-6)
    assert cap.volume_mm3 == pytest.approx(16000.0, rel=1e-3)
    assert lab.empty == ()


def test_priority_first_region_wins_and_reversing_changes_the_counts(
    fine_cube: trimesh.Trimesh,
) -> None:
    low = {"name": "low", "shape": TOP20, "assign": {"key": "W"}}
    high = {"name": "high", "shape": TOP30, "assign": {"key": "R"}}
    first_low = {
        s.name: s for s in label_faces(fine_cube, make_spec([low, high])).stats
    }
    first_high = {
        s.name: s for s in label_faces(fine_cube, make_spec([high, low])).stats
    }
    # low first: it swallows everything above z = 20, so `high` owns nothing
    assert first_low["low"].area_mm2 == pytest.approx(1600 + 4 * 20 * 40)
    assert first_low["high"].faces == 0
    # high first: `high` keeps its slab, `low` only gets the z 20..30 band
    assert first_high["high"].area_mm2 == pytest.approx(3200.0)
    assert first_high["low"].area_mm2 == pytest.approx(4 * 10 * 40)
    assert label_faces(fine_cube, make_spec([low, high])).empty == ("high",)
    assert label_faces(fine_cube, make_spec([high, low])).empty == ()


def test_sphere_and_halfspace_shapes_label_by_centroid(
    fine_cube: trimesh.Trimesh,
) -> None:
    spec = make_spec(
        [
            {
                "name": "ball",
                "shape": {"type": "sphere", "center": [0, 0, 40], "radius": 12},
                "assign": {"key": "R"},
            },
            {
                "name": "under",
                "shape": {"type": "halfspace", "normal": [0, 0, -1], "offset": -10},
                "assign": {"key": "W"},
            },
        ]
    )
    lab = label_faces(fine_cube, spec)
    centres = fine_cube.triangles_center
    inside = np.linalg.norm(centres - [0, 0, 40], axis=1) <= 12
    assert ((lab.labels == 0) == inside).all()
    below = (centres[:, 2] <= 10) & ~inside
    assert ((lab.labels == 1) == below).all()


def test_label_volume_is_none_for_an_open_mesh(cube: trimesh.Trimesh) -> None:
    open_mesh = trimesh.Trimesh(cube.vertices, cube.faces[2:], process=False)
    assert not open_mesh.is_watertight
    lab = label_faces(
        open_mesh, make_spec([{"name": "cap", "shape": TOP30, "assign": {"key": "W"}}])
    )
    assert all(s.volume_share is None for s in lab.stats)
    assert lab.stats[0].faces > 0


def _intersection_volume(a: trimesh.Trimesh, b: trimesh.Trimesh) -> float:
    inter = trimesh.boolean.boolean_manifold([a, b], "intersection")
    if len(inter.faces) == 0:
        return 0.0
    tri = (
        inter.triangles
    )  # divergence theorem; Trimesh.volume warns on degenerate (touching) results
    return float(np.einsum("ij,ij->", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])) / 6.0)


def test_split_parts_conserves_volume_and_parts_are_disjoint(
    cube: trimesh.Trimesh,
) -> None:
    spec = make_spec(
        [
            {
                "name": "ball",
                "shape": {"type": "sphere", "center": [0, 0, 20], "radius": 10},
                "assign": {"key": "R"},
            },
            {
                "name": "top",
                "shape": {"type": "halfspace", "normal": [0, 0, 1], "offset": 30},
                "assign": {"key": "W"},
            },
            {"name": "upper", "shape": TOP20, "assign": {"mix": "GW"}},
        ]
    )
    parts = split_parts(cube, spec)
    assert [p.name for p in parts] == ["ball", "top", "upper", "base"]
    total = sum(float(p.mesh.volume) for p in parts)
    assert total == pytest.approx(float(cube.volume), rel=0.005)
    by_name = {p.name: p for p in parts}
    assert float(by_name["top"].mesh.volume) == pytest.approx(16000.0, rel=1e-3)
    ball = 4 / 3 * math.pi * 10**3
    assert (
        0.99 * ball < float(by_name["ball"].mesh.volume) <= ball
    )  # icosphere(4) is inscribed
    for i, a in enumerate(parts):
        assert a.mesh.is_watertight
        for b in parts[i + 1 :]:
            assert _intersection_volume(a.mesh, b.mesh) < 1e-2, (a.name, b.name)


def test_split_parts_resolves_keys_to_slots_and_mix_letters_to_digits(
    cube: trimesh.Trimesh,
) -> None:
    spec = make_spec(
        [
            {"name": "top", "shape": TOP30, "assign": {"mix": "GW"}},
            {"name": "mid", "shape": TOP20, "assign": {"key": "R"}},
        ]
    )
    by_name = {p.name: p for p in split_parts(cube, spec)}
    assert by_name["top"].assign == Assign(mix="42")
    assert by_name["mid"].assign == Assign(slot=3)
    assert by_name["base"].assign == Assign(slot=1)


def test_split_parts_priority_changes_the_parts(cube: trimesh.Trimesh) -> None:
    low = {"name": "low", "shape": TOP20, "assign": {"key": "W"}}
    high = {"name": "high", "shape": TOP30, "assign": {"key": "R"}}
    low_first = {
        p.name: float(p.mesh.volume) for p in split_parts(cube, make_spec([low, high]))
    }
    high_first = {
        p.name: float(p.mesh.volume) for p in split_parts(cube, make_spec([high, low]))
    }
    assert "high" not in low_first and low_first["low"] == pytest.approx(
        32000.0, rel=1e-3
    )
    assert high_first["high"] == pytest.approx(16000.0, rel=1e-3)
    assert high_first["low"] == pytest.approx(16000.0, rel=1e-3)


def test_union_of_two_landmarks_is_one_part(cube: trimesh.Trimesh) -> None:
    spec = make_spec(
        [{"name": "eyes", "landmarks": ["eye_L", "eye_R"], "assign": {"key": "G"}}],
        {"eye": {"box": [5, 15, -10, 10, 10, 20], "mirror": "x"}},
    )
    (eyes, base) = split_parts(cube, spec)
    assert float(eyes.mesh.volume) == pytest.approx(2 * 10 * 20 * 10, rel=1e-6)
    assert len(eyes.mesh.split(only_watertight=False)) == 2
    assert float(base.mesh.volume) == pytest.approx(64000 - 4000, rel=1e-6)


def test_stl_shape_cuts_and_labels_like_the_same_box(
    tmp_path: Path, cube: trimesh.Trimesh, fine_cube: trimesh.Trimesh
) -> None:
    slab = trimesh.creation.box(extents=(60, 60, 20))
    slab.apply_translation((0, 0, 40))  # z 30..50
    slab.export(tmp_path / "slab.stl")
    (tmp_path / "spec.json").write_text(
        json.dumps(
            {
                "filaments": FILAMENTS,
                "default": {"key": "K"},
                "regions": [
                    {
                        "name": "slab",
                        "shape": {"type": "stl", "path": "slab.stl"},
                        "assign": {"key": "W"},
                    }
                ],
            }
        )
    )
    spec = load_colour_spec(tmp_path / "spec.json")
    parts = split_parts(cube, spec)
    assert float(parts[0].mesh.volume) == pytest.approx(16000.0, rel=1e-3)
    area = label_faces(fine_cube, spec).stats[0].area_mm2
    assert area == pytest.approx(3200.0, rel=0.02)  # voxel containment of the cutter


def test_split_parts_refuses_an_open_mesh(cube: trimesh.Trimesh) -> None:
    from errors import GateFailure

    open_mesh = trimesh.Trimesh(cube.vertices, cube.faces[2:], process=False)
    with pytest.raises(GateFailure, match="watertight"):
        split_parts(
            open_mesh,
            make_spec([{"name": "top", "shape": TOP30, "assign": {"key": "W"}}]),
        )


# ------------------------------------------------------------------------------------------ spec validation
def _rejects(**doc: Any) -> InvalidArgument:
    base: dict[str, Any] = {
        "filaments": FILAMENTS,
        "default": {"key": "K"},
        "regions": [],
    }
    base.update(doc)
    with pytest.raises(InvalidArgument) as exc:
        parse_colour_spec(base)
    return exc.value


def test_unknown_landmark_lists_the_known_ones() -> None:
    err = _rejects(
        landmarks={"nose": {"box": [-1, 1, -1, 1, 0, 1]}},
        regions=[{"name": "n", "landmark": "nsoe", "assign": {"key": "R"}}],
    )
    assert err.got == "'nsoe'" or "nsoe" in err.got
    assert err.accepted == ["nose"]


def test_assign_with_zero_or_two_fields_is_rejected() -> None:
    for assign in ({}, {"key": "R", "slot": 3}):
        err = _rejects(regions=[{"name": "n", "shape": TOP30, "assign": assign}])
        assert err.accepted == ["exactly one of slot, key, mix"]


def test_assign_to_an_unknown_filament_key_lists_the_loaded_keys() -> None:
    err = _rejects(regions=[{"name": "n", "shape": TOP30, "assign": {"key": "Z"}}])
    assert err.accepted == ["G", "K", "R", "W"]
    err = _rejects(regions=[{"name": "n", "shape": TOP30, "assign": {"mix": "KZ"}}])
    assert "Z" in err.got and "K" in err.accepted
    err = _rejects(default={"slot": 7})
    assert err.accepted == ["1", "2", "3", "4"]


def test_one_colour_mix_is_rejected_as_not_a_mix() -> None:
    err = _rejects(regions=[{"name": "n", "shape": TOP30, "assign": {"mix": "GG"}}])
    assert "two or more different filaments" in err.accepted[0]


def test_duplicate_names_and_filaments_and_unknown_fields_are_rejected() -> None:
    region = {"name": "n", "shape": TOP30, "assign": {"key": "R"}}
    assert _rejects(regions=[region, region]).got == "n"
    assert _rejects(regions=[{**region, "name": "base"}]).got == "base"
    assert _rejects(filaments=[*FILAMENTS, {**FILAMENTS[0], "slot": 5}]).got == "K"
    assert "assign" in _rejects(regions=[{**region, "asign": {"key": "R"}}]).accepted
    assert "regions" in _rejects(rgions=[]).accepted


def test_region_needs_exactly_one_geometry_and_one_colour_source() -> None:
    both = {"name": "n", "shape": TOP30, "landmark": "x", "assign": {"key": "R"}}
    assert (
        "exactly one of landmark, landmarks, shape"
        in _rejects(regions=[both]).accepted[0]
    )
    assert (
        "exactly one of assign, target"
        in _rejects(regions=[{"name": "n", "shape": TOP30}]).accepted[0]
    )


def test_bad_boxes_and_shapes_name_what_is_accepted() -> None:
    assert (
        _rejects(landmarks={"a": {"box": [1, 0, 0, 1, 0, 1]}})
        .accepted[0]
        .startswith("[x0, x1")
    )
    assert _rejects(landmarks={"a": {"box": [0, 1, 2]}}).accepted == [
        "a list of 6 numbers"
    ]
    bad = {"name": "n", "shape": {"type": "cone"}, "assign": {"key": "R"}}
    assert _rejects(regions=[bad]).accepted == ["sphere", "box", "halfspace", "stl"]
    bad = {
        "name": "n",
        "shape": {"type": "sphere", "center": [0, 0, 0], "radius": -1},
        "assign": {"key": "R"},
    }
    assert "positive" in _rejects(regions=[bad]).accepted[0]


def test_bad_filament_fields_are_rejected() -> None:
    assert "1..9" in _rejects(filaments=[{**FILAMENTS[0], "slot": 12}]).accepted[0]
    assert "non-digit" in _rejects(filaments=[{**FILAMENTS[0], "key": "7"}]).accepted[0]
    assert _rejects(filaments=[{**FILAMENTS[0], "hex": "red"}]).accepted == ["#RRGGBB"]


def test_load_spec_reads_yaml_and_json_and_rejects_other_files(tmp_path: Path) -> None:
    yaml_text = """
filaments:
  - {slot: 1, key: K, name: Black, hex: '#111111'}
  - {slot: 2, key: W, name: White, hex: '#f0f0f0'}
default: {key: K}
landmarks:
  eye: {box: [5, 14, -27, -15, 65, 76], mirror: x}
regions:
  - {name: eyes, landmarks: [eye_L, eye_R], assign: {mix: KW}}
"""
    (tmp_path / "s.yaml").write_text(yaml_text)
    spec = load_colour_spec(tmp_path / "s.yaml")
    assert [r.name for r in spec.regions] == ["eyes"]
    assert spec.regions[0].landmarks == ("eye_L", "eye_R")
    assert spec.regions[0].assign == Assign(mix="KW")
    assert spec.filaments[1] == Filament(2, "W", "White", "#f0f0f0")
    (tmp_path / "s.txt").write_text(yaml_text)
    with pytest.raises(InvalidArgument) as exc:
        load_colour_spec(tmp_path / "s.txt")
    assert ".yaml" in exc.value.accepted
    with pytest.raises(InputNotFound):
        load_colour_spec(tmp_path / "missing.yaml")


def test_target_region_without_the_mix_engine_raises_missing_dependency() -> None:
    try:
        importable = importlib.util.find_spec("slicing.mix_model") is not None
    except ImportError:
        importable = False
    if importable:
        pytest.skip(
            "the #410 recipe search is importable here; covered by its own tests"
        )
    with pytest.raises(MissingDependency):
        make_spec([{"name": "pink", "shape": TOP30, "target": "#e7afb5"}])


# ------------------------------------------------------------------------------------------ assign resolution
def test_mix_digits_maps_letters_and_digits_to_slots() -> None:
    fil = tuple(Filament(f["slot"], f["key"], f["name"], f["hex"]) for f in FILAMENTS)
    assert mix_digits(Assign(mix="GWG"), fil) == "424"
    assert mix_digits(Assign(mix="3W"), fil) == "32"
    assert resolve_assign(Assign(key="R"), fil) == Assign(slot=3)
    with pytest.raises(InvalidArgument):
        mix_digits(Assign(key="R"), fil)
    with pytest.raises(InvalidArgument) as exc:
        mix_digits(Assign(mix="G9"), fil)
    assert "9" in exc.value.got


# ------------------------------------------------------------------------------------------ mix rules
def _violations(pattern: str, heights: list[float] | None = None) -> list[str]:
    spec = make_spec([{"name": "m", "shape": TOP30, "assign": {"mix": pattern}}])
    check = check_mixes(spec, heights)
    return [v.region for v in check.violations]


@pytest.mark.parametrize("pattern", ["GW", "GWR", "WGR", "KWGR"])
def test_patterns_without_equal_neighbours_pass(pattern: str) -> None:
    assert _violations(pattern, [0.1, 0.12]) == []


@pytest.mark.parametrize("pattern", ["GWGG", "WGGW", "GWG", "GGWR", "WRKW"])
def test_adjacent_repeats_are_caught_including_the_seam(pattern: str) -> None:
    # GWG and WRKW only repeat across the seam: G,W,G,G,W,G
    assert _violations(pattern, [0.1]) == ["m"]


def test_thickness_over_the_cap_is_caught_and_the_same_pattern_passes_when_thin() -> (
    None
):
    spec = make_spec([{"name": "m", "shape": TOP30, "assign": {"mix": "GW"}}])
    thick = check_mixes(spec, [0.12, 0.2, 0.12])
    thin = check_mixes(spec, [0.12, 0.1, 0.12])
    assert [v.rule for v in thick.violations] == ["layer-too-thick"]
    assert thin.ok
    assert thick.max_layer_mm == pytest.approx(0.2)
    assert not check_mixes(
        spec, [0.2], mix_cap=0.24
    ).violations  # the cap is a parameter


def test_heights_can_be_given_per_region() -> None:
    spec = make_spec(
        [
            {"name": "a", "shape": TOP30, "assign": {"mix": "GW"}},
            {"name": "b", "shape": TOP20, "assign": {"mix": "RW"}},
        ]
    )
    check = check_mixes(spec, {"a": [0.1], "b": [0.3]})
    assert check.violations_by_region().keys() == {"b"}
    assert set(check.checked) == {"a", "b"}


def test_pure_assigns_are_not_checked() -> None:
    spec = make_spec([{"name": "n", "shape": TOP30, "assign": {"key": "R"}}])
    check = check_mixes(spec, [0.32])
    assert check.checked == () and check.ok


# ------------------------------------------------------------------------------------------ the step result
def test_plan_colours_pass_and_serialises(cube: trimesh.Trimesh) -> None:
    spec = make_spec(
        [
            {"name": "top", "shape": TOP30, "assign": {"mix": "GW"}},
            {"name": "mid", "shape": TOP20, "assign": {"key": "R"}},
        ]
    )
    plan = plan_colours(cube, spec, [0.1, 0.12, 0.1])
    assert isinstance(plan, StepResult) and isinstance(plan, ColourPlan)
    assert plan.verdict == "PASS", plan.reasons
    assert plan.volume_error < 0.005
    assert plan.mix_check is not None and plan.mix_check.checked == ("top",)
    data = json.loads(json.dumps(plan.to_dict()))  # plain JSON types only
    assert [p["name"] for p in data["parts"]] == ["top", "mid", "base"]
    assert data["parts"][0]["assign"] == {"mix": "42"}
    assert data["parts"][0]["z_range"] == [30.0, 40.0]
    assert data["thickness_checked"] is True
    assert "3 colour parts (1 mixed)" in plan.summary()


def test_plan_colours_fails_on_a_mix_rule_violation(cube: trimesh.Trimesh) -> None:
    ok = make_spec([{"name": "top", "shape": TOP30, "assign": {"mix": "GW"}}])
    bad = make_spec([{"name": "top", "shape": TOP30, "assign": {"mix": "GWG"}}])
    assert plan_colours(cube, ok, [0.1]).verdict == "PASS"
    plan = plan_colours(cube, bad, [0.1])
    assert plan.verdict == "FAIL"
    assert "top" in plan.reasons[0]
    assert plan.to_dict()["mix_check"]["ok"] is False
    thick = plan_colours(cube, ok, [0.3])
    assert thick.verdict == "FAIL" and "layer" in thick.reasons[0]


def test_plan_colours_fails_for_a_region_on_an_unknown_filament_without_raising(
    cube: trimesh.Trimesh,
) -> None:
    spec = ColourSpec(
        filaments=tuple(
            Filament(f["slot"], f["key"], f["name"], f["hex"]) for f in FILAMENTS
        ),
        default=Assign(key="K"),
        regions=(
            RegionSpec(
                "top", (BoxShape((-30, -30, 30), (30, 30, 50)),), Assign(key="Z")
            ),
        ),
        landmarks={},
    )
    plan = plan_colours(cube, spec)
    assert plan.verdict == "FAIL"
    assert "unknown filament" in plan.reasons[0] and "'top'" in plan.reasons[0]
    assert plan.parts == []


def test_plan_colours_warns_about_an_empty_region_and_drops_it(
    cube: trimesh.Trimesh,
) -> None:
    far = {"type": "box", "min": [100, 100, 100], "max": [110, 110, 110]}
    spec = make_spec(
        [
            {"name": "ghost", "shape": far, "assign": {"key": "R"}},
            {"name": "top", "shape": TOP30, "assign": {"key": "W"}},
        ]
    )
    plan = plan_colours(cube, spec)
    assert plan.verdict == "WARN"
    assert plan.empty_regions == ("ghost",)
    assert [p.name for p in plan.parts] == ["top", "base"]
    assert "ghost" in plan.summary()


def test_plan_colours_fails_on_an_open_mesh_instead_of_raising(
    cube: trimesh.Trimesh,
) -> None:
    open_mesh = trimesh.Trimesh(cube.vertices, cube.faces[2:], process=False)
    plan = plan_colours(
        open_mesh, make_spec([{"name": "top", "shape": TOP30, "assign": {"key": "W"}}])
    )
    assert plan.verdict == "FAIL"
    assert "watertight" in plan.reasons[0]
    assert plan.parts == []
