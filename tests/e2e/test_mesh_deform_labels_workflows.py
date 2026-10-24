"""`3d mesh deform --labels`: refine a patch of a labelled mesh and the labels follow, as a user runs it through bin/3d."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from geometry.meshfit import meshio
from meshfit_fixtures import cube_sphere, write_stl

from .workflow_helper import run_cli

SPEC = """\
seed: 1
regions:
  lap: {box: [-4, 4, -30, -10, -30, 30]}
  socket: {sphere: {center: [0, -20, 8], radius: 5}, falloff: 4}
ops:
  - {op: remesh, region: lap, max_edge: 1.0}
  - {op: displace, region: socket, direction: [0, -1, 0], amount: 1.0}
"""


def _areas(V: np.ndarray, F: np.ndarray) -> np.ndarray:
    t = V[F]
    return np.asarray(
        0.5 * np.linalg.norm(np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]), axis=1)
    )


def _setup(tmp_path: Path) -> tuple[Path, np.ndarray]:
    V, F = cube_sphere(16, 20.0)
    model = tmp_path / "ball.stl"
    write_stl(model, V, F)
    V, F = meshio.load_mesh(
        str(model)
    )  # the mesh as `mesh deform` loads it: labels belong to these faces
    labels = (V[F].mean(axis=1)[:, 2] > 5).astype(np.int64) + 2 * (
        V[F].mean(axis=1)[:, 1] < 0
    )
    np.savez(
        tmp_path / "ball.labels.npz",
        face_label=labels,
        face_group=labels * 10
        + 1,  # a second per-face array that must follow the same faces
        names=json.dumps(["skin", "top", "front", "front_top"]),
    )
    (tmp_path / "spec.yaml").write_text(SPEC)
    return model, labels


def test_labels_are_carried_to_the_refined_faces_and_extra_arrays_are_copied(
    tmp_path: Path,
) -> None:
    model, labels = _setup(tmp_path)
    result = run_cli(
        tmp_path,
        "mesh",
        "deform",
        str(model),
        "spec.yaml",
        "-o",
        "out.stl",
        "--labels",
        "ball.labels.npz",
        "--out-labels",
        "out.labels.npz",
        "--no-self-intersections",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "labels:" in result.stdout and "4 classes" in result.stdout
    V0, F0 = meshio.load_mesh(str(model))
    V1, F1 = meshio.load_mesh(str(tmp_path / "out.stl"))
    with np.load(tmp_path / "out.labels.npz") as saved:
        out_labels = saved["face_label"]
        assert np.array_equal(saved["face_group"], out_labels * 10 + 1)
        assert json.loads(str(saved["names"])) == ["skin", "top", "front", "front_top"]
    assert len(F1) > len(F0) and len(out_labels) == len(F1)
    assert set(np.unique(out_labels)) == set(np.unique(labels))
    # the refined patch took labels from its parent faces: it lies on the -Y side, labels 2 and 3 only
    centre = V1[F1].mean(axis=1)
    patch = (np.abs(centre[:, 0]) < 3.0) & (centre[:, 1] < -12.0)
    assert patch.sum() > 100 and set(np.unique(out_labels[patch])) <= {2, 3}
    report = json.loads((tmp_path / "out.stl.report.json").read_text())
    assert report["labels"]["faces_in"] == len(F0) and report["labels"][
        "faces_out"
    ] == len(F1)
    assert report["ops"][0]["longest_edge_in_zone_mm"] <= 1.0
    # untouched faces keep their label: the +Y half is far from both ops
    back_in = labels[V0[F0].mean(axis=1)[:, 1] > 8]
    back_out = out_labels[centre[:, 1] > 8]
    assert len(back_in) == len(back_out) and np.array_equal(
        np.sort(back_in), np.sort(back_out)
    )


def test_label_count_that_does_not_match_the_loaded_mesh_is_an_input_error(
    tmp_path: Path,
) -> None:
    model, labels = _setup(tmp_path)
    np.savez(tmp_path / "short.npz", face_label=labels[:-5])
    result = run_cli(
        tmp_path, "mesh", "deform", str(model), "spec.yaml", "--labels", "short.npz"
    )
    assert result.returncode == 2
    assert (
        f"{len(labels) - 5} face labels but the mesh has {len(labels)} faces"
        in result.stderr
    )
    assert not (tmp_path / "ball.deformed.stl").exists()


def test_labels_with_an_op_that_cannot_map_faces_is_refused_before_anything_is_written(
    tmp_path: Path,
) -> None:
    model, _ = _setup(tmp_path)
    (tmp_path / "decimate.yaml").write_text(
        "seed: 1\nops:\n  - {op: decimate, tolerance: 0.05}\n"
    )
    result = run_cli(
        tmp_path,
        "mesh",
        "deform",
        str(model),
        "decimate.yaml",
        "-o",
        "out.stl",
        "--labels",
        "ball.labels.npz",
    )
    assert result.returncode == 2
    assert "cannot carry per-face labels" in result.stderr
    assert not (tmp_path / "out.stl").exists()


def test_out_labels_without_labels_and_missing_face_label_array_are_usage_errors(
    tmp_path: Path,
) -> None:
    model, _ = _setup(tmp_path)
    alone = run_cli(
        tmp_path, "mesh", "deform", str(model), "spec.yaml", "--out-labels", "x.npz"
    )
    assert alone.returncode == 2 and "--out-labels needs --labels" in alone.stderr
    np.savez(tmp_path / "wrong.npz", other=np.arange(3))
    wrong = run_cli(
        tmp_path, "mesh", "deform", str(model), "spec.yaml", "--labels", "wrong.npz"
    )
    assert wrong.returncode == 2 and "no `face_label` array" in wrong.stderr


def test_help_documents_the_label_options_and_the_new_op() -> None:
    from commands.mesh import DEFORM_USAGE

    for word in ("--labels FILE.npz", "--out-labels FILE.npz", "displace", "max_edge"):
        assert word in DEFORM_USAGE
    doc = (
        Path(__file__).resolve().parents[2] / "docs" / "commands" / "mesh.md"
    ).read_text()
    for word in (
        "`--labels FILE.npz`",
        "`--out-labels FILE.npz`",
        "`displace`",
        "max_edge",
    ):
        assert word in doc
