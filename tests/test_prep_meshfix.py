"""Tests for prep.meshfix: diagnose counts every defect, the builtin backend repairs it locally, the registry works.

Each defective fixture asserts exact counts / analytic volumes, so a diagnose that miscounts or a repair that
resamples, eats surface or leaves a defect fails here. Meshes are small trimesh primitives.
"""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Iterator

import numpy as np
import pytest
import trimesh
from numpy.typing import NDArray

from errors import InvalidArgument, MissingDependency
from prep.contracts import StepResult
from prep.meshfix import (
    BuiltinBackend,
    Diagnosis,
    ExternalBackend,
    MeshFixBackend,
    MeshFixOptions,
    backends,
    diagnose,
    register_backend,
    repair,
    unregister_backend,
)

CUBE_VOLUME = 1000.0  # 10 mm cube


@pytest.fixture(autouse=True)
def _restore_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The real meshfix library (lib/mesh_repair.py) wins `auto` wherever it is buildable; these tests pin the
    # builtin/fake backends, so hide it unless a test installs its own stand-in module.
    monkeypatch.setitem(sys.modules, "mesh_repair", None)
    saved = backends()
    yield
    for b in backends():
        unregister_backend(b.name)
    for b in saved:
        register_backend(b)


def cube(subdivisions: int = 0) -> trimesh.Trimesh:
    m = trimesh.creation.box((10.0, 10.0, 10.0))
    for _ in range(subdivisions):
        m = m.subdivide()
    return trimesh.Trimesh(m.vertices, m.faces, process=True)


def tetra(points: list[list[float]]) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
    """A closed outward-oriented tetrahedron on four points."""
    v = np.array(points, dtype=np.float64)
    f = np.array([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]], dtype=np.int64)
    if trimesh.Trimesh(v, f, process=False).volume < 0:
        f = f[:, ::-1]
    return v, f


def weld(parts: list[tuple[NDArray[np.float64], NDArray[np.int64]]]) -> trimesh.Trimesh:
    """Concatenate (vertices, faces) parts and merge coincident vertices."""
    vs, fs, offset = [], [], 0
    for v, f in parts:
        vs.append(v)
        fs.append(f + offset)
        offset += len(v)
    return trimesh.Trimesh(np.vstack(vs), np.vstack(fs), process=True)


def parts_of(m: trimesh.Trimesh) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
    return np.asarray(m.vertices, dtype=np.float64), np.asarray(m.faces, dtype=np.int64)


def without_faces(m: trimesh.Trimesh, drop: list[int]) -> trimesh.Trimesh:
    keep = np.ones(len(m.faces), dtype=bool)
    keep[drop] = False
    return trimesh.Trimesh(m.vertices, m.faces[keep], process=False)


# ------------------------------------------------------------------------------------------- diagnose


def test_diagnose_clean_cube_exact_counts() -> None:
    d = diagnose(cube())
    assert (d.faces, d.vertices, d.components) == (12, 8, 1)
    assert d.watertight and d.winding_consistent and d.is_volume
    assert (d.boundary_edges, d.nonmanifold_edges, d.nonmanifold_vertices) == (0, 0, 0)
    assert (d.degenerate_faces, d.debris_shells) == (0, 0)
    assert d.volume_mm3 == pytest.approx(CUBE_VOLUME, abs=1e-9)
    assert d.extents_mm == pytest.approx([10.0, 10.0, 10.0])
    assert d.defects == []
    assert d.verdict == "PASS"


def test_diagnose_missing_face_counts_three_open_edges() -> None:
    d = diagnose(without_faces(cube(), [0]))
    assert d.boundary_edges == 3
    assert not d.watertight and not d.is_volume
    assert d.nonmanifold_edges == 0 and d.nonmanifold_vertices == 0
    assert d.verdict == "FAIL"
    assert d.defects == ["3 open edge(s)"]


def test_diagnose_one_flipped_face_is_winding_defect_only() -> None:
    m = cube()
    f = m.faces.copy()
    f[0] = f[0][::-1]
    d = diagnose(trimesh.Trimesh(m.vertices, f, process=False))
    assert d.watertight and not d.winding_consistent and not d.is_volume
    assert d.boundary_edges == 0
    assert d.verdict == "FAIL"
    assert d.defects == ["inconsistent winding on 3 edge(s)"]


def test_diagnose_inside_out_cube_has_negative_volume_not_winding_defect() -> None:
    m = cube()
    d = diagnose(trimesh.Trimesh(m.vertices, m.faces[:, ::-1], process=False))
    assert d.watertight and d.winding_consistent and not d.is_volume
    assert d.volume_mm3 == pytest.approx(-CUBE_VOLUME, abs=1e-9)
    assert d.verdict == "FAIL"


def test_diagnose_two_cubes_sharing_an_edge_counts_nonmanifold_edges() -> None:
    a = cube(3)
    b = trimesh.Trimesh(a.vertices + [10.0, 10.0, 0.0], a.faces, process=False)
    d = diagnose(weld([parts_of(a), parts_of(b)]))
    # the 10 mm shared edge is 8 segments of 1.25 mm, each in 2 faces of cube A and 2 of cube B
    assert d.nonmanifold_edges == 8
    assert (
        d.nonmanifold_vertices == 0
    )  # both cubes' fans at those vertices are linked through the shared edge
    assert d.boundary_edges == 0
    assert d.components == 2 and d.debris_shells == 0
    assert d.verdict == "FAIL"


def test_diagnose_two_cubes_touching_at_a_corner_counts_one_pinch_vertex() -> None:
    a = cube(1)
    b = trimesh.Trimesh(a.vertices + [10.0, 10.0, 10.0], a.faces, process=False)
    d = diagnose(weld([parts_of(a), parts_of(b)]))
    assert d.nonmanifold_vertices == 1
    assert d.nonmanifold_edges == 0 and d.boundary_edges == 0
    assert d.verdict == "FAIL"


def test_diagnose_degenerate_faces_counted() -> None:
    m = cube(2)  # edge x=5,y=5 has vertices every 2.5 mm
    v = m.vertices
    on_edge = np.nonzero((np.abs(v[:, 0] - 5) < 1e-9) & (np.abs(v[:, 1] - 5) < 1e-9))[0]
    assert len(on_edge) == 5
    zero_area = on_edge[:3]  # three collinear vertices
    faces = np.vstack([m.faces, [[0, 0, 1]], [zero_area]])
    d = diagnose(trimesh.Trimesh(v, faces, process=False))
    assert d.degenerate_faces == 2
    assert d.verdict == "FAIL"


def test_empty_and_all_degenerate_meshes_fail_with_a_reason_and_do_not_raise() -> None:
    pts = np.zeros((3, 3))
    empty = trimesh.Trimesh(pts, np.zeros((0, 3), dtype=np.int64), process=False)
    flat = trimesh.Trimesh(pts, np.array([[0, 0, 1]]), process=False)
    d = diagnose(empty)
    assert d.verdict == "FAIL" and d.defects == ["no non-degenerate faces"]
    assert diagnose(flat).degenerate_faces == 1
    for bad in (empty, flat):
        r = repair(bad, backend="builtin")
        assert r.verdict == "FAIL" and not r.after.is_volume


def test_diagnose_debris_only_is_warn_with_exact_count() -> None:
    parts = [parts_of(cube())]
    for p in ([20, 0, 0], [20, 5, 0], [20, 0, 5]):
        x, y, z = p
        parts.append(tetra([[x, y, z], [x + 1, y, z], [x, y + 1, z], [x, y, z + 1]]))
    d = diagnose(weld(parts))
    assert d.components == 4
    assert d.debris_shells == 3
    assert d.watertight and d.is_volume
    assert d.verdict == "WARN"


def test_diagnose_big_second_body_is_not_debris() -> None:
    parts = [
        parts_of(cube(1)),
        parts_of(
            trimesh.creation.box(
                (8, 8, 8),
                transform=trimesh.transformations.translation_matrix([30, 0, 0]),
            )
        ),
    ]
    d = diagnose(weld(parts))
    assert d.components == 2 and d.debris_shells == 0
    assert d.verdict == "PASS"


def test_diagnose_vectorised_on_large_mesh() -> None:
    # 82k faces; a per-vertex Python loop would take minutes, the vectorised pass a couple of seconds
    m = trimesh.creation.icosphere(subdivisions=6, radius=20.0)
    d = diagnose(m)
    assert d.faces == 81_920 and d.is_volume and d.verdict == "PASS"


# ------------------------------------------------------------------------------------------- repair


def test_repair_cube_with_three_debris_drops_them_and_keeps_volume() -> None:
    cube_v, cube_f = parts_of(cube(2))
    far = tetra([[20, 0, 0], [21, 0, 0], [20, 1, 0], [20, 0, 1]])
    at_corner = tetra(
        [[-5, -5, 5], [-6, -5, 5], [-5, -6, 5], [-5, -5, 6]]
    )  # shares the cube corner: pinch vertex
    on_edge = tetra(
        [[5, 5, 0], [5, 5, 2.5], [6, 6, 1], [6.3, 6, 2]]
    )  # lies on a cube edge: non-manifold edge
    m = weld([(cube_v, cube_f), far, at_corner, on_edge])
    before = diagnose(m)
    assert (
        before.debris_shells,
        before.nonmanifold_edges,
        before.nonmanifold_vertices,
    ) == (3, 1, 1)
    r = repair(m, backend="builtin")
    assert r.backend == "builtin"
    assert r.after.verdict == "PASS" and r.after.is_volume
    assert r.after.components == 1 and r.after.faces == len(cube_f)
    assert r.after.volume_mm3 == pytest.approx(CUBE_VOLUME, abs=1e-9)
    assert (
        r.max_deviation_mm == 0.0
    )  # only debris was removed, the cube surface is untouched
    assert r.verdict == "PASS"
    assert "dropped 3 debris shell(s)" in r.actions
    # intact surface keeps its exact coordinates (no resampling)
    assert set(map(tuple, np.round(r.mesh.vertices, 9))) == set(
        map(tuple, np.round(cube_v, 9))
    )


def test_repair_deleted_face_fills_hole_with_analytic_volume() -> None:
    r = repair(without_faces(cube(), [0]), backend="builtin")
    assert r.before.boundary_edges == 3
    assert r.after.is_volume and r.after.watertight
    assert r.after.faces == 12
    assert r.after.volume_mm3 == pytest.approx(CUBE_VOLUME, abs=1e-6)
    assert r.verdict == "PASS"
    assert r.max_deviation_mm == 0.0
    assert "filled 1 hole(s)" in r.actions


def test_repair_quad_hole_in_dense_cube_filled_flat() -> None:
    m = cube(2)  # 192 faces
    top = np.nonzero(
        np.isclose(m.triangles_center[:, 2], 5.0 - 0.0)
        & (np.abs(m.triangles_center[:, 0]) < 2.6)
        & (np.abs(m.triangles_center[:, 1]) < 2.6)
    )[0]
    assert len(top) >= 2
    r = repair(without_faces(m, list(top)), backend="builtin")
    assert r.after.is_volume
    assert r.after.volume_mm3 == pytest.approx(
        CUBE_VOLUME, abs=1e-6
    )  # the patch is flat like the face it replaces
    assert r.max_deviation_mm == 0.0


def test_repair_hole_in_curved_surface_stays_close_to_the_original() -> None:
    sphere = trimesh.creation.icosphere(subdivisions=3, radius=10.0)
    cap = np.nonzero(sphere.triangles_center[:, 2] > 9.0)[0]
    assert 3 <= len(cap) <= 100
    r = repair(without_faces(sphere, list(cap)), backend="builtin")
    assert r.after.is_volume
    assert r.after.volume_mm3 == pytest.approx(sphere.volume, rel=0.01)
    assert r.after.volume_mm3 < sphere.volume  # a flat patch lies inside the dome
    assert r.report["loops_filled"] == 1


def test_repair_flipped_face_restores_consistent_winding() -> None:
    m = cube(1)
    for face in (
        0,
        5,
    ):  # also with the root face flipped: the rest must not end up inverted
        f = m.faces.copy()
        f[face] = f[face][::-1]
        r = repair(trimesh.Trimesh(m.vertices, f, process=False), backend="builtin")
        assert r.before.winding_consistent is False
        assert r.after.is_volume and r.after.winding_consistent
        assert r.after.volume_mm3 == pytest.approx(CUBE_VOLUME, abs=1e-9)
        assert r.report["faces_flipped"] == 1
        assert r.max_deviation_mm == 0.0


def test_repair_inside_out_cube_is_turned_outward() -> None:
    m = cube(1)
    r = repair(
        trimesh.Trimesh(m.vertices, m.faces[:, ::-1], process=False), backend="builtin"
    )
    assert r.after.is_volume
    assert r.after.volume_mm3 == pytest.approx(CUBE_VOLUME, abs=1e-9)
    assert r.verdict == "PASS"


def test_repair_degenerate_faces_removed() -> None:
    m = cube(2)
    on_edge = np.nonzero(
        (np.abs(m.vertices[:, 0] - 5) < 1e-9) & (np.abs(m.vertices[:, 1] - 5) < 1e-9)
    )[0]
    faces = np.vstack([m.faces, [[0, 0, 1]], [on_edge[:3]]])
    r = repair(trimesh.Trimesh(m.vertices, faces, process=False), backend="builtin")
    assert r.before.degenerate_faces == 2
    assert r.after.verdict == "PASS" and r.after.faces == len(m.faces)
    assert r.after.volume_mm3 == pytest.approx(CUBE_VOLUME, abs=1e-9)


def test_repair_two_cubes_sharing_an_edge_leaves_two_manifold_bodies() -> None:
    a = cube(3)
    b = trimesh.Trimesh(a.vertices + [10.0, 10.0, 0.0], a.faces, process=False)
    m = weld([parts_of(a), parts_of(b)])
    r = repair(m, backend="builtin")
    assert r.before.nonmanifold_edges == 8
    assert r.after.nonmanifold_edges == 0 and r.after.nonmanifold_vertices == 0
    assert r.after.is_volume and r.after.components == 2
    # the cut removed some material around the shared edge, never added any
    assert 0.9 * 2 * CUBE_VOLUME < r.after.volume_mm3 < 2 * CUBE_VOLUME
    # the edge region really moved, so the repair must not claim to be invisible
    assert r.max_deviation_mm > 0.0
    assert r.verdict == ("WARN" if r.max_deviation_mm > 0.2 else "PASS")


def test_repair_corner_touching_cubes_become_two_manifold_bodies() -> None:
    a = cube(3)
    b = trimesh.Trimesh(a.vertices + [10.0, 10.0, 10.0], a.faces, process=False)
    r = repair(weld([parts_of(a), parts_of(b)]), backend="builtin")
    assert r.before.nonmanifold_vertices == 1
    assert (
        r.after.nonmanifold_vertices == 0
        and r.after.is_volume
        and r.after.components == 2
    )
    assert r.after.volume_mm3 < 2 * CUBE_VOLUME
    # `ring` is a ceiling: this defect is fixed in round 1 with the faces touching the pinch vertex only
    assert r.report["rounds"] == 1
    assert r.report["faces_cut"] == 9
    tight = repair(
        weld([parts_of(a), parts_of(b)]),
        backend="builtin",
        options=MeshFixOptions(ring=0),
    )
    assert tight.report["faces_cut"] == r.report["faces_cut"]
    assert tight.after.volume_mm3 == r.after.volume_mm3


def test_repair_cut_escalates_one_ring_per_round_up_to_ring() -> None:
    a = cube(3)
    b = trimesh.Trimesh(a.vertices + [10.0, 10.0, 0.0], a.faces, process=False)
    m = weld([parts_of(a), parts_of(b)])
    cuts = {
        ring: repair(m, backend="builtin", options=MeshFixOptions(ring=ring))
        for ring in (0, 1, 2)
    }
    assert all(
        r.after.is_volume and r.after.nonmanifold_edges == 0 and r.after.components == 2
        for r in cuts.values()
    )
    cut = {ring: int(r.report["faces_cut"]) for ring, r in cuts.items()}
    # round 1 always cuts with ring 0; this edge needs a second round, which may use one ring more.
    # So a larger ceiling cuts more, but only until the defect is gone: a ceiling of 2 is never reached here.
    assert cut[0] < cut[1]
    assert cut[1] == cut[2]
    assert all(r.report["rounds"] == 2 for r in cuts.values())
    assert cuts[0].max_deviation_mm < cuts[1].max_deviation_mm


def test_repair_leaves_loops_longer_than_max_hole_edges_and_fails() -> None:
    m = cube(2)
    top = np.nonzero(
        np.isclose(m.triangles_center[:, 2], 5.0)
        & (np.abs(m.triangles_center[:, 0]) < 2.6)
        & (np.abs(m.triangles_center[:, 1]) < 2.6)
    )[0]
    broken = without_faces(m, list(top))
    boundary = diagnose(broken).boundary_edges
    assert boundary > 4
    r = repair(
        broken, backend="builtin", options=MeshFixOptions(max_hole_edges=boundary - 1)
    )
    assert r.verdict == "FAIL"
    assert r.after.boundary_edges == boundary
    assert r.report["loops_left"] == 1
    assert r.report["loops_filled"] == 0
    # the same mesh with enough allowance is repaired: the limit is what decided the verdict
    ok = repair(
        broken, backend="builtin", options=MeshFixOptions(max_hole_edges=boundary)
    )
    assert ok.verdict == "PASS"


def test_repair_already_clean_returns_identical_arrays() -> None:
    m = cube(2)
    r = repair(m)
    assert r.actions == ["already clean"]
    assert r.backend == "none"
    assert r.verdict == "PASS" and r.max_deviation_mm == 0.0
    assert np.array_equal(r.mesh.vertices, m.vertices)
    assert np.array_equal(r.mesh.faces, m.faces)
    assert r.mesh is not m  # a copy: callers may mutate it


def test_repair_result_is_a_json_step_result() -> None:
    r = repair(without_faces(cube(), [0]))
    assert isinstance(r, StepResult)
    d = r.to_dict()
    assert "mesh" not in d
    assert json.loads(json.dumps(d)) == d
    assert d["before"]["boundary_edges"] == 3 and d["after"]["boundary_edges"] == 0
    assert d["verdict"] == "PASS" and d["backend"] == "builtin"
    assert "filled 1 hole(s)" in r.summary()
    assert isinstance(r.before, Diagnosis) and isinstance(r.before.summary(), str)
    assert json.loads(json.dumps(r.before.to_dict())) == r.before.to_dict()


def test_builtin_manifold3d_agrees_with_the_diagnosis() -> None:
    r = repair(without_faces(cube(), [0]), backend="builtin")
    assert r.report["manifold_status"] == "NoError"


def test_options_reject_unusable_values() -> None:
    with pytest.raises(InvalidArgument):
        MeshFixOptions(ring=-1)
    with pytest.raises(InvalidArgument):
        MeshFixOptions(max_rounds=0)
    with pytest.raises(InvalidArgument):
        MeshFixOptions(max_hole_edges=2)
    with pytest.raises(InvalidArgument):
        MeshFixOptions(debris_max_faces=-1)
    assert MeshFixOptions() == MeshFixOptions(300, 500, 50.0, 2, 6)


# ------------------------------------------------------------------------------------------- backends


class FakeBackend:
    """Stands in for a third-party engine: records the call and returns a known-good cube."""

    def __init__(self, name: str = "fake-test", ok: bool = True) -> None:
        self.name = name
        self._ok = ok
        self.calls: list[tuple[int, int, MeshFixOptions]] = []

    def available(self) -> bool:
        return self._ok

    def repair(
        self,
        vertices: NDArray[np.float64],
        faces: NDArray[np.int64],
        options: MeshFixOptions,
    ) -> tuple[
        NDArray[np.float64], NDArray[np.int64], dict[str, float | int | bool | str]
    ]:
        self.calls.append((len(vertices), len(faces), options))
        good = cube()
        return (
            np.asarray(good.vertices, dtype=np.float64),
            np.asarray(good.faces, dtype=np.int64),
            {"fake_key": 7, "engine": "fake"},
        )


def test_builtin_and_external_are_registered_at_import() -> None:
    names = [b.name for b in backends()]
    assert names[:2] == ["builtin", "meshfix-lib"]
    assert all(isinstance(b, MeshFixBackend) for b in backends())
    assert BuiltinBackend().available() is True


def test_registered_backend_is_called_by_name_and_its_report_surfaces() -> None:
    fake = FakeBackend()
    register_backend(fake)
    opts = MeshFixOptions(ring=1)
    r = repair(without_faces(cube(), [0]), backend="fake-test", options=opts)
    assert fake.calls == [(8, 11, opts)]
    assert r.backend == "fake-test"
    assert r.after.is_volume and r.verdict == "PASS"
    assert r.report == {"fake_key": 7, "engine": "fake"}
    assert r.to_dict()["report"]["fake_key"] == 7
    assert "fake_key=7" in r.actions


def test_a_backend_that_does_not_fix_the_mesh_yields_fail() -> None:
    class Lazy:
        name = "lazy"

        def available(self) -> bool:
            return True

        def repair(
            self,
            vertices: NDArray[np.float64],
            faces: NDArray[np.int64],
            options: MeshFixOptions,
        ) -> tuple[
            NDArray[np.float64], NDArray[np.int64], dict[str, float | int | bool | str]
        ]:
            return vertices, faces, {}

    register_backend(Lazy())
    r = repair(without_faces(cube(), [0]), backend="lazy")
    assert r.verdict == "FAIL"
    assert r.after.boundary_edges == 3


def test_unknown_backend_name_raises_listing_registered_names() -> None:
    with pytest.raises(InvalidArgument) as exc:
        repair(cube(), backend="nope")
    text = exc.value.render(color=False)
    assert (
        "nope" in text
        and "builtin" in text
        and "meshfix-lib" in text
        and "auto" in text
    )


def test_auto_prefers_available_external_backend_and_skips_unavailable_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules, "mesh_repair", None
    )  # the real library must not win in this test
    broken = without_faces(cube(), [0])
    down = FakeBackend("down", ok=False)
    up = FakeBackend("up", ok=True)
    register_backend(down)
    register_backend(up)
    r = repair(broken)
    assert r.backend == "up"
    assert down.calls == [] and len(up.calls) == 1


def test_external_unavailable_falls_back_to_builtin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules, "mesh_repair", None
    )  # an import of it raises ImportError
    ext = ExternalBackend()
    assert ext.available() is False
    r = repair(without_faces(cube(), [0]))
    assert r.backend == "builtin"
    assert r.verdict == "PASS"
    with pytest.raises(MissingDependency):
        repair(without_faces(cube(), [0]), backend="meshfix-lib")


def test_external_backend_forwards_the_arrays_and_no_builtin_options_to_mesh_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def repair_mesh(
        vertices: NDArray[np.float64], faces: NDArray[np.int64], **opts: object
    ) -> tuple[object, object, object]:
        seen.update(opts)
        seen["shapes"] = (vertices.shape, faces.shape)
        good = cube()
        return (
            good.vertices.astype(np.float32),
            good.faces.astype(np.int32),
            {"filled": np.int64(1)},
        )

    module = types.ModuleType("mesh_repair")
    module.repair_mesh = repair_mesh  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mesh_repair", module)
    opts = MeshFixOptions(max_hole_edges=50, ring=3)
    assert ExternalBackend().available() is True
    r = repair(without_faces(cube(), [0]), options=opts)  # 'auto' picks it
    assert r.backend == "meshfix-lib"
    assert (
        "max_hole_edges" not in seen and "ring" not in seen
    )  # the library's keyword set is its own
    assert seen["shapes"] == ((8, 3), (11, 3))
    assert r.mesh.vertices.dtype == np.float64 and r.mesh.faces.dtype == np.int64
    assert r.after.is_volume
    assert json.loads(json.dumps(r.to_dict()))["report"] == {"filled": 1}


def test_registering_same_name_replaces_and_unregister_removes() -> None:
    register_backend(FakeBackend("dup"))
    second = FakeBackend("dup")
    register_backend(second)
    assert [b.name for b in backends()].count("dup") == 1
    assert next(b for b in backends() if b.name == "dup") is second
    unregister_backend("dup")
    assert "dup" not in [b.name for b in backends()]
    assert any(isinstance(b, BuiltinBackend) for b in backends())
