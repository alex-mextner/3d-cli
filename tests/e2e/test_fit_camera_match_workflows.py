from __future__ import annotations

import json
from pathlib import Path

from .workflow_helper import (
    key_value_lines,
    png_size,
    require_cli_python_deps,
    require_imagemagick,
    require_python_module,
    require_working_openscad,
    run_cli,
    run_shell,
    write_pgm,
)


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


def _reference_proxy_model(path: Path) -> None:
    path.write_text(
        """
// A deliberately asymmetric "reference object": low base, right-side tower, front lip.
union() {
  cube([28, 18, 6]);
  translate([18, 4, 6]) cube([8, 6, 14]);
  translate([0, 0, 6]) cube([9, 4, 5]);
}
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _bad_generated_proxy_model(path: Path) -> None:
    path.write_text(
        """
// Plausible image-to-3D failure: similar bulk, but the tower/lip are on the wrong side.
union() {
  cube([28, 18, 6]);
  translate([2, 10, 6]) cube([8, 6, 14]);
  translate([19, 14, 6]) cube([9, 4, 5]);
}
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _write_original_reference_ppm(path: Path) -> None:
    rows: list[str] = []
    width = 10
    height = 6
    for y in range(height):
        for x in range(width):
            if 2 <= x <= 7 and 1 <= y <= 4:
                rows.append(f"{40 + x * 8} {80 + y * 10} {150 + x * 4}")
            else:
                rows.append(f"{215 - y * 7} {220 - x * 3} {205 + y * 5}")
    path.write_text(f"P3\n{width} {height}\n255\n" + "\n".join(rows) + "\n", encoding="ascii")


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


def test_fit_camera_proof_mode_writes_fail_closed_status_and_artifacts(tmp_path: Path) -> None:
    """Proof mode writes reusable audit artifacts even when the fit is not accepted."""
    require_working_openscad()
    require_python_module("numpy")
    require_python_module("PIL")
    require_python_module("scipy")
    model = tmp_path / "block.scad"
    reference = tmp_path / "ref.pgm"
    proof_reference = tmp_path / "original_ref.pgm"
    camera = tmp_path / "proof" / "camera.json"
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
    _write_original_reference_ppm(proof_reference)

    result = run_cli(
        tmp_path,
        "fit-camera",
        str(model),
        str(reference),
        "--mask-polarity",
        "light",
        "--proof-reference",
        str(proof_reference),
        "--search-mode",
        "proof",
        "--out",
        str(camera),
        "--rand",
        "1",
        "--refine",
        "0",
        "--opt-size",
        "48x32",
        "--final-size",
        "48x32",
        timeout=240,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(camera.read_text(encoding="utf-8"))
    assert payload["objective"] == "contour"
    assert payload["proof_reference"] == str(proof_reference)
    assert payload["fit_status"] in {"ok", "warning", "failed"}
    assert payload["diagnostic_only"] is (payload["fit_status"] != "ok")
    assert isinstance(payload["warnings"], list)
    assert "STATUS=" in result.stdout
    assert payload["spatial_panel"] == str(tmp_path / "proof" / "camera_spatial" / "proof_panel.png")
    assert payload["edge_overlay"] == str(tmp_path / "proof" / "camera_spatial" / "edge_overlay.png")
    metrics_path = tmp_path / "proof" / "camera_spatial" / "spatial_metrics.json"
    assert metrics_path.exists()
    assert Path(payload["spatial_panel"]).exists()
    assert Path(payload["edge_overlay"]).exists()
    assert Path(payload["fit_render"]).exists()
    assert Path(payload["overlay"]).exists()
    assert png_size(Path(payload["fit_render"])) == (48, 32)
    assert png_size(Path(payload["overlay"])) == (48, 32)
    assert png_size(Path(payload["spatial_panel"])) == (192, 56)
    assert png_size(Path(payload["edge_overlay"])) == (48, 32)
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert {
        "area_iou",
        "edge_f1@4",
        "edge_chamfer_px",
        "boundary_sdf_loss_px",
        "hausdorff_p95_px",
        "bbox_iou",
        "spatial_warning",
    } <= set(metrics)

    contour_camera = tmp_path / "contour" / "camera.json"
    contour_result = run_cli(
        tmp_path,
        "fit-camera",
        str(model),
        str(reference),
        "--mask-polarity",
        "light",
        "--objective",
        "contour",
        "--spatial-report",
        "",
        "--out",
        str(contour_camera),
        "--rand",
        "1",
        "--refine",
        "0",
        "--opt-size",
        "48x32",
        "--final-size",
        "48x32",
        timeout=240,
    )

    assert contour_result.returncode == 0, contour_result.stdout + contour_result.stderr
    contour_payload = json.loads(contour_camera.read_text(encoding="utf-8"))
    assert contour_payload["fit_status"] in {"warning", "failed"}
    assert contour_payload["diagnostic_only"] is True
    assert any("distinct non-mask" in warning for warning in contour_payload["warnings"])
    assert contour_payload["spatial_panel"] == str(tmp_path / "contour" / "camera_spatial" / "proof_panel.png")
    assert Path(contour_payload["spatial_panel"]).exists()
    assert (tmp_path / "contour" / "camera_spatial" / "spatial_metrics.json").exists()

    copied_mask = tmp_path / "copied_mask.pgm"
    write_pgm(
        copied_mask,
        [
            "0000000000",
            "0011111100",
            "0011111100",
            "0011111100",
            "0011111100",
            "0000000000",
        ],
    )
    copied_mask_camera = tmp_path / "copied-mask-proof" / "camera.json"
    copied_mask_result = run_cli(
        tmp_path,
        "fit-camera",
        str(model),
        str(reference),
        "--mask-polarity",
        "light",
        "--proof-reference",
        str(copied_mask),
        "--search-mode",
        "proof",
        "--out",
        str(copied_mask_camera),
        "--rand",
        "1",
        "--refine",
        "0",
        "--opt-size",
        "48x32",
        "--final-size",
        "48x32",
        timeout=240,
    )

    assert copied_mask_result.returncode == 0, copied_mask_result.stdout + copied_mask_result.stderr
    copied_mask_payload = json.loads(copied_mask_camera.read_text(encoding="utf-8"))
    assert copied_mask_payload["fit_status"] in {"warning", "failed"}
    assert copied_mask_payload["diagnostic_only"] is True
    assert any("distinct non-mask" in warning for warning in copied_mask_payload["warnings"])


def test_fit_camera_story_renders_reference_then_rejects_bad_proxy_model(tmp_path: Path) -> None:
    """A user fits a hidden reference view, reuses the pose, and rejects a bad 3D proxy by contours."""
    require_working_openscad()
    require_imagemagick()
    require_cli_python_deps(tmp_path, ["numpy", "pillow", "scipy"], ["numpy", "PIL", "scipy"])
    true_model = tmp_path / "reference_object.scad"
    bad_proxy = tmp_path / "bad_image_to_3d_proxy.scad"
    _reference_proxy_model(true_model)
    _bad_generated_proxy_model(bad_proxy)

    hidden_camera = "58,-76,34,14,9,8"
    result = run_shell(
        "\n".join(
            [
                "set -eu",
                'MAGICK="$(command -v magick || command -v convert)"',
                "mkdir -p fit gate",
                "",
                "# 1. The reference starts as a normal render/photo from an unknown camera.",
                f'"$PYTHON" "$THREED" render "{true_model}" --cam "{hidden_camera}" '
                "--size 120x80 -o reference.png > render.log",
                "",
                "# 2. The matching pipeline extracts the binary subject mask used for contours.",
                f'"$PYTHON" "$THREED" silhouette "{true_model}" --cam "{hidden_camera}" '
                "--size 120x80 -o reference_mask.png > silhouette.log",
                "",
                "# 3. Fit-camera searches from the model and mask, then writes a reusable camera.",
                f'"$PYTHON" "$THREED" fit-camera "{true_model}" reference_mask.png '
                "--mask-polarity light --backplate reference.png --objective contour "
                "--spatial-report fit/spatial --trace fit/trace.jsonl "
                "--out fit/camera.json --rand 10 --refine 2 --seed 3 "
                "--opt-size 120x80 --final-size 120x80 > fit/stdout.txt",
                '"$PYTHON" -c \'import json; print(json.load(open("fit/camera.json"))["camera_arg"])\' '
                "> fit/camera_arg.txt",
                'CAMERA="$(cat fit/camera_arg.txt)"',
                '"$MAGICK" reference_mask.png -threshold 50% -morphology EdgeOut Square:1 gate/reference_edge.png',
                "",
                "# 4. A generated proxy must be checked from the same fitted viewpoint.",
                f'"$PYTHON" "$THREED" silhouette "{bad_proxy}" --cam "$CAMERA" '
                "--size 120x80 -o gate/bad_proxy_mask.png > gate/bad_silhouette.log",
                '"$PYTHON" "$THREED" score gate/bad_proxy_mask.png reference_mask.png '
                "--masks -o gate/score | tee gate/score.txt | awk -F= '/^IoU=/{print $2}' "
                "> gate/bad_iou.txt",
                '"$MAGICK" gate/bad_proxy_mask.png -threshold 50% -morphology EdgeOut Square:1 gate/bad_proxy_edge.png',
                '"$PYTHON" "$THREED" score gate/bad_proxy_edge.png gate/reference_edge.png '
                "--masks -o gate/edge_score | tee gate/edge_score.txt | awk -F= '/^IoU=/{print $2}' "
                "> gate/bad_edge_iou.txt",
                "",
                "# 5. The human-readable gate report uses contour metrics, not exit code alone.",
                '"$PYTHON" - <<\'PY\' > gate/quality_report.md',
                "import json, pathlib",
                "fit = json.load(open('fit/spatial/spatial_metrics.json'))",
                "bad_iou = float(pathlib.Path('gate/bad_iou.txt').read_text())",
                "bad_edge_iou = float(pathlib.Path('gate/bad_edge_iou.txt').read_text())",
                "accepted = fit['edge_f1@4'] >= 0.50 and bad_iou >= 0.50 and bad_edge_iou >= 0.35",
                "print('# image-to-3D proxy gate')",
                "print(f'fitted_camera={json.load(open(\"fit/camera.json\"))[\"camera_arg\"]}')",
                "print(f'reference_edge_f1_at_4={fit[\"edge_f1@4\"]:.4f}')",
                "print(f'bad_proxy_iou={bad_iou:.4f}')",
                "print(f'bad_proxy_edge_iou={bad_edge_iou:.4f}')",
                "print('decision=' + ('ACCEPT' if accepted else 'REJECT'))",
                "PY",
            ]
        ),
        tmp_path,
        timeout=180,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    camera = json.loads((tmp_path / "fit" / "camera.json").read_text(encoding="utf-8"))
    assert len(camera["camera"]) == 6
    assert camera["objective"] == "contour"
    assert camera["backplate"] == "reference.png"
    assert camera["spatial_panel"] == "fit/spatial/proof_panel.png"
    assert png_size(tmp_path / "reference.png") == (120, 80)
    assert png_size(tmp_path / "fit" / "spatial" / "edge_overlay.png") == (120, 80)
    assert png_size(tmp_path / "fit" / "spatial" / "proof_panel.png") == (480, 104)

    fit_metrics = json.loads((tmp_path / "fit" / "spatial" / "spatial_metrics.json").read_text(encoding="utf-8"))
    assert fit_metrics["area_iou"] >= 0.35
    assert fit_metrics["edge_f1@4"] >= 0.50

    score = key_value_lines((tmp_path / "gate" / "score.txt").read_text(encoding="utf-8"))
    assert float(score["IoU"]) < 0.50
    assert (tmp_path / score["OVERLAY"]).exists()
    edge_score = key_value_lines((tmp_path / "gate" / "edge_score.txt").read_text(encoding="utf-8"))
    assert float(edge_score["IoU"]) < 0.35
    assert (tmp_path / edge_score["OVERLAY"]).exists()
    report = (tmp_path / "gate" / "quality_report.md").read_text(encoding="utf-8")
    assert "decision=REJECT" in report
    assert "bad_proxy_edge_iou=" in report


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
