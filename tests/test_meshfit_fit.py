"""Rasteriser and the heuristic silhouette fit: ground-truth masks from a known deformation, no hidden camera leak."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from errors import UsageError
from geometry.meshfit import deform, engine, fit, raster
from meshfit_fixtures import cube_sphere, spec

CENTER = np.array([0.0, 0.0, 0.0])
MM_PER_PX = 0.2
SIZE = 260


def _ortho(
    direction: tuple[float, float, float], up: tuple[float, float, float] = (0, 0, 1)
) -> raster.Camera:
    return raster.ortho_camera(
        np.array(direction), np.array(up), CENTER, MM_PER_PX, SIZE, SIZE
    )


def test_sphere_silhouette_area_matches_the_analytic_disc_even_with_sub_pixel_faces() -> (
    None
):
    V, F = cube_sphere(
        60, 20.0
    )  # faces ~0.5 mm = 2.5 px, a mix of tiny and normal triangles
    mask = raster.silhouette(_ortho((0, 1, 0)), V, F)
    expect = np.pi * 20.0**2 / MM_PER_PX**2
    assert abs(mask.sum() - expect) / expect < 0.03
    assert mask[SIZE // 2, SIZE // 2]  # solid at the centre
    from scipy import ndimage

    assert ndimage.binary_fill_holes(mask).sum() == mask.sum()  # no pinholes anywhere


def test_big_triangles_and_tiny_triangles_use_the_same_fill() -> None:
    V = np.array([[-20.0, 0.0, -20.0], [20.0, 0.0, -20.0], [0.0, 0.0, 20.0]])
    F = np.array([[0, 1, 2]])
    cam = _ortho((0, 1, 0))
    mask = raster.silhouette(cam, V, F)
    assert (
        abs(mask.sum() - 0.5 * 40.0 * 40.0 / MM_PER_PX**2)
        / (0.5 * 40.0 * 40.0 / MM_PER_PX**2)
        < 0.02
    )


def test_front_view_puts_plus_x_on_the_right_and_top_view_puts_plus_y_up() -> None:
    V, F = cube_sphere(12, 4.0, (10.0, 10.0, 0.0))
    front = raster.silhouette(_ortho((0, 1, 0)), V, F)
    top = raster.silhouette(_ortho((0, 0, -1), (0, 1, 0)), V, F)
    _, xs = np.nonzero(front)
    ys, _ = np.nonzero(top)
    assert xs.mean() > SIZE / 2 + 20  # +x is to the right of the image centre
    assert ys.mean() < SIZE / 2 - 20  # +y is above it (smaller row index)


def test_mask_iou_is_one_for_equal_masks_and_drops_for_a_shift() -> None:
    V, F = cube_sphere(30, 20.0)
    cam = _ortho((0, 1, 0))
    a = raster.silhouette(cam, V, F)
    assert raster.mask_iou(a, a) == 1.0
    assert raster.mask_iou(a, raster.silhouette(cam, V + [3.0, 0, 0], F)) < 0.9


def test_depth_buffer_picks_the_nearest_surface() -> None:
    V, F = cube_sphere(30, 20.0)
    cam = _ortho((0, 1, 0))
    uv, z = cam.project(V)
    _, zbuf, fbuf = raster.rasterize(uv, z, F, SIZE, SIZE, want_depth=True)
    assert zbuf is not None and fbuf is not None
    assert zbuf[SIZE // 2, SIZE // 2] == pytest.approx(
        -20.0, abs=0.4
    )  # the near pole of a sphere looking along +Y from the front
    assert (fbuf >= 0).sum() == raster.silhouette(cam, V, F).sum()


def test_camera_json_from_fit_camera_places_the_target_at_the_image_centre(
    tmp_path: Path,
) -> None:
    (tmp_path / "camera.json").write_text(json.dumps({"camera": [0, -200, 0, 0, 0, 0]}))
    mask = np.zeros((200, 300), dtype=bool)
    cam = fit.build_camera(
        {"camera_json": "camera.json"}, np.zeros((3, 3)), mask, str(tmp_path)
    )
    uv, depth = cam.project(np.array([[0.0, 0.0, 0.0]]))
    assert np.allclose(uv[0], [150.0, 100.0]) and depth[0] == pytest.approx(200.0)


def _write_mask(path: Path, mask: np.ndarray) -> None:
    from PIL import Image

    Image.fromarray((mask * 255).astype(np.uint8)).save(path)


def _scene(tmp_path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    """A sphere (the model) and a target where the +X pole was pulled out 4 mm; masks rendered from two fixed cameras."""
    pytest.importorskip("PIL")
    V, F = cube_sphere(40, 20.0)
    handles = np.nonzero(np.linalg.norm(V - [20.0, 0.0, 0.0], axis=1) < 4.0)[0]
    zone = np.linalg.norm(V - [20.0, 0.0, 0.0], axis=1) <= 15.0
    truth, _ = deform.solve_handles(
        V,
        F,
        handles,
        V[handles] + [4.0, 0.0, 0.0],
        np.zeros(len(V), dtype=bool),
        zone,
        "biharmonic",
    )
    cams = {"front": ((0, 1, 0), (0, 0, 1)), "top": ((0, 0, -1), (0, 1, 0))}
    views = []
    for name, (direction, up) in cams.items():
        _write_mask(
            tmp_path / f"{name}.png", raster.silhouette(_ortho(direction, up), truth, F)
        )
        views.append(
            {
                "name": name,
                "mask": f"{name}.png",
                "ortho": {
                    "direction": list(direction),
                    "up": list(up),
                    "center": [0, 0, 0],
                    "mm_per_px": MM_PER_PX,
                },
            }
        )
    op = {
        "op": "fit_silhouette",
        "views": views,
        "handles": [
            {
                "region": {"sphere": {"center": [20, 0, 0], "radius": 4.0}},
                "direction": [1, 0, 0],
                "max_move": 8.0,
            }
        ],
        "influence": 15.0,
        "step_mm": 2.0,
        "min_step_mm": 0.1,
        "iterations": 25,
    }
    return V, F, truth, op


def test_fit_silhouette_recovers_a_known_handle_pull_and_improves_iou(
    tmp_path: Path,
) -> None:
    V, F, truth, op = _scene(tmp_path)
    sp = spec(ops=[op])
    sp["base_dir"] = str(tmp_path)
    V2, _, report = engine.run_spec(V, F, sp, self_intersections=False)
    info = report["ops"][0]
    assert info["iou_after"] > info["iou_before"] + 0.002
    assert info["iou_after"] > 0.99
    assert info["handle_travel_mm"][0] == pytest.approx(
        4.0, abs=0.75
    )  # the hidden pull is 4 mm; the fit never saw it
    assert (
        np.abs(V2 - truth).max() < 1.0
    )  # and the surface ends up near the true target
    table = fit.format_table(info)
    assert "IoU" in table and "heuristic" in table and len(info["table"]) >= 2


def test_fit_silhouette_is_deterministic(tmp_path: Path) -> None:
    V, F, _, op = _scene(tmp_path)
    sp = spec(ops=[op])
    sp["base_dir"] = str(tmp_path)
    a = engine.run_spec(V, F, sp, self_intersections=False)
    b = engine.run_spec(V, F, sp, self_intersections=False)
    assert (
        a[2]["mesh_sha256"] == b[2]["mesh_sha256"]
        and a[2]["ops"][0]["table"] == b[2]["ops"][0]["table"]
    )


def test_fit_silhouette_respects_keepout_and_the_travel_limit(tmp_path: Path) -> None:
    V, F, _, op = _scene(tmp_path)
    op["handles"][0]["max_move"] = 1.0
    sp = spec(
        regions={
            "nose": {
                "sphere": {"center": [14.0, 10.0, 0.0], "radius": 5.0},
                "falloff": 2.0,
            }
        },
        keepout=["nose"],
        ops=[op],
    )
    sp["base_dir"] = str(tmp_path)
    V2, _, report = engine.run_spec(V, F, sp, self_intersections=False)
    locked = np.linalg.norm(V - [14.0, 10.0, 0.0], axis=1) <= 5.0
    assert locked.sum() > 10 and np.array_equal(V2[locked], V[locked])
    assert report["ops"][0]["handle_travel_mm"][0] <= 1.0 + 1e-9  # clipped at max_move
    assert report["keepout"]["gate"] == "PASS"


def test_fit_silhouette_needs_anchors_and_handles(tmp_path: Path) -> None:
    V, F, _, op = _scene(tmp_path)
    bad = {k: v for k, v in op.items() if k != "influence"}
    sp = spec(ops=[bad])
    sp["base_dir"] = str(tmp_path)
    with pytest.raises(UsageError, match="no anchors"):
        engine.run_spec(V, F, sp, self_intersections=False)
    with pytest.raises(UsageError, match="needs handles"):
        spec(ops=[{"op": "fit_silhouette", "views": op["views"], "handles": []}])


def test_fit_silhouette_writes_the_mesh_it_scored_with_a_keepout_fade_nearby(
    tmp_path: Path,
) -> None:
    from geometry.meshfit.regions import RegionContext

    V, F, _, op = _scene(tmp_path)
    regions = {
        "nose": {
            "sphere": {"center": [14.0, 10.0, 0.0], "radius": 3.0},
            "falloff": 10.0,
        }
    }
    sp = spec(regions=regions, keepout=["nose"], ops=[op])
    sp["base_dir"] = str(tmp_path)
    V2, _, _ = engine.run_spec(V, F, sp, self_intersections=False)
    pipe = engine.Pipeline(V, F, sp)
    ctx = RegionContext(V, F, sp["regions"], sp["base_dir"])
    wk = pipe._keepout_weight(ctx)
    assert ((wk > 0.0) & (wk < 1.0)).sum() > 20  # a real fade band
    scored, _ = fit.fit_silhouette(ctx, sp["ops"][0], pipe.lock, wk)
    assert np.array_equal(
        V2, scored
    )  # the fade is applied once, not again by the engine
