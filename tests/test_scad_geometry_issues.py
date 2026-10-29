"""`3d scad export-stl` recognises non-closed meshes in the wording of every OpenSCAD generation."""

from __future__ import annotations

import pytest

from commands.scad import _geometry_issues


@pytest.mark.parametrize(
    "output",
    [
        "ERROR: The given mesh is not closed! Unable to convert to CGAL_Nef_Polyhedron.",
        "ERROR: [manifold] Input mesh is not closed!",
        "ERROR: [manifold] Surface_mesh -> Manifold conversion failed: NotManifold",
        "WARNING: Object may not be a valid 2-manifold and may need repair!",
    ],
)
def test_open_meshes_are_blocking_non_manifold_geometry(output: str) -> None:
    blocking, _ = _geometry_issues(output)

    assert blocking == ["Non-manifold geometry (holes in mesh)"]


def test_a_clean_export_has_no_blocking_issue() -> None:
    assert _geometry_issues("Simple: yes\nVolumes: 2\n") == ([], [])
