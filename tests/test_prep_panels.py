"""Behavioural tests of the proof-pack panels (lib/prep/panels.py): colours must follow geometry, and a
defect-free input must NOT light up the defect colours."""

from __future__ import annotations

import numpy as np
import pytest
import trimesh

from errors import InvalidArgument
from prep.panels import (
    ARROW_COLOR,
    BED,
    BED_GREY,
    COM_COLOR,
    GREEN,
    LEGEND_H,
    LOAD_COM_COLOR,
    OK,
    OVERHANG,
    RED,
    SUPPORT_FILL,
    VERDICT_COLORS,
    WARN,
    YELLOW,
    before_after,
    contact_sheet,
    diff_face_masks,
    layer_band_plot,
    overhang_face_classes,
    overhang_heatmap,
    profile_with_slab,
    region_map,
    slab_corners,
    stability_diagram,
    verdict_matrix,
    verdict_matrix_cell,
)

S = 300  # render size of the 3-D tests


def _tilted_plate(alpha_deg: float) -> trimesh.Trimesh:
    """40x40x0.5 plate tilted `alpha` about X: its underside is an overhang of 90 - alpha degrees from vertical."""
    plate = trimesh.creation.box(extents=(40, 40, 0.5))
    plate.apply_transform(
        trimesh.transformations.rotation_matrix(np.radians(alpha_deg), (1, 0, 0))
    )
    plate.apply_translation(
        (0, 0, -plate.bounds[0][2] + 5)
    )  # lifted off the bed: no bed-contact faces
    return plate


def _is_red(img: np.ndarray) -> np.ndarray:
    img = img.astype(int)  # uint8 sums would wrap
    return (img[..., 0] > 100) & (img[..., 1] < 70) & (img[..., 2] < 70)


def _is_green(img: np.ndarray) -> np.ndarray:
    img = img.astype(int)
    return (img[..., 1] > img[..., 0] + 30) & (img[..., 1] > img[..., 2] + 20)


def _is_blue(img: np.ndarray) -> np.ndarray:
    return (img[..., 2] > 100) & (img[..., 0] < 80) & (img[..., 1] < 120)


def _object_pixels(body: np.ndarray) -> np.ndarray:
    """Non-white pixels of a render (without its legend)."""
    return (body != 255).any(axis=2)


# --- overhang heatmap -----------------------------------------------------------------------


def test_60_degree_plate_is_red_and_vertical_wall_is_green() -> None:
    plate = _tilted_plate(30.0)  # underside 60 deg from vertical
    cls = overhang_face_classes(plate)
    assert (cls == OVERHANG).sum() == 2  # the two triangles of the underside
    img = overhang_heatmap(plate, views=("low",), size=(S, S))[0]
    body = img[:S]
    obj = _object_pixels(body).sum()
    assert _is_red(body).sum() > 0.5 * obj
    assert (
        _is_green(body).sum() < 0.5 * _is_red(body).sum()
    )  # only the thin edge faces are green

    wall = trimesh.creation.box(extents=(40, 2, 40))
    wall.apply_translation((0, 0, 20))
    img = overhang_heatmap(wall, views=("front",), size=(S, S))[0]
    body = img[:S]
    obj = _object_pixels(body).sum()
    assert _is_green(body).sum() > 0.9 * obj
    assert _is_red(body).sum() == 0


def test_class_boundaries_follow_the_angle_from_vertical() -> None:
    assert (
        overhang_face_classes(_tilted_plate(50.0)) == WARN
    ).sum() == 2  # 40 deg: inside 35-45
    assert (
        overhang_face_classes(_tilted_plate(50.0), angle_deg=30.0) == OVERHANG
    ).sum() == 4  # the underside (40 deg) and the two end faces (50 deg) both exceed 30
    cls70 = overhang_face_classes(_tilted_plate(70.0))
    assert (
        cls70 == OVERHANG
    ).sum() == 2  # only the two end faces (70 deg); the 20 deg underside is fine
    assert (cls70 == OK).sum() == 10


def test_bed_contact_is_grey_not_overhang() -> None:
    box = trimesh.creation.box(extents=(20, 20, 20))
    box.apply_translation((0, 0, 10))  # sits on Z = 0
    cls = overhang_face_classes(box)
    assert (cls == BED).sum() == 2 and (cls == OVERHANG).sum() == 0
    img = overhang_heatmap(box, views=("bottom",), size=(S, S))[0]
    centre = img[S // 2, S // 2]
    assert (
        abs(int(centre[0]) - int(centre[1])) < 8
        and abs(int(centre[1]) - int(centre[2])) < 8
    )
    assert _is_red(img[:S]).sum() == 0


def test_heatmap_legend_lists_all_four_classes() -> None:
    img = overhang_heatmap(_tilted_plate(30.0), views=("iso",), size=(700, 700))[0]
    assert img.shape[0] >= 700 + LEGEND_H
    strip = img[700:]
    for colour in (GREEN, YELLOW, RED, BED_GREY):
        assert (strip == np.array(colour)).all(axis=2).sum() >= 200  # one 16x16 swatch


def test_heatmap_validates_angle() -> None:
    with pytest.raises(InvalidArgument):
        overhang_heatmap(_tilted_plate(30.0), angle_deg=95.0)


# --- before / after -------------------------------------------------------------------------


def _box_with_bump(bump_h: float) -> trimesh.Trimesh:
    base = trimesh.creation.box(extents=(20, 20, 10))
    bump = trimesh.creation.box(extents=(6, 6, bump_h))
    bump.apply_translation((0, 0, 5 + bump_h / 2 - 0.5))
    return base.union(bump, engine="manifold")


def test_diff_masks_mark_only_the_added_bump() -> None:
    base = trimesh.creation.box(extents=(20, 20, 10))
    after = _box_with_bump(4.0)
    m = diff_face_masks(base, after)
    assert m.removed.sum() == 0
    centres = after.triangles_center[m.added]
    assert len(centres) > 0
    assert (
        centres[:, 2] > 4.9
    ).all()  # everything flagged is at or above the old top face
    assert (np.abs(centres[:, :2]) <= 3.01).all()  # and inside the bump footprint
    assert m.tolerance_mm >= 0.3


def test_diff_masks_respect_tolerance() -> None:
    base = trimesh.creation.box(extents=(20, 20, 10))
    assert (
        diff_face_masks(base, _box_with_bump(1.1), tol_mm=0.3).added.sum() > 0
    )  # 0.6 mm proud
    assert (
        diff_face_masks(base, _box_with_bump(1.1), tol_mm=2.0).added.sum() == 0
    )  # within a 2 mm tolerance


def test_before_after_marks_added_material_red_and_nothing_else() -> None:
    base = trimesh.creation.box(extents=(20, 20, 10))
    sheet = before_after(base, _box_with_bump(4.0), views=("iso",), size=(S, S))
    w = sheet.shape[1]
    left, right = sheet[:, : w // 2], sheet[:, w // 2 :]
    assert _is_red(right).sum() > 200
    assert _is_red(left).sum() == 0
    assert _is_blue(sheet).sum() == 0


def test_before_after_marks_removed_material_blue() -> None:
    base = trimesh.creation.box(extents=(20, 20, 10))
    sheet = before_after(_box_with_bump(4.0), base, views=("iso",), size=(S, S))
    w = sheet.shape[1]
    assert _is_blue(sheet[:, : w // 2]).sum() > 200
    assert _is_blue(sheet[:, w // 2 :]).sum() == 0
    assert _is_red(sheet).sum() == 0


def test_identical_meshes_show_no_difference() -> None:
    base = trimesh.creation.icosphere(subdivisions=3, radius=10.0)
    sheet = before_after(base, base.copy(), views=("iso", "low"), size=(200, 200))
    assert _is_red(sheet).sum() == 0 and _is_blue(sheet).sum() == 0
    assert diff_face_masks(base, base.copy()).added.sum() == 0


def test_highlight_off_paints_nothing_even_when_different() -> None:
    base = trimesh.creation.box(extents=(20, 20, 10))
    sheet = before_after(
        base, _box_with_bump(4.0), views=("iso",), size=(200, 200), highlight_diff=False
    )
    assert _is_red(sheet).sum() == 0 and _is_blue(sheet).sum() == 0


# --- region map -----------------------------------------------------------------------------


def test_region_map_colours_faces_by_label_and_lists_them() -> None:
    cube = trimesh.creation.box(extents=(10, 10, 10))
    labels = np.where(cube.face_normals[:, 0] > 0.5, 2, 1)
    palette = {1: (230, 20, 20), 2: (20, 20, 230)}
    imgs = region_map(
        cube,
        labels,
        palette,
        {1: "body", 2: "eye"},
        views=("right", "left"),
        size=(S, S),
    )
    assert tuple(imgs[0][S // 2, S // 2]) != tuple(imgs[1][S // 2, S // 2])
    assert _is_blue(imgs[0][:S]).sum() > 0.5 * _object_pixels(imgs[0][:S]).sum()
    assert _is_blue(imgs[1][:S]).sum() == 0
    strip = imgs[0][S:]
    assert (strip == np.array((230, 20, 20))).all(axis=2).any()
    assert (strip == np.array((20, 20, 230))).all(axis=2).any()
    with pytest.raises(InvalidArgument):
        region_map(cube, labels[:-1], palette, {})


# --- contact sheet --------------------------------------------------------------------------


def test_contact_sheet_places_images_in_a_grid() -> None:
    a = np.full((50, 80, 3), (255, 0, 0), dtype=np.uint8)
    b = np.full((50, 80, 3), (0, 0, 255), dtype=np.uint8)
    c = np.full((30, 40, 3), (0, 255, 0), dtype=np.uint8)
    sheet = contact_sheet([a, b, c], ["a", "b", "c"], 2, "t")
    assert (sheet == np.array((255, 0, 0))).all(axis=2).sum() == a.shape[0] * a.shape[1]
    assert (sheet == np.array((0, 255, 0))).all(axis=2).sum() == c.shape[0] * c.shape[1]
    ys_a, xs_a = np.nonzero((sheet == np.array((255, 0, 0))).all(axis=2))
    ys_b, xs_b = np.nonzero((sheet == np.array((0, 0, 255))).all(axis=2))
    assert (
        ys_a.min() == ys_b.min() and xs_b.min() > xs_a.max()
    )  # same row, b to the right
    ys_c, _ = np.nonzero((sheet == np.array((0, 255, 0))).all(axis=2))
    assert ys_c.min() > ys_a.max()  # c on the second row
    assert sheet.shape[1] == 2 * (80 + 8) + 8
    with pytest.raises(InvalidArgument):
        contact_sheet([a], ["a", "b"], 1, "t")
    with pytest.raises(InvalidArgument):
        contact_sheet([], [], 1, "t")


# --- verdict matrix -------------------------------------------------------------------------


def test_verdict_matrix_colours_each_cell_by_verdict() -> None:
    rows, cols = ["iPhone", "Pixel"], ["upright", "leaning"]
    grid = [["PASS", "WARN"], ["FAIL", "SKIP"]]
    img = verdict_matrix(rows, cols, grid)
    for i in range(2):
        for j in range(2):
            x0, y0, _, _ = verdict_matrix_cell(rows, cols, i, j)
            assert tuple(img[y0 + 1, x0 + 1]) == VERDICT_COLORS[grid[i][j]]
    assert (
        VERDICT_COLORS["PASS"]
        != VERDICT_COLORS["WARN"]
        != VERDICT_COLORS["FAIL"]
        != VERDICT_COLORS["SKIP"]
    )
    risk = verdict_matrix(["x"], ["y"], [["RISK"]])
    x0, y0, _, _ = verdict_matrix_cell(["x"], ["y"], 0, 0)
    assert tuple(risk[y0 + 1, x0 + 1]) == VERDICT_COLORS["WARN"]


def test_verdict_matrix_rejects_bad_input() -> None:
    with pytest.raises(InvalidArgument):
        verdict_matrix(["a"], ["b"], [["MAYBE"]])
    with pytest.raises(InvalidArgument):
        verdict_matrix(["a", "b"], ["c"], [["PASS"]])


# --- layer bands ----------------------------------------------------------------------------


def test_layer_band_plot_line_height_tracks_layer_height() -> None:
    heights = [0.1] * 20 + [0.2] * 20
    tops = list(np.cumsum(heights))
    img = layer_band_plot(tops, heights)
    line = (img == np.array((30, 90, 200))).all(axis=2)
    ys, xs = np.nonzero(line)
    mid = (xs.min() + xs.max()) // 2
    assert (
        ys[xs < mid - 20].mean() > ys[xs > mid + 20].mean() + 20
    )  # thin layers drawn lower on the page


def test_layer_band_plot_shades_mix_ranges_only_when_given() -> None:
    heights = [0.12] * 50
    tops = list(np.cumsum(heights))
    band = (252, 226, 190)
    plain = layer_band_plot(tops, heights)
    mixed = layer_band_plot(tops, heights, mix_ranges=[(2.0, 4.0)])
    assert not (plain == np.array(band)).all(axis=2).any()
    shaded = (mixed == np.array(band)).all(axis=2)
    xs = np.nonzero(shaded[40:].any(axis=0))[
        0
    ]  # below the legend swatch drawn in the top strip
    # 2..4 mm of a 6 mm Z span is a third of the plot width (900 - 70 - 24 px)
    assert (xs.max() - xs.min()) == pytest.approx((900 - 94) * 2.0 / 6.0, rel=0.05)


def test_layer_band_plot_rejects_mismatched_lengths() -> None:
    with pytest.raises(InvalidArgument):
        layer_band_plot([1.0, 2.0], [0.2])


# --- stability ------------------------------------------------------------------------------

SQUARE = np.array([[-20, -20], [20, -20], [20, 20], [-20, 20]], dtype=float)


def test_stability_marks_com_at_its_projected_position() -> None:
    img = stability_diagram(SQUARE, (0, 0), size=(700, 700))
    assert (
        tuple(img[350, 350]) == COM_COLOR
    )  # symmetric polygon, centred CoM -> image centre
    assert tuple(img[560, 140]) == SUPPORT_FILL  # (-15, -15) mm at 14 px/mm


def test_stability_load_com_is_drawn_where_it_is() -> None:
    img = stability_diagram(SQUARE, (0, 0), (10, 0), size=(700, 700))
    assert tuple(img[350, 490]) == LOAD_COM_COLOR  # x = 10 mm -> 350 + 140
    assert tuple(img[350, 350]) == COM_COLOR


def test_stability_com_outside_polygon_extends_the_view() -> None:
    img = stability_diagram(SQUARE, (30, 0), size=(700, 700))
    # bbox x -20..30, y -20..20 -> 11.2 px/mm, centre x = 5: CoM at 350 + 25*11.2 = 630
    assert tuple(img[350, 630]) == COM_COLOR
    assert tuple(img[350, 350]) == SUPPORT_FILL


def test_stability_margin_arrow_only_when_margin_given() -> None:
    arrow = ARROW_COLOR
    without = stability_diagram(SQUARE, (0, 0), (10, 0))
    with_margin = stability_diagram(SQUARE, (0, 0), (10, 0), 10.0)
    assert not (without == np.array(arrow)).all(axis=2).any()
    ys, xs = np.nonzero((with_margin == np.array(arrow)).all(axis=2))
    # nearest edge of the loaded CoM at (10, 0) is x = 20: the arrow spans 10 mm = 140 px to the right of it
    on_row = ys == 350
    assert xs[on_row].max() - 490 >= 130


def test_stability_rejects_degenerate_polygon() -> None:
    with pytest.raises(InvalidArgument):
        stability_diagram(SQUARE[:2], (0, 0))


# --- profile with slab ----------------------------------------------------------------------


def test_slab_corners_follow_the_phone_fit_convention() -> None:
    upright = slab_corners(
        {"y0": 1.0, "z0": 2.0, "phi_deg": 0.0, "thickness": 3.0, "height": 10.0}
    )
    assert np.allclose(upright, [[1, 2], [1, 12], [-2, 12], [-2, 2]])
    lying = slab_corners(
        {"y0": 0.0, "z0": 0.0, "phi_deg": 90.0, "thickness": 3.0, "height": 10.0}
    )
    assert np.allclose(lying, [[0, 0], [10, 0], [10, 3], [0, 3]], atol=1e-9)
    with pytest.raises(InvalidArgument):
        slab_corners({"y0": 0.0})


def test_profile_shows_slab_and_flags_overlap_with_the_model() -> None:
    cube = trimesh.creation.box(extents=(40, 40, 40))  # YZ section is [-20, 20]^2
    blend = np.array([196, 149, 123])
    overlapping = {
        "y0": 0.0,
        "z0": 0.0,
        "phi_deg": 0.0,
        "thickness": 5.0,
        "height": 15.0,
        "width": 30.0,
    }
    img = profile_with_slab(cube, overlapping)
    assert int((np.abs(img.astype(int) - blend).max(axis=2) <= 2).sum()) > 500
    clear = {"y0": 40.0, "z0": 0.0, "phi_deg": 10.0, "thickness": 5.0, "height": 15.0}
    img = profile_with_slab(cube, clear)
    assert int((np.abs(img.astype(int) - blend).max(axis=2) <= 2).sum()) == 0
    # over the white background the slab is the same orange mixed with white instead
    over_white = np.array([244, 180, 136])
    assert int((np.abs(img.astype(int) - over_white).max(axis=2) <= 2).sum()) > 500
