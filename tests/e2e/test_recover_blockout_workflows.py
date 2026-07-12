"""Readable e2e stories for the closed image->3D parametric-recovery loop."""
from __future__ import annotations

import json
from pathlib import Path

from .workflow_helper import (
    png_size,
    require_cli_python_deps,
    require_working_openscad,
    run_cli,
    write_pgm,
)

_HEAVY = (["numpy", "pillow", "scipy"], ["numpy", "PIL", "scipy"])


def test_synthetic_parametric_recovery_closes_the_loop(tmp_path: Path) -> None:
    """A user proves the whole image->3D loop on a synthetic same-family target: recover
    hidden blockout params from a rendered reference without ever leaking them to the
    fitter, and get a durable ok label plus a 6-artifact proof panel."""
    require_working_openscad()
    require_cli_python_deps(tmp_path, *_HEAVY)
    out = tmp_path / "recover"

    result = run_cli(
        tmp_path, "recover-blockout", "--synthetic",
        "--out", str(out), "--size", "200x160", timeout=240,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STATUS=ok" in result.stdout

    payload = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert payload["recovery_status"] == "ok"
    assert payload["proven"] is True
    assert payload["fit_status"] == "ok"
    assert payload["veto"]["passed"] is True

    synthetic = payload["synthetic"]
    assert synthetic["within_tolerance"] is True
    assert synthetic["error_reduced"] is True
    # the recovery genuinely moved every continuous dimension toward the hidden truth
    for name, row in synthetic["per_param"].items():
        assert row["abs_error"] <= row["tolerance"], f"{name} outside tolerance: {row}"
        assert row["abs_error"] < row["start_error"], f"{name} did not improve: {row}"
    # the discrete feature came from the (mock) veto and matches the hidden count
    assert payload["expected_columns"] == int(synthetic["hidden_params"]["n_columns"])

    # 6-artifact proof panel: reference, mask, recovered render, contour error, metrics, status
    panel = Path(payload["proof_panel"])
    assert panel.exists()
    assert png_size(panel) == (200 * 3, (160 + 22) * 2)
    assert (out / "recovered_render.png").exists()
    assert (out / "reference.png").exists()
    assert (out / "reference_mask.png").exists()


def test_synthetic_recovery_records_changelog_of_accepted_edits(tmp_path: Path) -> None:
    """The monotonic refine leaves an auditable changelog: one accepted edit per row."""
    require_working_openscad()
    require_cli_python_deps(tmp_path, *_HEAVY)
    out = tmp_path / "recover"

    result = run_cli(
        tmp_path, "recover-blockout", "--synthetic",
        "--out", str(out), "--size", "200x160", timeout=240,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    changelog = (out / "changelog.md").read_text(encoding="utf-8")
    assert "match loop — changelog" in changelog
    accepted = [line for line in changelog.splitlines() if "**ok**" in line]
    assert accepted, "expected at least one accepted refine step in the changelog"


def test_fit_camera_seed_from_viewbank_reports_seeds_and_writes_pose(tmp_path: Path) -> None:
    """A user opts into view-bank seeding so the pose search starts from a coarse az/el
    grid instead of only random samples."""
    require_working_openscad()
    require_cli_python_deps(tmp_path, ["numpy", "pillow"], ["numpy", "PIL"])
    model = tmp_path / "block.scad"
    model.write_text(
        "width = 12; depth = 8; height = 6;\ncube([width, depth, height]);\n",
        encoding="utf-8",
    )
    reference = tmp_path / "ref.pgm"
    write_pgm(
        reference,
        [
            "0000000000",
            "0011111100",
            "0011111100",
            "0011111100",
            "0000000000",
        ],
    )
    camera = tmp_path / "vb" / "camera.json"

    result = run_cli(
        tmp_path, "fit-camera", str(model), str(reference),
        "--out", str(camera), "--seed-from-viewbank",
        "--rand", "2", "--refine", "1", "--opt-size", "80x48", "--final-size", "80x48",
        timeout=240,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "view-bank seeds=" in result.stdout
    payload = json.loads(camera.read_text(encoding="utf-8"))
    assert len(payload["camera"]) == 6
    assert camera.with_name("camera_fit.png").exists()
