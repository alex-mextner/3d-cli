"""threed_vision.bank: thumbnails from (textured) meshes, silhouette descriptors, the on-disk index and retrieval, plus the
`bank` / `recognise` commands in-process, all with fake encoders (numpy + OpenCV + trimesh, CI-runnable)."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
try:  # CI installs neither trimesh nor torch: only the mesh-rendering tests need it
    import trimesh
except ImportError:
    trimesh = None  # type: ignore[assignment]
needs_trimesh = pytest.mark.skipif(
    trimesh is None, reason="trimesh not installed (packages/threed-vision[bank])"
)
PIL_Image = pytest.importorskip("PIL.Image")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "threed-vision" / "src"))

from threed_vision import bank, cli, io, recognise  # noqa: E402
from threed_vision.errors import BadInput  # noqa: E402
from vision_fakes import NAMES, PALETTE, patch_encoders, scene  # noqa: E402

# --- helpers ------------------------------------------------------------------------------------------------------


def _ellipse(
    shape: tuple[int, int], centre: tuple[int, int], axes: tuple[int, int], angle: float
) -> Any:
    m = np.zeros(shape, np.uint8)
    cv2.ellipse(m, centre, axes, angle, 0, 360, 1, -1)
    return m.astype(bool)


def _best_flip_iou(a: Any, b: Any) -> float:
    return max(
        float((v & b).sum() / (v | b).sum()) for v in bank.silhouette_variants(a)
    )


def _coloured_box(
    extents: tuple[float, float, float], colour: tuple[int, int, int]
) -> Any:
    mesh = trimesh.creation.box(extents=extents)
    mesh.visual.face_colors = np.tile(
        np.array([*colour, 255], np.uint8), (len(mesh.faces), 1)
    )
    return mesh


# --- views ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("view", bank.VIEWS)
def test_every_view_is_a_proper_rotation(view: str) -> None:
    for ext in ([100.0, 40.0, 10.0], [10.0, 100.0, 40.0], [40.0, 10.0, 100.0]):
        m = bank.view_matrix(np.array(ext), view)
        np.testing.assert_allclose(m @ m.T, np.eye(3), atol=1e-9)
        assert np.linalg.det(m) == pytest.approx(1.0), (
            "a mirrored view would flip the object"
        )


def test_longest_axis_view_puts_the_longest_extent_vertical_and_the_shortest_towards_the_camera() -> (
    None
):
    m = bank.view_matrix(np.array([10.0, 100.0, 40.0]), "longest-axis")
    u, v, w = np.abs(m)
    assert np.argmax(v) == 1 and np.argmax(u) == 2 and np.argmax(w) == 0


# --- thumbnails from meshes -----------------------------------------------------------------------------------------


@needs_trimesh
def test_render_lays_the_tool_flat_and_reports_mm_per_px() -> None:
    mesh = trimesh.creation.box(
        extents=(10.0, 100.0, 40.0)
    )  # long axis Y, deliberately not Z
    r = bank.render_mesh(mesh, "longest-axis", px=192)
    ys, xs = np.nonzero(r.silhouette)
    height, width = np.ptp(ys) + 1, np.ptp(xs) + 1
    assert height == pytest.approx(192 * 0.9, abs=2)
    assert width / height == pytest.approx(40 / 100, abs=0.03)
    assert r.extents_mm == [100.0, 40.0, 10.0]
    assert r.mm_per_px == pytest.approx(100.0 / (192 * 0.9), rel=1e-6)
    assert not r.textured
    assert tuple(r.image[0, 0]) == bank.BG_RGB


@needs_trimesh
def test_unit_scale_converts_metres_to_millimetres() -> None:
    mesh = trimesh.creation.box(extents=(0.01, 0.1, 0.04))
    r = bank.render_mesh(mesh, "longest-axis", unit_scale=1000.0)
    assert r.extents_mm == [100.0, 40.0, 10.0]
    assert r.mm_per_px == pytest.approx(100.0 / (192 * 0.9), rel=1e-6)
    with pytest.raises(BadInput, match="unit-scale"):
        bank.render_mesh(mesh, unit_scale=0.0)


@needs_trimesh
def test_untextured_mesh_is_flagged_and_rendered_shaded_grey_blue() -> None:
    r = bank.render_mesh(trimesh.creation.box(extents=(100, 40, 10)))
    assert not r.textured
    ys, xs = np.nonzero(r.silhouette)
    pix = r.image[ys, xs].astype(float)
    assert pix[:, 2].mean() > pix[:, 0].mean(), "base colour is blue-grey"


@needs_trimesh
def test_vertex_coloured_mesh_renders_its_colour_and_counts_as_textured() -> None:
    r = bank.render_mesh(_coloured_box((100.0, 40.0, 10.0), (220, 30, 30)))
    assert r.textured
    ys, xs = np.nonzero(r.silhouette)
    mean = r.image[ys, xs].mean(axis=0)
    assert (
        mean[0] > 120 and mean[1] < 60 and mean[2] < 60
    )  # red, only darkened by the shading


@needs_trimesh
def test_image_textured_mesh_samples_the_texture() -> None:
    mesh = trimesh.creation.box(extents=(100.0, 40.0, 10.0))
    tex = np.zeros((8, 8, 3), np.uint8)
    tex[:] = (30, 200, 30)  # a plain green texture
    uv = np.full((len(mesh.vertices), 2), 0.5)
    mesh.visual = trimesh.visual.TextureVisuals(uv=uv, image=PIL_Image.fromarray(tex))
    r = bank.render_mesh(mesh)
    assert r.textured
    ys, xs = np.nonzero(r.silhouette)
    mean = r.image[ys, xs].mean(axis=0)
    assert mean[1] > 100 and mean[0] < 60 and mean[2] < 60


@needs_trimesh
def test_texture_sampling_follows_the_uv_of_each_face() -> None:
    mesh = trimesh.creation.box(extents=(1.0, 1.0, 1.0))
    tex = np.zeros((2, 2, 3), np.uint8)
    tex[0, 0], tex[0, 1], tex[1, 0], tex[1, 1] = (
        (255, 0, 0),
        (0, 255, 0),
        (0, 0, 255),
        (255, 255, 0),
    )
    left = mesh.vertices[:, 0] < 0
    uv = np.where(
        left[:, None], np.array([[0.1, 0.9]]), np.array([[0.9, 0.9]])
    )  # top-left red / top-right green
    vis = trimesh.visual.TextureVisuals(uv=uv, image=PIL_Image.fromarray(tex))
    colors = bank._texture_face_colors(mesh, vis)
    assert colors is not None and len(colors) == len(mesh.faces)
    assert {tuple(c) for c in colors} <= {(255, 0, 0), (0, 255, 0)}
    assert (colors[:, 0] == 255).any() and (colors[:, 1] == 255).any()


@needs_trimesh
@pytest.mark.parametrize("suffix", [".glb", ".obj"])
def test_image_textured_mesh_files_keep_their_texture_through_build_thumbnails(
    tmp_path: Path, suffix: str
) -> None:
    mesh = trimesh.creation.box(extents=(120.0, 30.0, 12.0))
    tex = np.zeros((64, 64, 3), np.uint8)
    tex[:, :32], tex[:, 32:] = (
        (220, 30, 30),
        (30, 30, 220),
    )  # red left half, blue right half
    uv = np.c_[(mesh.vertices[:, 0] > 0) * 0.75 + 0.1, np.full(len(mesh.vertices), 0.5)]
    mesh.visual = trimesh.visual.TextureVisuals(uv=uv, image=PIL_Image.fromarray(tex))
    src = tmp_path / "meshes"
    src.mkdir()
    mesh.export(src / f"widget{suffix}")
    path = bank.build_thumbnails(src, tmp_path / "bank", px=96)
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["textured"] is True and "warning" not in doc
    thumb = cv2.cvtColor(cv2.imread(str(path.parent / "widget.png")), cv2.COLOR_BGR2RGB)
    sil = cv2.imread(str(path.parent / "widget_sil.png"), cv2.IMREAD_GRAYSCALE) > 0
    pix = thumb[sil].astype(int)
    assert (pix[:, 0] > pix[:, 2] + 60).any() and (pix[:, 2] > pix[:, 0] + 60).any(), (
        "both texture colours appear"
    )


@needs_trimesh
def test_thumbnails_directory_writes_bank_json_with_warning_when_grey(
    tmp_path: Path,
) -> None:
    src = tmp_path / "meshes"
    src.mkdir()
    trimesh.creation.box(extents=(120, 30, 12)).export(src / "screwdriver_1.stl")
    trimesh.creation.box(extents=(60, 60, 20)).export(src / "tape_roll_1.stl")
    (src / "readme.txt").write_text("not a mesh", encoding="utf-8")
    path = bank.build_thumbnails(src, tmp_path / "bank", px=96)
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["textured"] is False and doc["n_textured"] == 0
    assert "untextured" in doc["warning"] and "colour" in doc["warning"]
    by_id = {e["id"]: e for e in doc["entries"]}
    assert set(by_id) == {"screwdriver_1", "tape_roll_1"}
    sd = by_id["screwdriver_1"]
    assert (
        sd["category"] == "screwdriver"
        and by_id["tape_roll_1"]["category"] == "roll of tape"
    )
    assert (
        sd["textured"] is False
        and sd["extents_mm"] == [120.0, 30.0, 12.0]
        and sd["mm_per_px"] > 0
    )
    thumb = cv2.imread(str(path.parent / sd["thumb"]))
    sil = cv2.imread(str(path.parent / sd["sil"]), cv2.IMREAD_GRAYSCALE)
    assert (
        thumb.shape == (96, 96, 3)
        and sil.shape == (96, 96)
        and set(np.unique(sil)) <= {0, 255}
    )


@needs_trimesh
def test_coloured_bank_has_no_warning_and_honours_the_categories_map(
    tmp_path: Path,
) -> None:
    src = tmp_path / "meshes"
    (src / "nested").mkdir(parents=True)
    _coloured_box((100, 20, 10), (200, 20, 20)).export(src / "thing.ply")
    _coloured_box((100, 20, 10), (20, 200, 20)).export(
        src / "nested" / "thing.ply"
    )  # same stem: ids stay unique
    (src / "categories.json").write_text(
        json.dumps({"thing": "gadget"}), encoding="utf-8"
    )
    doc = json.loads(
        bank.build_thumbnails(src, tmp_path / "bank", "top", 64).read_text(
            encoding="utf-8"
        )
    )
    assert doc["textured"] is True and "warning" not in doc and doc["view"] == "top"
    assert [e["id"] for e in doc["entries"]] == ["thing", "thing_2"]
    assert all(e["category"] == "gadget" and e["textured"] for e in doc["entries"])


def test_hand_made_bank_json_is_rebased_with_its_photos(tmp_path: Path) -> None:
    src = tmp_path / "photos"
    src.mkdir()
    cv2.imwrite(str(src / "a.png"), np.full((50, 30, 3), 200, np.uint8))
    (src / "bank.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "id": "a",
                        "category": "hammer",
                        "thumb": "a.png",
                        "extents_mm": [250, 40, 30],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    path = bank.build_thumbnails(src / "bank.json", tmp_path / "out")
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert (path.parent / "a.png").is_file()
    assert doc["entries"][0]["textured"] is True and doc["textured"] is True
    assert doc["entries"][0]["extents_mm"] == [250, 40, 30]


@needs_trimesh
def test_thumbnail_input_errors(tmp_path: Path) -> None:
    with pytest.raises(BadInput, match="unknown view"):
        bank.build_thumbnails(tmp_path, tmp_path / "o", "side")
    with pytest.raises(BadInput, match="not found"):
        bank.build_thumbnails(tmp_path / "missing", tmp_path / "o")
    with pytest.raises(BadInput, match="no meshes"):
        bank.build_thumbnails(tmp_path, tmp_path / "o")
    (tmp_path / "broken.stl").write_text("garbage", encoding="utf-8")
    with pytest.raises(BadInput, match="cannot read mesh|no triangles"):
        bank.build_thumbnails(tmp_path, tmp_path / "o")
    with pytest.raises(BadInput, match="at least 32"):
        bank.render_mesh(trimesh.creation.box(), px=8)
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    with pytest.raises(BadInput, match="not valid JSON"):
        bank.read_bank(tmp_path / "bad.json")


# --- silhouette descriptor ------------------------------------------------------------------------------------------


def test_silhouette_descriptor_is_translation_scale_and_rotation_invariant() -> None:
    base = bank.silhouette_descriptor(_ellipse((300, 400), (200, 150), (120, 30), 30))
    moved = bank.silhouette_descriptor(
        _ellipse((600, 500), (100, 420), (60, 15), -50)
    )  # other place, half size, other angle
    huge = bank.silhouette_descriptor(
        _ellipse((3000, 3000), (1500, 1500), (1200, 300), 30)
    )  # area-averaged downscale
    assert base.shape == (bank.SIL_SIZE, bank.SIL_SIZE) and base.dtype == bool
    assert _best_flip_iou(base, moved) > 0.93
    assert _best_flip_iou(base, huge) > 0.93


def test_silhouette_descriptor_puts_the_longest_axis_vertical() -> None:
    mask = np.zeros((200, 200), bool)
    mask[90:110, 20:180] = True  # a wide bar
    d = bank.silhouette_descriptor(mask)
    ys, xs = np.nonzero(d)
    assert np.ptp(ys) > 4 * np.ptp(xs)
    assert (np.ptp(ys) + 1) == pytest.approx(bank.SIL_SIZE * 0.9, abs=2)


def test_silhouette_descriptor_separates_different_shapes_and_handles_empty() -> None:
    bar = np.zeros((200, 200), bool)
    bar[20:180, 90:110] = True
    disc = _ellipse((200, 200), (100, 100), (60, 60), 0)
    assert (
        _best_flip_iou(
            bank.silhouette_descriptor(bar), bank.silhouette_descriptor(disc)
        )
        < 0.4
    )
    assert not bank.silhouette_descriptor(np.zeros((50, 50), bool)).any()
    variants = bank.silhouette_variants(bank.silhouette_descriptor(bar))
    assert variants.shape == (4, bank.SIL_SIZE, bank.SIL_SIZE)


def test_align_object_makes_a_rotated_tool_upright() -> None:
    rgb = np.full((300, 300, 3), 128, np.uint8)
    mask = np.zeros((300, 300), np.uint8)
    cv2.fillPoly(
        mask, [np.array([[60, 230], [75, 245], [240, 80], [225, 65]], np.int32)], 1
    )  # a diagonal bar
    rgb[mask > 0] = (220, 30, 30)
    h, w = bank.align_object(rgb, mask.astype(bool)).shape[:2]
    assert h > 3 * w, "the longest axis ends up vertical"
    views = bank.object_views(rgb, mask.astype(bool))
    np.testing.assert_array_equal(views[1], views[0][::-1, ::-1])
    assert bank.object_views(rgb, None)[0] is rgb


def test_hair_thin_long_masks_do_not_crash_the_descriptor() -> None:
    """A 3 px wide, 3000 px long mask shrinks to under one pixel across when scaled to the descriptor: it must survive."""
    horizontal = np.zeros((3200, 3200), bool)
    horizontal[1000:1003, 100:3100] = True
    vertical = np.zeros((3200, 3200), bool)
    vertical[100:3100, 2000:2003] = True
    a, b = bank.silhouette_descriptor(horizontal), bank.silhouette_descriptor(vertical)
    assert a.any() and b.any(), "the outline of a thin object is kept"
    assert _best_flip_iou(a, b) > 0.8, "and it is rotation invariant"


def test_category_rules_match_whole_words_and_name_only_default_categories() -> None:
    assert bank.category_from_name("jigsaw_blade") == "other"
    assert bank.category_from_name("seesaw") == "other"
    assert bank.category_from_name("drill_bit_set") == "bit set"
    assert bank.category_from_name("cordless_drill_12v") == "cordless drill"
    assert bank.category_from_name("spare_drill_battery") == "battery"
    assert bank.category_from_name("Hand-Saw 2") == "saw"
    assert {c for c, _ in bank.CATEGORY_RULES} <= set(recognise.DEFAULT_CATEGORIES)


@needs_trimesh
def test_material_only_colour_counts_as_textured_but_trimesh_defaults_do_not() -> None:
    red = trimesh.creation.box(extents=(100.0, 30.0, 10.0))
    red.visual = trimesh.visual.TextureVisuals(
        material=trimesh.visual.material.PBRMaterial(baseColorFactor=[220, 30, 30, 255])
    )
    r = bank.render_mesh(red)
    assert r.textured
    ys, xs = np.nonzero(r.silhouette)
    assert r.image[ys, xs].mean(axis=0)[0] > 120
    plain = trimesh.creation.box(extents=(100.0, 30.0, 10.0))
    plain.visual = trimesh.visual.TextureVisuals(
        material=trimesh.visual.material.PBRMaterial()
    )
    assert not bank.render_mesh(plain).textured


@needs_trimesh
def test_mesh_stems_whose_png_names_would_collide_stay_distinct(tmp_path: Path) -> None:
    src = tmp_path / "meshes"
    src.mkdir()
    trimesh.creation.box(extents=(100, 30, 10)).export(src / "x.stl")
    trimesh.creation.box(extents=(40, 40, 10)).export(
        src / "x_sil.stl"
    )  # its thumbnail "x_sil.png" is x's silhouette name
    path = bank.build_thumbnails(src, tmp_path / "bank", px=64)
    entries = json.loads(path.read_text(encoding="utf-8"))["entries"]
    files = [e[k] for e in entries for k in ("thumb", "sil")]
    assert len(files) == len(set(files)) == 4
    by_id = {e["id"]: e for e in entries}
    sil_of_x = cv2.imread(str(path.parent / by_id["x"]["sil"]), cv2.IMREAD_GRAYSCALE)
    assert set(np.unique(sil_of_x)) <= {0, 255}, (
        "x's silhouette was not overwritten by an RGB thumbnail"
    )


# --- index save / load / query --------------------------------------------------------------------------------------


def _coloured_bank_dir(tmp_path: Path) -> Path:
    """A 5-entry bank: one filled rectangle per colour, on the thumbnail background, with a silhouette."""
    d = tmp_path / "bank"
    d.mkdir()
    entries = []
    shapes = {
        "red": (20, 60),
        "green": (50, 50),
        "blue": (30, 40),
        "yellow": (60, 30),
        "cyan": (40, 70),
    }
    for name, (w, h) in shapes.items():
        img = np.full((192, 192, 3), bank.BG_RGB, np.uint8)
        sil = np.zeros((192, 192), np.uint8)
        y0, x0 = (192 - h * 2) // 2, (192 - w * 2) // 2
        img[y0 : y0 + h * 2, x0 : x0 + w * 2] = PALETTE[name]
        sil[y0 : y0 + h * 2, x0 : x0 + w * 2] = 255
        cv2.imwrite(str(d / f"{name}.png"), cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
        cv2.imwrite(str(d / f"{name}_sil.png"), sil)
        entries.append(
            {
                "id": f"{name}_1",
                "name": name,
                "category": name,
                "thumb": f"{name}.png",
                "sil": f"{name}_sil.png",
                "extents_mm": [float(h), float(w), 5.0],
                "mm_per_px": 0.5,
                "textured": True,
            }
        )
    (d / "bank.json").write_text(
        json.dumps(
            {"version": 1, "view": "longest-axis", "px": 192, "entries": entries}
        ),
        encoding="utf-8",
    )
    return d


def test_index_round_trips_through_disk_without_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_encoders(monkeypatch, bank)
    d = _coloured_bank_dir(tmp_path)
    out = bank.build_index(d / "bank.json", tmp_path / "idx", device="cpu")
    assert (out / "index.npz").is_file() and (out / "index.json").is_file()
    assert (out / "red.png").is_file(), "the index directory is self-contained"

    def boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("loading an index must not touch a model")

    monkeypatch.setattr(bank, "ClipEncoder", boom)
    monkeypatch.setattr(bank, "DinoEncoder", boom)
    idx = bank.load_index(out)
    assert [e["id"] for e in idx.entries] == [
        f"{n}_1" for n in ("red", "green", "blue", "yellow", "cyan")
    ]
    assert (
        idx.clip.shape == (5, len(NAMES))
        and idx.dino.shape[0] == 5
        and idx.sil.shape == (5, bank.SIL_SIZE, bank.SIL_SIZE)
    )
    assert idx.meta["n"] == 5 and idx.meta["models"] == {
        "clip": "clip-b32",
        "dino": "dinov2-s",
    }
    assert idx.textured_fraction == 1.0 and idx.root == out
    assert idx.sil.dtype == bool and idx.sil.any(axis=(1, 2)).all()
    again = bank.load_index(out / "index.json")  # a file inside the directory works too
    np.testing.assert_array_equal(again.clip, idx.clip)
    np.testing.assert_array_equal(again.sil, idx.sil)
    expected = bank.silhouette_descriptor(
        cv2.imread(str(d / "red_sil.png"), cv2.IMREAD_GRAYSCALE) > 0
    )
    np.testing.assert_array_equal(idx.sil[0], expected)


def test_index_in_place_and_embeddings_are_unit_vectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_encoders(monkeypatch, bank)
    d = _coloured_bank_dir(tmp_path)
    out = bank.build_index(d)
    assert out == d
    idx = bank.load_index(d)
    np.testing.assert_allclose(np.linalg.norm(idx.clip, axis=1), 1.0, atol=1e-5)
    np.testing.assert_allclose(np.linalg.norm(idx.dino, axis=1), 1.0, atol=1e-5)


def test_index_build_unloads_encoders_it_created_but_not_injected_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip, dino = patch_encoders(monkeypatch, bank)
    d = _coloured_bank_dir(tmp_path)
    bank.build_index(d / "bank.json", tmp_path / "a")
    assert not clip.loaded and not dino.loaded
    bank.build_index(d / "bank.json", tmp_path / "b", clip=clip, dino=dino)  # type: ignore[arg-type]
    assert clip.loaded and dino.loaded


def test_query_ranks_the_matching_colour_first_and_reports_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_encoders(monkeypatch, bank)
    out = bank.build_index(_coloured_bank_dir(tmp_path), tmp_path / "idx")
    rgb, instances = scene()
    for k, name in enumerate(NAMES[:5]):
        mask = instances[k]["mask"]
        hits = bank.query(out, rgb, 5, mask=mask, metric="clip+dino")
        assert hits[0]["category"] == name and hits[0]["metric"] == "clip+dino"
        assert [h["score"] for h in hits] == sorted(
            (h["score"] for h in hits), reverse=True
        )
    best = bank.query(out, rgb, 3, mask=instances[0]["mask"], metric="clip")
    assert len(best) == 3
    assert set(best[0]) == {
        "bank_id",
        "name",
        "category",
        "score",
        "metric",
        "extents_mm",
        "thumb",
    }
    assert Path(best[0]["thumb"]).is_file() and Path(best[0]["thumb"]).parent == out
    assert best[0]["extents_mm"] == [60.0, 20.0, 5.0]


def test_query_without_mask_uses_the_whole_crop_and_forbids_silhouette(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_encoders(monkeypatch, bank)
    out = bank.build_index(_coloured_bank_dir(tmp_path), tmp_path / "idx")
    crop = np.full((80, 40, 3), PALETTE["blue"], np.uint8)
    assert bank.query(out, crop, 1, metric="clip")[0]["category"] == "blue"
    assert bank.query(out, crop, 1)[0]["metric"] == "clip+dino", (
        "auto without a mask = appearance"
    )
    with pytest.raises(BadInput, match="needs a mask"):
        bank.query(out, crop, 1, metric="sil")
    with pytest.raises(BadInput, match="HxWx3"):
        bank.query(out, crop[:, :, 0], 1)


def test_query_silhouette_metric_ranks_by_shape_with_no_encoder_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_encoders(monkeypatch, bank)
    out = bank.build_index(_coloured_bank_dir(tmp_path), tmp_path / "idx")

    def boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("the silhouette metric must not load an encoder")

    monkeypatch.setattr(bank, "ClipEncoder", boom)
    monkeypatch.setattr(bank, "DinoEncoder", boom)
    # a 100x180 px upright rectangle has the aspect of the 'cyan' entry (40 x 70 -> 4:7)... use the exact cyan shape instead
    rgb = np.full((300, 300, 3), 128, np.uint8)
    mask = np.zeros((300, 300), bool)
    mask[40:180, 100:180] = True  # 80 x 140 = the 'cyan' 40x70 box
    hits = bank.query(out, rgb, 5, mask=mask, metric="sil")
    assert hits[0]["category"] == "cyan" and hits[0]["score"] > 0.9
    rotated = np.rot90(mask)  # lying down: the descriptor is rotation invariant
    assert (
        bank.query(out, rgb, 1, mask=np.ascontiguousarray(rotated), metric="sil")[0][
            "category"
        ]
        == "cyan"
    )


def test_query_category_filter_topk_and_auto_metric(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_encoders(monkeypatch, bank)
    out = bank.build_index(_coloured_bank_dir(tmp_path), tmp_path / "idx")
    rgb, instances = scene()
    hits = bank.query(
        out, rgb, 5, mask=instances[0]["mask"], categories=["green", "blue"]
    )
    assert {h["category"] for h in hits} == {"green", "blue"}
    assert bank.query(out, rgb, 0, mask=instances[0]["mask"]) == []
    idx = bank.load_index(out)
    assert bank.resolve_metric(idx, "auto", True) == "clip+dino"
    idx.entries[0]["textured"] = idx.entries[1]["textured"] = idx.entries[2][
        "textured"
    ] = False
    assert bank.resolve_metric(idx, "auto", True) == "sil", (
        "a mostly grey bank has no appearance to compare"
    )
    assert bank.resolve_metric(idx, "auto", False) == "clip+dino"
    with pytest.raises(BadInput, match="unknown metric"):
        bank.resolve_metric(idx, "euclid", True)
    with pytest.raises(BadInput, match="unknown metric"):
        bank.metric_parts("clip+euclid")


def test_load_index_errors_name_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(BadInput) as err:
        bank.load_index(tmp_path)
    assert "3d bank index" in err.value.render()
    patch_encoders(monkeypatch, bank)
    out = bank.build_index(_coloured_bank_dir(tmp_path), tmp_path / "idx")
    doc = json.loads((out / "index.json").read_text(encoding="utf-8"))
    doc["entries"].pop()
    (out / "index.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(BadInput, match="inconsistent"):
        bank.load_index(out)


def _photo_bank(tmp_path: Path, *, with_sil: bool = False) -> Path:
    """A hand-made bank of real-photo style entries: no `textured`, no `sil`, same file name in two class folders."""
    d = tmp_path / "photos"
    entries = []
    for cls, colour in (("hammer", "red"), ("pliers", "blue")):
        (d / cls).mkdir(parents=True)
        img = np.full((120, 60, 3), PALETTE[colour], np.uint8)
        cv2.imwrite(str(d / cls / "1.png"), cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
        entry = {
            "id": f"{cls}_1",
            "category": cls,
            "thumb": f"{cls}/1.png",
            "extents_mm": [250, 40, 30],
        }
        if with_sil:
            sil = np.zeros((120, 60), np.uint8)
            sil[10:110, 10:50] = 255
            cv2.imwrite(str(d / cls / "1_sil.png"), sil)
            entry["sil"] = f"{cls}/1_sil.png"
        entries.append(entry)
    (d / "bank.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")
    return d


def test_photo_bank_indexed_directly_is_textured_and_keeps_distinct_thumbnails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_encoders(monkeypatch, bank)
    d = _photo_bank(tmp_path)
    out = bank.build_index(d / "bank.json", tmp_path / "idx")
    idx = bank.load_index(out)
    assert (
        idx.textured_fraction == 1.0
        and bank.resolve_metric(idx, "auto", True) == "clip+dino"
    )
    thumbs = [
        Path(h["thumb"])
        for h in bank.query(
            idx, np.full((50, 50, 3), PALETTE["red"], np.uint8), 2, metric="clip"
        )
    ]
    assert [t.parent.name for t in thumbs] == ["hammer", "pliers"], (
        "folders keep the same-named photos apart"
    )
    red = cv2.cvtColor(cv2.imread(str(thumbs[0])), cv2.COLOR_BGR2RGB)
    assert tuple(red[0, 0]) == PALETTE["red"]
    rebased = bank.build_thumbnails(d / "bank.json", tmp_path / "rebased")
    doc = json.loads(rebased.read_text(encoding="utf-8"))
    assert (
        doc["textured"] is True
        and (rebased.parent / "hammer" / "1.png").is_file()
        and (rebased.parent / "pliers" / "1.png").is_file()
    )
    assert not (rebased.parent / "1.png").exists()


def test_entries_without_a_silhouette_are_left_out_of_shape_matching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_encoders(monkeypatch, bank)
    out = bank.build_index(_photo_bank(tmp_path) / "bank.json", tmp_path / "idx")
    rgb = np.zeros((100, 100, 3), np.uint8)
    mask = np.zeros((100, 100), bool)
    mask[10:90, 40:60] = True
    assert bank.query(out, rgb, 5, mask=mask, metric="sil") == [], (
        "no silhouettes: nothing to compare, not a list of zeros"
    )
    both = bank.build_index(
        _photo_bank(tmp_path / "with", with_sil=True) / "bank.json", tmp_path / "idx2"
    )
    assert len(bank.query(both, rgb, 5, mask=mask, metric="sil")) == 2


def test_rebuilding_an_index_refreshes_its_thumbnails_and_leaves_no_temp_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_encoders(monkeypatch, bank)
    d = _coloured_bank_dir(tmp_path)
    out = bank.build_index(d / "bank.json", tmp_path / "idx")
    first_build = bank.load_index(out).meta["build_id"]
    cv2.imwrite(
        str(d / "red.png"),
        cv2.cvtColor(
            np.full((192, 192, 3), PALETTE["magenta"], np.uint8), cv2.COLOR_RGB2BGR
        ),
    )
    bank.build_index(d / "bank.json", out)
    idx = bank.load_index(out)
    assert idx.meta["build_id"] != first_build
    assert (
        tuple(cv2.cvtColor(cv2.imread(str(out / "red.png")), cv2.COLOR_BGR2RGB)[0, 0])
        == PALETTE["magenta"]
    )
    assert sorted(p.name for p in out.glob("*.tmp")) == []


def test_index_and_bank_inputs_that_would_escape_or_mismatch_are_bad_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_encoders(monkeypatch, bank)
    d = _coloured_bank_dir(tmp_path)
    doc = json.loads((d / "bank.json").read_text(encoding="utf-8"))
    escape = tmp_path / "escape"
    shutil.copytree(d, escape)
    for field, value, needle in (
        ("thumb", "../bank/red.png", "must be relative"),
        ("thumb", "/etc/hostname", "must be relative"),
        ("sil", "missing_sil.png", "cannot read silhouette"),
        ("thumb", "missing.png", "cannot read thumbnail"),
    ):
        broken = json.loads(json.dumps(doc))
        broken["entries"][0][field] = value
        (escape / "bank.json").write_text(json.dumps(broken), encoding="utf-8")
        with pytest.raises(BadInput, match=needle):
            bank.build_index(escape / "bank.json")
    wrong_size = json.loads(json.dumps(doc))
    cv2.imwrite(str(d / "small_sil.png"), np.full((10, 10), 255, np.uint8))
    wrong_size["entries"][0]["sil"] = "small_sil.png"
    (d / "bank.json").write_text(json.dumps(wrong_size), encoding="utf-8")
    with pytest.raises(BadInput, match="silhouette is 10x10"):
        bank.build_index(d / "bank.json", tmp_path / "idx")
    dup = {"entries": [{"id": "a", "thumb": "a.png"}, {"id": "a", "thumb": "b.png"}]}
    (escape / "dup.json").write_text(json.dumps(dup), encoding="utf-8")
    with pytest.raises(BadInput, match="unique 'id'"):
        bank.read_bank(escape / "dup.json")


def test_stale_or_mixed_index_files_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_encoders(monkeypatch, bank)
    d = _coloured_bank_dir(tmp_path)
    one = bank.build_index(d / "bank.json", tmp_path / "one")
    two = bank.build_index(d / "bank.json", tmp_path / "two")
    (one / "index.npz").write_bytes(
        (two / "index.npz").read_bytes()
    )  # new embeddings next to the old entry list
    with pytest.raises(BadInput, match="different builds"):
        bank.load_index(one)
    doc = json.loads((two / "index.json").read_text(encoding="utf-8"))
    doc["meta"]["sil_size"] = 128
    (two / "index.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(BadInput, match="this build reads"):
        bank.load_index(two)


def test_masks_of_the_wrong_size_are_bad_input_not_index_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_encoders(monkeypatch, bank, recognise)
    out = bank.build_index(_coloured_bank_dir(tmp_path), tmp_path / "idx")
    rgb, instances = scene()
    with pytest.raises(BadInput, match="mask is 5x5"):
        bank.query(out, rgb, 1, mask=np.ones((5, 5), bool))
    with pytest.raises(BadInput, match="instance 1: the mask is 5x5"):
        recognise.Recogniser("cpu").classify(
            rgb, [{"id": 1, "mask": np.ones((5, 5), bool)}], NAMES
        )
    with pytest.raises(BadInput, match="at least 1"):
        recognise.Recogniser("cpu").classify(
            rgb, instances, NAMES, bank=out, bank_topk=0
        )
    with pytest.raises(BadInput, match="unknown metric"):
        recognise.Recogniser("cpu").classify(
            rgb, instances, NAMES, bank=out, metric="euclid"
        )


# --- commands, in process -------------------------------------------------------------------------------------------


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_commands_are_discovered() -> None:
    cmds = cli.discover()
    assert (
        "bank" in cmds
        and "recognise" in cmds
        and cmds["bank"].SUMMARY
        and cmds["recognise"].SUMMARY
    )


@needs_trimesh
def test_bank_thumbs_index_query_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_encoders(monkeypatch, bank)
    src = tmp_path / "meshes"
    src.mkdir()
    _coloured_box((100, 30, 10), (220, 30, 30)).export(src / "screwdriver_red.ply")
    _coloured_box((60, 60, 20), (30, 30, 220)).export(src / "tape_roll_blue.ply")
    code, out, err = _run(
        capsys,
        "bank",
        "thumbs",
        str(src),
        "-o",
        str(tmp_path / "bank"),
        "--px",
        "96",
        "--json",
    )
    assert code == 0 and err == "", "--json keeps stderr quiet when nothing is wrong"
    assert json.loads(out)["textured"] is True
    code, out, _ = _run(capsys, "bank", "index", str(tmp_path / "bank"), "--json")
    meta = json.loads(out)
    assert code == 0 and meta["n"] == 2 and meta["index"] == str(tmp_path / "bank")
    photo = np.full((200, 300, 3), 128, np.uint8)
    cv2.rectangle(photo, (40, 40), (140, 70), PALETTE["red"], -1)
    mask = np.zeros((200, 300), np.uint8)
    cv2.rectangle(mask, (40, 40), (140, 70), 255, -1)
    io.save_png(tmp_path / "photo.png", photo)
    io.save_png(tmp_path / "mask.png", mask)
    code, out, _ = _run(
        capsys,
        "bank",
        "query",
        str(tmp_path / "photo.png"),
        "--index",
        str(tmp_path / "bank"),
        "--mask",
        str(tmp_path / "mask.png"),
        "--topk",
        "2",
        "--json",
    )
    doc = json.loads(out)
    assert code == 0 and doc["masked"] and len(doc["matches"]) == 2
    assert (
        doc["matches"][0]["bank_id"] == "screwdriver_red"
        and doc["metric"] == "clip+dino"
    )
    code, out, _ = _run(
        capsys,
        "bank",
        "query",
        str(tmp_path / "photo.png"),
        "--index",
        str(tmp_path / "bank"),
        "--mask",
        str(tmp_path / "mask.png"),
        "--metric",
        "sil",
        "--category",
        "roll of tape",
    )
    assert code == 0 and "tape_roll_blue" in out and "screwdriver_red" not in out


@needs_trimesh
def test_bank_thumbs_warns_on_stderr_for_a_grey_bank(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "meshes"
    src.mkdir()
    trimesh.creation.box(extents=(100, 30, 10)).export(src / "x.stl")
    code, out, err = _run(
        capsys, "bank", "thumbs", str(src), "-o", str(tmp_path / "b"), "--px", "64"
    )
    assert (
        code == 0 and "WARNING" in err and "untextured" in err and "rendered x" in err
    )
    assert "1 entries (0 textured)" in out


def test_bank_command_error_exits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _run(
        capsys, "bank", "query", str(tmp_path / "nope.png"), "--index", str(tmp_path)
    )
    assert code == 2 and "image not found" in err
    img = tmp_path / "i.png"
    io.save_png(img, np.zeros((20, 20, 3), np.uint8))
    code, _, err = _run(
        capsys, "bank", "query", str(img), "--index", str(tmp_path / "missing")
    )
    assert code == 2 and "3d bank index" in err
    code, _, err = _run(
        capsys, "bank", "query", str(img), "--index", str(tmp_path), "--id", "3"
    )
    assert code == 2 and "--instances and --id go together" in err
    code, _, err = _run(
        capsys,
        "bank",
        "query",
        str(img),
        "--index",
        str(tmp_path),
        "--mask",
        str(tmp_path / "m.png"),
        "--instances",
        str(tmp_path / "i.json"),
        "--id",
        "1",
    )
    assert code == 2 and "alternatives" in err
    io.save_png(tmp_path / "m.png", np.zeros((5, 5), bool))
    code, _, err = _run(
        capsys,
        "bank",
        "query",
        str(img),
        "--index",
        str(tmp_path),
        "--mask",
        str(tmp_path / "m.png"),
    )
    assert code == 2 and "mask is 5x5" in err


def _segment_dir(tmp_path: Path) -> tuple[Path, Path]:
    """A synthetic photo + a `3d segment`-shaped instances.json (8 coloured rectangles)."""
    rgb, instances = scene()
    photo = tmp_path / "photo.png"
    io.save_png(photo, rgb)
    seg = tmp_path / "seg"
    records = []
    for inst in instances:
        rel = f"masks/{inst['id']:03d}.png"
        io.save_png(seg / rel, inst["mask"])
        records.append(
            {
                "id": inst["id"],
                "source": "amg",
                "bbox_xywh": io.mask_bbox_xywh(inst["mask"]),
                "area_px": int(inst["mask"].sum()),
                "mask": rel,
            }
        )
    io.write_json(
        seg / "instances.json",
        {
            "image": {"path": str(photo), "w": rgb.shape[1], "h": rgb.shape[0]},
            "instances": records,
        },
    )
    return photo, seg / "instances.json"


def test_recognise_command_json_and_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_encoders(monkeypatch, recognise, bank, bias=2.0)
    photo, inst = _segment_dir(tmp_path)
    cats = tmp_path / "cats.txt"
    cats.write_text("\n".join(NAMES), encoding="utf-8")
    code, out, _ = _run(
        capsys,
        "recognise",
        str(photo),
        "--instances",
        str(inst),
        "--categories",
        str(cats),
        "--topk",
        "2",
        "--json",
    )
    doc = json.loads(out)
    assert (
        code == 0
        and doc["calibrated"]
        and doc["categories"] == 8
        and doc["n_crops"] == 8
    )
    assert [r["text_topk"][0]["label"] for r in doc["instances"]] == NAMES
    code, out, _ = _run(
        capsys,
        "recognise",
        str(photo),
        "--instances",
        str(inst),
        "--categories",
        ",".join(NAMES),
        "--no-calibrate",
        "--ids",
        "2,3",
    )
    assert code == 0 and "#2" in out and "#1 " not in out and "calibrated=False" in out
    code, _, err = _run(
        capsys,
        "recognise",
        str(photo),
        "--instances",
        str(inst),
        "--categories",
        ",".join(NAMES),
        "--ids",
        "99",
    )
    assert code == 2 and "no instance with id" in err


def test_recognise_command_with_a_bank_and_input_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_encoders(monkeypatch, recognise, bank, bias=2.0)
    photo, inst = _segment_dir(tmp_path)
    out_dir = bank.build_index(_coloured_bank_dir(tmp_path), tmp_path / "idx")
    code, out, _ = _run(
        capsys,
        "recognise",
        str(photo),
        "--instances",
        str(inst),
        "--categories",
        ",".join(NAMES),
        "--bank",
        str(out_dir),
        "--topk",
        "1",
        "--bank-topk",
        "2",
        "--rerank-classes",
        "1",
        "--json",
    )
    first = json.loads(out)["instances"][0]
    assert (
        code == 0
        and first["bank_topk"][0]["bank_id"] == "red_1"
        and first["bank_restricted_to"] == ["red"]
    )
    code, _, err = _run(
        capsys, "recognise", str(tmp_path / "x.png"), "--instances", str(inst)
    )
    assert code == 2 and "image not found" in err
    code, _, err = _run(
        capsys, "recognise", str(photo), "--instances", str(tmp_path / "none.json")
    )
    assert code == 2 and "3d segment" in err
    small = tmp_path / "small.png"
    io.save_png(small, np.zeros((10, 10, 3), np.uint8))
    code, _, err = _run(capsys, "recognise", str(small), "--instances", str(inst))
    assert code == 2 and "the image is 10x10" in err
    code, _, err = _run(
        capsys, "recognise", str(photo), "--instances", str(inst), "--categories", "a,a"
    )
    assert code == 2 and "duplicate" in err


def test_recognise_command_validates_ids_and_metric_before_any_model_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clip, _ = patch_encoders(monkeypatch, recognise, bank)
    photo, inst = _segment_dir(tmp_path)
    code, _, err = _run(
        capsys, "recognise", str(photo), "--instances", str(inst), "--ids", "3,a"
    )
    assert code == 2 and "comma-separated integers" in err
    code, _, err = _run(
        capsys, "recognise", str(photo), "--instances", str(inst), "--ids", "99"
    )
    assert code == 2 and "no instance with id [99]" in err
    with pytest.raises(
        SystemExit
    ) as exit_info:  # argparse rejects an unknown metric even without --bank
        cli.main(
            ["recognise", str(photo), "--instances", str(inst), "--metric", "euclid"]
        )
    assert exit_info.value.code == 2
    capsys.readouterr()
    code, _, err = _run(
        capsys,
        "recognise",
        str(photo),
        "--instances",
        str(inst),
        "--bank-topk",
        "0",
        "--bank",
        str(tmp_path),
    )
    assert code == 2 and "must be at least 1" in err
    assert clip.image_calls == 0, "every one of those failed before CLIP ran"


def test_recognise_command_rejects_malformed_instances_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    photo, inst = _segment_dir(tmp_path)
    doc = json.loads(inst.read_text(encoding="utf-8"))
    del doc["instances"][0]["mask"]
    inst.write_text(json.dumps(doc), encoding="utf-8")
    code, _, err = _run(capsys, "recognise", str(photo), "--instances", str(inst))
    assert code == 2 and "not a `3d segment` instances.json" in err
    inst.write_text(json.dumps({"instances": [5]}), encoding="utf-8")
    code, _, err = _run(capsys, "recognise", str(photo), "--instances", str(inst))
    assert code == 2 and "not a `3d segment` instances.json" in err


def test_empty_mask_corrupt_index_and_sil_free_all_metric(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_encoders(monkeypatch, bank)
    out = bank.build_index(_photo_bank(tmp_path) / "bank.json", tmp_path / "idx")
    rgb = np.zeros((40, 40, 3), np.uint8)
    with pytest.raises(BadInput, match="is empty"):
        bank.query(out, rgb, 1, mask=np.zeros((40, 40), bool))
    mask = np.zeros((40, 40), bool)
    mask[5:35, 15:25] = True
    assert len(bank.query(out, rgb, 5, mask=mask, metric="all")) == 2, (
        "appearance parts still rank entries without silhouettes"
    )
    (out / "index.npz").write_bytes(b"")
    with pytest.raises(BadInput, match="cannot load a bank index"):
        bank.load_index(out)


@pytest.mark.parametrize(
    ("patch", "needle"),
    [
        ({"thumb": 5}, "'thumb' must be a string"),
        ({"thumb": None}, "needs a 'thumb' file name"),
        ({"extents_mm": "250x40"}, "'extents_mm' must be a list of 3 numbers"),
        ({"extents_mm": [1, 2]}, "'extents_mm' must be a list of 3 numbers"),
        ({"extents_mm": [1, "a", 3]}, "'extents_mm' must be a list of 3 numbers"),
        ({"mm_per_px": -1}, "'mm_per_px' must be a positive number"),
        ({"textured": "yes"}, "'textured' must be true or false"),
        ({"category": ["a"]}, "'category' must be a string"),
        ({"id": ["x"]}, "unique 'id'"),
        ({"id": ""}, "unique 'id'"),
    ],
)
def test_hand_made_bank_json_with_wrong_field_types_is_bad_input(
    tmp_path: Path, patch: dict[str, Any], needle: str
) -> None:
    entry: dict[str, Any] = {"id": "a", "thumb": "a.png", "extents_mm": [250, 40, 30]}
    entry.update(patch)
    (tmp_path / "bank.json").write_text(
        json.dumps({"entries": [entry]}), encoding="utf-8"
    )
    with pytest.raises(BadInput, match=needle):
        bank.read_bank(tmp_path / "bank.json")
    good = {
        "entries": [
            {
                "id": "a",
                "thumb": "a.png",
                "extents_mm": [250, 40.5, 30],
                "mm_per_px": 0.5,
                "textured": False,
            }
        ]
    }
    (tmp_path / "bank.json").write_text(json.dumps(good), encoding="utf-8")
    assert bank.read_bank(tmp_path)["entries"][0]["textured"] is False


def test_lopsided_masks_are_not_clipped_by_the_descriptor_frame() -> None:
    """A heavy handle + thin blade: its centre of mass is far from the middle of its extent, yet both ends must stay in frame."""
    mask = np.zeros((400, 200), bool)
    mask[10:70, 70:130] = True  # fat head 60 px wide
    mask[70:390, 95:105] = (
        True  # 320 px long thin shaft: the centre of mass sits near the head
    )
    d = bank.silhouette_descriptor(mask)
    ys = np.nonzero(d.any(axis=1))[0]
    assert ys.min() >= 1 and ys.max() <= bank.SIL_SIZE - 2, (
        "no row of the object touches or leaves the frame"
    )
    assert abs((ys.min() + ys.max()) / 2 - bank.SIL_SIZE / 2) < 3, (
        "the object is centred by its extent"
    )
    assert np.ptp(ys) + 1 == pytest.approx(bank.SIL_SIZE * 0.9, abs=2)


def test_thumbnail_and_query_crops_show_the_object_at_the_same_scale() -> None:
    thumb = np.full((192, 192, 3), bank.BG_RGB, np.uint8)
    thumb[20:172, 70:120] = (200, 30, 30)
    sil = np.zeros((192, 192), np.uint8)
    sil[20:172, 70:120] = 255
    from_thumb = bank._thumb_object(thumb, sil, "t")
    photo = np.full((500, 400, 3), 128, np.uint8)
    photo[100:400, 150:250] = (200, 30, 30)
    mask = np.zeros((500, 400), bool)
    mask[100:400, 150:250] = True
    from_query = bank.object_views(photo, mask)[0]
    assert from_thumb.shape[0] / from_thumb.shape[1] == pytest.approx(
        from_query.shape[0] / from_query.shape[1], rel=0.1
    )
    assert abs(from_thumb.shape[0] - 152) <= 6, (
        "tight crop: no 12 % margin around the thumbnail object"
    )


def test_hand_made_bank_and_index_documents_with_wrong_types_are_bad_input(
    tmp_path: Path,
) -> None:
    entry = {"id": "a", "thumb": "a.png"}
    for extra in ({"px": None}, {"px": "192px"}, {"view": 3}):
        (tmp_path / "bank.json").write_text(
            json.dumps({"entries": [entry], **extra}), encoding="utf-8"
        )
        with pytest.raises(
            BadInput, match="'view' must be a string and 'px' an integer"
        ):
            bank.read_bank(tmp_path / "bank.json")
    d = tmp_path / "idx"
    d.mkdir()
    np.savez(
        d / "index.npz",
        clip=np.zeros((1, 2), np.float32),
        dino=np.zeros((1, 2), np.float32),
        sil=np.zeros((1, 1), np.uint8),
        build_id=np.array("x"),
    )
    bad_docs: list[dict[str, Any]] = [
        {"meta": [], "entries": [{}]},
        {"meta": {}, "entries": [1]},
    ]
    for bad in bad_docs:
        (d / "index.json").write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(BadInput, match="cannot load a bank index"):
            bank.load_index(d)


@needs_trimesh
def test_a_tool_modelled_diagonally_still_lies_flat_with_its_true_extents() -> None:
    mesh = trimesh.creation.box(extents=(200.0, 24.0, 24.0))
    mesh.apply_transform(
        trimesh.transformations.rotation_matrix(np.radians(45), [0, 0, 1])
    )  # lying at 45 deg on the bed
    r = bank.render_mesh(mesh, "longest-axis", px=192)
    assert r.extents_mm == pytest.approx([200.0, 24.0, 24.0], abs=0.2)
    ys, xs = np.nonzero(r.silhouette)
    assert np.ptp(ys) > 5 * np.ptp(xs), "the long axis ends up vertical"
    assert r.mm_per_px == pytest.approx(200.0 / (192 * 0.9), rel=0.02)
    axis_aligned = bank.render_mesh(
        trimesh.creation.box(extents=(200.0, 24.0, 24.0)), "longest-axis", px=192
    )
    assert np.ptp(np.nonzero(axis_aligned.silhouette)[0]) > 5 * np.ptp(
        np.nonzero(axis_aligned.silhouette)[1]
    )


def test_descriptor_fits_a_body_with_a_longer_cross_extent() -> None:
    """A heavy 120x60 body with a thin 260 px lead sticking out sideways: the lead is longer than the body's principal axis."""
    mask = np.zeros((300, 400), bool)
    mask[100:160, 20:140] = True
    mask[128:132, 100:360] = True
    d = bank.silhouette_descriptor(mask)
    xs = np.nonzero(d.any(axis=0))[0]
    ys = np.nonzero(d.any(axis=1))[0]
    assert max(np.ptp(xs), np.ptp(ys)) + 1 >= bank.SIL_SIZE * 0.9 - 2
    assert (
        xs.min() >= 1
        and xs.max() <= bank.SIL_SIZE - 2
        and ys.min() >= 1
        and ys.max() <= bank.SIL_SIZE - 2
    )


@needs_trimesh
def test_categories_map_names_the_file_stem_before_the_generated_id(
    tmp_path: Path,
) -> None:
    src = tmp_path / "meshes"
    (src / "a").mkdir(parents=True)
    (src / "b").mkdir()
    for rel in ("a/thing.stl", "b/thing.stl", "thing_2.stl"):
        trimesh.creation.box(extents=(100, 30, 10)).export(src / rel)
    (src / "categories.json").write_text(json.dumps({"thing_2": "X"}), encoding="utf-8")
    entries = json.loads(
        bank.build_thumbnails(src, tmp_path / "bank", px=64).read_text(encoding="utf-8")
    )["entries"]
    labelled = [e["category"] for e in entries]
    assert labelled.count("X") == 1, (
        "only the file called thing_2.stl gets the category"
    )


def test_index_arrays_of_the_wrong_shape_are_bad_input(tmp_path: Path) -> None:
    d = tmp_path / "idx"
    d.mkdir()
    meta = {"version": bank.BANK_VERSION, "sil_size": bank.SIL_SIZE, "build_id": "x"}
    (d / "index.json").write_text(
        json.dumps({"meta": meta, "entries": [{"id": "a"}]}), encoding="utf-8"
    )
    good = {
        "clip": np.zeros((1, 2), np.float32),
        "dino": np.zeros((1, 2), np.float32),
        "build_id": np.array("x"),
    }
    for sil in (np.zeros((1, 3), np.uint8), np.zeros(5, np.uint8)):
        np.savez(d / "index.npz", sil=sil, **good)
        with pytest.raises(BadInput, match="inconsistent"):
            bank.load_index(d)
    np.savez(
        d / "index.npz",
        sil=np.zeros((1, 1152), np.uint8),
        clip=np.float32(1.0),
        dino=good["dino"],
        build_id=good["build_id"],
    )
    with pytest.raises(BadInput, match="inconsistent"):
        bank.load_index(d)
