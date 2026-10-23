from __future__ import annotations

from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
trimesh = pytest.importorskip("trimesh")

import mesh_repair  # noqa: E402
from errors import MissingDependency  # noqa: E402

from .workflow_helper import run_cli  # noqa: E402

_V = (
    np.array(
        [
            [0, 0, 0],
            [1, 0, 0],
            [1, 1, 0],
            [0, 1, 0],
            [0, 0, 1],
            [1, 0, 1],
            [1, 1, 1],
            [0, 1, 1],
        ],
        float,
    )
    * 20
)
_F = np.array(
    [
        [0, 3, 2],
        [0, 2, 1],
        [4, 5, 6],
        [4, 6, 7],
        [0, 1, 5],
        [0, 5, 4],
        [3, 7, 6],
        [3, 6, 2],
        [0, 4, 7],
        [0, 7, 3],
        [1, 2, 6],
        [1, 6, 5],
    ]
)


def _library_cache(tmp_path: Path) -> dict[str, str]:
    """The build of meshfix is cached under the host cache dir so the story does not rebuild it per test."""
    from cli import paths

    return {"XDG_CACHE_HOME": str(paths.cache_dir().parent)}


@pytest.fixture(scope="module", autouse=True)
def _meshfix_available() -> None:
    try:
        mesh_repair.load_meshfix()  # builds it once into the host cache when the checkout is missing
    except (MissingDependency, ImportError) as exc:
        pytest.skip(f"meshfix is not available here: {exc}")


def test_a_downloaded_model_with_a_hole_is_repaired_then_passes_the_gate(
    tmp_path: Path,
) -> None:
    """A user downloads an STL whose top face is missing: the gate fails it, `3d mesh --repair -o` writes a
    repaired copy, prints the before/after proof and passes it, and the copy passes the plain gate too."""
    part = tmp_path / "bracket.stl"
    trimesh.Trimesh(_V, np.delete(_F, [2, 3], axis=0), process=False).export(part)

    broken = run_cli(
        tmp_path, "mesh", "bracket.stl", env_extra=_library_cache(tmp_path)
    )
    assert broken.returncode == 1, broken.stdout + broken.stderr
    assert "watertight        : NO" in broken.stdout

    fixed = run_cli(
        tmp_path,
        "mesh",
        "bracket.stl",
        "--repair",
        "-o",
        "bracket.fixed.stl",
        timeout=600,
        env_extra=_library_cache(tmp_path),
    )
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr
    assert "watertight no -> yes" in fixed.stdout and "faces 10 -> 12" in fixed.stdout
    assert "repaired mesh written to bracket.fixed.stl" in fixed.stdout
    assert ">>> MESH CHECK: PASS" in fixed.stdout

    mesh = trimesh.load(tmp_path / "bracket.fixed.stl", process=True)
    assert mesh.is_watertight and mesh.volume == pytest.approx(8000.0, rel=1e-6)
    assert part.exists()  # the download itself is untouched

    again = run_cli(
        tmp_path, "mesh", "bracket.fixed.stl", env_extra=_library_cache(tmp_path)
    )
    assert again.returncode == 0, again.stdout + again.stderr


def test_repair_defaults_to_a_fixed_copy_next_to_the_model(tmp_path: Path) -> None:
    """Without -o the repaired mesh lands next to the model as <name>.fixed.stl."""
    trimesh.Trimesh(_V, np.delete(_F, [2, 3], axis=0), process=False).export(
        tmp_path / "part.stl"
    )
    result = run_cli(
        tmp_path,
        "mesh",
        "part.stl",
        "--repair",
        timeout=600,
        env_extra=_library_cache(tmp_path),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "part.fixed.stl").is_file()


def test_repair_cannot_overwrite_the_model_it_reads(tmp_path: Path) -> None:
    """A user who points `--repair -o` at the model itself is refused, and the original file stays intact."""
    trimesh.Trimesh(_V, np.delete(_F, [2, 3], axis=0), process=False).export(
        tmp_path / "part.stl"
    )
    before = (tmp_path / "part.stl").read_bytes()
    result = run_cli(
        tmp_path,
        "mesh",
        "part.stl",
        "--repair",
        "-o",
        "part.stl",
        env_extra=_library_cache(tmp_path),
    )
    assert result.returncode == 2
    assert "overwrite its input" in result.stderr
    assert (tmp_path / "part.stl").read_bytes() == before
