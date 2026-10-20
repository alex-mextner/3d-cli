"""Behavioural tests of the pure-numpy renderer (lib/prep/raster.py): geometry in pixels, shading, holes, sections."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np
import pytest
import trimesh
from scipy import ndimage

from errors import InvalidArgument
from prep.raster import (
    SECTION_FILL,
    SECTION_OUTLINE,
    camera_for,
    render_mesh,
    render_section,
    view_names,
    write_png,
)

WHITE = np.array([255, 255, 255], dtype=np.uint8)


def _mask(img: np.ndarray) -> np.ndarray:
    return (img != WHITE).any(axis=2)


def _cube(edge: float = 1.0) -> trimesh.Trimesh:
    return trimesh.creation.box(extents=(edge, edge, edge))


def test_cube_front_silhouette_matches_projected_size() -> None:
    mesh = _cube()
    cam = camera_for(mesh, "front", (900, 900))
    ys, xs = np.nonzero(_mask(render_mesh(mesh, view="front", size=(900, 900))))
    assert abs((xs.max() - xs.min() + 1) - cam.scale) <= 2
    assert abs((ys.max() - ys.min() + 1) - cam.scale) <= 2
    # the unit cube fills the image minus the 4 % margin on each side
    assert cam.scale == pytest.approx(900 * 0.92, rel=1e-9)


def test_projected_size_follows_the_model_not_the_image() -> None:
    mesh = trimesh.creation.box(extents=(2.0, 1.0, 0.5))
    img = render_mesh(mesh, view="front", size=(600, 300))
    ys, xs = np.nonzero(_mask(img))
    cam = camera_for(mesh, "front", (600, 300))
    assert abs((xs.max() - xs.min() + 1) - 2.0 * cam.scale) <= 2
    assert abs((ys.max() - ys.min() + 1) - 0.5 * cam.scale) <= 2


def test_lit_face_differs_from_shadowed_face() -> None:
    img = render_mesh(_cube(), view="iso", size=(600, 600))
    colours, counts = np.unique(img.reshape(-1, 3), axis=0, return_counts=True)
    big = [
        tuple(int(v) for v in c)
        for c, n in zip(colours, counts, strict=True)
        if n > 2000 and tuple(c) != (255, 255, 255)
    ]
    # the iso view shows front, side and top faces of the cube: three distinct brightnesses
    assert len(big) == 3
    assert len({sum(c) for c in big}) == 3


def test_flat_face_brightness_follows_lambert() -> None:
    # facing the camera head-on the front face gets ambient + (1-ambient)*max(0, L.toward) with L normalised
    light = (0.0, 0.0, 1.0)
    img = render_mesh(_cube(), view="front", light=light, ambient=0.25, size=(200, 200))
    centre = img[100, 100]
    expected = 190 * (0.25 + 0.75 * 1.0)
    assert abs(float(centre[0]) - expected) <= 2
    dark = render_mesh(
        _cube(), view="front", light=(1.0, 0.0, 0.0), ambient=0.25, size=(200, 200)
    )
    assert abs(float(dark[100, 100][0]) - 190 * 0.25) <= 2


def test_sphere_silhouette_area_is_pi_r_squared() -> None:
    radius = 10.0
    mesh = trimesh.creation.icosphere(subdivisions=4, radius=radius)
    cam = camera_for(mesh, "front", (900, 900))
    area = int(_mask(render_mesh(mesh, view="front", size=(900, 900))).sum())
    assert area == pytest.approx(np.pi * (radius * cam.scale) ** 2, rel=0.03)


@pytest.mark.parametrize("view", ["front", "iso", "low"])
def test_closed_sphere_has_no_interior_background(view: str) -> None:
    mesh = trimesh.creation.icosphere(subdivisions=4, radius=10.0)
    mask = _mask(render_mesh(mesh, view=view, size=(500, 500)))
    assert int((ndimage.binary_fill_holes(mask) & ~mask).sum()) == 0


def test_a_real_hole_is_not_filled() -> None:
    # control for the hole test above: the hole of a torus seen from above is genuine background
    torus = trimesh.creation.torus(
        major_radius=10.0, minor_radius=3.0, major_sections=64, minor_sections=24
    )
    mask = _mask(render_mesh(torus, view="top", size=(500, 500)))
    assert int((ndimage.binary_fill_holes(mask) & ~mask).sum()) > 5000


def test_face_colors_are_respected() -> None:
    mesh = _cube()
    red = np.tile(np.array([230, 20, 20], dtype=np.uint8), (len(mesh.faces), 1))
    img = render_mesh(mesh, view="iso", face_colors=red, size=(300, 300))
    pix = img[_mask(img)].astype(float)
    assert pix[:, 0].mean() > 2.5 * pix[:, 1].mean()
    assert pix[:, 0].mean() > 2.5 * pix[:, 2].mean()
    rgba = np.hstack([red, np.full((len(red), 1), 255, dtype=np.uint8)])
    assert np.array_equal(
        render_mesh(mesh, view="iso", face_colors=rgba, size=(300, 300)), img
    )


def test_per_face_colours_land_on_their_faces() -> None:
    # the +X faces blue, everything else red; the 'right' view looks at +X
    mesh = _cube()
    colours = np.tile(np.array([230, 20, 20], dtype=np.uint8), (len(mesh.faces), 1))
    colours[mesh.face_normals[:, 0] > 0.5] = (20, 20, 230)
    img = render_mesh(
        mesh, view="right", face_colors=colours, ambient=1.0, size=(200, 200)
    )
    assert tuple(img[100, 100]) == (20, 20, 230)
    img = render_mesh(
        mesh, view="left", face_colors=colours, ambient=1.0, size=(200, 200)
    )
    assert tuple(img[100, 100]) == (230, 20, 20)


def test_background_is_used_and_validated() -> None:
    img = render_mesh(_cube(), view="front", size=(100, 100), background=(1, 2, 3))
    assert tuple(img[0, 0]) == (1, 2, 3)
    assert img.dtype == np.uint8
    assert img.shape == (100, 100, 3)


def test_view_names_are_validated() -> None:
    assert {"iso", "front", "back", "left", "right", "top", "bottom", "low"} <= set(
        view_names()
    )
    for name in view_names():
        render_mesh(_cube(), view=name, size=(64, 64))
    with pytest.raises(InvalidArgument):
        render_mesh(_cube(), view="sideways")
    with pytest.raises(InvalidArgument):
        render_mesh(_cube(), view=(1.0, 2.0, 3.0))  # type: ignore[arg-type]
    with pytest.raises(InvalidArgument):
        render_mesh(_cube(), size=(4, 4))


def test_face_colors_shape_is_validated() -> None:
    with pytest.raises(InvalidArgument):
        render_mesh(_cube(), face_colors=np.zeros((3, 3), dtype=np.uint8))


def test_free_azimuth_elevation_view() -> None:
    mesh = _cube()
    assert np.array_equal(
        render_mesh(mesh, view=(0.0, 0.0), size=(200, 200)),
        render_mesh(mesh, view="front", size=(200, 200)),
    )
    assert not np.array_equal(
        render_mesh(mesh, view=(0.0, 45.0), size=(200, 200)),
        render_mesh(mesh, view="front", size=(200, 200)),
    )


def test_render_is_deterministic_and_seeded() -> None:
    mesh = trimesh.creation.icosphere(subdivisions=3)
    a = render_mesh(mesh, size=(300, 300))
    assert np.array_equal(a, render_mesh(mesh, size=(300, 300)))
    # the seed only moves sub-pixel sampling; the silhouette stays the same to within boundary pixels
    b = render_mesh(mesh, size=(300, 300), seed=7)
    assert abs(int(_mask(a).sum()) - int(_mask(b).sum())) < 0.01 * int(_mask(a).sum())


def test_nearest_surface_wins_the_z_buffer() -> None:
    near = trimesh.creation.box(extents=(1, 0.2, 1))
    far = trimesh.creation.box(extents=(1, 0.2, 1))
    far.apply_translation(
        (0, 2.0, 0)
    )  # behind `near` for the front view (camera at -Y)
    both = trimesh.util.concatenate([near, far])
    colours = np.zeros((len(both.faces), 3), dtype=np.uint8)
    colours[: len(near.faces)] = (255, 0, 0)
    colours[len(near.faces) :] = (0, 0, 255)
    img = render_mesh(
        both, view="front", face_colors=colours, ambient=1.0, size=(200, 200)
    )
    assert tuple(img[100, 100]) == (255, 0, 0)
    img = render_mesh(
        both, view="back", face_colors=colours, ambient=1.0, size=(200, 200)
    )
    assert tuple(img[100, 100]) == (0, 0, 255)


def test_section_of_cube_at_mid_plane_is_a_filled_square() -> None:
    cube = _cube(10.0)
    img = render_section(cube, (0, 0, 0), (0, 0, 1), (700, 700))
    painted = (img == np.array(SECTION_FILL)).all(axis=2) | (
        img == np.array(SECTION_OUTLINE)
    ).all(axis=2)
    # Plot2D fits 10 mm into 700 - 2*40 px -> 62 px/mm
    assert int(painted.sum()) == pytest.approx((10 * 62) ** 2, rel=0.015)
    ys, xs = np.nonzero(painted)
    assert (xs.max() - xs.min() + 1) == pytest.approx(620, abs=3)
    assert (ys.max() - ys.min() + 1) == pytest.approx(620, abs=3)


def test_section_of_a_tube_leaves_the_bore_empty() -> None:
    outer = trimesh.creation.cylinder(radius=10.0, height=20.0, sections=96)
    bore = trimesh.creation.cylinder(radius=5.0, height=30.0, sections=96)
    tube = outer.difference(bore, engine="manifold")
    img = render_section(tube, (0, 0, 0), (0, 0, 1), (700, 700))
    fill = (img == np.array(SECTION_FILL)).all(axis=2)
    scale = 620 / 20.0
    ring = np.pi * (10.0**2 - 5.0**2) * scale**2
    assert int(fill.sum()) == pytest.approx(ring, rel=0.05)
    assert tuple(img[350, 350]) == (255, 255, 255)  # centre of the bore


def test_section_plane_missing_the_model_is_blank() -> None:
    img = render_section(_cube(10.0), (0, 0, 50), (0, 0, 1), (300, 300))
    assert not (img == np.array(SECTION_FILL)).all(axis=2).any()


def test_section_axes_default_and_explicit() -> None:
    bar = trimesh.creation.box(extents=(30.0, 4.0, 10.0))
    # plane normal +Y: right = +X, so the wide dimension is horizontal
    img = render_section(bar, (0, 0, 0), (0, 1, 0), (600, 600))
    ys, xs = np.nonzero((img == np.array(SECTION_FILL)).all(axis=2))
    assert (xs.max() - xs.min()) > 2.5 * (ys.max() - ys.min())
    # explicit axes swap the picture: right = Z, up = X
    img = render_section(
        bar, (0, 0, 0), (0, 1, 0), (600, 600), right=(0, 0, 1), up=(1, 0, 0)
    )
    ys, xs = np.nonzero((img == np.array(SECTION_FILL)).all(axis=2))
    assert (ys.max() - ys.min()) > 2.5 * (xs.max() - xs.min())
    with pytest.raises(InvalidArgument):
        render_section(bar, (0, 0, 0), (0, 1, 0), right=(1, 0, 0))
    with pytest.raises(InvalidArgument):
        render_section(bar, (0, 0, 0), (0, 0, 0))


def _read_png(path: Path) -> np.ndarray:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, w, h = 8, b"", 0, 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        tag = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        assert struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])[
            0
        ] == zlib.crc32(tag + body)
        if tag == b"IHDR":
            w, h = struct.unpack(">II", body[:8])
        elif tag == b"IDAT":
            idat += body
        pos += 12 + length
    raw = np.frombuffer(zlib.decompress(idat), dtype=np.uint8).reshape(h, 1 + w * 3)
    assert (raw[:, 0] == 0).all()
    return raw[:, 1:].reshape(h, w, 3)


def test_write_png_round_trips(tmp_path: Path) -> None:
    rng = np.random.default_rng(1)
    rgb = rng.integers(0, 256, size=(37, 53, 3), dtype=np.uint8)
    out = tmp_path / "deep" / "dir" / "x.png"
    write_png(out, rgb)
    assert np.array_equal(_read_png(out), rgb)
    with pytest.raises(InvalidArgument):
        write_png(tmp_path / "bad.png", np.zeros((4, 4), dtype=np.uint8))
