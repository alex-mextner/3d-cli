"""Behaviour of `colour_regions`: nearest-face transfer, label-count errors, watertight disjoint volumes, rules, islands,
classification by parts, approved-class transfer, crease snapping and the verify gates (each with a control that FAILs).

All meshes are synthetic: a 10 mm icosphere split by z (two analytic 628 mm2 halves), a re-meshed perturbed copy, and a
12 mm box cut into small triangles (twelve sharp 90 degree edges the labels can sit on or beside).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import trimesh
from colour_regions_fixtures import (
    SPHERE_RULES,
    centroid_z,
    fine_box,
    save_labels,
    save_mesh,
    sphere,
    sphere_labels,
    write_project,
    write_rules,
)

from colour_regions import build as build_mod
from colour_regions import snap as snap_mod
from colour_regions import transfer as transfer_mod
from colour_regions import verify as verify_mod
from colour_regions.labels import clean_islands, nearest_face_labels, read_labels, resolve_labels
from colour_regions.rules import load_rules, parse_rules
from colour_regions.surface import Surf, load_mesh
from errors import InvalidArgument, UsageError


# ------------------------------------------------------------------------------------------ nearest-face transfer
def test_nearest_face_transfer_maps_labels_after_the_mesh_is_remeshed_and_perturbed(tmp_path: Path) -> None:
    """Labels made on one triangulation land on the right side of a re-meshed, jittered copy of the sphere."""
    old = sphere(subdivisions=4)
    new = sphere(subdivisions=3, jitter=0.03)  # different face count: a real nearest-face transfer, not by index
    fl = (centroid_z(old) > 0).astype(np.int16)
    save_mesh(old, tmp_path / "old.ply")
    save_labels(tmp_path / "old.npz", fl, ["lower", "upper"])
    s = Surf(new)
    got, names, stats = resolve_labels(f"{tmp_path / 'old.npz'}@{tmp_path / 'old.ply'}", s, tol=0.25)
    assert names == ["lower", "upper"]
    assert stats["mode"] == "nearest-face" and stats["faces"] == len(new.faces)
    away = np.abs(centroid_z(new)) > 1.2  # faces straddling the equator may go either way: the cut is a triangle wide
    assert away.sum() > 0.6 * len(new.faces)
    assert (got[away] == (centroid_z(new)[away] > 0)).all()
    assert stats["dropped_far"] == 0 and stats["dist_max"] < 0.25


def test_a_transferred_label_is_dropped_where_the_label_mesh_is_farther_than_the_tolerance() -> None:
    """A copy of the sphere 1 mm outside the label mesh gets no labels at tol 0.25 and all of them at tol 2."""
    old = sphere(radius=10.0, subdivisions=3)
    far = sphere(radius=11.0, subdivisions=3)
    fl = np.zeros(len(old.faces), np.int16)
    none, stats = nearest_face_labels(old, fl, np.asarray(far.triangles_center), tol=0.25)
    assert (none == -1).all() and stats["dropped_far"] == len(far.faces)
    kept, _ = nearest_face_labels(old, fl, np.asarray(far.triangles_center), tol=2.0)
    assert (kept == 0).all()


def test_identity_transfer_when_labels_belong_to_the_mesh_itself(tmp_path: Path) -> None:
    """Labels with the mesh's own digest are used face for face, and say so."""
    m = sphere(subdivisions=3)
    fl, names = sphere_labels(m)
    save_labels(tmp_path / "l.npz", fl, names, m)
    got, _, stats = resolve_labels(str(tmp_path / "l.npz"), Surf(m), tol=0.25)
    assert stats == {"mode": "identity", "faces": len(m.faces), "digest_checked": True}
    assert (got == fl).all()


# ------------------------------------------------------------------------------------------ wrong label files fail
def test_a_wrong_label_count_is_an_error_not_a_silent_pad(tmp_path: Path) -> None:
    """One label short: exit-2 error naming both counts and the remedy."""
    m = sphere(subdivisions=3)
    fl, names = sphere_labels(m)
    save_labels(tmp_path / "short.npz", fl[:-1], names)
    with pytest.raises(UsageError) as exc:
        resolve_labels(str(tmp_path / "short.npz"), Surf(m), tol=0.25)
    assert exc.value.exit_code == 2
    assert str(len(fl) - 1) in str(exc.value) and str(len(m.faces)) in str(exc.value)
    assert any("@mesh" in step for step in exc.value.remediation)


def test_labels_that_do_not_match_their_declared_mesh_fail(tmp_path: Path) -> None:
    """`labels@mesh` where the mesh has a different face count than the labels."""
    m = sphere(subdivisions=3)
    other = sphere(subdivisions=2)
    fl, names = sphere_labels(m)
    save_mesh(other, tmp_path / "other.ply")
    save_labels(tmp_path / "l.npz", fl, names)
    with pytest.raises(UsageError, match="face labels but"):
        resolve_labels(f"{tmp_path / 'l.npz'}@{tmp_path / 'other.ply'}", Surf(m), tol=0.25)


def test_labels_of_another_mesh_with_the_same_face_count_fail_on_the_digest(tmp_path: Path) -> None:
    """Same count, other geometry: the mesh_digest refuses the by-index transfer."""
    m = sphere(subdivisions=3)
    bent = sphere(subdivisions=3, jitter=0.2)
    fl, names = sphere_labels(m)
    save_labels(tmp_path / "l.npz", fl, names, bent)
    with pytest.raises(UsageError, match="mesh_digest differs"):
        resolve_labels(str(tmp_path / "l.npz"), Surf(m), tol=0.25)


@pytest.mark.parametrize("bad", [5, -2])
def test_label_ids_outside_the_names_are_rejected(tmp_path: Path, bad: int) -> None:
    """An id past the names list (or below -1) is a corrupt label file."""
    fl = np.array([0, 1, bad], np.int16)
    save_labels(tmp_path / "l.npz", fl, ["a", "b"])
    with pytest.raises(UsageError, match="outside -1..1"):
        read_labels(tmp_path / "l.npz")


def test_json_label_files_read_like_npz(tmp_path: Path) -> None:
    """The same contract in JSON."""
    save_labels(tmp_path / "l.json", np.array([0, -1, 1]), ["a", "b"])
    lf = read_labels(tmp_path / "l.json")
    assert lf.names == ["a", "b"] and lf.face_label.tolist() == [0, -1, 1]


# ------------------------------------------------------------------------------------------ build: volumes
def _overlap(a: trimesh.Trimesh, b: trimesh.Trimesh, n: int = 1500) -> tuple[float, float]:
    """(share of sampled points inside both, share inside either) over the joint bounding box."""
    lo = np.minimum(a.bounds[0], b.bounds[0])
    hi = np.maximum(a.bounds[1], b.bounds[1])
    p = np.random.default_rng(5).uniform(lo, hi, (n, 3))
    ia, ib = a.contains(p), b.contains(p)
    return float((ia & ib).sum()) / n, float((ia | ib).sum()) / n


def _build_sphere(tmp_path: Path) -> tuple[trimesh.Trimesh, Path, build_mod.BuildResult]:
    m = sphere(subdivisions=4)
    fl, names = sphere_labels(m)
    mesh = save_mesh(m, tmp_path / "ball.ply")
    labels = save_labels(tmp_path / "ball.npz", fl, names, m)
    rules = write_rules(tmp_path / "rules.json", SPHERE_RULES)
    res = build_mod.build(
        mesh, [build_mod.Track("sculpt", str(labels))], load_rules(rules), tmp_path / "work", verbose=False
    )
    return m, tmp_path / "work", res


def test_build_makes_one_watertight_positive_volume_per_group(tmp_path: Path) -> None:
    """Top and bottom groups each become one closed volume about 2 x depth x area thick; the belt (default colour) none."""
    m, work, res = _build_sphere(tmp_path)
    assert [r["name"] for r in res.regions] == ["top", "bottom"]
    z = centroid_z(m)
    for g in res.groups:
        patch = load_mesh(work / g["patch"])
        assert patch.is_watertight and patch.is_winding_consistent and patch.is_volume and patch.volume > 0
        face_area = float(m.area_faces[z > 3.0].sum() if g["name"] == "top" else m.area_faces[z < -3.0].sum())
        assert g["area_mm2"] == pytest.approx(face_area, rel=0.02)
        # a Voronoi slab depth mm to either side of the skin patch (the outer side is wider, the inner narrower)
        assert patch.volume == pytest.approx(2 * 1.5 * face_area, rel=0.2)
    assert not (work / "patches" / "default.ply").exists()


def test_build_volumes_are_disjoint_and_the_control_overlap_is_detected(tmp_path: Path) -> None:
    """The two colour volumes share (almost) no space; the same check on a volume against itself reports all of it."""
    _, work, _ = _build_sphere(tmp_path)
    top = load_mesh(work / "patches" / "top.ply")
    bottom = load_mesh(work / "patches" / "bottom.ply")
    both, either = _overlap(top, bottom)
    assert either > 0.05 and both < 0.001 * either
    both_same, either_same = _overlap(top, top)
    assert both_same == pytest.approx(either_same) and both_same > 0.05  # the detector would see an overlap


def test_build_volumes_start_on_the_label_border(tmp_path: Path) -> None:
    """The top volume contains a point just under a top face and none under a belt face one millimetre past the border."""
    m, work, _ = _build_sphere(tmp_path)
    top = load_mesh(work / "patches" / "top.ply")
    c, n, z = np.asarray(m.triangles_center), np.asarray(m.face_normals), centroid_z(m)
    rng = np.random.default_rng(2)
    inside_label = rng.choice(np.nonzero(z > 5.0)[0], 250, replace=False)
    in_belt = rng.choice(np.nonzero(np.abs(z) < 1.0)[0], 250, replace=False)
    assert top.contains(c[inside_label] - 0.3 * n[inside_label]).mean() > 0.99
    assert top.contains(c[in_belt] - 0.3 * n[in_belt]).mean() < 0.01


def test_build_writes_the_documented_outputs_and_reuses_unchanged_patches(tmp_path: Path) -> None:
    """regions.json is a `3d prep` regions list with relative stl paths; a second build is a cache hit."""
    m, work, res = _build_sphere(tmp_path)
    doc = json.loads((work / "regions.json").read_text(encoding="utf-8"))
    assert doc["regions"] == [
        {"name": "top", "assign": {"key": "W"}, "shape": {"type": "stl", "path": "patches/top.ply"}},
        {"name": "bottom", "assign": {"mix": "WR"}, "shape": {"type": "stl", "path": "patches/bottom.ply"}},
    ]
    assert doc["labels"] == ["top", "bottom", "belt"] and doc["groups"][1]["min_speck_mm3"] == 0.1
    z = np.load(work / "labels_final.npz")
    assert json.loads(str(z["group_names"])) == ["top", "bottom"]
    assert z["face_group"].shape == (len(m.faces),) and str(z["mesh_digest"])
    assert json.loads((work / "transfer.json").read_text(encoding="utf-8"))["sculpt"]["mode"] == "identity"
    stamp = (work / "patches" / "top.ply").stat().st_mtime_ns
    build_mod.build(tmp_path / "ball.ply", [build_mod.Track("sculpt", str(tmp_path / "ball.npz"))],
                    load_rules(tmp_path / "rules.json"), work, verbose=False)
    assert (work / "patches" / "top.ply").stat().st_mtime_ns == stamp
    build_mod.build(tmp_path / "ball.ply", [build_mod.Track("sculpt", str(tmp_path / "ball.npz"))],
                    load_rules(tmp_path / "rules.json"), work, verbose=False, force=True)
    assert (work / "patches" / "top.ply").stat().st_mtime_ns != stamp
    assert res.stats["sculpt"]["labels"]["top"] > 0


def test_a_label_without_a_rule_stops_the_build(tmp_path: Path) -> None:
    """The rules file is the only name -> colour map: an unmapped label name is an error naming it."""
    m = sphere(subdivisions=3)
    fl, _ = sphere_labels(m)
    save_mesh(m, tmp_path / "b.ply")
    save_labels(tmp_path / "l.npz", fl, ["top", "bottom", "unknown_thing"], m)
    with pytest.raises(UsageError, match="unknown_thing"):
        build_mod.build(tmp_path / "b.ply", [build_mod.Track("t", str(tmp_path / "l.npz"))],
                        load_rules(write_rules(tmp_path / "r.json", SPHERE_RULES)), tmp_path / "w", verbose=False)


def test_a_coloured_label_on_a_second_body_cannot_be_coloured(tmp_path: Path) -> None:
    """A label on a separate shell (a cavity) is refused instead of silently dropped."""
    outer = sphere(radius=10.0, subdivisions=3)
    cavity = sphere(radius=3.0, subdivisions=2)
    both = trimesh.util.concatenate([outer, cavity])
    fl = np.full(len(both.faces), 2, np.int16)
    fl[len(outer.faces):] = 0  # "top" on the cavity body
    save_mesh(both, tmp_path / "b.ply")
    save_labels(tmp_path / "l.npz", fl, ["top", "bottom", "belt"], both)
    with pytest.raises(UsageError, match="not the main surface"):
        build_mod.build(tmp_path / "b.ply", [build_mod.Track("t", str(tmp_path / "l.npz"))],
                        load_rules(write_rules(tmp_path / "r.json", SPHERE_RULES)), tmp_path / "w", verbose=False)


# ------------------------------------------------------------------------------------------ tracks
def _two_track_rules() -> Any:
    return parse_rules({"rules": [
        {"label": "x", "group": "x", "assign": {"key": "A"}, "depth_mm": 1, "pitch_mm": 0.3},
        {"label": "y", "group": "y", "assign": {"key": "B"}, "depth_mm": 1, "pitch_mm": 0.3},
        {"label": "w_.*", "group": "w", "assign": {"key": "W"}, "depth_mm": 1, "pitch_mm": 0.3, "owner": "white"},
        {"label": "base", "group": "default", "assign": None, "passive": True},
    ]})


def test_later_tracks_win_but_passive_labels_never_override(tmp_path: Path) -> None:
    """Track b labels the upper half y and the rest `base` (passive): the lower half keeps track a's x."""
    m = sphere(subdivisions=3)
    s = Surf(m)
    z = centroid_z(m)
    save_labels(tmp_path / "a.npz", np.zeros(s.nf, np.int16), ["x"], m)
    save_labels(tmp_path / "b.npz", np.where(z > 0, 0, 1).astype(np.int16), ["y", "base"], m)
    lab, names, _ = build_mod.merge_tracks(
        s, [build_mod.Track("a", str(tmp_path / "a.npz")), build_mod.Track("b", str(tmp_path / "b.npz"))], _two_track_rules()
    )
    assert names == ["x", "y", "base"]
    assert (lab[z > 0] == names.index("y")).all() and (lab[z <= 0] == names.index("x")).all()


def test_an_owned_label_comes_only_from_its_owner_track(tmp_path: Path) -> None:
    """`w_*` is owned by track `white`: the sculpt track's w_ labels are ignored while the white track is present."""
    m = sphere(subdivisions=3)
    s = Surf(m)
    z = centroid_z(m)
    save_labels(tmp_path / "sculpt.npz", np.where(z > 0, 0, -1).astype(np.int16), ["w_sculpt"], m)
    save_labels(tmp_path / "white.npz", np.where(z > 5, 0, -1).astype(np.int16), ["w_approved"], m)
    tracks = [build_mod.Track("white", str(tmp_path / "white.npz")), build_mod.Track("sculpt", str(tmp_path / "sculpt.npz"))]
    lab, names, stats = build_mod.merge_tracks(s, tracks, _two_track_rules())
    assert (lab[z > 5] == names.index("w_approved")).all() and (lab[z <= 5] == -1).all()
    assert stats["sculpt"]["ignored_owned_labels"] == ["w_sculpt"]
    lab_alone, names_alone, _ = build_mod.merge_tracks(s, tracks[1:], _two_track_rules())
    assert (lab_alone[z > 0] == names_alone.index("w_sculpt")).all()  # without the owner track the label counts


# ------------------------------------------------------------------------------------------ rules
@pytest.mark.parametrize(
    ("doc", "needle"),
    [
        ({"rules": [{"label": "a", "group": "g", "assign": {"key": "W"}}]}, "depth_mm"),
        ({"rules": [{"label": "a", "group": "g", "assign": {"key": "W", "mix": "WR"}, "depth_mm": 1, "pitch_mm": 1}]}, "assign"),
        ({"rules": [{"label": "(", "group": "g", "assign": None}]}, "regex"),
        ({"rules": [{"label": "a", "group": "bad name", "assign": None}]}, "group"),
        ({"rules": [{"label": "a", "group": "g", "assign": None, "colour": 1}]}, "colour"),
        ({"rules": [
            {"label": "a", "group": "g", "assign": {"key": "W"}, "depth_mm": 1, "pitch_mm": 1},
            {"label": "b", "group": "g", "assign": {"key": "K"}, "depth_mm": 1, "pitch_mm": 1},
        ]}, "disagree"),
        ({"rules": []}, "non-empty"),
        ({"rulez": []}, "rulez"),
    ],
)
def test_bad_rules_files_are_rejected_with_the_accepted_values(doc: dict[str, Any], needle: str) -> None:
    """Every malformed rule is an exit-2 error that names what was wrong."""
    with pytest.raises(InvalidArgument) as exc:
        parse_rules(doc)
    assert exc.value.exit_code == 2 and needle in str(exc.value)


def test_the_first_matching_rule_decides_and_groups_keep_rule_order() -> None:
    """Priority = order of the rules; the group of a name is that of the first rule that fully matches it."""
    rules = parse_rules({"rules": [
        {"label": "eye_iris", "group": "iris", "assign": {"mix": "GKGK"}, "depth_mm": 1.6, "pitch_mm": 0.1},
        {"label": "eye_.*", "group": "eye_rest", "assign": None},
        {"label": "white_.*", "group": "white", "assign": {"key": "W"}, "depth_mm": 3, "pitch_mm": 0.15},
    ], "island_mm2": 5})
    assert rules.rule_of("eye_iris").group == "iris" and rules.rule_of("eye_lid").group == "eye_rest"
    assert rules.rule_of("white_belly").coloured and not rules.rule_of("eye_lid").coloured
    assert rules.group_order() == ["iris", "eye_rest", "white"] and rules.island_mm2 == 5
    with pytest.raises(UsageError, match="eye"):
        rules.rule_of("xeye_iris")  # a regex matches the WHOLE label name


# ------------------------------------------------------------------------------------------ island hygiene
def test_specks_are_merged_but_a_small_label_is_kept() -> None:
    """A 2-face fragment of a big label merges into its surround; a label that is itself tiny survives."""
    m = sphere(subdivisions=4)
    s = Surf(m)
    z = centroid_z(m)
    nb = s.neighbours()
    big = np.nonzero(z > 3.0)[0]
    speck = [int(np.nonzero(z <= -5.0)[0][0])]
    speck.append(int(nb[speck[0]][0]))
    toe = [int(np.nonzero(z <= -5.0)[0][-1])]
    toe.append(int(nb[toe[0]][0]))
    lab = np.full(s.nf, -1, np.int16)
    lab[big] = 0
    lab[speck] = 0  # same label as the big one, but a disconnected 2-face piece
    lab[toe] = 1  # a label of its own that is only 2 faces in total
    assert s.area[speck].sum() < 3.0 and s.area[toe].sum() < 3.0
    cleaned = clean_islands(s, lab.copy(), lab, 3.0)
    assert (cleaned[speck] == -1).all()  # merged into the default surround
    assert (cleaned[toe] == 1).all()  # the feature itself
    assert (cleaned[big] == 0).all()


# ------------------------------------------------------------------------------------------ classify
def _half_mesh(m: trimesh.Trimesh, keep: np.ndarray[Any, Any], shift: tuple[float, float, float] = (0, 0, 0)) -> trimesh.Trimesh:
    h = trimesh.Trimesh(np.asarray(m.vertices) + np.asarray(shift), np.asarray(m.faces)[keep], process=False)
    return h


def test_classify_by_parts_labels_the_skin_by_the_part_it_lies_on_in_the_parts_frame() -> None:
    """Two part meshes (shifted frame) label a sphere's skin white / pink; the first listed part wins where both claim a face."""
    m = sphere(subdivisions=3)
    z = centroid_z(m)
    shift = (7.0, -2.0, 1.0)
    white = _half_mesh(m, z > 0, shift)
    pink = _half_mesh(m, z > 6, shift)
    res = transfer_mod.classify_by_parts(m, [("pink", pink), ("white", white)], tol=0.06, shift=shift)
    assert res.names == ["pink", "white"]
    assert (res.face_label[z > 6] == 0).all()  # claimed by both: the first listed wins
    assert (res.face_label[(z > 0) & (z <= 6)] == 1).all()
    assert (res.face_label[z <= 0] == -1).all()
    wrong = transfer_mod.classify_by_parts(m, [("white", white)], tol=0.06)  # parts frame not undone
    assert (wrong.face_label == -1).all()


def test_splitting_giant_faces_lets_a_differently_cut_part_label_them_by_surface_distance() -> None:
    """A box skin of 12 giant triangles against a part cut into 1.9 mm triangles along a plane that crosses them: with
    centroids only, whole giant faces take the label by where their centroid is (the area is wrong); split to 1.2 mm
    and judged by distance to the part surface, the labelled area is the part's (to the width of the jagged cut)."""
    skin = trimesh.creation.box((20, 20, 20))
    fine_v, fine_f = trimesh.remesh.subdivide_to_size(np.asarray(skin.vertices), np.asarray(skin.faces), 1.9)
    fine = trimesh.Trimesh(fine_v, fine_f, process=False)
    cut = np.asarray(fine.triangles_center)[:, 0] > 3.1
    right = _half_mesh(fine, cut)
    plain = transfer_mod.classify_by_parts(skin, [("right", right)], tol=0.06)
    split = transfer_mod.classify_by_parts(skin, [("right", right)], tol=0.06, split_edge=1.2)
    area_right = float(split.mesh.area_faces[split.face_label == 0].sum())
    expected = float(fine.area_faces[cut].sum())
    assert split.n_split > 0 and len(split.mesh.faces) == len(split.face_label)
    assert area_right == pytest.approx(expected, rel=0.08)
    assert split.mesh.area == pytest.approx(skin.area)  # splitting does not change the surface
    plain_area = float(skin.area_faces[plain.face_label == 0].sum())
    assert abs(plain_area - expected) > 0.25 * expected  # the control: by centroids alone the labelled area is off
    boxed = transfer_mod.classify_by_parts(skin, [("right", right)], tol=0.06, split_edge=1.2, split_box=(-30, 30, -30, 30, 5, 30))
    assert 12 < len(boxed.mesh.faces) < len(split.mesh.faces)  # only the faces inside the box were split


# ------------------------------------------------------------------------------------------ transfer-class
def _class_source() -> tuple[trimesh.Trimesh, np.ndarray[Any, Any], list[str]]:
    src = sphere(subdivisions=4)
    z = centroid_z(src)
    fl = np.where(z > 0, 0, 1).astype(np.int16)  # white upper, black lower
    fl[z > 9.0] = 2  # a pink cap of unknown class on top of the white
    return src, fl, ["white", "black", "pink"]


def test_transfer_class_carries_the_approved_class_to_a_remeshed_mesh() -> None:
    """The white half lands on the new mesh's upper half; the excluded pink cap is not a source, so its faces are white
    too; without the exclusion they are not."""
    src, fl, names = _class_source()
    new = Surf(sphere(subdivisions=3, jitter=0.02))
    z = centroid_z(new.mesh)
    common: dict[str, Any] = {"positive": ["white"], "max_dist": 6.0, "island_mm2": 6.0, "fill_boxes": []}
    lab, rep = transfer_mod.transfer_class(new, src, fl, names, exclude=["pink"], **common)
    away = np.abs(z) > 1.2
    assert (lab[away] == (z[away] > 0).astype(int) - 1).all()  # upper -> 0 (white), lower -> -1
    assert rep["source_excluded_faces"] == int((fl == 2).sum())
    pole = z > 9.3
    assert pole.any() and (lab[pole] == 0).all()
    lab_kept, _ = transfer_mod.transfer_class(new, src, fl, names, exclude=[], **common)
    assert (lab_kept[pole] == -1).all()  # the pink cap is "not white": the control that the exclusion matters


def test_transfer_class_gives_no_label_beyond_max_dist_and_rejects_unknown_names() -> None:
    """A mesh 4 mm outside the source is beyond --max-dist 1 everywhere; a misspelt class name lists the accepted ones."""
    src, fl, names = _class_source()
    far = Surf(sphere(radius=14.0, subdivisions=3))
    lab, rep = transfer_mod.transfer_class(far, src, fl, names, positive=["white"], exclude=[], max_dist=1.0, island_mm2=6.0, fill_boxes=[])
    assert (lab == -1).all() and rep["beyond_max_dist"] == far.nf
    with pytest.raises(InvalidArgument) as exc:
        transfer_mod.transfer_class(far, src, fl, names, positive=["whit"], exclude=[], max_dist=1.0, island_mm2=6.0, fill_boxes=[])
    assert "white" in exc.value.accepted


def test_island_fill_never_overrides_what_the_approved_build_has() -> None:
    """A black speck in white is filled when the approved faces there were white; it stays when the approved build has it."""
    m = sphere(subdivisions=4)
    s = Surf(m)
    nb = s.neighbours()
    seed = int(np.nonzero(centroid_z(m) > 5.0)[0][0])
    island = np.array([seed, int(nb[seed][0])])
    lab = np.zeros(s.nf, np.int16)
    lab[island] = -1
    near = np.zeros(s.nf)
    approved_white = np.ones(s.nf, bool)  # the approved build is white all over: the island is noise
    filled, n = transfer_mod.fill_islands(s, lab, approved_white, near, min_area=6.0, max_dist=6.0)
    assert (filled == 0).all() and n["other_in_positive"] == 1
    approved_has_it = approved_white.copy()
    approved_has_it[island] = False  # the approved build is black there: keep it
    kept, n2 = transfer_mod.fill_islands(s, lab, approved_has_it, near, min_area=6.0, max_dist=6.0)
    assert (kept[island] == -1).all() and n2["other_in_positive"] == 0
    boxed, done = transfer_mod.fill_box_islands(s, lab, [((-20, 20, -20, 20, 5, 20), 6.0)])
    assert (boxed == 0).all() and done == 1  # a box fill does not ask the approved build
    outside, none = transfer_mod.fill_box_islands(s, lab, [((-20, 20, -20, 20, -20, -10), 6.0)])
    assert (outside[island] == -1).all() and none == 0


# ------------------------------------------------------------------------------------------ snap
def _border_distance_to_crease(m: trimesh.Trimesh, lab: np.ndarray[Any, Any]) -> float:
    """Largest |z - 6| over the midpoints of mesh edges between differently labelled faces (the box's top rim is z = 6)."""
    fa = np.asarray(m.face_adjacency)
    diff = lab[fa[:, 0]] != lab[fa[:, 1]]
    ev = np.asarray(m.vertices)[np.asarray(m.face_adjacency_edges)[diff]]
    mid = ev.mean(axis=1)
    return float(np.abs(mid[:, 2] - 6.0).max())


def test_snap_pulls_a_border_from_the_flat_side_onto_the_crease(tmp_path: Path) -> None:
    """A zone drawn 1.2 mm below the top rim of a box ends exactly on the rim after snapping within 2.5 mm."""
    box = fine_box()
    s = Surf(box)
    z = centroid_z(box)
    lab = np.where(z > 4.8, 0, -1).astype(np.int16)  # top face + the upper 1.2 mm of the four sides
    assert _border_distance_to_crease(box, lab) == pytest.approx(1.2, abs=0.3)
    _, cost = snap_mod.crease_cost(s, 0.9, 15.0)
    snapped = snap_mod.snap_labels(s, lab, np.zeros(s.nf, bool), 2.5, cost)
    assert _border_distance_to_crease(box, snapped) < 0.6
    centre = np.asarray(box.triangles_center)
    interior = (z > 5.99) & (np.abs(centre[:, 0]) < 5) & (np.abs(centre[:, 1]) < 5)
    assert interior.sum() > 1000 and (snapped[interior] == 0).all()  # the top face keeps its label (corners may flip)


def test_snap_leaves_fixed_labels_and_far_borders_alone() -> None:
    """With the zone `fixed` nothing moves; a band narrower than the distance to the crease moves nothing useful."""
    box = fine_box()
    s = Surf(box)
    z = centroid_z(box)
    lab = np.where(z > 4.8, 0, -1).astype(np.int16)
    _, cost = snap_mod.crease_cost(s, 0.9, 15.0)
    fixed = snap_mod.fixed_mask(lab, ["zone"], ["zone"])
    same = snap_mod.snap_labels(s, lab, fixed, 2.5, cost)
    assert (same == lab).all()
    narrow = snap_mod.snap_labels(s, lab, np.zeros(s.nf, bool), 0.4, cost)
    assert _border_distance_to_crease(box, narrow) > 0.8  # 0.4 mm cannot reach a crease 1.2 mm away


# ------------------------------------------------------------------------------------------ verify
def _top_group(box: trimesh.Trimesh, z_min: float) -> np.ndarray[Any, Any]:
    return np.where(centroid_z(box) > z_min, 0, -1).astype(int)


def test_gate_one_passes_for_a_border_on_the_rim_and_fails_for_one_beside_it() -> None:
    """The top face of a box (border = the rim) passes both gate 1 checks; the same zone drawn 1.2 mm down the sides fails."""
    box = fine_box()
    s = Surf(box)
    th = verify_mod.Thresholds()
    on_rim = verify_mod.gate_edges(s, _top_group(box, 5.9), ["top"], th, set(), set())
    assert on_rim["real_pass"] and on_rim["teeth_pass"] and on_rim["pass"]
    assert on_rim["gated_share"] > 0.95 and on_rim["shifted_erode"] < on_rim["gated_share"] - th.shift_margin
    beside = verify_mod.gate_edges(s, _top_group(box, 4.8), ["top"], th, set(), set())
    assert not beside["real_pass"] and not beside["pass"] and beside["gated_share"] < 0.2 < on_rim["gated_share"]
    # the teeth can FAIL: demand more separation from the shifted borders (or from chance) than the labels have
    no_sep = verify_mod.gate_edges(s, _top_group(box, 5.9), ["top"], verify_mod.Thresholds(shift_margin=1.1), set(), set())
    assert no_sep["real_pass"] and not no_sep["teeth_pass"] and not no_sep["pass"]
    no_chance = verify_mod.gate_edges(s, _top_group(box, 5.9), ["top"], verify_mod.Thresholds(chance_margin=0.95), set(), set())
    assert no_chance["real_pass"] and not no_chance["teeth_pass"] and not no_chance["pass"]


def test_gate_one_waives_groups_and_gates_approved_ones_against_chance_only() -> None:
    """With the badly placed bottom zone waived, the well placed top zone alone decides; as an APPROVED group the bottom
    zone must only beat chance + the approved margin (it does not); a group on the rim does."""
    box = fine_box()
    s = Surf(box)
    th = verify_mod.Thresholds()
    z = centroid_z(box)
    group = np.where(z > 5.9, 0, np.where(z < -4.8, 1, -1))  # top on the rim, bottom 1.2 mm down the sides
    names = ["top", "bottom"]
    plain = verify_mod.gate_edges(s, group, names, th, set(), set())
    assert not plain["real_pass"]  # the bottom zone fails its own minimum
    waived = verify_mod.gate_edges(s, group, names, th, {"bottom"}, set())
    assert waived["per_group"]["bottom"]["gated"] is False and waived["real_pass"]
    approved = verify_mod.gate_edges(s, group, names, th, set(), {"bottom"})
    assert approved["approved"]["bottom"]["on_edge"] < approved["chance"] + verify_mod.APPROVED_MARGIN
    assert not approved["approved_pass"] and not approved["pass"]
    on_rim = verify_mod.gate_edges(s, group, names, th, set(), {"top"})
    assert on_rim["approved_pass"]


def test_gate_two_agrees_for_the_built_volumes_and_collapses_when_the_groups_are_swapped(tmp_path: Path) -> None:
    """The volumes of `build` hold the points under their own faces (>= 0.97); swapping the two patches' names fails the gate."""
    m, work, _ = _build_sphere(tmp_path)
    group, names, _ = build_mod.load_groups(work)
    s = Surf(m)
    th = verify_mod.Thresholds()
    good = verify_mod.gate_volumes(s, group, names, verify_mod.load_regions(work / "regions.json"), th, n=1500)
    assert good["pass"] and good["agree"] >= 0.97 and good["agree_permuted"] < 0.6 * 0.97
    swapped = [("bottom", p) if n == "top" else ("top", p) for n, p in verify_mod.load_regions(work / "regions.json")]
    bad = verify_mod.gate_volumes(s, group, names, swapped, th, n=1500)
    assert not bad["pass"] and bad["agree"] <= 0.51 < good["agree"]  # the coloured half of the sample is all wrong


def test_verify_work_reports_unknown_groups_and_a_mismatched_mesh(tmp_path: Path) -> None:
    """--waive of a group that does not exist, and labels of a different mesh, are exit-2 errors."""
    m, work, _ = _build_sphere(tmp_path)
    group, names, _ = build_mod.load_groups(work)
    common: dict[str, Any] = {"th": verify_mod.Thresholds(), "approved": set(), "regions": None}
    with pytest.raises(UsageError, match="nope"):
        verify_mod.verify_work(tmp_path / "ball.ply", group, names, waive={"nope"}, **common)
    other = save_mesh(sphere(subdivisions=3), tmp_path / "other.ply")
    with pytest.raises(UsageError, match="labels_final.npz has"):
        verify_mod.verify_work(other, group, names, waive=set(), **common)



# ------------------------------------------------------------------------------------------ review regressions
def test_refine_is_conforming_keeps_the_surface_and_the_original_vertices() -> None:
    """A boxed refinement splits the neighbours' shared edges too (the mesh stays closed), keeps the original vertices
    where they were, preserves the area and leaves no face with its centroid in the box over the edge limit."""
    skin = fine_box(side=12.0, edge=3.0)
    box = (-30.0, 30.0, -30.0, 30.0, 5.0, 30.0)  # the top face only
    fine, parent = transfer_mod.refine(skin, 1.0, box)
    assert len(fine.faces) > len(skin.faces) and fine.area == pytest.approx(skin.area)
    assert fine.is_watertight and np.array_equal(np.asarray(fine.vertices)[: len(skin.vertices)], np.asarray(skin.vertices))
    assert len(parent) == len(fine.faces) and parent.max() < len(skin.faces)
    tri = np.asarray(fine.triangles)
    edges = np.linalg.norm(tri[:, [1, 2, 0]] - tri, axis=2).max(axis=1)
    in_box = transfer_mod.in_box(np.asarray(fine.triangles_center), box)
    assert (edges[in_box] <= 1.0 + 1e-9).all() and in_box.any()
    # every child lies in the plane or the triangle of its parent: its centroid is on the parent's plane
    pn = np.asarray(skin.face_normals)[parent]
    po = np.asarray(skin.triangles)[parent][:, 0]
    assert np.abs(np.einsum("ij,ij->i", np.asarray(fine.triangles_center) - po, pn)).max() < 1e-9


def test_snap_keeps_a_fixed_label_and_does_not_ring_it_with_default_colour() -> None:
    """A fixed patch inside a snapped zone stays as it is and the zone faces around it keep their label."""
    box = fine_box()
    s = Surf(box)
    z = centroid_z(box)
    c = np.asarray(box.triangles_center)
    lab = np.where(z > 4.8, 0, -1).astype(np.int16)
    patch = (z > 5.99) & (np.abs(c[:, 0]) < 2) & (np.abs(c[:, 1]) < 2)
    lab[patch] = 1
    _, cost = snap_mod.crease_cost(s, 0.9, 15.0)
    snapped = snap_mod.snap_labels(s, lab, lab == 1, 2.5, cost)
    assert (snapped[patch] == 1).all()
    around = (z > 5.99) & (np.abs(c[:, 0]) < 5) & (np.abs(c[:, 1]) < 5) & ~patch
    assert (snapped[around] == 0).all()


def test_a_passive_label_with_a_colour_is_a_rules_error() -> None:
    """`passive` means "nothing here": combining it with an assign would silently drop the colour."""
    with pytest.raises(InvalidArgument, match="passive"):
        parse_rules({"rules": [{"label": "a", "group": "g", "assign": {"key": "W"}, "depth_mm": 1, "pitch_mm": 1, "passive": True}]})


def test_a_tiny_default_colour_piece_inside_a_coloured_group_is_a_speck_like_any_other() -> None:
    """The proven behaviour: only tiny pieces of a COLOURED group are kept as features; a default-colour hole that small
    inside a coloured group is filled."""
    m = sphere(subdivisions=4)
    s = Surf(m)
    z = centroid_z(m)
    seed = int(np.nonzero(z > 5.0)[0][0])
    hole = np.array([seed, int(s.neighbours()[seed][0])])
    lab = np.where(z > 3.0, 0, -1).astype(np.int16)
    lab[hole] = 1  # a label of its own (default colour), 2 faces
    group = np.where(lab == 0, 0, -1).astype(np.int16)
    assert s.area[hole].sum() < 3.0
    assert (clean_islands(s, group, lab, 3.0)[hole] == 0).all()


def test_gate_one_with_only_approved_groups_judges_them_and_with_nothing_to_judge_refuses() -> None:
    """All groups approved: the real-share and teeth checks are n/a, approval alone decides; all waived: exit-2 refusal."""
    box = fine_box()
    s = Surf(box)
    th = verify_mod.Thresholds()
    group = _top_group(box, 5.9)
    only_approved = verify_mod.gate_edges(s, group, ["top"], th, set(), {"top"})
    assert only_approved["gated_groups"] == 0 and only_approved["pass"]
    assert any("n/a" in line for line in verify_mod.format_edges(only_approved, th, set()))
    with pytest.raises(UsageError, match="no colour border left"):
        verify_mod.gate_edges(s, group, ["top"], th, {"top"}, set())


def test_labels_with_a_declared_mesh_of_the_same_size_but_other_geometry_are_refused(tmp_path: Path) -> None:
    """`labels@mesh` and `transfer-class` check the file's mesh_digest against the declared mesh, not only the face count."""
    m = sphere(subdivisions=3)
    bent = sphere(subdivisions=3, jitter=0.2)
    fl, names = sphere_labels(m)
    save_labels(tmp_path / "l.npz", fl, names, m)
    save_mesh(bent, tmp_path / "bent.ply")
    save_mesh(m, tmp_path / "m.ply")
    with pytest.raises(UsageError, match="belong to another mesh"):
        resolve_labels(f"{tmp_path / 'l.npz'}@{tmp_path / 'bent.ply'}", Surf(m), tol=0.25)
    with pytest.raises(UsageError, match="belong to another mesh"):
        transfer_mod.transfer_class_file(
            tmp_path / "m.ply", f"{tmp_path / 'l.npz'}@{tmp_path / 'bent.ply'}", tmp_path / "o.npz",
            positive=["top"], exclude=[], as_name="x", max_dist=6.0, island_mm2=6.0, fill_boxes=[],
        )
    got, _, stats = resolve_labels(f"{tmp_path / 'l.npz'}@{tmp_path / 'm.ply'}", Surf(m), tol=0.25)
    assert stats["mode"] == "identity" and (got == fl).all()


@pytest.mark.parametrize("ids", [[0, 65535, 1], [0, 70000, 1], [0.0, 1.0, 0.0]])
def test_label_ids_are_validated_before_they_are_narrowed_to_int16(tmp_path: Path, ids: list[float]) -> None:
    """65535 would wrap to -1 (unlabelled) and 70000 to a valid id; floats are not ids."""
    np.savez(tmp_path / "l.npz", face_label=np.asarray(ids), names=np.array(json.dumps(["a", "b"])))
    with pytest.raises(UsageError, match="outside -1..1|integer ids"):
        read_labels(tmp_path / "l.npz")


# ------------------------------------------------------------------------------------------ post-fix review regressions
def test_negative_box_and_shift_values_reach_the_parser_as_values() -> None:
    """argparse would take `-50,50,...` for a flag: the documented `--split-box -50,50,-20,20,0,40` must parse."""
    from colour_regions import cli

    argv = ["skin.stl", "--part", "a=b.stl", "--out", "o.npz", "--shift", "-10,20,0", "--split-edge", "2", "--split-box", "-50,50,-20,20,0,40"]
    ns = cli.PARSERS["classify"]().parse_args(cli.glue_list_values(argv))
    assert ns.shift == "-10,20,0" and ns.split_box == "-50,50,-20,20,0,40"
    with pytest.raises(UsageError):
        cli.PARSERS["classify"]().parse_args(argv)  # the control: unglued, the same argv is refused


def test_classify_writes_the_mesh_its_labels_belong_to_even_when_nothing_was_long_enough(tmp_path: Path) -> None:
    """A split box reorders faces; the labels must name the written mesh or they could never be used again."""
    skin = fine_box(side=12.0, edge=3.0)
    save_mesh(skin, tmp_path / "skin.ply")
    save_mesh(trimesh.Trimesh(np.asarray(skin.vertices), np.asarray(skin.faces)[centroid_z(skin) > 0], process=False), tmp_path / "top.ply")
    report = transfer_mod.classify_file(
        tmp_path / "skin.ply", [("top", tmp_path / "top.ply")], tmp_path / "c.npz",
        tol=0.06, shift=(0.0, 0.0, 0.0), split_edge=50.0, split_box=(-30, 30, -30, 30, 0, 30),
    )
    assert report["split_faces"] == 0 and report["mesh"] == "c.mesh.ply"
    got, _, stats = resolve_labels(str(tmp_path / "c.npz"), Surf(tmp_path / "c.mesh.ply"), tol=0.25)
    assert stats["mode"] == "identity" and (got == 0).sum() > 0


def test_bad_user_values_are_structured_errors() -> None:
    """A stringly `passive`, an invalid regex and a zero sample count are exit-2 errors, not tracebacks or silent."""
    from colour_regions import cli

    with pytest.raises(InvalidArgument, match="passive"):
        parse_rules({"rules": [{"label": "a", "group": "g", "assign": None, "passive": "false"}]})
    with pytest.raises(InvalidArgument, match="regex"):
        snap_mod.fixed_mask(np.array([0]), ["a"], ["("])
    with pytest.raises(UsageError):
        cli.PARSERS["verify"]().parse_args(["m.stl", "--work", "w", "--samples", "0"])


def test_a_group_of_only_tiny_pieces_is_skipped_with_a_warning_not_an_abort(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A group whose every piece is under the cutting limit makes no patch, and the build still writes regions.json."""
    m = sphere(subdivisions=4)
    z = centroid_z(m)
    s = Surf(m)
    seed = int(np.nonzero(z > 5.0)[0][0])
    speck = np.array([seed])
    fl = np.where(z < -3.0, 1, -1).astype(np.int16)
    fl[speck] = 0
    assert s.area[speck].sum() < 0.3
    save_mesh(m, tmp_path / "b.ply")
    save_labels(tmp_path / "l.npz", fl, ["top", "bottom"], m)
    rules = load_rules(write_rules(tmp_path / "r.json", SPHERE_RULES, island_mm2=0.01))
    res = build_mod.build(tmp_path / "b.ply", [build_mod.Track("t", str(tmp_path / "l.npz"))], rules, tmp_path / "w", verbose=False)
    assert [r["name"] for r in res.regions] == ["bottom"] and (tmp_path / "w" / "regions.json").is_file()
    assert "every piece is under" in capsys.readouterr().err


# ------------------------------------------------------------------------------------------ legacy parity regressions
def _giant_box_with_parts() -> tuple[trimesh.Trimesh, trimesh.Trimesh]:
    """A 20 mm box of 12 giant triangles and the same box cut into 1.9 mm triangles (a different triangulation)."""
    skin = trimesh.creation.box((20, 20, 20))
    v, f = trimesh.remesh.subdivide_to_size(np.asarray(skin.vertices), np.asarray(skin.faces), 1.9)
    return skin, trimesh.Trimesh(v, f, process=False)


def test_classify_judges_only_the_listed_parts_by_surface_and_keeps_the_parents_decision_for_the_rest() -> None:
    """With --surface-parts white the `other` part is decided by the parent's centroid (no giant face qualifies), the
    white part by distance to its surface; with the default every part is judged by surface."""
    skin, fine = _giant_box_with_parts()
    c = np.asarray(fine.triangles_center)
    other = _half_mesh(fine, (c[:, 2] > 9.9) & (c[:, 0] > 3.1))  # on the top face
    white = _half_mesh(fine, (c[:, 2] < -9.9) & (c[:, 0] > 3.1))  # on the bottom face
    parts = [("other", other), ("white", white)]
    box = (-30.0, 30.0, -30.0, 30.0, -30.0, 30.0)
    only_white = transfer_mod.classify_by_parts(skin, parts, 0.06, split_edge=1.2, split_box=box, surface_parts=["white"])
    everything = transfer_mod.classify_by_parts(skin, parts, 0.06, split_edge=1.2, split_box=box)

    def count(r: transfer_mod.Classified, name: str) -> int:
        return int((r.face_label == r.names.index(name)).sum())

    plain = transfer_mod.classify_by_parts(skin, parts, 0.06)  # no split: the parents' centroid decision
    _, parent = transfer_mod.refine(skin, 1.2, box)
    inherited = int((plain.face_label[parent] == plain.names.index("other")).sum())
    assert count(only_white, "other") == inherited  # `other` keeps exactly what its parents' centroids said
    fc = np.asarray(everything.mesh.triangles_center)
    on_strip = (everything.face_label == everything.names.index("other"))
    assert count(everything, "other") < inherited  # judged by surface, the parents' wedges off the strip are gone
    assert (fc[on_strip][:, 0] > 1.0).all() and (fc[on_strip][:, 2] > 9.9).all()
    assert count(only_white, "white") > 0 and count(everything, "white") == count(only_white, "white")
    with pytest.raises(InvalidArgument) as exc:
        transfer_mod.classify_by_parts(skin, parts, 0.06, split_edge=1.2, surface_parts=["whit"])
    assert "white" in exc.value.accepted


def test_a_surface_judged_part_replaces_the_parents_centroid_decision_in_the_box() -> None:
    """A refined face the parent's centroid called white but that lies off the white surface is NOT white inside the box
    (the legacy wedge fix), and stays white outside it."""
    skin, fine = _giant_box_with_parts()
    c = np.asarray(fine.triangles_center)
    # the white part covers the centroid of one giant top triangle only partly: it is the x > 3.1 strip of the top face
    white = _half_mesh(fine, (c[:, 2] > 9.9) & (c[:, 0] > 3.1))
    inside = transfer_mod.classify_by_parts(skin, [("white", white)], 0.06, split_edge=1.2, split_box=(-30, 30, -30, 30, -30, 30))
    fc = np.asarray(inside.mesh.triangles_center)
    wrong = (inside.face_label == 0) & (fc[:, 0] < 1.0)  # white faces far from the strip (the strip's triangles reach 1.9 mm past x = 3.1)
    assert not wrong.any()  # surface test: only the strip


def test_transfer_class_matches_by_centroid_by_default_and_by_surface_on_request() -> None:
    """A 40 x 10 plate of two giant triangles (white below the diagonal, black above): a target face on the white triangle
    but nearer the black triangle's centroid is black by centroid and white by surface."""
    src = trimesh.Trimesh(
        np.array([[0, 0, 0], [40, 0, 0], [40, 10, 0], [0, 10, 0]], float), np.array([[0, 1, 2], [0, 2, 3]]), process=False
    )  # triangle 0 (white): below the diagonal y = x / 4; triangle 1 (black): above it
    names, fl = ["white", "black"], np.array([0, 1])
    tgt = trimesh.Trimesh(
        np.array([[14.0, 2.0, 0.0], [14.4, 2.0, 0.0], [14.0, 2.4, 0.0], [35.0, 1.0, 0.0], [35.4, 1.0, 0.0], [35.0, 1.4, 0.0]]),
        np.array([[0, 1, 2], [3, 4, 5]]),
        process=False,
    )
    new = Surf(tgt)
    common: dict[str, Any] = {"positive": ["white"], "exclude": [], "max_dist": 50.0, "island_mm2": 0.0001, "fill_boxes": []}
    by_centroid, _ = transfer_mod.transfer_class(new, src, fl, names, match="centroid", **common)
    by_surface, _ = transfer_mod.transfer_class(new, src, fl, names, match="surface", **common)
    cen = np.asarray(new.c)
    assert (cen[:, 1] < cen[:, 0] / 4).all()  # both targets lie on the white triangle
    assert (by_surface == 0).all()
    c_white, c_black = np.asarray(src.triangles_center)
    nearest_black = np.linalg.norm(cen - c_black, axis=1) < np.linalg.norm(cen - c_white, axis=1)
    assert nearest_black.tolist() == [True, False]
    assert ((by_centroid == -1) == nearest_black).all()
    with pytest.raises(InvalidArgument):
        transfer_mod.transfer_class(new, src, fl, names, match="nearest", **common)


def test_the_refined_skin_is_stored_in_double_precision(tmp_path: Path) -> None:
    """A float32 PLY moves the centroids a refined skin was classified with: ties between two source faces would flip."""
    m = trimesh.Trimesh(np.array([[0, 0, 0], [1.0000001234567, 0, 0], [0, 1.0000007654321, 0.3333333333]]), np.array([[0, 1, 2]]), process=False)
    transfer_mod.write_ply_f64(m, tmp_path / "m.ply")
    back = load_mesh(tmp_path / "m.ply", process=False)
    assert np.array_equal(np.asarray(back.vertices), np.asarray(m.vertices)) and back.faces.tolist() == [[0, 1, 2]]
    m.export(str(tmp_path / "f32.ply"))
    assert not np.array_equal(np.asarray(load_mesh(tmp_path / "f32.ply", process=False).vertices), np.asarray(m.vertices))  # the control


def test_full_reach_stops_a_cell_at_the_far_wall_of_a_thin_plate(tmp_path: Path) -> None:
    """A 5 mm plate coloured 3 mm deep from the top: the legacy grid cannot see the bottom wall 5 mm away and the cell
    runs past the mid-plane; with full reach it stops there."""
    from colour_regions.volume import shell_volume

    plate = trimesh.creation.box((40, 40, 5))
    plate.apply_translation([0, 0, 2.5])
    s = Surf(plate)
    top = np.asarray(plate.triangles_center)[:, 2] > 4.9
    zmin = {}
    for reach in (False, True):
        cell = shell_volume(s, top, 3.0, 0.25, tmp_path / f"cell_{reach}.ply", full_reach=reach)
        zmin[reach] = float(cell.bounds[0][2])
    assert zmin[False] < 2.3  # the legacy cell crosses the mid-plane at z = 2.5
    assert zmin[True] > 2.3


def test_verify_parts_gate_reads_the_built_projects_parts_and_fails_when_they_are_swapped(tmp_path: Path) -> None:
    """Gate 2 from a project's manifest + parts: a default part under everything, the two colour parts over it, in the
    project's frame (shift). Swapping the colour parts' assigns FAILs."""
    m, work, _ = _build_sphere(tmp_path)
    group, names, assign = build_mod.load_groups(work)
    s = Surf(m)
    shift = [5.0, -3.0, 2.0]
    th = verify_mod.Thresholds()
    good = write_project(tmp_path / "good", m, work, shift, {"slot": 2}, {"mix": "23"})  # top = W (slot 2), bottom = WR (mix "23")
    swapped = write_project(tmp_path / "swapped", m, work, shift, {"mix": "23"}, {"slot": 2})
    ok = verify_mod.gate_parts(s, group, assign, names, good, th, "K", n=1500)
    bad = verify_mod.gate_parts(s, group, assign, names, swapped, th, "K", n=1500)
    assert ok["pass"] and ok["agree"] >= 0.97 and ok["agree_permuted"] < 0.6 * 0.97 and ok["source"] == "parts"
    assert not bad["pass"] and bad["agree"] < 0.6
    with pytest.raises(UsageError, match="default-key"):
        verify_mod.gate_parts(s, group, assign, names, good, th, "X", n=1500)


def test_a_later_part_keeps_its_claim_when_an_earlier_surface_judged_part_loses_the_face() -> None:
    """[white, pink] with only white judged by surface: a refined face the parent's centroid gave to BOTH, but that is off
    the white surface, falls back to pink instead of the default colour."""
    skin, fine = _giant_box_with_parts()
    c = np.asarray(fine.triangles_center)
    strip = (c[:, 2] > 9.9) & (c[:, 0] > 3.1)
    skin_c = np.asarray(skin.triangles_center)
    top_giants = np.nonzero(skin_c[:, 2] > 9.9)[0]
    # both parts claim the top giant triangles by centroid: a tiny face at each centroid (white's 0.05 mm above the skin)
    n = len(top_giants)

    def micro(offset: float) -> trimesh.Trimesh:
        p = skin_c[top_giants] + [0, 0, offset]
        return trimesh.Trimesh(np.vstack([p, p + [0.01, 0, 0], p + [0, 0.01, 0]]), np.array([[i, i + n, i + 2 * n] for i in range(n)]), process=False)

    white = trimesh.util.concatenate([_half_mesh(fine, strip), micro(0.05)])
    pink = micro(0.0)
    box = (-30.0, 30.0, -30.0, 30.0, -30.0, 30.0)
    res = transfer_mod.classify_by_parts(skin, [("white", white), ("pink", pink)], 0.06, split_edge=1.2, split_box=box, surface_parts=["white"])
    fc = np.asarray(res.mesh.triangles_center)
    far_from_micro = np.min([np.linalg.norm(fc - g, axis=1) for g in skin_c[top_giants]], axis=0) > 0.5
    off_strip = (fc[:, 2] > 9.9) & (fc[:, 0] < 1.0) & far_from_micro
    assert off_strip.any() and (res.face_label[off_strip] == res.names.index("pink")).all()
    with pytest.raises(UsageError, match="needs --split-edge"):
        transfer_mod.classify_by_parts(skin, [("white", white)], 0.06, surface_parts=["white"])
    with pytest.raises(InvalidArgument):
        transfer_mod.classify_by_parts(skin, [("white", white)], 0.06, surface_parts=["whit"])


def test_the_default_cell_sees_a_giant_neighbour_whose_centroid_is_far(tmp_path: Path) -> None:
    """The top face of a 60 mm box coloured 3 mm deep: the side faces are giant triangles whose centroids lie 20 mm below
    the grid, but their triangles run along it, so a point 0.4 mm from the side wall is the wall's, not the cell's."""
    from colour_regions.volume import shell_volume

    box = trimesh.creation.box((60, 60, 60))
    s = Surf(box)
    top = np.asarray(box.triangles_center)[:, 2] > 29.0
    cell = shell_volume(s, top, 3.0, 0.3, tmp_path / "top.ply")
    inside_wall = np.array([[29.6, 0.0, 28.5]])  # 1.5 mm under the top face, 0.4 mm from the x = 30 wall
    well_inside = np.array([[0.0, 0.0, 28.5]])
    assert bool(cell.contains(well_inside)[0]) and not bool(cell.contains(inside_wall)[0])


def test_gate_parts_takes_slot_assigns_and_ignores_stray_files_and_reports_bad_manifests(tmp_path: Path) -> None:
    """A `{"slot": N}` group assign, a `.DS_Store` in the parts directory and a manifest without `parts` are handled."""
    m, work, _ = _build_sphere(tmp_path)
    group, names, assign = build_mod.load_groups(work)
    s = Surf(m)
    th = verify_mod.Thresholds()
    stem = write_project(tmp_path / "p", m, work, [1.0, 2.0, 3.0], {"slot": 2}, {"mix": "23"})
    (Path(str(stem) + ".parts") / ".DS_Store").write_text("x")
    slot_assign = [{"slot": 2} if a == {"key": "W"} else a for a in assign]
    ok = verify_mod.gate_parts(s, group, slot_assign, names, stem, th, "K", n=1500)
    assert ok["pass"] and set(ok["per_group"]) == {"<none>", "top", "bottom"}
    with pytest.raises(UsageError, match="not a filament key"):
        verify_mod.gate_parts(s, group, [{"key": "X"}, assign[1]], names, stem, th, "K", n=1500)
    Path(str(stem) + ".manifest.json").write_text(json.dumps({"shift": [0, 0, 0]}), encoding="utf-8")
    with pytest.raises(UsageError, match="not a project manifest"):
        verify_mod.gate_parts(s, group, assign, names, stem, th, "K", n=1500)
