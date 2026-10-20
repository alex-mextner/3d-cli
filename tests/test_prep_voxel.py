"""Voxel occupancy: volumes match the analytic solids, cavities stay empty until filled."""

from __future__ import annotations

import numpy as np
import pytest
import trimesh

from prep import voxel


def test_box_volume_is_exact() -> None:
    box = trimesh.creation.box((10, 20, 30))
    grid = voxel.voxelize(box, pitch=0.5)
    assert abs(grid.volume_mm3() - box.volume) < 1e-6 * box.volume


def test_sphere_volume_within_one_percent() -> None:
    sphere = trimesh.creation.icosphere(subdivisions=4, radius=10)
    grid = voxel.voxelize(sphere, pitch=0.25)
    assert abs(grid.volume_mm3() - sphere.volume) / sphere.volume < 0.01


def test_hollow_shell_cavity_is_air_until_fill_enclosed() -> None:
    outer = trimesh.creation.icosphere(subdivisions=3, radius=10)
    inner = trimesh.creation.icosphere(subdivisions=3, radius=6)
    inner.invert()  # inward-facing normals make the cavity a hole of the shell
    shell = trimesh.util.concatenate([outer, inner])
    grid = voxel.voxelize(shell, pitch=0.5)
    centre = tuple(int(i) for i in (np.array(grid.shape) // 2))
    assert not grid.occ[centre]  # the cavity is air
    filled = voxel.fill_enclosed(grid.occ)
    assert filled[centre]
    assert filled.sum() > grid.occ.sum()
    # filling only adds the cavity: it now equals the solid outer sphere
    solid = voxel.voxelize(outer, pitch=0.5)
    assert (
        abs(float(filled.sum()) - float(solid.occ.sum())) / float(solid.occ.sum())
        < 0.01
    )


def test_open_air_stays_unfilled_and_disc_is_symmetric() -> None:
    occ = np.zeros((9, 9, 9), dtype=bool)
    occ[0:2, :, :] = True  # a wall touching the border encloses nothing
    assert not voxel.fill_enclosed(occ)[5, 5, 5]
    d = voxel.disc(2.0)
    assert d.shape == (5, 5) and d[2, 2] and d.sum() == 13 and (d == d.T).all()


def test_pitch_must_be_positive() -> None:
    with pytest.raises(ValueError):
        voxel.voxelize(trimesh.creation.box((1, 1, 1)), pitch=0.0)
