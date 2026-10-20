"""Orientation search: analytic overhang/contact/height values, ranking, verdicts, speed."""

from __future__ import annotations

import json
import time

import numpy as np
import pytest
import trimesh
from shapely.geometry import Polygon

from errors import InvalidArgument
from prep.contracts import StepResult
from prep.frame import to_print_frame
from prep.orient import (
    OrientationCandidate,
    OrientationReport,
    apply,
    search_orientations,
)


def _box(x: float, y: float, z: float) -> trimesh.Trimesh:
    return to_print_frame(trimesh.creation.box(extents=(x, y, z)))


def _tilted_box(a: float, b: float, c: float, alpha_deg: float) -> trimesh.Trimesh:
    m = trimesh.creation.box(extents=(a, b, c))
    m.apply_transform(
        trimesh.transformations.rotation_matrix(np.radians(alpha_deg), [1, 0, 0])
    )
    return to_print_frame(m)


def _by_label(report: OrientationReport, label: str) -> OrientationCandidate:
    return next(c for c in report.candidates if c.label == label)


def _wedge(base: float, rise: float, width: float) -> trimesh.Trimesh:
    """Right-triangle prism: base along X, rise along Z, depth `width` along Y, lying on its base."""
    prism = trimesh.creation.extrude_polygon(
        Polygon([(0, 0), (base, 0), (0, rise)]), width
    )
    prism.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
    return to_print_frame(prism)


def test_box_lying_beats_standing_on_its_small_face() -> None:
    report = search_orientations(_box(10, 10, 40))
    assert report.current.label == "current"
    assert report.current.contact_area_mm2 == pytest.approx(100.0)
    assert report.current.height_mm == pytest.approx(40.0)
    assert report.current.overhang_area_mm2 == 0.0
    assert report.best.label != "current"
    assert report.best.rank == 1 and report.best.score < report.current.score
    assert report.best.contact_area_mm2 == pytest.approx(
        400.0
    )  # lying on a 10 x 40 face
    assert report.best.height_mm == pytest.approx(10.0)
    assert report.best.overhang_area_mm2 == 0.0
    assert report.verdict == "WARN"


def test_current_orientation_already_best_passes() -> None:
    report = search_orientations(_box(40, 40, 10))  # flat plate lying on its big face
    assert (
        report.best.label == "current" and report.best.rank == report.current.rank == 1
    )
    assert report.verdict == "PASS"


@pytest.mark.parametrize(
    ("alpha", "expected_overhang"),
    [(30.0, 30.0 * 20.0), (60.0, 30.0 * 10.0)],
)
def test_tilted_box_overhang_height_and_line_contact_match_trigonometry(
    alpha: float, expected_overhang: float
) -> None:
    a, b, c = 30.0, 20.0, 10.0
    report = search_orientations(_tilted_box(a, b, c, alpha), top_k=1)
    cur = report.current
    # 30 deg: the tilted bottom face (a x b) faces down at cos(30) = .87 > sin(45); the sides face down at sin(30) = .5.
    # 60 deg: the bottom face is at cos(60) = .5 (fine) but the -y face (a x c) faces down at sin(60) = .87 (overhang).
    assert cur.overhang_area_mm2 == pytest.approx(expected_overhang)
    assert cur.contact_area_mm2 == 0.0  # it rests on a line, not a face
    assert cur.height_mm == pytest.approx(
        b * np.sin(np.radians(alpha)) + c * np.cos(np.radians(alpha))
    )
    assert cur.com_margin_mm < 0.0  # the centre of mass is not over a line contact


def test_wedge_overhang_and_contact_equal_analytic_values() -> None:
    base, rise, width = 40.0, 30.0, 20.0
    hyp = float(np.hypot(base, rise))
    wedge = _wedge(base, rise, width)
    r45 = search_orientations(wedge, angle_deg=45.0, top_k=500)
    r60 = search_orientations(wedge, angle_deg=60.0, top_k=500)
    # upright on the base: the slope faces up, base contact is base x width
    assert r45.current.overhang_area_mm2 == 0.0
    assert r45.current.contact_area_mm2 == pytest.approx(base * width)
    # upside down: the slope faces down at an angle with n.d = base... n_z = base/hyp = .8
    flipped = _by_label(r45, "flipped (+Z down)")
    assert flipped.overhang_area_mm2 == pytest.approx(hyp * width)  # .8 > sin(45) = .71
    assert flipped.contact_area_mm2 == 0.0
    assert flipped.height_mm == pytest.approx(rise)
    assert (
        _by_label(r60, "flipped (+Z down)").overhang_area_mm2 == 0.0
    )  # .8 < sin(60) = .87
    # on the vertical leg: nothing faces down, contact is rise x width, height is the base length
    on_leg = _by_label(r45, "-X down")
    assert on_leg.overhang_area_mm2 == 0.0
    assert on_leg.contact_area_mm2 == pytest.approx(rise * width)
    assert on_leg.height_mm == pytest.approx(base)


def test_inverted_t_has_its_wings_as_overhang_and_flipping_removes_it() -> None:
    stem = trimesh.creation.box(extents=(10, 20, 15))
    bar = trimesh.creation.box(extents=(40, 20, 5))
    bar.apply_translation([0, 0, 10])
    t_shape = to_print_frame(trimesh.boolean.union([stem, bar], engine="manifold"))
    report = search_orientations(t_shape)
    assert report.current.label == "current"
    assert report.current.overhang_area_mm2 == pytest.approx(
        (40 - 10) * 20.0
    )  # the two wings' undersides
    assert report.current.contact_area_mm2 == pytest.approx(10 * 20.0)
    assert report.best.label != "current"
    assert report.best.overhang_area_mm2 == 0.0
    assert report.best.score < report.current.score
    assert report.verdict == "WARN"
    assert "cuts the overhang area by 100%" in " ".join(report.reasons)


def test_sphere_fails_because_every_orientation_keeps_an_overhang_island() -> None:
    sphere = to_print_frame(trimesh.creation.icosphere(subdivisions=3, radius=10.0))
    failed = search_orientations(sphere)
    assert failed.verdict == "FAIL"
    assert failed.best_islands_mm2 and failed.best_islands_mm2[0] > 25.0
    # negative control: the same model passes once only huge islands count
    relaxed = search_orientations(sphere, island_min_mm2=1e6)
    assert relaxed.verdict != "FAIL" and relaxed.best_islands_mm2 == []


def test_apply_returns_candidate_orientation_in_print_frame() -> None:
    report = search_orientations(_box(10, 10, 40))
    placed = apply(_box(10, 10, 40), report.best)
    assert placed.bounds[0][2] == pytest.approx(0.0)
    assert placed.extents[2] == pytest.approx(report.best.height_mm)
    assert placed.volume == pytest.approx(4000.0)  # a proper rotation: no mirroring


def test_report_is_a_strict_json_step_result() -> None:
    report = search_orientations(_box(10, 10, 40), top_k=2)
    assert isinstance(report, StepResult)
    json.dumps(report.to_dict(), allow_nan=False)
    assert "orientations" in report.summary()
    assert any(c.label == "current" for c in report.candidates)


def test_current_is_listed_even_when_ranked_outside_top_k() -> None:
    report = search_orientations(_box(10, 10, 40), top_k=1)
    assert [c.label for c in report.candidates][-1] == "current"
    assert len(report.candidates) == 2


def test_speed_on_a_100k_face_mesh() -> None:
    mesh = to_print_frame(trimesh.creation.icosphere(subdivisions=6, radius=30.0))
    assert len(mesh.faces) > 80_000
    t0 = time.perf_counter()
    report = search_orientations(mesh)
    elapsed = time.perf_counter() - t0
    assert elapsed < 30.0, f"{elapsed:.1f}s"
    assert report.evaluated >= 96


@pytest.mark.parametrize(
    "kwargs",
    [
        {"angle_deg": 0.0},
        {"angle_deg": 90.0},
        {"top_k": 0},
        {"bed_tol": 0.0},
        {"candidates": -1},
    ],
)
def test_invalid_arguments_are_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises(InvalidArgument):
        search_orientations(_box(1, 1, 1), **kwargs)  # type: ignore[arg-type]
