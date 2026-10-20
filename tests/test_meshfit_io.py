"""Mesh I/O: canonical float32 load, byte-reproducible STL / 3MF writers."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest
from errors import InputNotFound, UsageError
from geometry.meshfit import meshio
from meshfit_fixtures import cube_sphere


def test_stl_round_trip_is_exact_and_the_file_is_byte_reproducible(
    tmp_path: Path,
) -> None:
    V, F = cube_sphere(10, 20.0)
    a, b = tmp_path / "a.stl", tmp_path / "b.stl"
    sha_a, sha_b = meshio.save_mesh(str(a), V, F), meshio.save_mesh(str(b), V, F)
    assert sha_a == sha_b == hashlib.sha256(a.read_bytes()).hexdigest()
    V2, F2 = meshio.load_mesh(str(a))
    assert V2.shape == V.shape and F2.shape == F.shape
    # every original vertex is still present, bit for bit (float32 values survive the file)
    assert set(map(tuple, V.tolist())) == set(map(tuple, V2.tolist()))


def test_load_returns_float32_representable_vertices(tmp_path: Path) -> None:
    V, F = cube_sphere(6, 17.3)
    meshio.save_mesh(str(tmp_path / "m.stl"), V, F)
    V2, _ = meshio.load_mesh(str(tmp_path / "m.stl"))
    assert np.array_equal(V2, V2.astype(np.float32).astype(np.float64))


def test_3mf_is_deterministic_and_reads_back(tmp_path: Path) -> None:
    V, F = cube_sphere(8, 20.0)
    a, b = tmp_path / "a.3mf", tmp_path / "b.3mf"
    assert meshio.save_mesh(str(a), V, F) == meshio.save_mesh(str(b), V, F)
    V2, F2 = meshio.load_mesh(
        str(a)
    )  # the tool's own reader (trimesh's 3MF loader needs networkx)
    assert len(F2) == len(F) and meshio.describe(V2, F2)["watertight"]
    assert meshio.describe(V2, F2)["volume_mm3"] == pytest.approx(
        meshio.describe(V, F)["volume_mm3"], rel=1e-5
    )


def test_mesh_hash_equals_the_hash_of_the_written_stl(tmp_path: Path) -> None:
    V, F = cube_sphere(8, 20.0)
    assert meshio.mesh_sha256(V, F) == meshio.save_mesh(str(tmp_path / "x.stl"), V, F)


def test_errors_are_structured(tmp_path: Path) -> None:
    with pytest.raises(InputNotFound):
        meshio.load_mesh(str(tmp_path / "missing.stl"))
    (tmp_path / "bad.stl").write_text("not a mesh")
    with pytest.raises(UsageError):
        meshio.load_mesh(str(tmp_path / "bad.stl"))
    V, F = cube_sphere(4, 1.0)
    with pytest.raises(UsageError, match="unsupported output format"):
        meshio.save_mesh(str(tmp_path / "out.xyz"), V, F)
