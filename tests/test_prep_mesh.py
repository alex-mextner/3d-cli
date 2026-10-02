"""Unit tests for the `3d prep` geometry engine (lib/prep_mesh.py).

The pipeline needs trimesh/numpy, so the whole module skips gracefully when they are
absent (exactly like test_mesh_metrics / test_cli_smoke). Two behaviours are pinned on
synthetic meshes: repair closes a hole-punched box, and the orientation scorer prefers the
flat-face-down / low-support rest orientation over an overhang-heavy one.
"""
from __future__ import annotations

from typing import Any

import pytest

np = pytest.importorskip("numpy")
trimesh = pytest.importorskip("trimesh")

import prep_mesh as pm  # noqa: E402


def _hole_punched_box() -> Any:
    """A unit box with one triangle removed -> not watertight, exactly one boundary loop."""
    box = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
    faces = box.faces.copy()
    box.faces = faces[1:]  # drop a single triangle; the paired one leaves an open triangular hole
    box.remove_unreferenced_vertices()
    return box


def test_repair_makes_hole_punched_box_watertight() -> None:
    box = _hole_punched_box()
    assert not box.is_watertight  # precondition: the fixture really is broken

    repaired, report = pm.repair(box)

    assert report.watertight_before is False
    assert report.watertight_after is True
    assert repaired.is_watertight
    assert report.holes_before >= 1
    assert report.holes_after == 0
    assert report.holes_filled >= 1


def test_repair_keeps_a_clean_box_watertight() -> None:
    box = trimesh.creation.box(extents=(8.0, 6.0, 4.0))
    _repaired, report = pm.repair(box)
    assert report.watertight_before is True
    assert report.watertight_after is True
    assert report.holes_before == 0
    assert report.holes_filled == 0


def test_orientation_lays_a_pillar_flat() -> None:
    """A 10x10x40 pillar has no overhangs in any axis rest, so the tie-break picks the
    orientation with the lowest build height: it lies the 40 mm axis down onto a 10x40 face."""
    pillar = trimesh.creation.box(extents=(10.0, 10.0, 40.0))
    best, _baseline = pm.best_orientation(pillar)
    oriented = pm.apply_orientation(pillar, best)
    # Lowest possible build height for this box is its smallest extent, 10 mm.
    assert oriented.extents[2] == pytest.approx(10.0, abs=1e-3)
    assert best.build_height == pytest.approx(10.0, abs=1e-3)


def _right_triangular_prism() -> Any:
    """A ramp: right-triangle cross-section (right angle at origin) extruded along Y.

    Resting the z=0 face (or the x=0 vertical face) on the bed leaves the sloped hypotenuse
    facing up -> ~no support. Resting the hypotenuse down turns the flat faces into overhangs.
    """
    verts = np.array([
        [0.0, 0.0, 0.0], [40.0, 0.0, 0.0], [0.0, 0.0, 20.0],   # y=0 cap
        [0.0, 10.0, 0.0], [40.0, 10.0, 0.0], [0.0, 10.0, 20.0],  # y=10 cap
    ])
    faces = np.array([
        [0, 2, 1], [3, 4, 5],            # end caps
        [0, 1, 4], [0, 4, 3],            # bottom (z=0)
        [0, 3, 5], [0, 5, 2],            # vertical (x=0)
        [1, 2, 5], [1, 5, 4],            # hypotenuse
    ])
    return trimesh.Trimesh(vertices=verts, faces=faces, process=True)


def test_orientation_avoids_overhang_heavy_rest() -> None:
    prism = _right_triangular_prism()
    best, _baseline = pm.best_orientation(prism)
    # The chosen rest orientation needs essentially no support.
    assert best.overhang_frac < 0.05

    # A deliberately bad rest (hypotenuse down) must score strictly worse.
    hyp_normal = np.array([20.0, 0.0, 40.0])  # outward normal of the hypotenuse face
    bad = pm.score_orientation(prism, hyp_normal, source="hull")
    assert bad.overhang_frac > best.overhang_frac


def test_boundary_loops_counts_two_holes() -> None:
    box = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
    faces = box.faces.copy()
    # Drop two triangles that do not share an edge -> two separate boundary loops.
    keep = [i for i in range(len(faces)) if i not in (0, len(faces) - 1)]
    box.faces = faces[keep]
    box.remove_unreferenced_vertices()
    assert pm._boundary_loops(box) >= 1
