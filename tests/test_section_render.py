"""Engineering cross-sections: violet hatched cut faces, per-part colours elsewhere (#586)."""

from __future__ import annotations

import pathlib

import numpy as np
import pytest
from dimension_fixtures import BLUE, GREEN, RED, Box, write_3mf

pytest.importorskip("PIL")
trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")

from dimension_model import RenderError, load_raw  # noqa: E402
from dimension_parts import build_model  # noqa: E402
from PIL import Image  # noqa: E402
from section_render import (  # noqa: E402
    CUT_FILL,
    CUT_HATCH,
    cut_model,
    render,
)

OUTER, INNER = 20.0, 10.0
RING_AREA = OUTER**2 - INNER**2  # 300 mm^2: a hollow box cut through its middle


@pytest.fixture(scope="module")
def hollow(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A 20 mm cube with a closed 10 mm cavity: its section is a ring, not a square."""
    outer = trimesh.creation.box(extents=(OUTER, OUTER, OUTER))
    inner = trimesh.creation.box(extents=(INNER, INNER, INNER))
    solid = outer.difference(inner, engine="manifold")
    path = tmp_path_factory.mktemp("hollow") / "hollow.stl"
    solid.export(path)
    return str(path)


def _area(cap: np.ndarray) -> float:
    cross = np.cross(cap[:, 1] - cap[:, 0], cap[:, 2] - cap[:, 0])
    return float(np.linalg.norm(cross, axis=1).sum() / 2.0)


def _cut(path: str, plane: str, keep: str, at: float | None = None) -> object:
    return cut_model(build_model(load_raw(path), "t"), plane, keep, at)


@pytest.mark.parametrize("plane", ["YZ", "XZ", "XY"])
@pytest.mark.parametrize("keep", ["neg", "pos"])
def test_cap_is_the_ring_whatever_plane_and_side(
    hollow: str, plane: str, keep: str
) -> None:
    cut = _cut(hollow, plane, keep)
    assert cut.open_loops == 0  # type: ignore[attr-defined]
    assert _area(cut.cap) == pytest.approx(RING_AREA, rel=1e-3)  # type: ignore[attr-defined]
    axis = "XYZ".index({"YZ": "X", "XZ": "Y", "XY": "Z"}[plane])
    assert np.allclose(cut.cap[:, :, axis], cut.at)  # type: ignore[attr-defined]  # the cap lies ON the plane


def test_a_cut_through_the_cavity_wall_only_has_no_hole(hollow: str) -> None:
    """3 mm from the outside the plane misses the 10 mm cavity: the cap is the whole 20 x 20 square."""
    cut = _cut(hollow, "XZ", "neg", at=-OUTER / 2 + 3.0)
    assert _area(cut.cap) == pytest.approx(OUTER**2, rel=1e-3)  # type: ignore[attr-defined]


def test_a_plane_outside_the_model_is_a_clear_error(hollow: str) -> None:
    with pytest.raises(RenderError, match="misses the model"):
        _cut(hollow, "XZ", "neg", at=500.0)


def _count(image: Image.Image, rgb: tuple[int, ...], tol: int = 6) -> int:
    arr = np.asarray(image.convert("RGB")).astype(int)
    return int((np.abs(arr - np.array(rgb)).max(axis=2) <= tol).sum())


def test_picture_has_a_violet_hatched_cut_face_and_is_exactly_the_size(
    hollow: str, tmp_path: pathlib.Path
) -> None:
    out = str(tmp_path / "s.png")
    info = render(hollow, out, plane="XZ", size=(777, 555))
    image = Image.open(out)
    assert image.size == (777, 555)
    fill = tuple(int(CUT_FILL[i : i + 2], 16) for i in (1, 3, 5))
    assert _count(image, fill) > 2000  # the violet cut face
    assert _count(image, CUT_HATCH, tol=3) > 300  # its hatch lines and outline
    assert info["open_outlines"] == 0 and info["cap_triangles"] > 0
    assert info["legend"] is not None  # the legend always names the "cut face"


def test_hatch_lines_run_at_45_degrees(hollow: str, tmp_path: pathlib.Path) -> None:
    out = str(tmp_path / "s.png")
    render(hollow, out, plane="XZ", size=(1200, 900))
    arr = np.asarray(Image.open(out).convert("RGB")).astype(int)
    ink = (np.abs(arr - np.array(CUT_HATCH)).max(axis=2) <= 3)[100:800]
    ys, xs = np.nonzero(ink)
    # On a 45-degree line rising to the right x + y is constant: the hatch pixels cluster on few values.
    sums = np.bincount((xs + ys) % 13)  # step is 13 px horizontally at 1200 px width
    assert sums.max() > 3 * sums.mean()


def test_cavity_is_not_painted_as_cut_face(hollow: str, tmp_path: pathlib.Path) -> None:
    """The cut face is a ring: the cavity pixel at the picture centre is not the violet fill."""
    out = str(tmp_path / "s.png")
    render(hollow, out, plane="XZ", size=(1200, 900))
    image = Image.open(out).convert("RGB")
    fill = np.array([int(CUT_FILL[i : i + 2], 16) for i in (1, 3, 5)])
    cx, cy = 600, 46 + (900 - 46 - 70) // 2
    patch = np.asarray(image.crop((cx - 6, cy - 6, cx + 6, cy + 6))).astype(int)
    assert not (np.abs(patch - fill).max(axis=2) <= 6).all()


def test_two_parts_keep_distinct_colours_and_get_a_legend(
    tmp_path: pathlib.Path,
) -> None:
    model = write_3mf(
        tmp_path / "a.3mf",
        [Box("lid", 0, RED), Box("base", 20, GREEN), Box("clip", 40, BLUE)],
    )
    out = str(tmp_path / "a.png")
    info = render(str(model), out, plane="XZ", at=5.0, parts=[f"{BLUE}=PCB:stand_in"])
    assert {p["name"] for p in info["parts"]} == {"lid", "base", "PCB"}
    assert (
        len({p["color"] for p in info["parts"]} | {CUT_FILL}) == 4
    )  # no part takes the cut-face violet
    image = Image.open(out)
    strip = np.asarray(image.convert("RGB"))[info["legend"]["box"][1] :]
    for part in info["parts"]:
        want = np.array([int(part["color"][i : i + 2], 16) for i in (1, 3, 5)])
        assert (strip == want).all(axis=2).sum() > 50, part["name"]
    assert info["cap_triangles"] > 0


def test_a_part_close_to_the_cut_violet_is_given_another_colour(
    tmp_path: pathlib.Path,
) -> None:
    model = write_3mf(
        tmp_path / "a.3mf", [Box("violet", 0, "#B28ADA"), Box("other", 20, GREEN)]
    )
    info = render(str(model), str(tmp_path / "a.png"), plane="XZ", at=5.0)
    violet = next(p for p in info["parts"] if p["name"] == "violet")
    assert violet["color"] != "#B28ADA" and violet["color_source"] == "palette"


def test_size_too_small_for_the_legend_is_a_clear_error(tmp_path: pathlib.Path) -> None:
    boxes = [Box(f"a rather long part name {i}", 15 * i) for i in range(30)]
    model = write_3mf(tmp_path / "a.3mf", boxes)
    with pytest.raises(RenderError, match="no room"):
        render(
            str(model), str(tmp_path / "a.png"), plane="XZ", at=5.0, size=(1200, 300)
        )


def test_a_plane_grazing_the_far_face_stays_inside_the_model(
    hollow: str, tmp_path: pathlib.Path
) -> None:
    """9.9999999 is inside a [-10, 10] cube: the grazing nudge must not push it past the face."""
    info = render(
        hollow,
        str(tmp_path / "s.png"),
        plane="YZ",
        keep="pos",
        at=OUTER / 2 - 1e-7,
        size=(600, 450),
    )
    assert OUTER / 2 - 1e-2 < info["at_mm"] < OUTER / 2


def test_the_output_folder_is_created(hollow: str, tmp_path: pathlib.Path) -> None:
    out = tmp_path / "previews" / "deeper" / "sec.png"
    render(hollow, str(out), plane="XZ", size=(600, 450))
    assert out.is_file()


def test_the_nearer_surface_is_painted_over_the_farther_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Two overlapping triangles, red nearer the viewer than green: the overlap shows red."""
    import section_render as sr
    from dimension_model import RawGroup, RawModel

    eye, right, up = sr._basis("YZ", "neg")
    centre = np.array([-20.0, 0.0, 0.0])
    near = np.array([centre - 2 * right - 2 * up, centre + 2 * right - 2 * up, centre + 2 * up])
    far = near - 10 * eye  # farther from the viewer along the eye
    anchor = trimesh.creation.box(extents=(2, 2, 2))
    anchor.apply_translation([0, 40, 0])
    verts = np.vstack([near, far, anchor.vertices])
    faces = np.vstack([[0, 1, 2], [3, 4, 5], anchor.faces + 6])
    raw = RawModel(
        verts,
        faces,
        np.array([0, 1] + [2] * len(anchor.faces)),
        [
            RawGroup("near", "red", "#FF0000", object_id="near"),
            RawGroup("far", "green", "#00FF00", object_id="far"),
            RawGroup("anchor", "blue", "#0000FF", object_id="anchor"),
        ],
    )
    monkeypatch.setattr(sr, "load_raw", lambda *a, **k: raw)
    out = tmp_path / "depth.png"
    sr.render("unused.3mf", str(out), at=0)
    arr = np.asarray(Image.open(out).convert("RGB")).astype(int)[:700]  # the view, not the legend swatches
    red = (arr[:, :, 0] > 120) & (arr[:, :, 1] < 80) & (arr[:, :, 2] < 80)
    green = (arr[:, :, 1] > 120) & (arr[:, :, 0] < 80) & (arr[:, :, 2] < 80)
    # orthographic: the green triangle sits exactly behind the red one, so none of it may show
    assert red.sum() > 0 and green.sum() < 0.02 * red.sum()
