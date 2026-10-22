"""threed_vision loft geometry, hard rasteriser, closed-form `fit_auto`, `view_check`, mesh export (numpy + OpenCV; trimesh optional)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vision_fit_synth as S  # noqa: E402  (also puts packages/threed-vision/src on sys.path)

from threed_vision import view_check  # noqa: E402
from threed_vision.errors import BadInput  # noqa: E402
from threed_vision.fit import fit_auto  # noqa: E402
from threed_vision.io import to_jsonable  # noqa: E402
from threed_vision.loft import build_faces, loft_vertices, make_fit  # noqa: E402
from threed_vision.mesh_export import FORMATS, emit_mesh, export_mesh  # noqa: E402
from threed_vision.render import hard_mask, mesh_from_fit, view_iou  # noqa: E402


def _json_ok(obj: object) -> None:
    json.dumps(to_jsonable(obj), allow_nan=False)


def gt_fit(name: str, scene: dict, *, rot_err: np.ndarray | None = None) -> dict:
    """The ground-truth parameters of a synthetic scene as a fit dict (optionally with a wrong pose)."""
    spec = S.GT[name]
    w = S.prof(spec["w"], 8)
    R = scene["R"] if rot_err is None else S.rodrigues(rot_err) @ scene["R"]
    return make_fit(
        template=spec["template"],
        K=8,
        L=spec["L"],
        c=np.zeros(8),
        w=w,
        aspect=spec["aspect"],
        R0=R,
        t_mm=scene["t"],
        method="soft",
    )


# ------------------------------------------------------------------ geometry
def test_loft_mesh_is_closed_and_extents_match_dims() -> None:
    K, M = 8, 24
    w = np.linspace(5, 12, K)
    V = loft_vertices(180.0, np.zeros(K), w, 0.4, 4.0, M)
    F = build_faces(K, M)
    edges = np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    assert (counts == 2).all(), (
        "every edge must be shared by exactly two triangles (watertight)"
    )
    ext = V.max(0) - V.min(0)
    assert ext == pytest.approx([180.0, 2 * 12.0, 2 * 0.4 * 12.0], rel=1e-9)


def test_fit_schema_roundtrips_through_json() -> None:
    sc = S.topdown_scene("screwdriver", 30)
    fit = fit_auto(sc["views"], known_depth=sc["depth"], planar=True)
    again = json.loads(json.dumps(to_jsonable(fit)))
    V1, _ = mesh_from_fit(fit)
    V2, _ = mesh_from_fit(again)
    assert np.allclose(V1, V2)
    for key in (
        "kind",
        "template",
        "params",
        "dims_mm",
        "pose",
        "views",
        "confidence",
        "timing_s",
        "method",
        "tries",
        "attempts",
        "closed_form",
    ):
        assert key in fit
    assert set(fit["params"]) == {"L_mm", "c_mm", "w_mm", "aspect"}
    assert set(fit["dims_mm"]) == {"length_mm", "max_width_mm", "thickness_mm"}
    assert set(fit["pose"]) == {"R0", "t_mm", "axis"}
    assert set(fit["confidence"]) == {
        "train_min_iou",
        "heldout_iou",
        "n_views",
        "status",
    }


# ------------------------------------------------------------------ hard rasteriser guards
def test_hard_mask_degenerate_mesh_is_empty_not_an_error() -> None:
    V = np.array([[0, 0, 650.0], [np.nan, 0, 650], [0, 5, 650]])
    F = np.array([[0, 1, 2]])
    m = hard_mask(V, F, S.KF, np.eye(3), np.zeros(3), S.SIZE)
    assert m.shape == (S.SIZE[1], S.SIZE[0]) and not m.any()
    assert not hard_mask(
        np.array([[np.inf, 0, 1], [0, 0, 1], [0, 1, 1.0]]),
        F,
        S.KF,
        np.eye(3),
        np.zeros(3),
        S.SIZE,
    ).any()


def test_hard_mask_huge_and_behind_camera_coordinates_are_fast_and_finite() -> None:
    V = np.array(
        [[-1e12, -1e12, 650.0], [1e12, 0, 650.0], [0, 1e12, 650.0], [0, 0, -50.0]]
    )
    F = np.array([[0, 1, 2], [0, 1, 3]])
    t0 = time.perf_counter()
    m = hard_mask(V, F, S.KF, np.eye(3), np.zeros(3), S.SIZE)
    assert time.perf_counter() - t0 < 2.0
    assert m.dtype == bool and m.shape == (S.SIZE[1], S.SIZE[0])


def test_view_iou_matches_full_frame_iou() -> None:
    sc = S.topdown_scene("pliers_flat", 20)
    m = sc["views"][0]["mask"]
    shifted = np.roll(m, 3, axis=1)
    roi = view_iou(shifted, sc["V"], sc["F"], S.KF, np.eye(3), np.zeros(3))
    full_model = hard_mask(sc["V"], sc["F"], S.KF, np.eye(3), np.zeros(3), S.SIZE)
    full = float((full_model & shifted).sum() / (full_model | shifted).sum())
    assert roi == pytest.approx(full, abs=1e-9)


# ------------------------------------------------------------------ input validation
@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda v: v.update(mask=np.zeros_like(v["mask"])), "foreground"),
        (lambda v: v.update(mask=np.pad(np.ones((1, 1), bool), 50)), "foreground"),
        (lambda v: v.update(K=np.eye(2)), "3x3"),
        (lambda v: v.update(K=np.array([[0.0, 0, 576], [0, 0.0, 768], [0, 0, 1.0]])), "focal"),
        (lambda v: v.update(t=np.array([np.nan, 0, 650.0])), "NaN"),
        (lambda v: v.update(R=np.eye(3) * 2), "rotation"),
    ],
    ids=["empty", "one-pixel", "K-shape", "K-zero", "t-nan", "R-scaled"],
)
def test_bad_views_are_bad_input(mutate, match: str) -> None:
    v = dict(S.topdown_scene("screwdriver")["views"][0])
    mutate(v)
    with pytest.raises(BadInput, match=match):
        fit_auto([v], known_depth=650.0, planar=True)


def test_bad_arguments_are_bad_input() -> None:
    sc = S.topdown_scene("screwdriver")
    with pytest.raises(BadInput, match="at least one view"):
        fit_auto([])
    with pytest.raises(BadInput, match="known_depth"):
        fit_auto(sc["views"])  # one view, no scale
    with pytest.raises(BadInput, match="known_depth"):
        fit_auto(sc["views"], planar=True)
    with pytest.raises(BadInput, match="unknown fixed dimension"):
        fit_auto(sc["views"], known_depth=650.0, fixed_dims={"height_mm": 3})
    with pytest.raises(BadInput, match="positive finite"):
        fit_auto(sc["views"], known_depth=650.0, fixed_dims={"length_mm": float("nan")})
    with pytest.raises(BadInput, match="unknown template"):
        fit_auto(sc["views"], known_depth=650.0, template="sphere")


def test_two_views_without_parallax_are_bad_input() -> None:
    v = S.topdown_scene("pliers_flat")["views"][0]
    with pytest.raises(BadInput, match="parallax"):
        fit_auto([v, dict(v)])


# ------------------------------------------------------------------ closed form fit_auto
@pytest.mark.parametrize(
    ("name", "template", "p"),
    [("box_elongated", "box", 8.0), ("screwdriver", "lathe", 2.0)],
)
def test_template_follows_closed_form_kind_and_no_optimiser_runs(
    name: str, template: str, p: float
) -> None:
    sc = S.topdown_scene(name, 30)
    fit = fit_auto(sc["views"], known_depth=sc["depth"], planar=True)
    assert fit["template"]["name"] == template and fit["template"]["p"] == p
    assert (
        fit["method"] == "closed_form" and fit["tries"] == 0 and fit["device"] is None
    )
    assert fit["timing_s"]["refine"] == 0.0 and fit["timing_s"]["cma"] == 0.0
    assert (
        fit["confidence"]["status"] == "single_view"
        and fit["confidence"]["train_min_iou"] >= 0.9
    )
    assert fit["dims_mm"]["length_mm"] == pytest.approx(
        sc["spec"]["L"], rel=0.03
    )  # scale recovery of a calibrated single view
    _json_ok(fit)


def test_closed_form_is_milliseconds() -> None:
    sc = S.topdown_scene("box_elongated", 30)
    t = time.perf_counter()
    fit = fit_auto(sc["views"], known_depth=sc["depth"], planar=True)
    assert (
        fit["method"] == "closed_form" and time.perf_counter() - t < 2.0
    )  # generous: shared CI box; measured ~0.1-0.2 s


def test_template_override_wins_over_kind() -> None:
    sc = S.topdown_scene("box_elongated", 30)
    fit = fit_auto(
        sc["views"],
        known_depth=sc["depth"],
        planar=True,
        template="flat",
        iou_accept=0.0,
    )
    assert fit["template"] == {
        "name": "flat",
        "p": 4.0,
        "stations": 8,
        "ring": fit["template"]["ring"],
    }


def test_thickness_is_the_template_prior_in_a_single_top_down_view() -> None:
    sc = S.topdown_scene("box_elongated", 30)
    fit = fit_auto(sc["views"], known_depth=sc["depth"], planar=True)
    assert fit["params"]["aspect"] == pytest.approx(
        0.5
    )  # box prior; unobservable from above
    assert fit["dims_mm"]["thickness_mm"] == pytest.approx(
        0.5 * fit["dims_mm"]["max_width_mm"]
    )


def test_fixed_dims_are_exact_hard_constraints_in_closed_form() -> None:
    sc = S.topdown_scene("box_elongated", 30)
    fixed = {
        "length_mm": 236.0,
        "max_width_mm": 31.5,
        "thickness_mm": 9.25,
    }  # a few % off the measurement, like a user correcting scale
    fit = fit_auto(
        sc["views"],
        known_depth=sc["depth"],
        planar=True,
        fixed_dims=fixed,
        iou_accept=0.0,
    )
    for k, v in fixed.items():
        assert fit["dims_mm"][k] == pytest.approx(v, rel=1e-12)
    V, _ = export_mesh(
        fit
    )  # length and thickness are exact in the mesh; the lateral extent also carries the centre-line offsets c_mm
    ext = V.max(0) - V.min(0)
    assert ext[0] == pytest.approx(236.0, rel=1e-9) and ext[2] == pytest.approx(
        9.25, rel=1e-9
    )
    assert 31.5 <= ext[1] <= 31.5 + np.ptp(fit["params"]["c_mm"]) + 1e-9
    assert fit["fixed_dims"] == fixed
    partial = fit_auto(
        sc["views"],
        known_depth=sc["depth"],
        planar=True,
        fixed_dims={"length_mm": 100.0},
        iou_accept=0.0,
    )
    assert partial["dims_mm"]["length_mm"] == pytest.approx(100.0, rel=1e-12)
    assert partial["dims_mm"]["max_width_mm"] != pytest.approx(
        41.5, rel=0.2
    )  # untouched dimension stays measured


def test_huge_fixed_width_stays_finite() -> None:
    sc = S.topdown_scene("box_elongated", 30)
    fit = fit_auto(
        sc["views"],
        known_depth=sc["depth"],
        planar=True,
        fixed_dims={"max_width_mm": 1e9},
        iou_accept=0.0,
    )
    assert fit["dims_mm"]["max_width_mm"] == pytest.approx(1e9)
    assert 0.0 <= fit["confidence"]["train_min_iou"] <= 1.0
    _json_ok(fit)


def test_closed_form_fit_is_deterministic() -> None:
    sc = S.topdown_scene("pliers_flat", 40)
    a = fit_auto(sc["views"], known_depth=sc["depth"], planar=True, seed=7)
    b = fit_auto(sc["views"], known_depth=sc["depth"], planar=True, seed=7)
    for fit in (a, b):
        fit.pop("timing_s")
        for att in fit["attempts"]:
            att.pop("time_s")
    assert json.dumps(to_jsonable(a), sort_keys=True) == json.dumps(
        to_jsonable(b), sort_keys=True
    )


def test_multi_view_closed_form_locates_the_object() -> None:
    sc = S.scene("screwdriver", 0)
    fit = fit_auto(
        sc["views"], iou_accept=0.0, template="lathe"
    )  # three calibrated views, no known_depth needed
    assert fit["method"] == "closed_form" and fit["confidence"]["n_views"] == 3
    assert np.linalg.norm(np.array(fit["pose"]["t_mm"]) - sc["t"]) < 40.0
    assert len(fit["views"]) == 3 and all(0.0 <= v["iou"] <= 1.0 for v in fit["views"])


# ------------------------------------------------------------------ view check
def test_view_check_contract_with_a_true_fit_and_a_wrong_one() -> None:
    sc = S.scene("pliers_flat", 3)
    good = gt_fit("pliers_flat", sc)
    ok = view_check.check(good, sc["heldout"])
    assert set(ok) >= {
        "views",
        "min_iou",
        "status",
        "needs_views",
        "flag_below",
        "mode",
        "sanity",
    }
    assert ok["mode"] == "heldout" and ok["status"] == "ok" and ok["needs_views"] == []
    assert ok["views"][0]["iou"] >= 0.8 and ok["views"][0]["flag"] is False
    assert ok["min_iou"] == ok["views"][0]["iou"]
    bad = gt_fit(
        "pliers_flat", sc, rot_err=np.array([0.0, 0.0, 1.2])
    )  # same shape spun 70 degrees about the camera axis
    res = view_check.check(bad, sc["heldout"] + sc["views"][:1])
    assert res["status"] == "flagged" and res["min_iou"] < 0.75
    assert any(v["flag"] for v in res["views"])
    assert 1 <= len(res["needs_views"]) <= 3
    for n in res["needs_views"]:
        assert (
            -180 <= n["azimuth_deg"] <= 180
            and -90 <= n["elevation_deg"] <= 90
            and n["reason"]
        )
        assert pytest.approx(1.0, abs=1e-6) == float(
            np.linalg.norm(n["direction_object"])
        )
    _json_ok(res)


def test_view_check_flag_threshold_is_respected() -> None:
    sc = S.scene("box_elongated", 1)
    fit = gt_fit("box_elongated", sc)
    iou = view_check.check(fit, sc["heldout"], flag_below=0.5)["views"][0]["iou"]
    assert not view_check.check(fit, sc["heldout"], flag_below=iou - 1e-6)["views"][0][
        "flag"
    ]
    assert view_check.check(fit, sc["heldout"], flag_below=min(iou + 1e-3, 1.0))[
        "views"
    ][0]["flag"]
    with pytest.raises(BadInput, match="flag_below"):
        view_check.check(fit, sc["heldout"], flag_below=1.5)


def test_view_check_single_photo_mode_is_unverified_and_honest() -> None:
    sc = S.topdown_scene("box_elongated", 30)
    fit = fit_auto(sc["views"], known_depth=sc["depth"], planar=True)
    res = view_check.check(fit, [])
    assert (
        res["mode"] == "single_photo" and res["views"] == [] and res["min_iou"] is None
    )
    assert res["status"] == "unverified"  # never "ok": nothing independent was compared
    assert [c["name"] for c in res["sanity"]["checks"]] == [
        "canonical_views_nondegenerate",
        "thickness_prior",
        "fill_ratio",
    ]
    assert res["sanity"]["ok"] is True
    names = {c["name"] for c in res["sanity"]["canonical"]}
    assert names == {"top", "side", "end", "three_quarter"}
    top = next(c for c in res["sanity"]["canonical"] if c["name"] == "top")
    assert top["area_mm2"] == pytest.approx(
        0.9 * fit["dims_mm"]["length_mm"] * fit["dims_mm"]["max_width_mm"], rel=0.2
    )  # a box fills its bounding rectangle
    assert res["needs_views"], "one photo of a box must ask for another view"
    # a photo taken from above of a flat/box shape: the first request is NOT another top-down view
    assert abs(fit["views"][0]["elevation_deg"]) > 80  # the photo looked down the thickness axis (signed: the camera sits on -z)
    assert abs(res["needs_views"][0]["elevation_deg"]) < 80  # so the first request is NOT another top-down view
    _json_ok(res)


def test_view_check_sanity_flags_an_absurd_fit() -> None:
    sc = S.topdown_scene("pliers_flat", 20)
    fit = fit_auto(
        sc["views"],
        known_depth=sc["depth"],
        planar=True,
        template="flat",
        iou_accept=0.0,
    )
    fit["params"]["aspect"] = 0.004  # a 0.1 mm sheet for a flat-template tool
    res = view_check.check(fit, [])
    assert res["status"] == "flagged"
    failed = [c["name"] for c in res["sanity"]["checks"] if not c["ok"]]
    assert "thickness_prior" in failed


def test_view_check_rejects_a_malformed_fit() -> None:
    with pytest.raises(BadInput, match="not a threed_vision fit"):
        view_check.check({"params": {}}, [])
    sc = S.topdown_scene("box_elongated")
    fit = fit_auto(sc["views"], known_depth=sc["depth"], planar=True)
    fit["pose"]["t_mm"][0] = float("nan")
    with pytest.raises(BadInput, match="non-finite"):
        view_check.check(fit, [])


def test_overlays_are_rgb_images() -> None:
    sc = S.scene("pliers_flat", 3)
    imgs = view_check.render_overlays(gt_fit("pliers_flat", sc), sc["heldout"])
    assert (
        len(imgs) == 1 and imgs[0].ndim == 3 and imgs[0].shape[2] == 3 and imgs[0].any()
    )


# ------------------------------------------------------------------ mesh export
@pytest.mark.parametrize("fmt", ["glb", "stl", "obj"])
def test_emit_mesh_is_watertight_mm_and_matches_dims(fmt: str) -> None:
    trimesh = pytest.importorskip("trimesh")
    # centre line straight (c = 0): the lateral mesh extent equals the max width exactly
    fit = gt_fit("box_elongated", S.scene("box_elongated", 0))
    data = emit_mesh(fit, fmt)
    assert isinstance(data, bytes) and data
    import io

    loaded = trimesh.load(io.BytesIO(data), file_type=fmt, force="mesh")
    assert loaded.is_watertight and loaded.is_winding_consistent
    assert loaded.volume > 0
    assert sorted(loaded.extents) == pytest.approx(
        sorted([240.0, 30.0, 15.0]), rel=1e-4
    )
    assert loaded.volume == pytest.approx(
        240 * 30 * 15, rel=0.05
    )  # p=8 section ~ a box (units are mm)


def test_emit_mesh_world_frame_places_the_object() -> None:
    trimesh = pytest.importorskip("trimesh")
    sc = S.topdown_scene("pliers_flat", 30)
    fit = fit_auto(sc["views"], known_depth=sc["depth"], planar=True)
    import io

    world = trimesh.load(
        io.BytesIO(emit_mesh(fit, "stl", frame="world")), file_type="stl", force="mesh"
    )
    local = trimesh.load(
        io.BytesIO(emit_mesh(fit, "stl")), file_type="stl", force="mesh"
    )
    assert (
        np.linalg.norm(world.bounds.mean(0) - np.array(fit["pose"]["t_mm"]))
        < 0.2 * fit["dims_mm"]["length_mm"]
    )
    assert (
        np.abs(local.bounds.mean(0)).max() < 0.1 * fit["dims_mm"]["length_mm"]
    )  # object frame is centred
    assert world.volume == pytest.approx(local.volume, rel=1e-3)


def test_emit_scad_needs_no_trimesh_and_formats_are_validated() -> None:
    sc = S.topdown_scene("screwdriver", 30)
    fit = fit_auto(sc["views"], known_depth=sc["depth"], planar=True)
    scad = emit_mesh(fit, "scad").decode()
    assert scad.count("polyhedron(") == 1 and "points" in scad and "faces" in scad
    assert set(FORMATS) == {"glb", "stl", "obj", "scad"}
    with pytest.raises(BadInput, match="unknown mesh format"):
        emit_mesh(fit, "step")
    with pytest.raises(BadInput, match="unknown frame"):
        emit_mesh(fit, "scad", frame="camera")


# ------------------------------------------------------------------ regression: a tilted axis must not "explain" an upright tool
def test_aligned_profile_recovers_the_centre_line_from_a_tilted_start() -> None:
    from threed_vision.fit_init import aligned_profile

    img = np.zeros((400, 400), np.uint8)
    cv2.line(img, (200, 40), (200, 360), 1, 28)  # vertical bar
    fp = aligned_profile(img > 0, 8, angle_deg=45.0)
    assert abs(abs(fp["axis_deg"]) - 90.0) < 4.0
    assert max(abs((q["hi_mm"] + q["lo_mm"]) / 2) for q in fp["stations"]) < 8.0  # centres sit on the axis: no shear


def test_recorded_screwdriver_is_not_fitted_with_a_sheared_diagonal_axis() -> None:
    """Before the centre-line alignment this real mask (32 x 203 px, vertical) was 'explained' at IoU 0.91 by a 45-degree axis with
    a +-80 mm centre ramp and a length of 178 mm instead of ~255 mm."""
    crop = S.recorded_mask("screwdriver")
    full = np.zeros((1500, 2000), bool)
    full[518 : 518 + crop.shape[0], 848 : 848 + crop.shape[1]] = crop
    K = np.array([[937.5, 0, 1000.0], [0, 937.5, 750.0], [0, 0, 1.0]])
    fit = fit_auto([{"mask": full, "K": K, "R": np.eye(3), "t": np.zeros(3)}], known_depth=1184.0, planar=True, iou_accept=0.0)
    assert fit["dims_mm"]["length_mm"] == pytest.approx(255.3, rel=0.06)
    assert abs(fit["pose"]["axis"][1]) > 0.99  # upright in the image
    assert max(abs(c) for c in fit["params"]["c_mm"]) < 0.1 * fit["dims_mm"]["length_mm"]


# ------------------------------------------------------------------ review regressions
def test_hull_mask_is_the_hard_mask_of_the_loft() -> None:
    from threed_vision.render import hull_mask

    sc = S.scene("pliers_flat", 2)
    fit = gt_fit("pliers_flat", sc)
    V, F = mesh_from_fit(fit, ring=12)
    for v in sc["views"]:
        full = hard_mask(V, F, v["K"], v["R"], v["t"], S.SIZE)
        hull = hull_mask(V, 8, 12, v["K"], v["R"], v["t"], S.SIZE)
        assert (full & hull).sum() / (full | hull).sum() > 0.97  # ~900-pixel silhouettes: boundary pixels differ
    assert not hull_mask(np.full((98, 3), np.nan), 8, 12, S.KF, np.eye(3), np.zeros(3), S.SIZE).any()


def test_retry_dedupe_key_distinguishes_rolls_when_the_axis_is_along_world_x() -> None:
    """The first ROW of R0 is (1, 0, 0) for every roll of an x-axis object; the old key merged all rolls into one retry."""
    from threed_vision.fit_init import pose_candidates
    from threed_vision.fit_silhouette import _seen_key

    st = {"t0": np.array([0.0, 0.0, 650.0]), "axis": np.array([1.0, 0.0, 0.0]), "L": 200.0, "w": np.full(8, 10.0), "c": np.zeros(8)}
    cands = pose_candidates([st], "box", planar=False, fixed_dims=None)
    keys = {_seen_key(c) for c in cands if c["aspect"] == 0.5 and c["c"][0] >= 0}
    assert len(keys) >= 12  # 12 rolls x 2 flips are all distinct poses (some flips coincide with rolls: at least the 12 rolls)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda v: v.update(K=[[1, 0, 0], [0, 1]]),  # ragged
        lambda v: v.update(R="identity"),
        lambda v: v.update(K=[["f", 0, 0], [0, "f", 0], [0, 0, 1]]),
        lambda v: v.update(mask="not a mask"),
    ],
    ids=["ragged-K", "R-string", "K-string", "mask-string"],
)
def test_malformed_view_values_are_bad_input_not_tracebacks(mutate) -> None:
    v = dict(S.topdown_scene("screwdriver")["views"][0])
    mutate(v)
    with pytest.raises(BadInput):
        fit_auto([v], known_depth=650.0, planar=True)
    with pytest.raises(BadInput, match="must be a dict"):
        fit_auto([None], known_depth=650.0)  # type: ignore[list-item]


def test_max_tries_below_one_is_bad_input() -> None:
    from threed_vision.fit_silhouette import fit_object

    sc = S.topdown_scene("screwdriver")
    with pytest.raises(BadInput, match="max_tries"):
        fit_object(sc["views"], None, known_depth=sc["depth"], planar=True, max_tries=0)


def _good_fit() -> dict:
    sc = S.topdown_scene("box_elongated")
    return fit_auto(sc["views"], known_depth=sc["depth"], planar=True)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda f: f["params"]["w_mm"].__setitem__(2, None), "non-finite"),
        (lambda f: f["template"].update(name="cylinder"), "unknown template"),
        (lambda f: f["pose"].update(t_mm=[1.0, 2.0]), "malformed"),
        (lambda f: f["params"].update(w_mm=[0.0] * 8), "degenerate"),
        (lambda f: f["params"].update(L_mm=0.0), "degenerate"),
        (lambda f: f["params"].update(c_mm=[0.0] * 5), "malformed"),
        (lambda f: f.pop("pose"), "not a threed_vision fit"),
    ],
    ids=["null-width", "foreign-template", "short-t", "zero-widths", "zero-length", "ragged-c", "no-pose"],
)
def test_view_check_and_export_reject_every_malformed_fit_cleanly(mutate, match: str) -> None:
    fit = json.loads(json.dumps(to_jsonable(_good_fit())))
    mutate(fit)
    with pytest.raises(BadInput, match=match):
        view_check.check(fit, [])
    with pytest.raises(BadInput, match=match):
        emit_mesh(fit, "scad")


def test_a_truncated_fit_without_dims_or_views_still_checks() -> None:
    fit = json.loads(json.dumps(to_jsonable(_good_fit())))
    del fit["dims_mm"], fit["views"]  # derived from params
    assert view_check.check(fit, [])["status"] == "unverified"


def test_extra_view_iou_decides_the_status_not_the_thickness_prior() -> None:
    """A thin plate fitted at aspect 0.05 is outside the flat band, yet if its held-out view is predicted well it is NOT flagged."""
    sc = S.scene("pliers_flat", 3)
    fit = gt_fit("pliers_flat", sc)
    fit["params"]["aspect"] = 0.05
    assert view_check.check(fit, [])["status"] == "flagged"  # alone: implausible thickness
    res = view_check.check(fit, sc["heldout"], flag_below=0.5)
    assert res["sanity"]["ok"] is False and res["views"][0]["flag"] is False
    assert res["status"] == "ok"


def test_k_must_be_a_pinhole_matrix() -> None:
    v = dict(S.topdown_scene("screwdriver")["views"][0])
    v["K"] = np.array([[700.0, 0, 576], [0, 700.0, 768], [0, 0, 0]])
    with pytest.raises(BadInput, match="last row"):
        fit_auto([v], known_depth=650.0, planar=True)


def test_planar_means_one_view() -> None:
    sc = S.scene("screwdriver", 0)
    with pytest.raises(BadInput, match="ONE top-down view"):
        fit_auto(sc["views"], planar=True)


def test_missing_refiner_is_recorded_and_the_closed_form_fit_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    from threed_vision import fit_silhouette
    from threed_vision.errors import MissingDependency

    def no_torch() -> None:
        raise MissingDependency("torch (the soft-silhouette refiner)", remediation="install threed-vision[fit]")

    monkeypatch.setattr(fit_silhouette, "_torch", no_torch)
    sc = S.topdown_scene("pliers_flat", 20)
    fit = fit_auto(sc["views"], known_depth=sc["depth"], planar=True, iou_accept=1.01)
    assert fit["method"] == "closed_form" and [a["method"] for a in fit["attempts"]] == ["closed_form", "soft"]
    assert "torch" in fit["attempts"][1]["skipped"]
    _json_ok(fit)
    with pytest.raises(MissingDependency):  # an explicit GPU request is never silently downgraded
        fit_auto(sc["views"], known_depth=sc["depth"], planar=True, iou_accept=1.01, device="cuda")


def test_view_check_rejects_unusable_ring() -> None:
    for bad in ("12a", 1e8, True, 2):
        fit = json.loads(json.dumps(to_jsonable(_good_fit())))
        fit["template"]["ring"] = bad
        with pytest.raises(BadInput, match="ring"):
            view_check.check(fit, [])
