"""Teeth tests for the G-code mix gate (slicing.mix_gcode): measured on synthetic sliced G-code."""

from __future__ import annotations

from pathlib import Path

import pytest
from head_sequence_fixtures import gcode, project_3mf

from slicing.head_sequence import ProjectError
from slicing.mix_gcode import FAIL, PASS, SKIP, check, layer_thickness, preflight_check
from slicing.mix_pattern import MixRules

# virtual filament 5 = pattern "14" (heads 1 and 4), as in head_sequence_fixtures
THIN = [0.2] + [0.12] * 11
ROW_141 = "1,4,1,1,50,0,g,w,m2,z0,xa0,xb0,d0,o0,u701,cm1,141"


def heights_z(heights: list[float]) -> dict[int, float]:
    out, z = {}, 0.0
    for i, h in enumerate(heights):
        z += h
        out[i] = z
    return out


def alternating(n: int, a: int = 1, b: int = 4) -> list[dict[str, int]]:
    return [{"A": a if i % 2 == 0 else b} for i in range(n)]


def test_layer_thickness_follows_the_real_z_steps() -> None:
    th = layer_thickness(heights_z([0.2, 0.08, 0.16, 0.32]))
    assert [round(t, 4) for t in th.values()] == [0.2, 0.08, 0.16, 0.32]


def test_one_thin_layer_of_each_colour_passes(tmp_path: Path) -> None:
    proj = project_3mf(tmp_path / "p.3mf", {"A": 5})
    g = gcode(tmp_path / "p.gcode", alternating(12), heights=THIN)
    report = check(str(g), str(proj))
    (r,) = report.results
    assert (r.status, r.sequence) == (PASS, "141414141414")
    assert report.status == PASS
    assert r.report is not None and r.report.max_layer_mm == pytest.approx(0.12)


def test_the_minor_colour_on_consecutive_layers_fails_and_names_the_layers(
    tmp_path: Path,
) -> None:
    proj = project_3mf(tmp_path / "p.3mf", {"A": 5})
    # head 4 is printed on layers 2 and 3 in a row: GWG-style doubling of the additional colour
    heads = [1, 4, 4, 1, 4, 1, 4, 1]
    g = gcode(
        tmp_path / "p.gcode", [{"A": h} for h in heads], heights=[0.2] + [0.12] * 7
    )
    (r,) = check(str(g), str(proj), MixRules(max_run_mm=5.0)).results
    assert r.status == FAIL
    assert "layers 1-2" in r.detail and "0.240 mm band" in r.detail
    assert r.report is not None and r.report.max_minor_run_mm == pytest.approx(0.24)


def test_a_doubled_majority_colour_fails_by_default_and_passes_when_allowed(
    tmp_path: Path,
) -> None:
    """Recipe 141 (head 1 on two of three layers): printed as planned, head 1 doubles at the seam."""
    proj = project_3mf(tmp_path / "p.3mf", {"A": 5}, mixed=ROW_141)
    heads = [1, 4, 1, 1, 4, 1, 1, 4, 1]
    g = gcode(tmp_path / "p.gcode", [{"A": h} for h in heads], heights=[0.12] * 9)
    assert check(str(g), str(proj)).results[0].status == FAIL
    assert check(str(g), str(proj), MixRules(max_run_mm=0.24)).results[0].status == PASS
    # ... but the same print of a 1:1 recipe is the additional colour doubled: never allowed
    one_to_one = project_3mf(tmp_path / "q.3mf", {"A": 5})
    assert (
        check(str(g), str(one_to_one), MixRules(max_run_mm=0.24)).results[0].status
        == FAIL
    )


def test_a_thick_mixed_layer_fails(tmp_path: Path) -> None:
    proj = project_3mf(tmp_path / "p.3mf", {"A": 5})
    g = gcode(tmp_path / "p.gcode", alternating(10), heights=[0.2] + [0.32] * 9)
    (r,) = check(str(g), str(proj)).results
    assert r.status == FAIL
    assert "thicker than the 0.12 mm cap" in r.detail and "0.320 mm" in r.detail


def test_variable_layer_heights_are_measured_in_mm(tmp_path: Path) -> None:
    proj = project_3mf(tmp_path / "p.3mf", {"A": 5})
    thin = [0.2, 0.08, 0.1, 0.12, 0.08, 0.12, 0.1, 0.08]
    ok = gcode(tmp_path / "ok.gcode", alternating(8), heights=thin)
    assert check(str(ok), str(proj)).results[0].status == PASS
    # one layer swells to 0.16 mm inside the mixed region
    thick = [*thin[:4], 0.16, *thin[5:]]
    bad = gcode(tmp_path / "bad.gcode", alternating(8), heights=thick)
    (r,) = check(str(bad), str(proj)).results
    assert (
        r.status == FAIL and "first at layer 4" in r.detail and "0.160 mm" in r.detail
    )
    # the same layers read as fine when the cap is configured wider
    assert (
        check(str(bad), str(proj), MixRules(layer_cap_mm=0.16)).results[0].status
        == PASS
    )


def test_the_bed_layer_is_exempt_from_the_cap(tmp_path: Path) -> None:
    proj = project_3mf(tmp_path / "p.3mf", {"A": 5})
    g = gcode(tmp_path / "p.gcode", alternating(6), heights=[0.3] + [0.12] * 5)
    assert check(str(g), str(proj)).results[0].status == PASS


def test_a_mix_part_beside_a_solid_part_of_the_same_object(tmp_path: Path) -> None:
    """Object A = a mix part (filament 5: heads 1/4) plus a solid part in head 4 (the shared head)."""
    proj = project_3mf(tmp_path / "p.3mf", {"A": [5, 4]})
    layers = [{"A": [4]}, {"A": [4]}] + [
        {"A": [1, 4] if i % 2 == 0 else [4]} for i in range(8)
    ]  # solid head 4 alone below; the mix prints head 1 / head 4 on top
    g = gcode(tmp_path / "p.gcode", layers, heights=[0.2] + [0.12] * 9)
    (r,) = check(str(g), str(proj)).results
    assert (
        r.status == PASS and r.first_layer == 2 and r.sequence == "1414141"
    )  # the window ends on the last layer of the mix-only head
    # the same, with the mix printing head 1 on two layers in a row
    doubled = [
        {"A": [4]},
        {"A": [4]},
        {"A": [1, 4]},
        {"A": [1, 4]},
        {"A": [4]},
        {"A": [1, 4]},
        {"A": [4]},
    ]
    g2 = gcode(tmp_path / "p2.gcode", doubled, heights=[0.2] + [0.12] * 6)
    assert check(str(g2), str(proj)).results[0].status == FAIL


def test_a_head_shared_with_the_solid_colour_is_not_guessed(tmp_path: Path) -> None:
    proj = project_3mf(tmp_path / "p.3mf", {"A": [5, 1, 4]})
    g = gcode(tmp_path / "p.gcode", [{"A": [1, 4]}] * 6, heights=[0.12] * 6)
    (r,) = check(str(g), str(proj)).results
    assert r.status == SKIP and "cannot be told apart" in r.detail


def test_ratio_rows_and_pure_objects_are_not_measured(tmp_path: Path) -> None:
    proj = project_3mf(tmp_path / "p.3mf", {"A": 6, "B": 1})
    g = gcode(tmp_path / "p.gcode", [{"A": 1, "B": 1}] * 4, heights=[0.12] * 4)
    report = check(str(g), str(proj))
    assert [(r.object, r.status) for r in report.results] == [("A", SKIP)]
    assert report.status == SKIP


def test_without_a_project_the_alternating_stretch_is_measured(tmp_path: Path) -> None:
    """Solid head 1 below and solid head 4 above are colour regions, not a mix: only the stretch
    between the first and the last head change is judged."""
    heights = [0.2] + [0.12] * 9
    good = gcode(
        tmp_path / "good.gcode",
        [{"A": h} for h in [1, 1, 1, 4, 1, 4, 1, 4, 4, 4]],
        heights=heights,
    )
    (r,) = check(str(good)).results
    assert (r.status, r.sequence) == (PASS, "4141")
    bad = gcode(
        tmp_path / "bad.gcode",
        [{"A": h} for h in [1, 1, 4, 1, 1, 4, 4, 1, 4, 4]],
        heights=heights,
    )
    assert check(str(bad)).results[0].status == FAIL


def test_a_plain_colour_change_is_not_a_mix_without_a_project(tmp_path: Path) -> None:
    g = gcode(
        tmp_path / "p.gcode", [{"A": h} for h in [1] * 5 + [4] * 5], heights=[0.32] * 10
    )
    (r,) = check(str(g)).results
    assert r.status == SKIP and "no alternation" in r.detail


def test_no_layer_z_is_a_skip_that_says_so(tmp_path: Path) -> None:
    g = gcode(tmp_path / "p.gcode", alternating(6))
    (r,) = check(str(g)).results
    assert r.status == SKIP and "Z" in r.detail


def test_preflight_shape(tmp_path: Path) -> None:
    proj = project_3mf(tmp_path / "p.3mf", {"A": 5})
    good = gcode(tmp_path / "g.gcode", alternating(8), heights=[0.2] + [0.12] * 7)
    bad = gcode(tmp_path / "b.gcode", alternating(8), heights=[0.2] + [0.32] * 7)
    assert preflight_check(str(good), str(proj))["status"] == PASS
    result = preflight_check(str(bad), str(proj))
    assert result["status"] == FAIL and result["lines"]
    broken = tmp_path / "x.3mf"
    broken.write_bytes(b"not a zip")
    assert preflight_check(str(good), str(broken))["status"] == SKIP
    with pytest.raises(ProjectError):
        check(str(good), str(broken))
