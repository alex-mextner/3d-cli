"""Unit tests for lib/vision_ghost.py: shell collection, wall quads, 3D ghost render and blend."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

import vision_geometry as vg
import vision_ghost as gh

pytest.importorskip("PIL")

LAYER_H = 0.5
LAYERS = 40  # 20 mm tall boxes


def _look_at(eye: np.ndarray, target: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    fwd = (target - eye) / np.linalg.norm(target - eye)
    right = np.cross(fwd, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right)
    down = np.cross(fwd, right)
    rot = np.stack([right, down, fwd])
    return vg._rodrigues_inv(rot), -rot @ eye


# A pinhole camera in front of the bed (-Y), 5 mm above the nozzle plane, looking along +Y.
_R, _T = _look_at(np.array([100.0, -150.0, 5.0]), np.array([100.0, 100.0, 0.0]))
CAM = vg.CameraModel(
    width=640,
    height=360,
    fx=500.0,
    fy=500.0,
    cx=320.0,
    cy=180.0,
    dist=(0.0, 0.0, 0.0, 0.0, 0.0),
    rvec=tuple(_R),
    tvec=tuple(_T),
)


def _square(x0: float, y0: float, size: float) -> list[tuple[float, float]]:
    return [
        (x0, y0),
        (x0 + size, y0),
        (x0 + size, y0 + size),
        (x0, y0 + size),
        (x0, y0),
    ]


def _feed(
    builder: gh.ShellBuilder,
    boxes: list[tuple[list[tuple[float, float]], int]],
    layers: int = LAYERS,
) -> None:
    """Print every box's outline as an outer wall on each layer, plus infill that must be ignored."""
    builder.comment("filament_colour = #1B5E20;#FFFFFF")
    for n in range(1, layers + 1):
        z = round(n * LAYER_H, 3)
        for ring, tool in boxes:
            builder.move(n, z, tool, None, ring[0][0], ring[0][1], False)  # travel
            for x, y in ring[1:]:
                builder.move(n, z, tool, "Outer wall", x, y, True)
            builder.move(n, z, tool, "Sparse infill", ring[2][0], ring[2][1], True)


NEAR = (_square(80.0, 40.0, 40.0), 1)  # white box near the camera
FAR = (_square(80.0, 140.0, 40.0), 0)  # green box straight behind it
FAR_SIDE = (
    _square(115.0, 140.0, 40.0),
    0,
)  # green box behind, half past the near one's right


def _shell(
    boxes: list[tuple[list[tuple[float, float]], int]] | None = None,
) -> gh.Shell:
    b = gh.ShellBuilder(min_step_mm=0.5)
    _feed(b, boxes if boxes is not None else [NEAR, FAR])
    return b.finish()


def test_builder_keeps_walls_per_layer_and_tool() -> None:
    shell = _shell()
    assert shell.colours == ("#1B5E20", "#FFFFFF")
    assert not shell.truncated
    assert (
        len(shell.layer) == 2 * LAYERS
    )  # one closed outline per box per layer, no infill
    assert sorted(shell.layer_z) == list(range(1, LAYERS + 1))
    assert shell.layer_z[4] == pytest.approx(2.0)
    first = shell.xy[shell.starts[0] : shell.starts[1]]
    assert np.allclose(first, np.array(NEAR[0], np.float32))
    assert set(shell.tool.tolist()) == {0, 1}


def test_builder_merges_short_steps_but_keeps_the_end() -> None:
    b = gh.ShellBuilder(min_step_mm=1.0)
    b.move(1, 0.2, 0, None, 0.0, 0.0, False)
    for i in range(1, 11):
        b.move(1, 0.2, 0, "outer wall", i * 0.25, 0.0, True)
    shell = b.finish()
    pts = shell.xy[shell.starts[0] : shell.starts[1]]
    assert pts[-1].tolist() == [2.5, 0.0]
    assert len(pts) < 6
    assert np.all(np.diff(pts[:-1, 0]) >= 1.0 - 1e-6)


def test_builder_samples_arcs_including_full_circles() -> None:
    b = gh.ShellBuilder(min_step_mm=0.5)
    b.move(1, 0.2, 0, None, 10.0, 0.0, False)
    # G3 X10 Y0 I-10 J0: a full counter-clockwise circle of radius 10 around the origin
    b.move(1, 0.2, 0, "outer wall", 10.0, 0.0, True, arc=(-10.0, 0.0, False))
    b.move(2, 0.4, 0, None, 30.0, 0.0, False)
    # G3 X20 Y10 I-10 J0: counter-clockwise quarter around (20, 0), through the upper half
    b.move(2, 0.4, 0, "outer wall", 20.0, 10.0, True, arc=(-10.0, 0.0, False))
    b.move(3, 0.6, 0, None, 30.0, 0.0, False)
    # G2 X20 Y-10 I-10 J0: clockwise quarter around (20, 0), through the lower half
    b.move(3, 0.6, 0, "outer wall", 20.0, -10.0, True, arc=(-10.0, 0.0, True))
    shell = b.finish()
    circle = shell.xy[shell.starts[0] : shell.starts[1]]
    assert np.allclose(np.hypot(circle[:, 0], circle[:, 1]), 10.0, atol=1e-3)
    assert len(circle) > 100 and np.allclose(circle[0], circle[-1])
    assert circle[:, 1].max() > 9.9 and circle[:, 1].min() < -9.9  # all the way round
    for i, upper in ((1, True), (2, False)):
        arc = shell.xy[shell.starts[i] : shell.starts[i + 1]]
        assert np.allclose(np.hypot(arc[:, 0] - 20.0, arc[:, 1]), 10.0, atol=1e-3)
        assert len(arc) > 20
        assert (arc[:, 1].min() >= -1e-3) if upper else (arc[:, 1].max() <= 1e-3)


def test_builder_caps_one_endless_wall_while_collecting() -> None:
    b = gh.ShellBuilder(max_points=10, min_step_mm=0.0)
    b.move(1, 0.2, 0, None, 0.0, 0.0, False)
    for x in range(1, 101):
        b.move(1, 0.2, 0, "outer wall", float(x), 0.0, True)
        assert len(b._run) // 2 <= 11  # memory stays bounded mid-path
    shell = b.finish()
    assert shell.truncated and shell.points <= 10


def test_builder_truncates_instead_of_growing_without_bound() -> None:
    b = gh.ShellBuilder(max_points=50)
    _feed(b, [NEAR])
    shell = b.finish()
    assert shell.truncated
    assert shell.points <= 50
    assert gh.render(shell, CAM, 10, (360, 640)) is None


def test_bands_tile_the_height_and_end_at_the_top_layer() -> None:
    lz = {n: n * LAYER_H for n in range(1, 11)}
    b = gh.bands(lz, 3, 10, 3, 1.0)
    assert list(b) == [4, 7, 10]
    assert b[4] == (1.0, 2.0) and b[7] == (2.0, 3.5) and b[10] == (3.5, 5.0)


def test_wall_quads_are_vertical_segments_of_the_kept_layers() -> None:
    shell = _shell([NEAR])
    q, tools, layers = gh.wall_quads(shell, {2: (0.5, 1.0)})
    assert q.shape == (4, 4, 3)  # four sides of one outline
    assert set(layers.tolist()) == {2} and set(tools.tolist()) == {1}
    assert np.allclose(q[:, :2, 2], 0.5) and np.allclose(q[:, 2:, 2], 1.0)
    assert np.allclose(q[:, 0, :2], q[:, 3, :2]) and np.allclose(
        q[:, 1, :2], q[:, 2, :2]
    )


def _alpha(g: gh.Ghost) -> np.ndarray:
    a = np.zeros(g.shape[0] * g.shape[1])
    a[g.index] = 1 - g.inv_alpha[:, 0] / 256
    return a.reshape(g.shape)


def _top_v(cam: vg.CameraModel, x: float, y: float, h: float, z: float) -> float:
    return float(cam.project(np.array([[x, y, h]]), z)[0, 1])


def test_ghost_covers_only_what_is_left_above_the_current_layer() -> None:
    shell = _shell([NEAR])
    layer = 20
    z = shell.layer_z[layer]
    g = gh.render(shell, CAM, layer, (360, 640))
    assert g is not None and g.layer == layer
    a = _alpha(g)
    u = int(round(CAM.project(np.array([[100.0, 40.0, z]]), z)[0, 0]))
    v_now = _top_v(CAM, 100.0, 40.0, z, z)
    v_top = _top_v(CAM, 100.0, 40.0, LAYERS * LAYER_H, z)
    col = a[:, u]
    assert (
        col[int(v_top) + 3 : int(v_now) - 3].min() > 0.3
    )  # the rest of the front wall
    # nothing over the printed part below, past the glow of the band printed now
    assert col[int(v_now) + 20 :].max() == 0
    assert col[: int(v_top) - 4].max() == 0  # nothing above the finished top


def test_printed_part_occludes_the_ghost_behind_it() -> None:
    # tall near box seen from below its top: its printed part hides the far box's ghost
    low = dataclasses.replace(CAM)
    r, t = _look_at(np.array([100.0, -150.0, -15.0]), np.array([100.0, 100.0, -15.0]))
    low = dataclasses.replace(low, rvec=tuple(r), tvec=tuple(t))
    far_only = gh.render(_shell([FAR]), low, 30, (360, 640))
    both = gh.render(_shell([NEAR, FAR]), low, 30, (360, 640))
    assert far_only is not None and both is not None
    hidden = set(far_only.index.tolist()) - set(both.index.tolist())
    assert len(hidden) > 0.5 * len(far_only.index)


def test_near_ghost_covers_the_far_one_and_is_more_opaque() -> None:
    shell = _shell([NEAR, FAR_SIDE])
    layer = 10
    z = shell.layer_z[layer]
    g = gh.render(shell, CAM, layer, (360, 640))
    assert g is not None
    a = _alpha(g)
    frame = np.zeros((360, 640, 3), np.uint8)
    g.blend(frame)

    def patch(x: float, y: float, h: float) -> tuple[np.ndarray, np.ndarray]:
        u, v = np.round(CAM.project(np.array([[x, y, h]]), z)[0]).astype(int)
        return a[v - 2 : v + 3, u - 2 : u + 3], frame[v - 2 : v + 3, u - 2 : u + 3]

    near_a, _ = patch(100.0, 40.0, 12.0)  # middle of the near box's front face
    far_a, far_c = patch(
        150.0, 140.0, 12.0
    )  # far box's front face, right of the near box
    behind_a, behind_c = patch(
        118.0, 40.0, 12.0
    )  # near face where the far box is behind it
    assert np.median(near_a) > np.median(far_a) > 0
    green = np.median(far_c[..., 1]) > 1.15 * np.median(far_c[..., 0])
    assert green  # tool 0 is green
    assert np.median(behind_a) > 0
    r, gr = np.median(behind_c[..., 0]), np.median(behind_c[..., 1])
    assert (
        r > 100 and abs(r - gr) < 40
    )  # white tool 1 in front, not the green behind it


def test_blend_changes_only_ghost_pixels_by_the_table() -> None:
    g = gh.render(_shell([NEAR]), CAM, 20, (360, 640))
    assert g is not None
    rng = np.random.default_rng(1)
    frame = rng.integers(0, 256, (360, 640, 3), dtype=np.uint8)
    before = frame.copy()
    out = g.blend(frame)
    assert out is frame
    flat_b, flat_a = before.reshape(-1, 3), frame.reshape(-1, 3)
    mask = np.zeros(len(flat_b), bool)
    mask[g.index] = True
    assert np.array_equal(flat_a[~mask], flat_b[~mask])
    expect = (flat_b[g.index].astype(np.uint16) * g.inv_alpha + g.premult) >> 8
    assert np.array_equal(flat_a[g.index], expect.astype(np.uint8))
    with pytest.raises(ValueError):
        g.blend(np.zeros((10, 10, 3), np.uint8))


def test_nothing_to_draw_returns_none() -> None:
    shell = _shell([NEAR])
    assert gh.render(shell, CAM, LAYERS, (360, 640)) is None  # last layer: nothing left
    assert gh.render(shell, CAM, 999, (360, 640)) is None  # unknown layer
    assert gh.render(gh.ShellBuilder().finish(), CAM, 1, (360, 640)) is None


def test_palette_lifts_dark_filament_washes_and_falls_back() -> None:
    pal = gh.palette(("#000000", "#FFFFFF"), 3)
    tint = np.array(gh.GHOST_TINT, np.float32)
    assert pal.shape == (3, 3)
    assert pal[0].max() >= gh.MIN_LUMA * (1 - gh.TINT)  # black lifted, then washed
    assert np.allclose(pal[1], 255 * (1 - gh.TINT) + tint * gh.TINT)
    fallback = np.array([int(gh.FALLBACK_COLOURS[2][i : i + 2], 16) for i in (1, 3, 5)])
    assert np.allclose(pal[2], fallback * (1 - gh.TINT) + tint * gh.TINT)
