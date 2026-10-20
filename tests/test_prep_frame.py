"""Print frame: on the bed, XY-centred; loading rejects missing/unsupported/empty files."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import trimesh

from errors import InputNotFound, InvalidArgument
from prep import frame


def test_to_print_frame_puts_lowest_point_at_zero_and_centres_xy() -> None:
    box = trimesh.creation.box((10, 20, 30))
    box.apply_translation([100, -50, 7])
    out = frame.to_print_frame(box)
    lo, hi = out.bounds
    assert np.allclose(lo, [-5, -10, 0]) and np.allclose(hi, [5, 10, 30])
    assert box.bounds[0][0] == 95  # the input is untouched


def test_rotated_lays_a_standing_box_on_its_big_face() -> None:
    box = trimesh.creation.box((10, 10, 40))
    rot = trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0])[:3, :3]
    out = frame.rotated(box, rot)
    assert np.allclose(out.extents, [10, 40, 10]) and abs(out.bounds[0][2]) < 1e-9


def test_rotated_rejects_a_non_3x3_matrix() -> None:
    with pytest.raises(InvalidArgument):
        frame.rotated(trimesh.creation.box((1, 1, 1)), np.eye(4))


def test_load_roundtrip_and_errors(tmp_path: Path) -> None:
    p = tmp_path / "b.stl"
    trimesh.creation.box((2, 3, 4)).export(p)
    m = frame.load_mesh(p)
    assert abs(m.volume - 24) < 1e-6
    with pytest.raises(InputNotFound):
        frame.load_mesh(tmp_path / "missing.stl")
    bad = tmp_path / "x.txt"
    bad.write_text("nope")
    with pytest.raises(InvalidArgument):
        frame.load_mesh(bad)


def test_digests_change_with_content(tmp_path: Path) -> None:
    a, b = tmp_path / "a.stl", tmp_path / "b.stl"
    trimesh.creation.box((1, 1, 1)).export(a)
    trimesh.creation.box((1, 1, 2)).export(b)
    assert frame.file_digest(a) != frame.file_digest(b)
    assert frame.file_digest(a) == frame.file_digest(a)
    m1, m2 = trimesh.creation.box((1, 1, 1)), trimesh.creation.box((1, 1, 2))
    assert frame.mesh_digest(m1) != frame.mesh_digest(m2)
