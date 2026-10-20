"""Behaviour of prep.layers: severity-driven layer heights, mix caps, Lipschitz limit, Orca profile text.

Synthetic solids with known surface slopes: a vertical cylinder (every wall facet has n_z = 0 -> hmax everywhere),
an icosphere (shallow only at the poles), a flat cone (15 deg from horizontal -> fine everywhere) and a steep cone.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace

import numpy as np
import pytest
import trimesh

from errors import InvalidArgument
from prep.contracts import Assign, Part, StepResult
from prep.layers import (
    LayerPlan,
    heights_by_part,
    mix_zranges_from,
    parse_profile_text,
    plan_layers,
    plan_problems,
    simulate_layers,
)
from prep.regions import parse_colour_spec, plan_colours

HMIN, HMAX = 0.08, 0.32
CAP = 0.12


def _raised(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    mesh.apply_translation((0, 0, -mesh.bounds[0][2]))
    return mesh


@pytest.fixture(scope="module")
def cylinder() -> trimesh.Trimesh:
    return _raised(trimesh.creation.cylinder(radius=10, height=40, sections=64))


@pytest.fixture(scope="module")
def sphere() -> trimesh.Trimesh:
    return _raised(trimesh.creation.icosphere(subdivisions=4, radius=10))


def _mid_heights(plan: LayerPlan) -> np.ndarray:
    """Layer heights without the fixed first layer."""
    return np.array(plan.heights[1:])


# ------------------------------------------------------------------------------------------ severity
def test_vertical_cylinder_is_coarse_everywhere(cylinder: trimesh.Trimesh) -> None:
    plan = plan_layers(cylinder)
    h = _mid_heights(plan)
    assert plan.verdict == "PASS"
    assert plan.stats["at_hmin"] == 0
    assert float(np.median(h)) == pytest.approx(HMAX)
    # only the last 3 mm may be scaled (down to 0.75 x) to land the stack on the top
    assert h.min() >= 0.75 * HMAX - 1e-6
    assert plan.heights[0] == pytest.approx(0.2)


def test_sphere_gets_hmin_only_near_the_poles_and_stays_a_minority(
    sphere: trimesh.Trimesh,
) -> None:
    plan = plan_layers(sphere)
    h = _mid_heights(plan)
    assert h.min() == pytest.approx(HMIN, abs=1e-6)
    assert plan.stats["at_hmin_pct"] < 40.0
    z_mid = np.array(plan.z_tops[1:]) - h / 2  # layer mid heights
    fine = h <= HMIN + 0.005
    assert (np.abs(z_mid[fine] - 10.0) > 6.0).all(), (
        "hmin layers must sit on the polar caps, not the walls"
    )
    equator = np.abs(z_mid - 10.0) < 2.0
    assert equator.any() and (h[equator] >= 0.25).all(), (
        "the near-vertical equator takes the coarse layers"
    )


def test_shallow_cone_is_fine_and_steep_cone_is_coarse() -> None:
    shallow = _raised(
        trimesh.creation.cone(radius=30, height=8, sections=64)
    )  # 15 deg wall
    steep = _raised(
        trimesh.creation.cone(radius=5, height=40, sections=64)
    )  # 83 deg wall
    fine = plan_layers(shallow)
    coarse = plan_layers(steep)
    assert fine.stats["median"] <= HMIN + 0.005
    assert coarse.stats["at_hmin"] == 0 and coarse.stats["median"] >= 0.3


def test_hmin_and_hmax_parameters_bound_the_heights(sphere: trimesh.Trimesh) -> None:
    plan = plan_layers(sphere, hmin=0.1, hmax=0.2)
    h = _mid_heights(plan)
    assert h.min() >= 0.1 - 1e-6 and h.max() <= 0.2 + 1e-6
    assert plan.stats["max"] <= 0.2 + 1e-6


# ------------------------------------------------------------------------------------------ mix caps
def test_mix_range_caps_every_layer_over_it_and_without_it_they_are_thicker(
    cylinder: trimesh.Trimesh,
) -> None:
    capped = plan_layers(cylinder, mix_zranges=[(10.0, 20.0)], mix_cap=CAP)
    plain = plan_layers(cylinder)
    over = capped.heights_between(10.0, 20.0)
    assert len(over) > 80 and max(over) <= CAP + 5e-4
    assert (
        min(plain.heights_between(10.0, 20.0)) >= 0.3
    )  # the control: the same layers are 0.32 without the range
    assert len(capped.heights) > len(plain.heights)
    assert capped.verdict == "PASS" and plain.verdict == "PASS"
    # the cap is local: well above the range the walls are coarse again
    assert min(capped.heights_between(30.0, 36.0)) >= 0.3


def test_mix_cap_is_a_parameter(cylinder: trimesh.Trimesh) -> None:
    thin = plan_layers(cylinder, mix_zranges=[(10.0, 20.0)], mix_cap=0.1)
    assert max(thin.heights_between(10.0, 20.0)) <= 0.1 + 5e-4
    assert max(thin.heights_between(10.0, 20.0)) > 0.099


def test_mix_cap_below_hmin_is_rejected(cylinder: trimesh.Trimesh) -> None:
    with pytest.raises(InvalidArgument) as exc:
        plan_layers(cylinder, mix_zranges=[(10.0, 20.0)], mix_cap=0.05)
    assert "hmin" in exc.value.accepted[0]
    with pytest.raises(InvalidArgument):
        plan_layers(cylinder, hmin=0.4, hmax=0.3)


def test_mix_range_on_the_bed_lowers_the_first_layer(cylinder: trimesh.Trimesh) -> None:
    on_bed = plan_layers(cylinder, mix_zranges=[(0.0, 10.0)], mix_cap=CAP)
    off_bed = plan_layers(cylinder, mix_zranges=[(10.0, 20.0)], mix_cap=CAP)
    assert on_bed.first_height == pytest.approx(CAP) and on_bed.heights[
        0
    ] == pytest.approx(CAP)
    assert off_bed.first_height == pytest.approx(0.2)
    assert on_bed.verdict == "PASS"


def test_mix_zranges_from_parts_merges_overlaps_and_ignores_pure_parts() -> None:
    def part(name: str, z0: float, z1: float, assign: Assign) -> Part:
        box = trimesh.creation.box(extents=(5, 5, z1 - z0))
        box.apply_translation((0, 0, (z0 + z1) / 2))
        return Part(name, box, assign)

    parts = [
        part("a", 10, 20, Assign(mix="42")),
        part("b", 18, 25, Assign(mix="31")),
        part("c", 40, 50, Assign(mix="42")),
        part("pure", 0, 60, Assign(slot=1)),
    ]
    assert mix_zranges_from(parts) == ((10.0, 25.0), (40.0, 50.0))
    assert mix_zranges_from([parts[3]]) == ()


def test_plan_and_colour_check_agree_end_to_end(cylinder: trimesh.Trimesh) -> None:
    """The plan built from the parts' mix ranges passes `check_mixes`; the plan without them fails it."""
    spec = parse_colour_spec(
        {
            "filaments": [
                {"slot": 1, "key": "K", "name": "Black", "hex": "#111111"},
                {"slot": 2, "key": "W", "name": "White", "hex": "#f0f0f0"},
            ],
            "default": {"key": "K"},
            "regions": [
                {
                    "name": "band",
                    "shape": {
                        "type": "box",
                        "min": [-20, -20, 10],
                        "max": [20, 20, 20],
                    },
                    "assign": {"mix": "KW"},
                }
            ],
        }
    )
    from prep.regions import split_parts

    parts = split_parts(cylinder, spec)
    zones = mix_zranges_from(parts)
    assert zones == ((10.0, 20.0),)
    good = plan_layers(cylinder, mix_zranges=zones)
    bad = plan_layers(cylinder)
    ok = plan_colours(cylinder, spec, heights_by_part(good, parts))
    broken = plan_colours(cylinder, spec, heights_by_part(bad, parts))
    assert ok.verdict == "PASS", ok.reasons
    assert broken.verdict == "FAIL" and "band" in broken.reasons[0]


# ------------------------------------------------------------------------------------------ Lipschitz
@pytest.mark.parametrize("slope", [0.1, 0.2])
def test_neighbouring_layers_differ_by_at_most_slope_times_height(
    sphere: trimesh.Trimesh, slope: float
) -> None:
    plan = plan_layers(sphere, slope=slope, mix_zranges=[(0.0, 5.0)])
    h = _mid_heights(plan)
    assert (np.abs(np.diff(h)) <= slope * h[:-1] + 1e-3).all()
    assert plan.stats["max_step_ratio"] <= slope + 0.01


def test_without_a_slope_limit_the_cap_boundary_is_a_cliff(
    cylinder: trimesh.Trimesh,
) -> None:
    """Control for the limit above: an enormous slope lets 0.32 -> 0.12 happen between two layers."""
    plan = plan_layers(cylinder, slope=50.0, mix_zranges=[(10.0, 20.0)])
    assert plan.stats["max_step_ratio"] > 0.4
    limited = plan_layers(cylinder, slope=0.2, mix_zranges=[(10.0, 20.0)])
    assert limited.stats["max_step_ratio"] <= 0.21


# ------------------------------------------------------------------------------------------ top and text
def test_stack_lands_on_the_model_top(
    sphere: trimesh.Trimesh, cylinder: trimesh.Trimesh
) -> None:
    for mesh, height in ((sphere, 20.0), (cylinder, 40.0)):
        plan = plan_layers(mesh)
        assert plan.height_mm == pytest.approx(height, abs=1e-6)
        assert plan.z_tops[-1] == pytest.approx(height, abs=abs(plan.top_error) + 1e-9)
        assert abs(plan.top_error) <= HMIN / 2
        assert plan.z_tops[0] == pytest.approx(plan.first_height)
        assert plan.z_tops[-1] == pytest.approx(sum(plan.heights))
        assert plan.profile[-1][0] == pytest.approx(height, abs=1e-3)


def test_height_not_a_multiple_of_the_layer_still_lands_within_half_hmin() -> None:
    for height in (17.37, 23.011, 31.94):
        cyl = _raised(trimesh.creation.cylinder(radius=8, height=height, sections=48))
        plan = plan_layers(cyl)
        assert abs(plan.top_error) <= HMIN / 2, height


def test_profile_text_has_the_orca_format_and_round_trips(
    sphere: trimesh.Trimesh,
) -> None:
    plan = plan_layers(sphere, mix_zranges=[(5.0, 8.0)], object_id=3)
    assert re.fullmatch(
        r"object_id=3\|(-?\d+\.\d{6};){2,}-?\d+\.\d{6}\n",
        plan.profile_text.replace("\n", "") + "\n",
    )
    assert plan.profile_text.endswith("\n") and plan.profile_text.count("\n") == 1
    object_id, points = parse_profile_text(plan.profile_text)
    assert object_id == 3
    assert len(points) == len(plan.profile)
    assert np.allclose(np.array(points), np.array(plan.profile), atol=1e-6)
    assert points[0][0] == 0.0 and points[-1][0] == pytest.approx(
        plan.height_mm, abs=1e-3
    )
    zs = [z for z, _ in points]
    assert zs == sorted(zs)
    # re-simulating the TEXT gives the planned stack
    tops = simulate_layers(
        zs, [h for _, h in points], points[-1][0], plan.first_height, HMIN
    )
    assert np.allclose(tops, plan.z_tops, atol=1e-6)


def test_malformed_profile_text_is_rejected() -> None:
    for text in (
        "0;0.2;10;0.2\n",
        "object_id=x|0;0.2;1;0.2\n",
        "object_id=1|0;0.2;1\n",
        "object_id=1|0;0.2\n",
    ):
        with pytest.raises(InvalidArgument):
            parse_profile_text(text)


# ------------------------------------------------------------------------------------------ the step result
def test_step_result_contract_and_json(sphere: trimesh.Trimesh) -> None:
    plan = plan_layers(sphere, mix_zranges=[(5.0, 8.0)])
    assert isinstance(plan, StepResult)
    data = json.loads(json.dumps(plan.to_dict()))
    assert data["layers"] == len(plan.heights) == plan.stats["layers"]
    assert data["profile_text"] == plan.profile_text
    assert data["mix_zranges"] == [[5.0, 8.0]]
    assert sum(plan.stats["bands"].values()) == len(plan.heights) - 1
    assert (
        "mix zone" in plan.summary()
        and f"{plan.stats['layers']} layers" in plan.summary()
    )


def test_plan_problems_flag_a_layer_over_the_cap_and_a_missed_top(
    cylinder: trimesh.Trimesh,
) -> None:
    plan = plan_layers(cylinder, mix_zranges=[(10.0, 20.0)])
    assert plan_problems(plan) == []
    thick = replace(plan, mix_zranges=((30.0, 36.0),))  # the layers there are 0.32 mm
    assert any("layer over a mix range" in p for p in plan_problems(thick))
    shifted = replace(plan, z_tops=(*plan.z_tops[:-1], plan.z_tops[-1] + 0.1))
    assert any("object top" in p for p in plan_problems(shifted))
    unsealed = replace(
        plan,
        profile=(*plan.profile[:-1], (plan.profile[-1][0] + 0.01, plan.profile[-1][1])),
    )
    assert any("Orca would drop" in p for p in plan_problems(unsealed))


def test_empty_or_flat_mesh_is_an_invalid_argument() -> None:
    with pytest.raises(InvalidArgument):
        plan_layers(trimesh.Trimesh())
    flat = trimesh.Trimesh(
        vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]], faces=[[0, 1, 2]]
    )
    with pytest.raises(InvalidArgument):
        plan_layers(flat)
