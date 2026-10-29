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
    """A user renders a plate of separate parts and gets per-part footprint dimensions, then gates the sidecar with `--check --require-dims`."""
    model = write_3mf(
        tmp_path / "plate.3mf", [Box("left", 0, RED), Box("right", 60, GREEN)]
    )
    spec = tmp_path / "dims.json"
    spec.write_text(
        json.dumps(
            [
                {
                    "name": "gap",
                    "kind": "gap",
                    "parts": ["left", "right"],
                    "from": [10, 5, 10],
                    "to": [60, 5, 10],
                }
            ]
        ),
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
    """A user with a Y-up model renders it with `--up y --overall always` and the sidecar records the axis."""
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
    """A user mistypes `--up`, `--overall` or `--dims-from` and gets a usage error naming the flag, not a traceback."""
    post = tmp_path / "post.stl"
    trimesh.creation.box(extents=(10, 40, 10)).export(post)
    done = run_cli(
        tmp_path, "dimension", str(post), "-o", str(tmp_path / "x.png"), flag, value
    )
    assert done.returncode == 2
    assert flag in done.stderr
    assert "Traceback" not in done.stderr


def test_a_declared_measurement_that_fits_no_view_exits_2(tmp_path: Path) -> None:
    """A user declares a measurement no view can show and is told which one, with exit 2."""
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
    assert done.returncode == 2
    assert "eye" in done.stderr
    assert "Traceback" not in done.stderr


def test_require_dims_is_only_for_check(tmp_path: Path) -> None:
    """A user passing `--require-dims` to a render is told it belongs to `--check`."""
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
    assert done.returncode == 2
    assert "belongs to --check" in done.stderr


def test_a_dim_across_separate_parts_exits_2_naming_both_and_a_declared_gap_passes(
    tmp_path: Path,
) -> None:
    """A user dimensioning across two separate parts is refused naming both, while a dimension inside one part is drawn and passes `--check`."""
    model = write_3mf(
        tmp_path / "plate.3mf", [Box("left", 0, RED), Box("right", 60, GREEN)]
    )
    refused = run_cli(
        tmp_path, "dimension", str(model), "-o", str(tmp_path / "x.png"),
        "--dim", "across:5,5,5:65,5,5",
    )  # fmt: skip
    assert refused.returncode == 2
    assert "Traceback" not in refused.stderr
    assert (
        "left" in refused.stderr
        and "right" in refused.stderr
        and "kind: gap" in refused.stderr
    )
    assert not (tmp_path / "x.png").exists()
    within = tmp_path / "ok.png"
    ok = run_cli(
        tmp_path,
        "dimension",
        str(model),
        "-o",
        str(within),
        "--dim",
        "edge:0,5,5:10,5,5",
    )
    assert ok.returncode == 0, ok.stderr
    assert run_cli(tmp_path, "dimension", "--check", str(within)).returncode == 0


def test_check_fails_a_sidecar_that_spans_separate_parts(tmp_path: Path) -> None:
    """A user's `--check` fails a sidecar whose dimension spans separate parts."""
    model = write_3mf(
        tmp_path / "plate.3mf", [Box("left", 0, RED), Box("right", 60, GREEN)]
    )
    out = tmp_path / "p.png"
    assert run_cli(tmp_path, "dimension", str(model), "-o", str(out)).returncode == 0
    side = Path(f"{out}.dims.json")
    data = json.loads(side.read_text(encoding="utf-8"))
    data["dimensions"][0]["spans_separated"] = True
    side.write_text(json.dumps(data), encoding="utf-8")
    failed = run_cli(tmp_path, "dimension", "--check", str(out))
    assert failed.returncode == 1
    assert "spans-separated-parts" in failed.stdout
