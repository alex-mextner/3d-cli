"""`3d mesh deform` / `3d mesh fit-silhouette` through bin/3d: the workflow a user runs, with its gates and artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pytest
from geometry.meshfit import meshio, raster
from meshfit_fixtures import cube_sphere, write_stl

from .workflow_helper import REPO_ROOT, run_cli

SPEC = """\
seed: 5
regions:
  eye: {sphere: {center: [20, 0, 0], radius: 6}, falloff: 4}
  crown: {zrange: [8, 30], falloff: 6}
keepout: [eye]
ops:
  - {op: smooth, region: crown, iterations: 3}
  - {op: inflate, region: crown, amount: 0.4}
  - {op: fur, amplitude: 0.3, pitch: 3.0, length: 6.0, overhang_safe: true}
  - {op: remesh, target_edge: 1.0}
"""


def _model(tmp_path: Path) -> Path:
    V, F = cube_sphere(30, 20.0)
    path = tmp_path / "ball.stl"
    write_stl(path, V, F)
    (tmp_path / "spec.yaml").write_text(SPEC)
    return path


def test_deform_writes_the_mesh_report_and_summary_with_the_keepout_gate(
    tmp_path: Path,
) -> None:
    """A user deforms a model: it writes the mesh, report and summary with the keep-out gate passing."""
    model = _model(tmp_path)
    result = run_cli(
        tmp_path,
        "mesh",
        "deform",
        str(model),
        "spec.yaml",
        "-o",
        "out.stl",
        "--report",
        "report.json",
        "--verify-determinism",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert ">>> DEFORM: PASS" in result.stdout
    assert re.search(
        r"keep-out: \d+ locked vertices, .* max displacement 0\.0 mm .* -> PASS",
        result.stdout,
    )
    assert "determinism: identical over two runs" in result.stdout
    report = json.loads((tmp_path / "report.json").read_text())
    assert (
        report["status"] == "PASS" and report["keepout"]["max_displacement_mm"] == 0.0
    )
    assert [op["op"] for op in report["ops"]] == ["smooth", "inflate", "fur", "remesh"]
    assert report["watertight_before"] is True and report["watertight_after"] is True
    assert (
        report["output"]["sha256"]
        == hashlib.sha256((tmp_path / "out.stl").read_bytes()).hexdigest()
    )
    assert (
        report["determinism"]["identical"] is True
        and report["output"]["faces"] > report["input"]["faces"]
    )
    # the protected zone of the INPUT file is still, bit for bit, in the OUTPUT file
    before, _ = meshio.load_mesh(str(model))
    after, _ = meshio.load_mesh(str(tmp_path / "out.stl"))
    zone = before[np.linalg.norm(before - [20.0, 0.0, 0.0], axis=1) <= 6.0]
    assert len(zone) > 20 and set(map(tuple, zone.tolist())) <= set(
        map(tuple, after.tolist())
    )


def test_same_spec_gives_the_same_sha256_regardless_of_thread_count(
    tmp_path: Path,
) -> None:
    """A user deforms with different thread counts and gets the same sha256."""
    model = _model(tmp_path)
    shas = []
    for threads in ("1", "8"):
        out = tmp_path / f"out_{threads}.stl"
        run = run_cli(
            tmp_path,
            "mesh",
            "deform",
            str(model),
            "spec.yaml",
            "-o",
            str(out),
            "--no-self-intersections",
            env_extra={
                "OMP_NUM_THREADS": threads,
                "OPENBLAS_NUM_THREADS": threads,
                "MKL_NUM_THREADS": threads,
            },
        )
        assert run.returncode == 0, run.stderr
        shas.append(hashlib.sha256(out.read_bytes()).hexdigest())
    assert shas[0] == shas[1]


def test_3mf_output_is_reproducible(tmp_path: Path) -> None:
    """A user exports 3MF twice from one spec and gets byte-identical files."""
    model = _model(tmp_path)
    for name in ("a", "b"):
        assert (
            run_cli(
                tmp_path,
                "mesh",
                "deform",
                str(model),
                "spec.yaml",
                "-o",
                f"{name}.3mf",
                "--no-self-intersections",
            ).returncode
            == 0
        )
    assert (tmp_path / "a.3mf").read_bytes() == (tmp_path / "b.3mf").read_bytes()


def test_a_vertex_moving_op_next_to_a_keepout_zone_fails_the_gate_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """A user's decimate op moves vertices near a keep-out zone: the gate fails and no file is written."""
    model = _model(tmp_path)
    (tmp_path / "bad.yaml").write_text(
        SPEC.replace("{op: remesh, target_edge: 1.0}", "{op: decimate, tolerance: 0.1}")
    )
    result = run_cli(
        tmp_path, "mesh", "deform", str(model), "bad.yaml", "-o", "never.stl"
    )
    assert result.returncode == 1
    assert "keep-out" in result.stderr and "keepout_tolerance_mm" in result.stderr
    assert not (tmp_path / "never.stl").exists()


def test_invalid_spec_and_missing_files_are_usage_errors(tmp_path: Path) -> None:
    """A user mistypes a spec key or points at a missing file and gets a usage error naming it."""
    model = _model(tmp_path)
    (tmp_path / "typo.yaml").write_text("ops:\n  - {op: smooth, iteratoins: 3}\n")
    typo = run_cli(tmp_path, "mesh", "deform", str(model), "typo.yaml")
    assert typo.returncode == 2
    assert "unknown keys" in typo.stderr
    assert "iteratoins" in typo.stderr
    missing = run_cli(
        tmp_path, "mesh", "deform", str(tmp_path / "nope.stl"), "spec.yaml"
    )
    assert missing.returncode == 2
    assert "file not found" in missing.stderr


def test_help_documents_every_flag_and_the_docs_page_matches() -> None:
    """A user reads the help text and the docs page: both document every flag and op."""
    from commands import mesh

    usage = mesh.DEFORM_USAGE
    doc = (REPO_ROOT / "docs" / "commands" / "mesh.md").read_text(encoding="utf-8")
    flags = set(re.findall(r"^\s+(--[a-z-]+)", usage, flags=re.M)) | {"-o", "--out"}
    assert {
        "--report",
        "--collage",
        "--verify-determinism",
        "--seed",
        "--no-self-intersections",
        "--json",
    } <= flags
    for flag in sorted(flags):
        assert flag in doc, (
            f"{flag} is in `3d mesh deform --help` but not in docs/commands/mesh.md"
        )
    for op in (
        "smooth",
        "inflate",
        "draw",
        "pinch",
        "crease",
        "scale_local",
        "symmetrize",
        "grab",
        "fur",
        "remesh",
        "decimate",
        "repair",
        "fit_silhouette",
    ):
        assert op in usage and op in doc


def test_help_subcommand_prints_the_deform_usage(tmp_path: Path) -> None:
    """A user runs `3d mesh deform --help` and sees the flags and keep-out gate."""
    result = run_cli(tmp_path, "mesh", "deform", "--help")
    assert result.returncode == 0
    assert "--verify-determinism" in result.stdout
    assert "keep-out" in result.stdout


def test_collage_and_fit_silhouette_workflow(tmp_path: Path) -> None:
    """A user fits a mesh to a silhouette, writes a collage, and gets a usage error without a fit op."""
    pytest.importorskip("PIL")
    from PIL import Image

    V, F = cube_sphere(30, 20.0)
    write_stl(tmp_path / "ball.stl", V, F)
    truth = V.copy()
    near = np.linalg.norm(V - [20.0, 0.0, 0.0], axis=1) < 9.0
    truth[near] += [3.0, 0.0, 0.0] * (
        1.0 - np.linalg.norm(V[near] - [20.0, 0.0, 0.0], axis=1) / 9.0
    )[:, None]
    cam = raster.ortho_camera(
        np.array([0.0, 1.0, 0.0]),
        np.array([0.0, 0.0, 1.0]),
        np.zeros(3),
        0.25,
        200,
        200,
    )
    Image.fromarray((raster.silhouette(cam, truth, F) * 255).astype(np.uint8)).save(
        tmp_path / "front.png"
    )
    (tmp_path / "fit.yaml").write_text(
        """\
seed: 1
regions:
  nose: {sphere: {center: [20, 0, 0], radius: 3}}
ops:
  - op: fit_silhouette
    views:
      - {name: front, mask: front.png, ortho: {direction: [0, 1, 0], up: [0, 0, 1], center: [0, 0, 0], mm_per_px: 0.25}}
    handles:
      - {region: nose, direction: [1, 0, 0], max_move: 6}
    influence: 14
    step_mm: 1.5
"""
    )
    result = run_cli(
        tmp_path,
        "mesh",
        "fit-silhouette",
        "ball.stl",
        "fit.yaml",
        "-o",
        "fit.stl",
        "--collage",
        "fit.png",
        "--no-self-intersections",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "iter  step_mm   IoU" in result.stdout and "heuristic" in result.stdout
    report = json.loads((tmp_path / "fit.stl.report.json").read_text())
    fit_op = report["ops"][0]
    assert (
        fit_op["iou_after"] > fit_op["iou_before"]
        and fit_op["handle_travel_mm"][0] > 1.0
    )
    assert (tmp_path / "fit.png").read_bytes()[:4] == b"\x89PNG"
    (tmp_path / "plain.yaml").write_text("ops:\n  - {op: smooth}\n")
    no_fit = run_cli(
        tmp_path, "mesh", "fit-silhouette", "ball.stl", "plain.yaml", "-o", "x.stl"
    )
    assert no_fit.returncode == 2 and "no `fit_silhouette` op" in no_fit.stderr
