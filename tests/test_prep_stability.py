"""Stability analysis: analytic mass, CoM shift, tip angle, tap/pull safety factors, verdict gates."""

from __future__ import annotations

import json

import numpy as np
import pytest
import trimesh

from errors import InvalidArgument
from prep.contracts import StepResult
from prep.frame import to_print_frame
from prep.stability import ExternalLoad, StabilityReport, analyse

RHO = 1.24e-3  # g/mm3


def _cube(side: float = 20.0) -> trimesh.Trimesh:
    return to_print_frame(trimesh.creation.box(extents=(side, side, side)))


def _column() -> trimesh.Trimesh:
    return to_print_frame(
        trimesh.creation.cylinder(radius=5.0, height=100.0, sections=64)
    )


def _scenario(report: StabilityReport, name: str):  # type: ignore[no-untyped-def]
    return next(s for s in report.scenarios if s.name == name)


def test_cube_com_support_and_tip_angle_are_exact() -> None:
    report = analyse(_cube())
    assert report.volume_mm3 == pytest.approx(8000.0)
    assert report.com_mm == pytest.approx([0.0, 0.0, 10.0], abs=1e-6)
    assert report.support_area_mm2 == pytest.approx(400.0)
    assert report.contact_area_mm2 == pytest.approx(400.0)
    alone = _scenario(report, "part alone")
    assert alone.min_margin_mm == pytest.approx(10.0)
    assert alone.tip_angle_deg == pytest.approx(45.0, abs=1e-6)  # atan(10 / 10)
    assert report.verdict == "PASS"


def test_mass_equals_analytic_shell_plus_infill_formula() -> None:
    side, infill, perim = 20.0, 0.15, 4 * 0.42
    inner = (side - 2 * perim) ** 3
    expected = RHO * ((side**3 - inner) + infill * inner)
    report = analyse(_cube(side), infill=infill, perimeters=4, line_width=0.42)
    assert report.mass_g == pytest.approx(expected, rel=0.01)
    # negative control: rounding the wall zone to whole 0.5 mm cells (1.5 mm) would be ~5 % off
    rounded = RHO * ((side**3 - (side - 3.0) ** 3) + infill * (side - 3.0) ** 3)
    assert abs(report.mass_g - rounded) / expected > 0.02


def test_mass_is_linear_and_monotonic_in_infill_and_scales_with_density() -> None:
    masses = [analyse(_cube(), infill=f).mass_g for f in (0.0, 0.15, 0.5, 1.0)]
    assert masses == sorted(masses) and len(set(masses)) == 4
    assert masses[3] == pytest.approx(8000.0 * RHO)  # 100 % infill is solid PLA
    rep = analyse(_cube(), infill=0.0)
    assert masses[0] == pytest.approx(RHO * rep.wall_zone_mm3)
    assert masses[2] == pytest.approx(
        RHO * (rep.wall_zone_mm3 + 0.5 * rep.infill_zone_mm3)
    )
    assert rep.wall_zone_mm3 + rep.infill_zone_mm3 == pytest.approx(rep.volume_mm3)
    assert analyse(_cube(), density_g_cm3=2.48).mass_g == pytest.approx(
        2 * analyse(_cube()).mass_g
    )
    assert analyse(_cube(), perimeters=0, infill=0.15).mass_g == pytest.approx(
        0.15 * 8000.0 * RHO
    )
    sweep = analyse(_cube(), infill_sweep=(0.1, 0.3)).mass_vs_infill_g
    assert sweep["0.1"] < sweep["0.3"]


def test_load_shifts_the_centre_of_mass_by_the_mass_weighted_amount() -> None:
    load = ExternalLoad("phone", 30.0, (5.0, -2.0, 30.0))
    report = analyse(_cube(), loads=(load,))
    alone = _scenario(report, "part alone")
    with_load = _scenario(report, "with phone")
    m = alone.mass_g
    assert with_load.mass_g == pytest.approx(m + 30.0)
    assert with_load.com_mm[0] == pytest.approx(30.0 * 5.0 / (m + 30.0), abs=1e-6)
    assert with_load.com_mm[1] == pytest.approx(30.0 * -2.0 / (m + 30.0), abs=1e-6)
    assert with_load.com_mm[2] == pytest.approx(
        (m * 10.0 + 30.0 * 30.0) / (m + 30.0), abs=1e-6
    )
    assert with_load.tip_angle_deg < alone.tip_angle_deg


def test_tall_column_fails_the_default_tip_angle_and_a_cube_does_not() -> None:
    column = analyse(_column())
    assert column.verdict == "FAIL"
    alone = _scenario(column, "part alone")
    assert alone.tip_angle_deg == pytest.approx(
        np.degrees(np.arctan(5.0 / 50.0)), abs=0.7
    )
    assert "tip angle" in " ".join(column.reasons)


def test_tap_at_the_top_tips_the_column_but_the_same_tap_low_does_not() -> None:
    force = 0.02
    kwargs = {
        "forces_n": (force,),
        "tip_min_deg": 3.0,
        "tip_warn_deg": 5.0,
        "mu_bed": 10.0,
    }
    normal = (-1.0, 0.0, 0.0)  # the tap pushes toward +x
    high = ExternalLoad("cap", 4.0, (0.0, 0.0, 20.0), (0.0, 0.0, 100.0), normal)
    low = ExternalLoad("cap", 4.0, (0.0, 0.0, 20.0), (0.0, 0.0, 10.0), normal)
    rep_high = analyse(_column(), loads=(high,), **kwargs)  # type: ignore[arg-type]
    rep_low = analyse(_column(), loads=(low,), **kwargs)  # type: ignore[arg-type]
    s_high, s_low = _scenario(rep_high, "with cap"), _scenario(rep_low, "with cap")
    weight = s_high.mass_g / 1000 * 9.81
    for scen, z in ((s_high, 100.0), (s_low, 10.0)):
        tap = next(t for t in scen.tests if t.kind == "tap")
        # the push faces +x, so it tips about the polygon's +x extreme
        margin_x = max(p[0] for p in rep_high.support_polygon_mm) - scen.com_mm[0]
        assert tap.safety_factor == pytest.approx(
            weight * margin_x / (force * z), rel=1e-6
        )
        assert tap.edge_normal == pytest.approx([1.0, 0.0], abs=0.1)
    assert next(t for t in s_high.tests if t.kind == "tap").safety_factor < 1.0
    assert rep_high.verdict == "FAIL"
    assert next(t for t in s_low.tests if t.kind == "tap").safety_factor > 1.5
    assert rep_low.verdict == "PASS"


def test_cube_tap_and_pull_safety_factors_match_the_torque_balance() -> None:
    normal = (-1.0, 0.0, 1.0)  # screen faces -x and up; a tap pushes toward +x and down
    load = ExternalLoad("p", 10.0, (0.0, 0.0, 25.0), (0.0, 0.0, 25.0), normal)
    force = 0.5
    report = analyse(_cube(), loads=(load,), forces_n=(force,), mu_bed=10.0)
    scen = _scenario(report, "with p")
    weight = scen.mass_g / 1000 * 9.81
    tap = next(t for t in scen.tests if t.kind == "tap")
    fx, fz = (
        force / np.sqrt(2),
        force / np.sqrt(2),
    )  # components of the push: +x and down
    overturning = (
        fx * 25.0 - fz * 10.0
    )  # horizontal push at 25 mm height minus the downward part 10 mm inside the edge
    assert tap.overturning_nmm == pytest.approx(overturning)
    assert tap.safety_factor == pytest.approx(weight * 10.0 / overturning, rel=1e-6)
    assert tap.mu_slide == pytest.approx(fx / (weight + fz))
    assert tap.edge_normal == pytest.approx([1.0, 0.0], abs=1e-9)
    pull = next(t for t in scen.tests if t.kind == "pull")
    assert pull.overturning_nmm == pytest.approx(
        force * 25.0
    )  # horizontal pull toward -x at 25 mm
    assert pull.mu_slide == pytest.approx(force / weight)
    assert pull.edge_normal == pytest.approx([-1.0, 0.0], abs=1e-9)


@pytest.mark.parametrize(
    ("target_sf", "verdict"), [(2.0, "PASS"), (1.25, "WARN"), (0.8, "FAIL")]
)
def test_safety_factor_bands_decide_the_verdict(target_sf: float, verdict: str) -> None:
    base = analyse(_cube(), loads=(ExternalLoad("p", 10.0, (0.0, 0.0, 25.0)),))
    weight = _scenario(base, "with p").mass_g / 1000 * 9.81
    force = (
        weight * 10.0 / (25.0 * target_sf)
    )  # horizontal push at z = 25 about the x = +10 edge
    load = ExternalLoad("p", 10.0, (0.0, 0.0, 25.0), (0.0, 0.0, 25.0), (-1.0, 0.0, 0.0))
    report = analyse(_cube(), loads=(load,), forces_n=(force,), mu_bed=10.0)
    tap = next(t for t in _scenario(report, "with p").tests if t.kind == "tap")
    assert tap.safety_factor == pytest.approx(target_sf, rel=1e-6)
    assert tap.verdict == verdict and report.verdict == verdict


def test_sliding_beyond_bed_friction_warns() -> None:
    load = ExternalLoad("p", 10.0, (0.0, 0.0, 25.0), (0.0, 0.0, 5.0), (-1.0, 0.0, 0.0))
    slippery = analyse(_cube(), loads=(load,), forces_n=(0.05,), mu_bed=0.01)
    grippy = analyse(_cube(), loads=(load,), forces_n=(0.05,), mu_bed=0.5)
    assert slippery.verdict == "WARN" and "needs mu" in " ".join(slippery.reasons)
    assert grippy.verdict == "PASS"


def test_centre_of_mass_outside_the_support_polygon_fails() -> None:
    base = trimesh.creation.box(extents=(10, 10, 5))
    arm = trimesh.creation.box(extents=(40, 10, 20))
    arm.apply_translation(
        [15.0, 0.0, 12.5]
    )  # spans x -5..35 above a base spanning x -5..5
    part = to_print_frame(trimesh.boolean.union([base, arm], engine="manifold"))
    report = analyse(part)
    alone = _scenario(report, "part alone")
    assert alone.min_margin_mm < 0.0
    assert report.verdict == "FAIL" and "outside the support polygon" in " ".join(
        report.reasons
    )


def test_mesh_without_solid_fails_instead_of_raising() -> None:
    speck = trimesh.creation.box(extents=(0.05, 0.05, 0.05))
    speck.apply_translation([0.0, 0.0, 0.025])
    report = analyse(speck)
    assert report.verdict == "FAIL" and report.scenarios == []
    assert report.summary().startswith("stability FAIL")


def test_report_is_a_strict_json_step_result() -> None:
    load = ExternalLoad("p", 10.0, (0.0, 0.0, 25.0), (0.0, 0.0, 25.0), (-1.0, 0.0, 0.0))
    report = analyse(_cube(), loads=(load,))
    assert isinstance(report, StepResult)
    json.dumps(report.to_dict(), allow_nan=False)
    assert "tip angle" in report.summary()


def test_invalid_inputs_are_rejected() -> None:
    with pytest.raises(InvalidArgument):
        analyse(_cube(), infill=1.5)
    with pytest.raises(InvalidArgument):
        analyse(_cube(), forces_n=(0.0,))
    with pytest.raises(InvalidArgument):
        ExternalLoad("p", 0.0, (0.0, 0.0, 0.0))
    with pytest.raises(InvalidArgument):
        ExternalLoad("p", 1.0, (0.0, 0.0, 0.0), tap_point_mm=(0.0, 0.0, 1.0))
