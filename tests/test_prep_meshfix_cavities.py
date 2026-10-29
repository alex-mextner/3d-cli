"""Closed shells that point inward: an internal cavity is correct and must survive repair, a separate body is a defect."""

from __future__ import annotations

import sys
from collections.abc import Iterator

import numpy as np
import pytest
import trimesh

from prep.meshfix import diagnose, repair

CUBE_VOLUME = 20.0**3


@pytest.fixture(autouse=True)
def _builtin_only(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setitem(
        sys.modules, "mesh_repair", None
    )  # the external library must not win `auto`
    yield


def two_shells(outer: trimesh.Trimesh, inner: trimesh.Trimesh) -> trimesh.Trimesh:
    """`outer` as is plus `inner` turned inside out (normals point into `inner`)."""
    return trimesh.Trimesh(
        np.vstack([outer.vertices, inner.vertices]),
        np.vstack([outer.faces, inner.faces[:, ::-1] + len(outer.vertices)]),
        process=False,
    )


def hollow(cavity: float = 8.0) -> trimesh.Trimesh:
    return two_shells(
        trimesh.creation.box((20.0, 20.0, 20.0)),
        trimesh.creation.box((cavity, cavity, cavity)),
    )


def test_a_tiny_cavity_is_not_debris() -> None:
    d = diagnose(
        hollow(cavity=3.0)
    )  # 27 mm3 and 12 faces: under the debris limits, but it is a cavity
    assert d.debris_shells == 0 and d.verdict == "PASS"
    assert d.volume_mm3 == pytest.approx(CUBE_VOLUME - 27.0, abs=1e-6)


def test_repair_keeps_the_cavity_when_the_outer_shell_has_a_hole() -> None:
    m = hollow()
    faces = m.faces.copy()
    open_outer = trimesh.Trimesh(
        m.vertices, faces[1:], process=False
    )  # one triangle of the outer shell is missing
    r = repair(open_outer, backend="builtin")
    assert r.after.is_volume
    assert r.after.volume_mm3 == pytest.approx(CUBE_VOLUME - 8.0**3, rel=1e-6)


def test_a_separate_inside_out_body_is_a_defect_and_repair_turns_it_outward() -> None:
    far = trimesh.creation.box(
        (4.0, 4.0, 4.0),
        transform=trimesh.transformations.translation_matrix([30.0, 0.0, 0.0]),
    )
    mixed = two_shells(trimesh.creation.box((20.0, 20.0, 20.0)), far)
    d = diagnose(mixed)
    assert not d.is_volume and d.verdict == "FAIL"
    r = repair(mixed, backend="builtin")
    assert r.after.is_volume
    assert r.after.volume_mm3 == pytest.approx(CUBE_VOLUME + 64.0, abs=1e-6)


@pytest.mark.parametrize("reverse_faces", [False, True])
def test_an_inside_out_shell_poking_out_of_the_outer_one_is_not_a_cavity(
    reverse_faces: bool,
) -> None:
    """Nested means every vertex inside: the verdict must not depend on which face comes first."""
    poking = trimesh.creation.box(
        (8.0, 8.0, 8.0),
        transform=trimesh.transformations.translation_matrix([8.0, 0.0, 0.0]),
    )
    m = two_shells(trimesh.creation.box((20.0, 20.0, 20.0)), poking)
    if reverse_faces:
        m = trimesh.Trimesh(m.vertices, m.faces[::-1], process=False)
    d = diagnose(m)
    assert not d.is_volume and d.verdict == "FAIL"


def test_a_second_part_behind_the_outer_shell_does_not_hide_the_cavity() -> None:
    """The containment ray also crosses a separate solid placed along it: only the outer shell counts."""
    behind = trimesh.creation.box(
        (12.0, 12.0, 12.0),
        transform=trimesh.transformations.translation_matrix([17.3, 8.0, 23.1]),
    )
    both = trimesh.util.concatenate([hollow(), behind])
    d = diagnose(both)
    assert d.is_volume and d.verdict == "PASS"
    assert d.volume_mm3 == pytest.approx(CUBE_VOLUME - 8.0**3 + 12.0**3, abs=1e-6)
