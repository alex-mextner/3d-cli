#!/usr/bin/env python3
"""Emit spatial-aware fit-camera experiment commands.

This is a small stdlib-only harness. It does not import or modify fit-camera internals.
It records local optional assets, checks whether they exist, and prints reproducible
command sequences for the light fallback path or the heavier model-backed paths.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ExperimentCase:
    name: str
    model: str
    reference: str
    notes: str


CASES: tuple[ExperimentCase, ...] = (
    ExperimentCase(
        name="pantheon-gflash-front",
        model="/Users/ultra/xp/3d-tests/gflash-3dcli/pantheon.scad",
        reference="/Users/ultra/xp/3d-tests/gflash-3dcli/references/front.jpg",
        notes="Known negative control; old area-IoU-only fit can look better than it is.",
    ),
    ExperimentCase(
        name="pantheon-gflash-oblique",
        model="/Users/ultra/xp/3d-tests/gflash-3dcli/pantheon.scad",
        reference="/Users/ultra/xp/3d-tests/gflash-3dcli/references/oblique.jpg",
        notes="Oblique Pantheon view; useful for multi-view aggregate checks.",
    ),
    ExperimentCase(
        name="pantheon-gpro-front",
        model="/Users/ultra/xp/3d-tests/gpro-3dcli/pantheon.scad",
        reference="/Users/ultra/xp/3d-tests/gpro-3dcli/references/front.jpg",
        notes="Second generated Pantheon model for cross-run metric ranking.",
    ),
    ExperimentCase(
        name="lego-loco-emerald-side",
        model="/Users/ultra/xp/garage-band/projects/lego-loco/assembly.scad",
        reference="/Users/ultra/xp/garage-band/projects/lego-loco/references/ref_emerald_night_side.jpg",
        notes="Side-elevation real object; good depth/channel candidate.",
    ),
    ExperimentCase(
        name="lego-loco-orient-side",
        model="/Users/ultra/xp/garage-band/projects/lego-loco/assembly.scad",
        reference="/Users/ultra/xp/garage-band/projects/lego-loco/references/ref_orient_express_side.jpg",
        notes="Second side reference for same model family.",
    ),
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def shell_join(args: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in args)


def find_case(name: str) -> ExperimentCase:
    for case in CASES:
        if case.name == name:
            return case
    valid = ", ".join(case.name for case in CASES)
    raise SystemExit(f"unknown case {name!r}; valid: {valid}")


def case_status(case: ExperimentCase) -> dict[str, object]:
    model = Path(case.model)
    reference = Path(case.reference)
    return {
        **asdict(case),
        "model_exists": model.is_file(),
        "reference_exists": reference.is_file(),
        "ready": model.is_file() and reference.is_file(),
    }


def fallback_commands(case: ExperimentCase, out: Path) -> list[list[str]]:
    bin_3d = str(repo_root() / "bin" / "3d")
    prep = out / "preprocess"
    compare = out / "compare"
    camera_json = out / "camera.json"
    return [
        ["mkdir", "-p", str(out), str(prep), str(compare)],
        [bin_3d, "preprocess", case.reference, "-o", str(prep), "--force-fallback"],
        [
            bin_3d,
            "fit-camera",
            case.model,
            str(prep / "mask.png"),
            "--out",
            str(camera_json),
            "--rand",
            "250",
            "--refine",
            "100",
            "--seed",
            "11",
            "--mask-polarity",
            "light",
            "--backplate",
            case.reference,
        ],
        [
            bin_3d,
            "compare",
            case.model,
            case.reference,
            "--out",
            str(compare),
            "--rand",
            "250",
            "--refine",
            "100",
        ],
    ]


def rembg_commands(case: ExperimentCase, out: Path) -> list[list[str]]:
    prep = out / "rembg"
    bin_3d = str(repo_root() / "bin" / "3d")
    preprocess_tool = str(repo_root() / "lib" / "preprocess_reference.py")
    return [
        ["mkdir", "-p", str(prep)],
        [
            "uv",
            "run",
            "--python",
            "3.12",
            "--with",
            "opencv-python-headless,numpy,pillow",
            "--with",
            "rembg[cpu]",
            preprocess_tool,
            case.reference,
            "--out-dir",
            str(prep),
        ],
        [
            bin_3d,
            "fit-camera",
            case.model,
            str(prep / "mask.png"),
            "--out",
            str(out / "rembg-camera.json"),
            "--rand",
            "250",
            "--refine",
            "100",
            "--seed",
            "11",
            "--mask-polarity",
            "light",
            "--backplate",
            case.reference,
        ],
    ]


def depth_commands(case: ExperimentCase, out: Path) -> list[list[str]]:
    prep = out / "depth-anything"
    preprocess_tool = str(repo_root() / "lib" / "preprocess_reference.py")
    return [
        ["mkdir", "-p", str(prep)],
        [
            "uv",
            "run",
            "--python",
            "3.12",
            "--with",
            "opencv-python-headless,numpy,pillow",
            "--with",
            "transformers>=4.45",
            "--with",
            "torch",
            preprocess_tool,
            case.reference,
            "--out-dir",
            str(prep),
        ],
    ]


def write_synthetic_model(path: Path) -> None:
    path.write_text(
        """$fn=48;
union() {
  translate([-18, -10, 0]) cube([36, 20, 16]);
  translate([2, -8, 16]) cube([15, 16, 18]);
  translate([-12, 0, 24]) rotate([90, 0, 0]) cylinder(h=22, r=5, center=true);
  translate([22, 8, 5]) sphere(r=6);
}
""",
        encoding="utf-8",
    )


def write_sphere_model(path: Path) -> None:
    path.write_text("$fn=64;\nsphere(r=20);\n", encoding="utf-8")


def write_fourfold_model(path: Path) -> None:
    path.write_text(
        """$fn=64;
union() {
  cylinder(h=30, r=18, center=true);
  for (a = [0:90:270]) {
    rotate([0, 0, a]) translate([24, 0, 0]) cube([12, 8, 12], center=true);
  }
}
""",
        encoding="utf-8",
    )


def find_openscad() -> str:
    env = os.environ.get("OPENSCAD")
    if env and Path(env).exists():
        return env
    found = shutil.which("openscad")
    if found:
        return found
    for candidate in (
        "/opt/homebrew/bin/openscad",
        "/usr/local/bin/openscad",
        "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD",
    ):
        if Path(candidate).exists():
            return candidate
    raise SystemExit("openscad not found; install with: brew install --cask openscad")


def run_checked(cmd: list[str], cwd: Path | None = None) -> None:
    print(shell_join(cmd), flush=True)
    completed = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if completed.returncode != 0:
        sys.stderr.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        raise SystemExit(completed.returncode)


def cam_from_params(params: list[float], center: list[float]) -> list[float]:
    sys.path.insert(0, str(repo_root() / "lib"))
    from fit_camera import cam_from_params as fit_camera_cam_from_params  # type: ignore

    return fit_camera_cam_from_params(params, center)


def render_openscad(model: Path, camera: list[float], size: tuple[int, int], out: Path) -> None:
    cam_arg = ",".join(f"{v:.3f}" for v in camera)
    w, h = size
    run_checked([find_openscad(), "--render", "-o", str(out), f"--camera={cam_arg}", f"--imgsize={w},{h}", str(model)])


def write_openscad_mask(render_png: Path, mask_png: Path, size: tuple[int, int]) -> None:
    from PIL import Image
    import numpy as np

    bg = np.array([255, 255, 229])
    arr = np.asarray(Image.open(render_png).convert("RGB").resize(size), dtype=np.int16)
    diff = np.abs(arr - bg).sum(axis=2)
    mask = np.where(diff > 30, 0, 255).astype(np.uint8)
    Image.fromarray(mask, "L").save(mask_png)


def make_demo_frames(
    *,
    model: Path,
    reference: Path,
    camera_json: Path,
    trace_jsonl: Path,
    out_dir: Path,
    size: tuple[int, int],
) -> dict[str, str]:
    from PIL import Image, ImageDraw
    import numpy as np

    sys.path.insert(0, str(repo_root() / "lib"))
    from fit_camera import array_to_mask, ref_mask  # type: ignore
    from scipy import ndimage  # type: ignore
    from spatial_fit_metrics import binary_contour, spatial_fit_metrics  # type: ignore

    out_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    data = json.loads(camera_json.read_text(encoding="utf-8"))
    center = [float(v) for v in data["center"]]
    trace_rows = [json.loads(line) for line in trace_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    selected = trace_rows[-24:] if len(trace_rows) > 24 else trace_rows
    if not selected:
        final_params = data["params"]
        selected = [
            {
                "phase": "final",
                "iteration": 0,
                "params": [
                    final_params["azim"],
                    final_params["elev"],
                    final_params["dist"],
                    final_params["panx"],
                    final_params["panz"],
                ],
                "iou": data["iou"],
            }
        ]

    w, h = size
    reference_mask = ref_mask(str(reference), w, h, 150, "dark")
    ref_img = Image.open(reference).convert("RGB").resize((w, h))
    frame_paths: list[Path] = []
    edge_values: list[float] = []

    for idx, row in enumerate(selected):
        render_path = out_dir / f"candidate_{idx:03d}.png"
        camera = cam_from_params([float(v) for v in row["params"]], center)
        render_openscad(model, camera, size, render_path)
        render_arr = np.asarray(Image.open(render_path).convert("RGB").resize((w, h)), dtype=np.int16)
        render_mask = array_to_mask(render_arr)
        metrics = spatial_fit_metrics(render_mask, reference_mask)
        edge_values.append(float(metrics.edge_chamfer_px))

        ref_edge = binary_contour(reference_mask)
        render_edge = binary_contour(render_mask)
        dist_to_render = ndimage.distance_transform_edt(np.logical_not(render_edge))
        dist_to_ref = ndimage.distance_transform_edt(np.logical_not(ref_edge))
        matched_ref = np.logical_and(ref_edge, dist_to_render <= 3.0)
        matched_render = np.logical_and(render_edge, dist_to_ref <= 3.0)
        missed_ref = np.logical_and(ref_edge, np.logical_not(matched_ref))
        missed_render = np.logical_and(render_edge, np.logical_not(matched_render))

        overlay_arr = np.zeros((h, w, 3), dtype=np.uint8)
        overlay_arr[matched_ref | matched_render] = (245, 245, 245)
        overlay_arr[missed_ref] = (235, 55, 55)
        overlay_arr[missed_render] = (30, 205, 230)
        overlay = Image.fromarray(overlay_arr, "RGB")
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rectangle([0, 0, w - 1, 32], fill=(0, 0, 0))
        overlay_draw.text((10, 8), "W=match  R=ref  C=model", fill=(245, 245, 245))

        chart_w = w
        chart = Image.new("RGB", (chart_w, h), "white")
        draw = ImageDraw.Draw(chart)
        draw.text((12, 10), f"candidate {idx + 1}/{len(selected)}", fill=(20, 20, 20))
        draw.text((12, 32), "primary metric: contour distance", fill=(20, 20, 20))
        draw.text((12, 54), f"Chamfer={metrics.edge_chamfer_px:.2f}px  p95={metrics.hausdorff_p95_px:.2f}px", fill=(20, 20, 20))
        draw.text((12, 76), f"F1@4={metrics.edge_f1_at_4:.3f}  area IoU secondary={metrics.area_iou:.3f}", fill=(70, 70, 70))
        if metrics.spatial_warning:
            draw.text((12, 98), f"WARN: {metrics.spatial_warning[:42]}", fill=(180, 20, 20))
        if len(edge_values) > 1:
            top, bottom = 140, h - 24
            max_v = max(edge_values)
            min_v = min(edge_values)
            span = max(1e-6, max_v - min_v)
            draw.text((12, top - 22), "Chamfer trend, lower is better", fill=(70, 70, 70))
            pts = []
            for j, value in enumerate(edge_values):
                x = 16 + j * (chart_w - 32) / max(1, len(selected) - 1)
                y = bottom - (value - min_v) * (bottom - top) / span
                pts.append((x, y))
            draw.line(pts, fill=(30, 90, 180), width=3)
            for point in pts:
                draw.ellipse([point[0] - 3, point[1] - 3, point[0] + 3, point[1] + 3], fill=(30, 90, 180))

        frame = Image.new("RGB", (w * 3, h), "white")
        frame.paste(ref_img, (0, 0))
        frame.paste(overlay, (w, 0))
        frame.paste(chart, (w * 2, 0))
        frame_path = frames_dir / f"frame_{idx:03d}.png"
        frame.save(frame_path)
        frame_paths.append(frame_path)

    gif_path = out_dir / "candidate_evolution.gif"
    images = [Image.open(path).convert("RGB") for path in frame_paths]
    images[0].save(gif_path, save_all=True, append_images=images[1:], duration=350, loop=0)

    mp4_path = out_dir / "candidate_evolution.mp4"
    if shutil.which("ffmpeg"):
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-framerate",
                "3",
                "-i",
                str(frames_dir / "frame_%03d.png"),
                "-pix_fmt",
                "yuv420p",
                str(mp4_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    return {
        "frames": str(frames_dir),
        "gif": str(gif_path),
        "mp4": str(mp4_path) if mp4_path.exists() else "",
    }


def _normalized_rgb_mse(a: object, b: object, mask: object) -> float:
    import numpy as np

    aa = np.asarray(a, dtype=np.float32)
    bb = np.asarray(b, dtype=np.float32)
    mm = np.asarray(mask).astype(bool)
    if not mm.any():
        return float("inf")
    diff = (aa[mm] - bb[mm]) / 255.0
    return float(np.mean(diff * diff))


def _neighbor_delta(axis_deltas: list[float], delta: float, toward_truth: bool) -> float | None:
    if delta == 0.0:
        return None
    sorted_deltas = sorted(axis_deltas)
    idx = sorted_deltas.index(delta)
    if delta > 0:
        next_idx = idx - 1 if toward_truth else idx + 1
    else:
        next_idx = idx + 1 if toward_truth else idx - 1
    if next_idx < 0 or next_idx >= len(sorted_deltas):
        return None
    candidate = sorted_deltas[next_idx]
    if candidate == 0.0 or (candidate > 0) == (delta > 0):
        return candidate
    return None


def _draw_pose_sensitivity_plot(payload: dict[str, object], out_path: Path) -> None:
    from PIL import Image, ImageDraw

    axes = payload["axes"]
    assert isinstance(axes, list)
    width = 1100
    row_h = 210
    margin_l = 86
    margin_r = 40
    plot_w = width - margin_l - margin_r
    height = 70 + row_h * len(axes)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.text((24, 18), "Pose sensitivity: boundary SDF loss around hidden-camera truth", fill=(20, 20, 20))
    draw.text((24, 40), "green=toward truth lowers loss and away raises it; red=local monotonicity failure", fill=(70, 70, 70))

    for axis_idx, axis in enumerate(axes):
        assert isinstance(axis, dict)
        rows = axis["rows"]
        assert isinstance(rows, list)
        y0 = 70 + axis_idx * row_h
        plot_top = y0 + 26
        plot_bottom = y0 + row_h - 36
        deltas = [float(row["delta"]) for row in rows if isinstance(row, dict)]
        losses = [float(row["boundary_sdf_loss_px"]) for row in rows if isinstance(row, dict)]
        min_x, max_x = min(deltas), max(deltas)
        min_y, max_y = min(losses), max(losses)
        y_span = max(1e-6, max_y - min_y)
        x_span = max(1e-6, max_x - min_x)

        label = str(axis["label"])
        score = float(axis["monotonic_fraction"])
        draw.text((24, y0 + 2), f"{label}  local monotonic={score:.2f}", fill=(20, 20, 20))
        draw.line([(margin_l, plot_bottom), (width - margin_r, plot_bottom)], fill=(190, 190, 190), width=1)
        draw.line([(margin_l, plot_top), (margin_l, plot_bottom)], fill=(190, 190, 190), width=1)
        zero_x = margin_l + (0.0 - min_x) * plot_w / x_span
        draw.line([(zero_x, plot_top), (zero_x, plot_bottom)], fill=(120, 120, 120), width=1)
        points: list[tuple[float, float]] = []
        for row in rows:
            assert isinstance(row, dict)
            x = margin_l + (float(row["delta"]) - min_x) * plot_w / x_span
            y = plot_bottom - (float(row["boundary_sdf_loss_px"]) - min_y) * (plot_bottom - plot_top) / y_span
            points.append((x, y))
        if len(points) > 1:
            draw.line(points, fill=(40, 80, 160), width=2)
        for row, point in zip(rows, points):
            assert isinstance(row, dict)
            delta = float(row["delta"])
            if delta == 0.0:
                color = (20, 80, 220)
            elif bool(row["locally_monotonic"]):
                color = (20, 150, 80)
            else:
                color = (205, 55, 55)
            draw.ellipse([point[0] - 5, point[1] - 5, point[0] + 5, point[1] + 5], fill=color)
        draw.text((margin_l, plot_bottom + 8), f"{min_x:g}", fill=(80, 80, 80))
        draw.text((width - margin_r - 40, plot_bottom + 8), f"{max_x:g}", fill=(80, 80, 80))
        draw.text((8, plot_top), f"{max_y:.1f}px", fill=(80, 80, 80))
        draw.text((8, plot_bottom - 10), f"{min_y:.1f}px", fill=(80, 80, 80))
    image.save(out_path)


def evaluate_pose_sensitivity(
    *,
    model: Path,
    reference_render: Path,
    reference_mask: Path,
    out_dir: Path,
    center: list[float],
    true_params: list[float],
    size: tuple[int, int],
) -> dict[str, object]:
    from PIL import Image
    import numpy as np

    sys.path.insert(0, str(repo_root() / "lib"))
    from fit_camera import array_to_mask, ref_mask  # type: ignore
    from spatial_fit_metrics import spatial_fit_metrics  # type: ignore

    out_dir.mkdir(parents=True, exist_ok=True)
    renders = out_dir / "renders"
    renders.mkdir(parents=True, exist_ok=True)
    w, h = size
    refm = ref_mask(str(reference_mask), w, h, 150, "dark")
    ref_rgb = np.asarray(Image.open(reference_render).convert("RGB").resize((w, h)), dtype=np.int16)

    sweep_specs: list[tuple[str, int, list[float]]] = [
        ("azimuth_deg", 0, [-24.0, -12.0, -6.0, -3.0, 0.0, 3.0, 6.0, 12.0, 24.0]),
        ("elevation_deg", 1, [-16.0, -8.0, -4.0, -2.0, 0.0, 2.0, 4.0, 8.0, 16.0]),
        ("distance_mm", 2, [-48.0, -24.0, -12.0, -6.0, 0.0, 6.0, 12.0, 24.0, 48.0]),
        ("target_x_mm", 3, [-24.0, -12.0, -6.0, -3.0, 0.0, 3.0, 6.0, 12.0, 24.0]),
        ("target_z_mm", 4, [-24.0, -12.0, -6.0, -3.0, 0.0, 3.0, 6.0, 12.0, 24.0]),
    ]
    axes_payload: list[dict[str, object]] = []

    for label, param_idx, deltas in sweep_specs:
        rows: list[dict[str, object]] = []
        by_delta: dict[float, dict[str, object]] = {}
        for delta in deltas:
            params = list(true_params)
            params[param_idx] += delta
            render_path = renders / f"{label}_{delta:+.1f}.png".replace("+", "p").replace("-", "m")
            render_openscad(model, cam_from_params(params, center), size, render_path)
            render_rgb = np.asarray(Image.open(render_path).convert("RGB").resize((w, h)), dtype=np.int16)
            render_mask_arr = array_to_mask(render_rgb)
            metrics = spatial_fit_metrics(render_mask_arr, refm)
            union_mask = np.logical_or(render_mask_arr.astype(bool), refm.astype(bool))
            row: dict[str, object] = {
                "delta": delta,
                "params": [round(float(value), 6) for value in params],
                "area_iou": metrics.area_iou,
                "edge_chamfer_px": metrics.edge_chamfer_px,
                "boundary_sdf_loss_px": metrics.boundary_sdf_loss_px,
                "hausdorff_p95_px": metrics.hausdorff_p95_px,
                "rgb_shading_mse_proxy": _normalized_rgb_mse(render_rgb, ref_rgb, union_mask),
                "render": str(render_path),
            }
            rows.append(row)
            by_delta[delta] = row

        monotonic_hits = 0
        monotonic_total = 0
        for row in rows:
            delta = float(row["delta"])
            if delta == 0.0:
                row["toward_truth_decreases_error"] = None
                row["away_truth_increases_error"] = None
                row["locally_monotonic"] = None
                continue
            toward_delta = _neighbor_delta(deltas, delta, toward_truth=True)
            away_delta = _neighbor_delta(deltas, delta, toward_truth=False)
            loss = float(row["boundary_sdf_loss_px"])
            toward_ok = toward_delta is not None and float(by_delta[toward_delta]["boundary_sdf_loss_px"]) < loss
            away_ok = away_delta is None or float(by_delta[away_delta]["boundary_sdf_loss_px"]) > loss
            row["toward_truth_delta"] = toward_delta
            row["away_truth_delta"] = away_delta
            row["toward_truth_decreases_error"] = toward_ok
            row["away_truth_increases_error"] = away_ok
            row["locally_monotonic"] = toward_ok and away_ok
            monotonic_hits += 1 if row["locally_monotonic"] else 0
            monotonic_total += 1
        near_truth = [row for row in rows if abs(float(row["delta"])) in (2.0, 3.0, 6.0)]
        axes_payload.append(
            {
                "label": label,
                "parameter_index": param_idx,
                "monotonic_fraction": monotonic_hits / monotonic_total if monotonic_total else 0.0,
                "near_truth_all_toward_steps_improve": all(
                    bool(row["toward_truth_decreases_error"]) for row in near_truth
                ),
                "rows": rows,
            }
        )

    payload: dict[str, object] = {
        "objective": "local boundary signed-distance field plus symmetric Chamfer; RGB shading MSE is a proxy only",
        "depth_normal_mismatch": {
            "available": False,
            "reason": "OpenSCAD CLI render does not expose depth or normal buffers; use Blender/Manifold/OpenGL pass for this tier.",
            "proxy": "rgb_shading_mse_proxy",
        },
        "truth": {
            "center": center,
            "params": true_params,
            "camera": cam_from_params(true_params, center),
        },
        "axes": axes_payload,
    }
    plot = out_dir / "pose_sensitivity.png"
    _draw_pose_sensitivity_plot(payload, plot)
    payload["plot"] = str(plot)
    json_path = out_dir / "pose_sensitivity.json"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {"json": str(json_path), "plot": str(plot), "renders": str(renders), "summary": axes_payload}


def _draw_view_bank_plot(payload: dict[str, object], out_path: Path) -> None:
    from PIL import Image, ImageDraw

    azimuths = [float(value) for value in payload["azimuths"]]  # type: ignore[index]
    elevations = [float(value) for value in payload["elevations"]]  # type: ignore[index]
    cells = payload["cells"]
    assert isinstance(cells, list)
    cell_w, cell_h = 120, 86
    margin_l, margin_t = 90, 86
    width = margin_l + cell_w * len(azimuths) + 30
    height = margin_t + cell_h * len(elevations) + 42
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.text((18, 18), "View-bank retrieval: boundary SDF loss", fill=(20, 20, 20))
    draw.text((18, 38), "lower is better; blue cell is hidden-camera grid point", fill=(70, 70, 70))
    losses = [float(cell["boundary_sdf_loss_px"]) for cell in cells if isinstance(cell, dict)]
    min_loss, max_loss = min(losses), max(losses)
    span = max(1e-6, max_loss - min_loss)
    true_az = float(payload["true_azimuth_deg"])
    true_el = float(payload["true_elevation_deg"])
    by_pair = {
        (float(cell["azimuth_deg"]), float(cell["elevation_deg"])): cell
        for cell in cells
        if isinstance(cell, dict)
    }
    for x_idx, az in enumerate(azimuths):
        x = margin_l + x_idx * cell_w
        draw.text((x + 22, margin_t - 24), f"{az:g}deg", fill=(40, 40, 40))
    for y_idx, el in enumerate(elevations):
        y = margin_t + y_idx * cell_h
        draw.text((16, y + 28), f"{el:g}deg", fill=(40, 40, 40))
        for x_idx, az in enumerate(azimuths):
            x = margin_l + x_idx * cell_w
            cell = by_pair[(az, el)]
            loss = float(cell["boundary_sdf_loss_px"])
            heat = int(255 - 180 * (loss - min_loss) / span)
            color = (255, heat, heat)
            outline = (30, 90, 210) if az == true_az and el == true_el else (190, 190, 190)
            draw.rectangle([x, y, x + cell_w - 6, y + cell_h - 6], fill=color, outline=outline, width=3)
            draw.text((x + 12, y + 22), f"SDF {loss:.2f}", fill=(20, 20, 20))
            draw.text((x + 12, y + 44), f"IoU {float(cell['area_iou']):.2f}", fill=(50, 50, 50))
    image.save(out_path)


def evaluate_view_bank_retrieval(
    *,
    model: Path,
    reference_mask: Path,
    out_dir: Path,
    center: list[float],
    true_params: list[float],
    size: tuple[int, int],
) -> dict[str, object]:
    import numpy as np
    from PIL import Image

    sys.path.insert(0, str(repo_root() / "lib"))
    from fit_camera import array_to_mask, ref_mask  # type: ignore
    from spatial_fit_metrics import spatial_fit_metrics  # type: ignore

    out_dir.mkdir(parents=True, exist_ok=True)
    renders = out_dir / "renders"
    renders.mkdir(parents=True, exist_ok=True)
    w, h = size
    refm = ref_mask(str(reference_mask), w, h, 150, "dark")
    azimuths = [75.0, 105.0, 135.0, 165.0, 195.0]
    elevations = [0.0, 10.0, 20.0, 30.0, 40.0]
    cells: list[dict[str, object]] = []
    for el in elevations:
        for az in azimuths:
            params = [az, el, true_params[2], true_params[3], true_params[4]]
            render_path = renders / f"az_{az:.0f}_el_{el:.0f}.png"
            render_openscad(model, cam_from_params(params, center), size, render_path)
            render_rgb = np.asarray(Image.open(render_path).convert("RGB").resize((w, h)), dtype=np.int16)
            metrics = spatial_fit_metrics(array_to_mask(render_rgb), refm)
            cells.append(
                {
                    "azimuth_deg": az,
                    "elevation_deg": el,
                    "area_iou": metrics.area_iou,
                    "edge_chamfer_px": metrics.edge_chamfer_px,
                    "boundary_sdf_loss_px": metrics.boundary_sdf_loss_px,
                    "hausdorff_p95_px": metrics.hausdorff_p95_px,
                    "render": str(render_path),
                }
            )
    ranked = sorted(cells, key=lambda cell: float(cell["boundary_sdf_loss_px"]))
    payload: dict[str, object] = {
        "descriptor": "coarse view bank ranked by boundary signed-distance loss; this is retrieval, not a globally unique hash",
        "azimuths": azimuths,
        "elevations": elevations,
        "true_azimuth_deg": true_params[0],
        "true_elevation_deg": true_params[1],
        "top5": ranked[:5],
        "cells": cells,
    }
    plot = out_dir / "view_bank_heatmap.png"
    _draw_view_bank_plot(payload, plot)
    payload["plot"] = str(plot)
    json_path = out_dir / "view_bank.json"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    top = ranked[0]
    return {
        "json": str(json_path),
        "plot": str(plot),
        "renders": str(renders),
        "top1": top,
        "top5": ranked[:5],
    }


def _pose_metrics_for_params(
    *,
    model: Path,
    reference_mask: object,
    center: list[float],
    params: list[float],
    size: tuple[int, int],
    render_path: Path,
) -> dict[str, object]:
    import numpy as np
    from PIL import Image

    sys.path.insert(0, str(repo_root() / "lib"))
    from fit_camera import array_to_mask  # type: ignore
    from spatial_fit_metrics import spatial_fit_metrics  # type: ignore

    render_openscad(model, cam_from_params(params, center), size, render_path)
    render_rgb = np.asarray(Image.open(render_path).convert("RGB").resize(size), dtype=np.int16)
    metrics = spatial_fit_metrics(array_to_mask(render_rgb), reference_mask)
    return {
        "params": [round(float(value), 6) for value in params],
        "area_iou": metrics.area_iou,
        "edge_chamfer_px": metrics.edge_chamfer_px,
        "boundary_sdf_loss_px": metrics.boundary_sdf_loss_px,
        "hausdorff_p95_px": metrics.hausdorff_p95_px,
        "render": str(render_path),
    }


def _draw_symmetry_equivalence_plot(payload: dict[str, object], out_path: Path) -> None:
    from PIL import Image, ImageDraw

    width, height = 1180, 820
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.text((24, 18), "Pose objective modulo equivalence classes", fill=(20, 20, 20))
    draw.text(
        (24, 42),
        "plateau/equivalent minima are valid; non-equivalent backside should stay high loss",
        fill=(70, 70, 70),
    )
    sections = [
        ("sphere_unobservable", "Sphere azimuth: unobservable DOF plateau"),
        ("fourfold_equivalence", "4-fold model: azimuth equivalence modulo 90deg"),
        ("asymmetric_backside", "Asymmetric model: false backside rejection"),
    ]
    y = 88
    for key, title in sections:
        section = payload[key]
        assert isinstance(section, dict)
        draw.text((24, y), title, fill=(20, 20, 20))
        rows = section["rows"] if "rows" in section else [section["truth"], section["backside"]]
        assert isinstance(rows, list)
        losses = [float(row["boundary_sdf_loss_px"]) for row in rows if isinstance(row, dict)]
        max_loss = max(1.0, max(losses))
        x0, bar_w = 300, 760
        yy = y + 30
        for row in rows[:8]:
            assert isinstance(row, dict)
            label = str(row.get("azimuth_deg", row.get("label", "")))
            loss = float(row["boundary_sdf_loss_px"])
            w = max(2, int(bar_w * loss / max_loss))
            color = (40, 150, 80) if bool(row.get("equivalent", False)) else (190, 80, 60)
            if loss <= float(section.get("min_loss_px", loss)) + float(section.get("equivalence_tolerance_px", 0.0)):
                color = (40, 120, 210)
            draw.text((44, yy + 2), label, fill=(60, 60, 60))
            draw.rectangle([x0, yy, x0 + w, yy + 16], fill=color)
            draw.text((x0 + w + 8, yy), f"{loss:.2f}px", fill=(40, 40, 40))
            yy += 26
        status = str(section.get("decision", ""))
        draw.text((44, yy + 8), status, fill=(70, 70, 70))
        y = yy + 52
    image.save(out_path)


def evaluate_symmetry_equivalence(
    *,
    asymmetric_model: Path,
    reference_mask: Path,
    out_dir: Path,
    center: list[float],
    true_params: list[float],
    size: tuple[int, int],
) -> dict[str, object]:
    sys.path.insert(0, str(repo_root() / "lib"))
    from fit_camera import ref_mask  # type: ignore

    out_dir.mkdir(parents=True, exist_ok=True)
    renders = out_dir / "renders"
    renders.mkdir(parents=True, exist_ok=True)
    refm_asym = ref_mask(str(reference_mask), size[0], size[1], 150, "dark")
    tolerance = 0.5

    sphere_model = out_dir / "sphere_unobservable.scad"
    write_sphere_model(sphere_model)
    sphere_ref = out_dir / "sphere_ref.png"
    sphere_mask = out_dir / "sphere_mask.png"
    sphere_center = [0.0, 0.0, 0.0]
    sphere_params = [0.0, 20.0, 120.0, 0.0, 0.0]
    render_openscad(sphere_model, cam_from_params(sphere_params, sphere_center), size, sphere_ref)
    write_openscad_mask(sphere_ref, sphere_mask, size)
    refm_sphere = ref_mask(str(sphere_mask), size[0], size[1], 150, "dark")
    sphere_rows = []
    for azimuth in [0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0]:
        row = _pose_metrics_for_params(
            model=sphere_model,
            reference_mask=refm_sphere,
            center=sphere_center,
            params=[azimuth, 20.0, 120.0, 0.0, 0.0],
            size=size,
            render_path=renders / f"sphere_az_{azimuth:.0f}.png",
        )
        row["azimuth_deg"] = azimuth
        row["equivalent"] = True
        sphere_rows.append(row)
    sphere_losses = [float(row["boundary_sdf_loss_px"]) for row in sphere_rows]

    fourfold_model = out_dir / "fourfold_equivalence.scad"
    write_fourfold_model(fourfold_model)
    fourfold_ref = out_dir / "fourfold_ref.png"
    fourfold_mask = out_dir / "fourfold_mask.png"
    fourfold_center = [0.0, 0.0, 0.0]
    fourfold_params = [45.0, 20.0, 140.0, 0.0, 0.0]
    render_openscad(fourfold_model, cam_from_params(fourfold_params, fourfold_center), size, fourfold_ref)
    write_openscad_mask(fourfold_ref, fourfold_mask, size)
    refm_fourfold = ref_mask(str(fourfold_mask), size[0], size[1], 150, "dark")
    fourfold_rows = []
    for azimuth in [45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0, 360.0]:
        equivalent = ((azimuth - 45.0) % 90.0) == 0.0
        row = _pose_metrics_for_params(
            model=fourfold_model,
            reference_mask=refm_fourfold,
            center=fourfold_center,
            params=[azimuth, 20.0, 140.0, 0.0, 0.0],
            size=size,
            render_path=renders / f"fourfold_az_{azimuth:.0f}.png",
        )
        row["azimuth_deg"] = azimuth
        row["equivalent"] = equivalent
        fourfold_rows.append(row)
    fourfold_losses = [float(row["boundary_sdf_loss_px"]) for row in fourfold_rows]
    fourfold_min = min(fourfold_losses)
    low_loss_rows = [
        row for row in fourfold_rows if float(row["boundary_sdf_loss_px"]) <= fourfold_min + tolerance
    ]

    truth = _pose_metrics_for_params(
        model=asymmetric_model,
        reference_mask=refm_asym,
        center=center,
        params=true_params,
        size=size,
        render_path=renders / "asymmetric_truth.png",
    )
    truth["label"] = "truth azimuth"
    truth["azimuth_deg"] = true_params[0]
    truth["equivalent"] = True
    backside_params = list(true_params)
    backside_params[0] = (backside_params[0] + 180.0) % 360.0
    backside = _pose_metrics_for_params(
        model=asymmetric_model,
        reference_mask=refm_asym,
        center=center,
        params=backside_params,
        size=size,
        render_path=renders / "asymmetric_backside.png",
    )
    backside["label"] = "backside +180deg"
    backside["azimuth_deg"] = backside_params[0]
    backside["equivalent"] = False

    payload: dict[str, object] = {
        "definition": (
            "Pose-sensitive objectives should be evaluated over pose equivalence classes: "
            "symmetry-induced plateaus are valid, but non-equivalent wrong views must not be accepted."
        ),
        "sphere_unobservable": {
            "dof": "azimuth",
            "equivalence": "all azimuths equivalent for a sphere silhouette",
            "equivalence_tolerance_px": tolerance,
            "loss_range_px": max(sphere_losses) - min(sphere_losses),
            "unobservable_dof_detected": max(sphere_losses) - min(sphere_losses) <= tolerance,
            "min_loss_px": min(sphere_losses),
            "rows": sphere_rows,
            "decision": "accept plateau as unobservable DOF, not failure",
        },
        "fourfold_equivalence": {
            "dof": "azimuth",
            "equivalence": "azimuth modulo 90deg",
            "equivalence_tolerance_px": tolerance,
            "min_loss_px": fourfold_min,
            "low_loss_azimuths": [row["azimuth_deg"] for row in low_loss_rows],
            "low_loss_rows_match_equivalence": all(bool(row["equivalent"]) for row in low_loss_rows),
            "rows": fourfold_rows,
            "decision": "accept multiple minima only when they match declared rotational symmetry",
        },
        "asymmetric_backside": {
            "equivalence": "none for +180deg backside on asymmetric model",
            "equivalence_tolerance_px": tolerance,
            "min_loss_px": float(truth["boundary_sdf_loss_px"]),
            "truth": truth,
            "backside": backside,
            "rows": [truth, backside],
            "backside_rejected": (
                float(backside["boundary_sdf_loss_px"]) > float(truth["boundary_sdf_loss_px"]) + tolerance
            ),
            "decision": "reject low-confidence backside unless geometry declares equivalence",
        },
    }
    plot = out_dir / "symmetry_equivalence.png"
    _draw_symmetry_equivalence_plot(payload, plot)
    payload["plot"] = str(plot)
    json_path = out_dir / "symmetry_equivalence.json"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {"json": str(json_path), "plot": str(plot), "renders": str(renders), "summary": payload}


def run_synthetic_oracle(out: Path) -> dict[str, object]:
    out.mkdir(parents=True, exist_ok=True)
    model = out / "synthetic_asymmetric.scad"
    reference = out / "known_reference.png"
    reference_mask = out / "known_mask.png"
    camera_json = out / "camera.json"
    trace = out / "trace.jsonl"
    spatial_report = out / "spatial-report"
    write_synthetic_model(model)
    known_center = [5.0, 0.0, 21.0]
    known_params = [135.0, 20.0, 170.0, 0.0, 0.0]
    known_camera = cam_from_params(known_params, known_center)
    render_openscad(model, known_camera, (360, 240), reference)
    write_openscad_mask(reference, reference_mask, (360, 240))
    run_checked(
        [
            str(repo_root() / "bin" / "3d"),
            "fit-camera",
            str(model),
            str(reference_mask),
            "--out",
            str(camera_json),
            "--opt-size",
            "240x160",
            "--final-size",
            "360x240",
            "--rand",
            "120",
            "--refine",
            "55",
            "--seed",
            "7",
            "--objective",
            "contour",
            "--backplate",
            str(reference),
            "--spatial-report",
            str(spatial_report),
            "--trace",
            str(trace),
        ]
    )
    video = make_demo_frames(
        model=model,
        reference=reference_mask,
        camera_json=camera_json,
        trace_jsonl=trace,
        out_dir=out / "demo",
        size=(240, 160),
    )
    pose_sensitivity = evaluate_pose_sensitivity(
        model=model,
        reference_render=reference,
        reference_mask=reference_mask,
        out_dir=out / "pose-sensitivity",
        center=known_center,
        true_params=known_params,
        size=(240, 160),
    )
    view_bank = evaluate_view_bank_retrieval(
        model=model,
        reference_mask=reference_mask,
        out_dir=out / "view-bank",
        center=known_center,
        true_params=known_params,
        size=(240, 160),
    )
    symmetry_equivalence = evaluate_symmetry_equivalence(
        asymmetric_model=model,
        reference_mask=reference_mask,
        out_dir=out / "symmetry-equivalence",
        center=known_center,
        true_params=known_params,
        size=(240, 160),
    )
    metrics = json.loads((spatial_report / "spatial_metrics.json").read_text(encoding="utf-8"))
    result = {
        "model": str(model),
        "reference": str(reference),
        "reference_mask": str(reference_mask),
        "camera_json": str(camera_json),
        "spatial_metrics": metrics,
        "proof_panel": str(spatial_report / "proof_panel.png"),
        "edge_overlay": str(spatial_report / "edge_overlay.png"),
        "trace": str(trace),
        "demo": video,
        "pose_sensitivity": pose_sensitivity,
        "view_bank": view_bank,
        "symmetry_equivalence": symmetry_equivalence,
    }
    (out / "manifest.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return result


def emit_shell(case: ExperimentCase, out: Path, include_heavy: bool) -> str:
    blocks = [
        "# light fallback path: OpenCV mask, pseudo-depth, fit-camera, compare",
        *[shell_join(cmd) for cmd in fallback_commands(case, out)],
    ]
    if include_heavy:
        blocks.extend(
            [
                "",
                "# optional rembg mask tier",
                *[shell_join(cmd) for cmd in rembg_commands(case, out)],
                "",
                "# optional Depth Anything tier; run one image at a time",
                *[shell_join(cmd) for cmd in depth_commands(case, out)],
            ]
        )
    return "\n".join(blocks)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-assets", action="store_true", help="print local asset readiness JSON")
    parser.add_argument("--emit", choices=("json", "shell"), help="emit experiment commands")
    parser.add_argument("--case", default=CASES[0].name, help="case name for --emit")
    parser.add_argument("--out", default="/tmp/3d-spatial/experiment", help="output directory for --emit")
    parser.add_argument("--include-heavy", action="store_true", help="include rembg and Depth Anything commands")
    parser.add_argument(
        "--run-synthetic-oracle",
        action="store_true",
        help="execute a local synthetic oracle fit and write proof panel + candidate-evolution GIF/MP4",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check_assets:
        print(json.dumps([case_status(case) for case in CASES], indent=2))
    if args.emit:
        case = find_case(args.case)
        out = Path(args.out).expanduser().resolve()
        if args.emit == "json":
            payload = {
                "case": case_status(case),
                "out": str(out),
                "fallback_commands": fallback_commands(case, out),
                "rembg_commands": rembg_commands(case, out) if args.include_heavy else [],
                "depth_commands": depth_commands(case, out) if args.include_heavy else [],
            }
            print(json.dumps(payload, indent=2))
        else:
            print(emit_shell(case, out, args.include_heavy))
    if args.run_synthetic_oracle:
        run_synthetic_oracle(Path(args.out).expanduser().resolve())
    if not args.check_assets and not args.emit and not args.run_synthetic_oracle:
        build_parser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
