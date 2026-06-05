"""Tests for the headless STL -> USDZ converter (lib/usdz.py).

Gated behind pxr (usd-core) and trimesh: if either is absent the suite skips rather
than fails. Only the STL path is exercised — the .scad branch shells out to `bin/3d`,
which imports every command module (incl. half-written siblings during the swarm), so
it is intentionally not driven here.
"""
from __future__ import annotations

import os
import zipfile

import pytest

pytest.importorskip("pxr")
trimesh = pytest.importorskip("trimesh")

from usdz import _sanitize_prim_name, mesh_to_usdz  # noqa: E402


def test_cube_stl_to_usdz(tmp_path) -> None:
    stl = tmp_path / "cube.stl"
    box = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
    box.export(str(stl))

    out = tmp_path / "cube.usdz"
    nfaces = mesh_to_usdz(str(stl), str(out), color=(0.3, 0.55, 0.85), name="cube")

    assert nfaces == 12  # a box is 12 triangles
    assert out.exists()
    assert os.path.getsize(out) > 0
    assert zipfile.is_zipfile(str(out))


def test_dirty_name_is_sanitized() -> None:
    assert _sanitize_prim_name("my-part.v2") == "my_part_v2"
    assert _sanitize_prim_name("9lives").startswith("m_")
    assert _sanitize_prim_name("") == "m_"


def test_empty_mesh_rejected(tmp_path) -> None:
    import numpy as np

    empty = trimesh.Trimesh(vertices=np.zeros((0, 3)), faces=np.zeros((0, 3), dtype=int))
    stl = tmp_path / "empty.ply"
    empty.export(str(stl))
    with pytest.raises(ValueError):
        mesh_to_usdz(str(stl), str(tmp_path / "x.usdz"), name="empty")
