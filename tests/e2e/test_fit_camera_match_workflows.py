from __future__ import annotations

import json
from pathlib import Path

from .workflow_helper import require_python_module, require_working_openscad, run_cli, run_shell, write_pgm


def _camera_model(path: Path) -> None:
    path.write_text(
        """
width = 10; // [6:20]
depth = 8; // [6:20]
height = 6; // [4:16]
cube([width, depth, height]);
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_fit_camera_quick_search_writes_pose_and_overlay(tmp_path: Path) -> None:
    """A user locks a camera pose before comparing future silhouette scores."""
    require_working_openscad()
    require_python_module("numpy")
    require_python_module("PIL")
    model = tmp_path / "block.scad"
    reference = tmp_path / "ref.pgm"
    camera = tmp_path / "match" / "camera.json"
    _camera_model(model)
    write_pgm(
        reference,
        [
            "0000000000",
            "0011111100",
            "0011111100",
            "0011111100",
            "0011111100",
            "0000000000",
        ],
    )

    result = run_cli(
        tmp_path,
        "fit-camera",
        str(model),
        str(reference),
        "--out",
        str(camera),
        "--rand",
        "2",
        "--refine",
        "1",
        "--opt-size",
        "80x48",
        "--final-size",
        "80x48",
        timeout=240,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(camera.read_text(encoding="utf-8"))
    assert set(payload) >= {"camera", "camera_arg", "iou", "ssim"}
    assert len(payload["camera"]) == 6
    assert isinstance(payload["iou"], float)
    assert isinstance(payload["ssim"], float)
    assert camera.with_name("camera_fit.png").exists()
    assert camera.with_name("camera_overlay.png").exists()


def test_match_explains_when_no_tunable_constants_exist(tmp_path: Path) -> None:
    """A user gets a clear match-loop stop when the constants file has no editable scalars."""
    reference = tmp_path / "ref.pgm"
    model = tmp_path / "literal.scad"
    write_pgm(reference, ["000", "010", "000"])
    model.write_text("cube([4, 4, 4]);\n", encoding="utf-8")

    result = run_cli(tmp_path, "match", str(model), str(reference), "--rounds", "1", "--dry-run")

    assert result.returncode == 2
    assert "no numeric scalar constants found to tune" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr


def test_match_failure_can_be_redirected_into_a_handoff_log(tmp_path: Path) -> None:
    """A user captures match-loop diagnostics as a handoff artifact for model cleanup."""
    reference = tmp_path / "ref.pgm"
    model = tmp_path / "literal.scad"
    write_pgm(reference, ["000", "010", "000"])
    model.write_text("cube([4, 4, 4]);\n", encoding="utf-8")

    result = run_shell(
        f'"$PYTHON" "$THREED" match "{model}" "{reference}" --rounds 1 --dry-run > match.log; '
        'printf "%s" "$?" > status.txt',
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "status.txt").read_text(encoding="utf-8") == "2"
    log = (tmp_path / "match.log").read_text(encoding="utf-8")
    assert "match: no numeric scalar constants found to tune" in log
