"""`3d dimension` --up / --overall / --dims-from / --check --require-dims through bin/3d (#594-#596)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from dimension_fixtures import GREEN, RED, Box, write_3mf

from .workflow_helper import run_cli

trimesh = pytest.importorskip("trimesh")


def _sidecar(image: Path) -> dict:  # type: ignore[type-arg]
    return json.loads(Path(f"{image}.dims.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_plate_render_has_per_part_dims_and_declared_measurements_gate(
    tmp_path: Path,
) -> None:
    model = write_3mf(
        tmp_path / "plate.3mf", [Box("left", 0, RED), Box("right", 60, GREEN)]
    )
    spec = tmp_path / "dims.json"
    spec.write_text(
        json.dumps([{"name": "gap", "from": [10, 5, 5], "to": [60, 5, 5]}]),
        encoding="utf-8",
    )
    out = tmp_path / "plate.png"
    done = run_cli(
        tmp_path, "dimension", str(model), "-o", str(out), "--dims-from", str(spec)
    )
    assert done.returncode == 0, done.stderr
    data = _sidecar(out)
    assert data["overall"] == "per-part" and data["required_dims"] == ["gap"]
    checked = run_cli(tmp_path, "dimension", "--check", str(out), "--require-dims")
    assert checked.returncode == 0, checked.stdout
    plain = tmp_path / "plain.png"
    assert run_cli(tmp_path, "dimension", str(model), "-o", str(plain)).returncode == 0
    refused = run_cli(tmp_path, "dimension", "--check", str(plain), "--require-dims")
    assert refused.returncode == 1 and "no-required-dims" in refused.stdout


def test_up_and_overall_flags_reach_the_renderer(tmp_path: Path) -> None:
    post = tmp_path / "post.stl"
    trimesh.creation.box(extents=(10, 40, 10)).export(post)
    out = tmp_path / "post.png"
    done = run_cli(
        tmp_path,
        "dimension",
        str(post),
        "-o",
        str(out),
        "--up",
        "y",
        "--overall",
        "always",
    )
    assert done.returncode == 0, done.stderr
    assert _sidecar(out)["up"] == "Y"
    assert run_cli(tmp_path, "dimension", "--check", str(out)).returncode == 0


@pytest.mark.parametrize(
    ("flag", "value"),
    [("--up", "W"), ("--overall", "sometimes"), ("--dims-from", "missing.json")],
)
def test_bad_values_are_structured_usage_errors(
    tmp_path: Path, flag: str, value: str
) -> None:
    post = tmp_path / "post.stl"
    trimesh.creation.box(extents=(10, 40, 10)).export(post)
    done = run_cli(
        tmp_path, "dimension", str(post), "-o", str(tmp_path / "x.png"), flag, value
    )
    assert (
        done.returncode == 2 and flag in done.stderr and "Traceback" not in done.stderr
    )


def test_a_declared_measurement_that_fits_no_view_exits_2(tmp_path: Path) -> None:
    post = tmp_path / "post.stl"
    trimesh.creation.box(extents=(10, 40, 10)).export(post)
    spec = tmp_path / "dims.json"
    spec.write_text(
        json.dumps([{"name": "eye", "from": [0, 0, 0], "to": [5, -5, 7.0711]}]),
        encoding="utf-8",
    )
    done = run_cli(
        tmp_path,
        "dimension",
        str(post),
        "-o",
        str(tmp_path / "x.png"),
        "--dims-from",
        str(spec),
    )
    assert done.returncode == 2 and "eye" in done.stderr


def test_require_dims_is_only_for_check(tmp_path: Path) -> None:
    post = tmp_path / "post.stl"
    trimesh.creation.box(extents=(10, 40, 10)).export(post)
    done = run_cli(
        tmp_path,
        "dimension",
        str(post),
        "-o",
        str(tmp_path / "x.png"),
        "--require-dims",
    )
    assert done.returncode == 2 and "belongs to --check" in done.stderr
