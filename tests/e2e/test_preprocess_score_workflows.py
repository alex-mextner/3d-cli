from __future__ import annotations

from pathlib import Path

from .workflow_helper import key_value_lines, require_binary, require_python_module, run_cli, run_shell, write_pgm


def test_score_masks_outputs_metrics_and_overlay_artifact(tmp_path: Path) -> None:
    """A user compares two ready masks and receives parseable fit metrics plus an overlay."""
    require_binary("magick")
    reference = tmp_path / "reference.pgm"
    candidate = tmp_path / "candidate.pgm"
    outdir = tmp_path / "score"
    write_pgm(
        reference,
        [
            "000000",
            "011100",
            "011100",
            "011100",
            "000000",
        ],
    )
    write_pgm(
        candidate,
        [
            "000000",
            "001110",
            "001110",
            "001110",
            "000000",
        ],
    )

    result = run_cli(tmp_path, "score", str(candidate), str(reference), "--masks", "-o", str(outdir))

    assert result.returncode == 0, result.stderr
    metrics = key_value_lines(result.stdout)
    assert metrics["FRAME"] == "6x5"
    assert metrics["AE"] == "6"
    assert float(metrics["IoU"]) == 0.5
    assert (outdir / "overlay.png").exists()
    assert metrics["OVERLAY"] == str(outdir / "overlay.png")


def test_score_metrics_are_easy_to_filter_in_a_shell_pipeline(tmp_path: Path) -> None:
    """A user pipes score output into normal text tooling to keep only the IoU."""
    require_binary("magick")
    write_pgm(tmp_path / "a.pgm", ["000", "010", "000"])
    write_pgm(tmp_path / "b.pgm", ["000", "010", "000"])

    result = run_shell(
        '"$PYTHON" "$THREED" score a.pgm b.pgm --masks -o score-out | '
        "awk -F= '/^IoU=/{print $2}' > iou.txt",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "iou.txt").read_text(encoding="utf-8") == "1.0000\n"
    assert (tmp_path / "score-out" / "overlay.png").exists()


def test_preprocess_fallback_writes_mask_depth_and_quality_summary(tmp_path: Path) -> None:
    """A user preprocesses a reference image and inspects the generated mask/depth files."""
    require_python_module("cv2")
    require_python_module("PIL")
    require_python_module("numpy")
    require_binary("magick")
    ref = tmp_path / "reference.png"
    outdir = tmp_path / "pre"
    result_image = run_shell(
        "magick -size 48x32 xc:white -fill black -draw 'rectangle 12,8 34,24' reference.png",
        tmp_path,
    )
    assert result_image.returncode == 0, result_image.stderr

    result = run_cli(tmp_path, "preprocess", str(ref), "-o", str(outdir), "--force-fallback", timeout=180)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (outdir / "mask.png").exists()
    assert (outdir / "depth.png").exists()
    assert "mask.png" in result.stdout
    assert "coverage" in result.stdout
    assert "bbox_xywh" in result.stdout
