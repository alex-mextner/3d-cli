"""Tests for `3d halves-check` — do the two halves of a part split for printing still fit?

Synthetic halves (tests/halves_fixtures.py) stand in for real split models: a matching
pair, a pair whose cut faces differ (one tier 3 mm longer), and pairs whose dowel holes
moved or vanished. Each is checked both directly and through an Orca-layout 3MF carrying
the pair metadata, and as an STL pair in the assembly frame.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import cast

import numpy as np
import pytest

import halves_check as hc
from halves_fixtures import (
    DOWELS,
    TREE,
    TREE_LONG_TIER,
    half_mesh,
    pair_objects,
    write_orca_3mf,
    write_stl,
)

CUT = (np.array([0.0, 1.0, 0.0]), 0.0)


def _pair(mesh2: hc.Mesh, dowels: int | None = 2) -> hc.Pair:
    return hc.Pair("T", hc.Half("T1", [half_mesh(1)]), hc.Half("T2", [mesh2]), CUT[0], CUT[1], dowels, "test")


def test_matching_halves_pass_with_equal_faces_and_coinciding_dowels() -> None:
    rep = hc.check_pair(_pair(half_mesh(2)), hc.Options())

    assert rep["status"] == "PASS", rep["failures"]
    assert rep["max_dev_mm"] <= 0.1
    assert rep["iou"] >= 0.99
    assert len(rep["holes_a"]) == len(rep["holes_b"]) == 2
    assert max(h["offset_mm"] for h in rep["hole_pairs"]) <= 0.1
    assert all(abs(h["d_a"] - 3.0) < 0.15 for h in rep["hole_pairs"])
    assert rep["face_size_a"] == pytest.approx(rep["face_size_b"], abs=0.1)


def test_cut_faces_that_differ_fail_with_the_overhang_width() -> None:
    rep = hc.check_pair(_pair(half_mesh(2, outline=TREE_LONG_TIER)), hc.Options())

    assert rep["status"] == "FAIL"
    assert 2.5 <= rep["max_dev_mm"] <= 3.5
    assert any("outline" in f for f in rep["failures"])


def test_moved_dowel_hole_fails() -> None:
    moved = [(DOWELS[0][0] + 1.0, DOWELS[0][1]), DOWELS[1]]
    rep = hc.check_pair(_pair(half_mesh(2, dowels=moved)), hc.Options())

    assert rep["status"] == "FAIL"
    assert any("dowel" in f for f in rep["failures"])


def test_missing_dowel_hole_fails_against_the_expected_count() -> None:
    rep = hc.check_pair(_pair(half_mesh(2, dowels=DOWELS[:1])), hc.Options())

    assert rep["status"] == "FAIL"
    assert any("dowel" in f for f in rep["failures"])


def test_half_on_the_wrong_side_of_the_cut_fails() -> None:
    rep = hc.check_pair(_pair(half_mesh(1)), hc.Options())

    assert rep["status"] == "FAIL"
    assert any("side" in f for f in rep["failures"])


def test_project_3mf_pairs_are_found_and_checked(tmp_path: Path) -> None:
    good = write_orca_3mf(tmp_path / "good.3mf", pair_objects("T", half_mesh(1), half_mesh(2), colour_split=True))
    bad = write_orca_3mf(tmp_path / "bad.3mf", pair_objects("T", half_mesh(1), half_mesh(2, outline=TREE_LONG_TIER)))

    pairs, warnings = hc.load_3mf(good)
    assert [(p.pair_id, p.a.name, p.b.name) for p in pairs] == [("T", "T1", "T2")]
    assert warnings == []
    assert len(pairs[0].a.meshes) == 2              # both colour parts of the half
    assert hc.check_pair(pairs[0], hc.Options())["status"] == "PASS"
    bad_pairs, _ = hc.load_3mf(bad)
    assert hc.check_pair(bad_pairs[0], hc.Options())["status"] == "FAIL"


def test_object_moved_in_the_slicer_still_maps_to_the_assembly(tmp_path: Path) -> None:
    objs = pair_objects("T", half_mesh(1), half_mesh(2))
    # lift half 2 by 1 mm: the slicer re-centres the object, its local frame shifts by a translation
    objs[1]["parts"] = [(V + (0.0, 0.0, 1.0), F) for V, F in cast(list, objs[1]["parts"])]
    pairs, _ = hc.load_3mf(write_orca_3mf(tmp_path / "moved.3mf", objs))

    assert hc.check_pair(pairs[0], hc.Options())["status"] == "PASS"


def test_unmarked_numbered_halves_are_reported_by_name(tmp_path: Path) -> None:
    path = write_orca_3mf(tmp_path / "unmarked.3mf", pair_objects("T", half_mesh(1), half_mesh(2), meta=False))
    pairs, warnings = hc.load_3mf(path)

    assert pairs == []
    assert len(warnings) == 1 and "T1" in warnings[0] and "T2" in warnings[0]
    assert hc.main([str(path)]) == 0
    assert hc.main([str(path), "--strict-names"]) == 1


def test_half_without_its_partner_is_an_error(tmp_path: Path) -> None:
    objs = pair_objects("T", half_mesh(1), half_mesh(2))[:1]
    path = write_orca_3mf(tmp_path / "lonely.3mf", objs)

    with pytest.raises(hc.MetadataError, match="T"):
        hc.load_3mf(path)


def test_bad_matrix_metadata_is_an_error(tmp_path: Path) -> None:
    objs = pair_objects("T", half_mesh(1), half_mesh(2))
    objs[0]["meta"]["halves_to_assembly"] = "1 0 0"  # type: ignore[index]
    path = write_orca_3mf(tmp_path / "badmeta.3mf", objs)

    with pytest.raises(hc.MetadataError, match="halves_to_assembly"):
        hc.load_3mf(path)


def test_stl_pair_in_assembly_frame_finds_the_cut_plane(tmp_path: Path) -> None:
    a = write_stl(tmp_path / "a.stl", half_mesh(1))
    b = write_stl(tmp_path / "b.stl", half_mesh(2))
    b_bad = write_stl(tmp_path / "b_bad.stl", half_mesh(2, outline=TREE_LONG_TIER))

    pair = hc.load_stl_pair([str(a)], [str(b)], plane=None, name="T")
    assert abs(abs(float(pair.normal[1])) - 1.0) < 1e-6 and abs(pair.offset) < 1e-6
    assert hc.check_pair(pair, hc.Options())["status"] == "PASS"
    assert hc.check_pair(hc.load_stl_pair([str(a)], [str(b_bad)], plane=None, name="T"), hc.Options())["status"] == "FAIL"


def test_explicit_plane_spec_is_parsed() -> None:
    n, d = hc.parse_plane("y=12.5")
    assert list(n) == [0.0, 1.0, 0.0] and d == 12.5
    n, d = hc.parse_plane("0 0 2 4")
    assert list(n) == [0.0, 0.0, 1.0] and d == 2.0
    with pytest.raises(ValueError):
        hc.parse_plane("w=1")


def test_main_reports_table_json_and_png(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = write_orca_3mf(tmp_path / "bad.3mf", pair_objects("T", half_mesh(1), half_mesh(2, outline=TREE_LONG_TIER)))

    assert hc.main([str(bad), "--png", str(tmp_path / "png")]) == 1
    out = capsys.readouterr().out
    assert "T1/T2" in out and "FAIL" in out
    png = tmp_path / "png" / "bad__T.png"
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    assert hc.main([str(bad), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "FAIL"
    assert report["pairs"][0]["pair"] == "T" and report["pairs"][0]["status"] == "FAIL"


def test_directory_scan_checks_every_project(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_orca_3mf(tmp_path / "a.3mf", pair_objects("T", half_mesh(1), half_mesh(2)))
    sub = tmp_path / "sub"
    sub.mkdir()
    write_orca_3mf(sub / "b.3mf", pair_objects("U", half_mesh(1), half_mesh(2)))
    with zipfile.ZipFile(tmp_path / "plain.3mf", "w") as zf:      # no pairs at all: skipped
        zf.writestr("3D/3dmodel.model", '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources/><build/></model>')

    assert hc.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "T1/T2" in out and "U1/U2" in out and "PASS" in out
    assert hc.main([str(tmp_path / "plain.3mf"), "--require-pairs"]) == 1


def test_face_mask_fills_the_cut_section_exactly() -> None:
    V, F = half_mesh(1, dowels=[])
    seg = hc.section_segments(V, F, CUT[0], 0.05)
    e1, e2 = hc.plane_basis(CUT[0])
    grid = hc.Grid.around([seg], e1, e2, 0.05)
    area = hc.fill_mask(seg, grid, e1, e2).sum() * 0.05 ** 2
    # shoelace area of the outline
    xs, zs = np.array([p[0] for p in TREE]), np.array([p[1] for p in TREE])
    expected = 0.5 * abs(np.dot(xs, np.roll(zs, -1)) - np.dot(zs, np.roll(xs, -1)))
    assert area == pytest.approx(expected, rel=0.01)


def test_command_check_is_callable_from_other_gates(tmp_path: Path) -> None:
    from commands.halves_check import check

    good = write_orca_3mf(tmp_path / "good.3mf", pair_objects("T", half_mesh(1), half_mesh(2)))
    bad = write_orca_3mf(tmp_path / "bad.3mf", pair_objects("T", half_mesh(1), half_mesh(2, outline=TREE_LONG_TIER)))
    assert check([str(good)]) == 0
    assert check([str(bad)], "--json") == 1
    result = hc.check_inputs([str(tmp_path)])
    assert result["status"] == "FAIL" and result["exit_code"] == 1
    assert {r["file"] for r in result["pairs"]} == {"good.3mf", "bad.3mf"}


def test_inch_unit_project_is_measured_in_millimetres(tmp_path: Path) -> None:
    good = write_orca_3mf(tmp_path / "inch.3mf", pair_objects("T", half_mesh(1), half_mesh(2)), unit="inch")
    bad = write_orca_3mf(tmp_path / "inch_bad.3mf",
                         pair_objects("T", half_mesh(1), half_mesh(2, outline=TREE_LONG_TIER)), unit="inch")

    rep = hc.check_pair(hc.load_3mf(good)[0][0], hc.Options())
    assert rep["status"] == "PASS", rep["failures"]
    assert rep["face_size_a"] == pytest.approx([32.0, 46.0], abs=0.1)
    assert 2.5 <= hc.check_pair(hc.load_3mf(bad)[0][0], hc.Options())["max_dev_mm"] <= 3.5


@pytest.mark.parametrize("cut2", [None, "0 0 0 0", "0 1 0 nan"])
def test_cut_plane_must_be_given_and_valid_on_both_halves(tmp_path: Path, cut2: str | None) -> None:
    objs = pair_objects("T", half_mesh(1), half_mesh(2))
    meta1 = cast(dict, objs[0]["meta"])
    meta2 = cast(dict, objs[1]["meta"])
    if cut2 is None:
        del meta2["halves_cut"]
    else:
        meta1["halves_cut"] = meta2["halves_cut"] = cut2
    path = write_orca_3mf(tmp_path / "cut.3mf", objs)

    with pytest.raises(hc.MetadataError, match="halves_cut"):
        hc.load_3mf(path)
    assert hc.main([str(path)]) == 2


def test_malformed_component_transform_is_a_metadata_error(tmp_path: Path) -> None:
    path = write_orca_3mf(tmp_path / "t.3mf", pair_objects("T", half_mesh(1), half_mesh(2)))
    with zipfile.ZipFile(path) as zf:
        files = {n: zf.read(n) for n in zf.namelist()}
    files["3D/3dmodel.model"] = files["3D/3dmodel.model"].replace(b'transform="1 0 0 0 1 0 0 0 1 ', b'transform="1 0 0 ', 1)
    with zipfile.ZipFile(path, "w") as zf:
        for n, data in files.items():
            zf.writestr(n, data)

    with pytest.raises(hc.MetadataError, match="transform"):
        hc.load_3mf(path)


@pytest.mark.parametrize(("dowels1", "dowels2", "match"), [("two", None, "whole number"), ("2.5", "2.5", "whole number"),
                                                            ("-1", None, "negative"), ("1", "2", "disagree")])
def test_bad_or_conflicting_dowel_count_is_a_metadata_error(tmp_path: Path, dowels1: str, dowels2: str | None,
                                                            match: str) -> None:
    objs = pair_objects("T", half_mesh(1, dowels=DOWELS[:1]), half_mesh(2, dowels=DOWELS[:1]))
    cast(dict, objs[0]["meta"])["halves_dowels"] = dowels1
    if dowels2 is not None:
        cast(dict, objs[1]["meta"])["halves_dowels"] = dowels2
    path = write_orca_3mf(tmp_path / "dowels.3mf", objs)

    with pytest.raises(hc.MetadataError, match=match):
        hc.load_3mf(path)
    assert hc.main([str(path)]) == 2


def test_non_finite_assembly_matrix_is_a_metadata_error(tmp_path: Path) -> None:
    objs = pair_objects("T", half_mesh(1), half_mesh(2))
    meta = cast(dict, objs[0]["meta"])
    meta["halves_to_assembly"] = " ".join(["nan"] + meta["halves_to_assembly"].split()[1:])
    path = write_orca_3mf(tmp_path / "m.3mf", objs)

    with pytest.raises(hc.MetadataError, match="halves_to_assembly"):
        hc.load_3mf(path)


def test_non_numeric_vertex_is_a_metadata_error_not_a_crash(tmp_path: Path) -> None:
    path = write_orca_3mf(tmp_path / "v.3mf", pair_objects("T", half_mesh(1), half_mesh(2)))
    with zipfile.ZipFile(path) as zf:
        files = {n: zf.read(n) for n in zf.namelist()}
    model = next(n for n, data in files.items() if b"<vertex " in data)
    files[model] = files[model].replace(b'<vertex x="', b'<vertex x="abc', 1)
    with zipfile.ZipFile(path, "w") as zf:
        for n, data in files.items():
            zf.writestr(n, data)

    result = hc.check_inputs([str(path)])
    assert result["exit_code"] == 2 and "not numeric" in result["errors"][0]


def test_too_fine_raster_is_refused_before_allocating(tmp_path: Path) -> None:
    path = write_orca_3mf(tmp_path / "r.3mf", pair_objects("T", half_mesh(1), half_mesh(2)))

    result = hc.check_inputs([str(path)], opts=hc.Options(res=1e-6))
    assert result["exit_code"] == 2 and "coarser --res" in result["errors"][0]
    assert hc.main([str(path), "--res", "0.000001"]) == 2


@pytest.mark.parametrize("args", [["--tol", "nan"], ["--tol", "inf"], ["--tol", "0"], ["--tol", "-1"],
                                  ["--res", "nan"], ["--res", "0"], ["--min-iou", "nan"], ["--min-iou", "1.5"],
                                  ["--plane", "y=0"], ["--pair", "a.stl", "b.stl", "--plane", "nan 0 1 0"],
                                  ["--pair", "a.stl", "b.stl", "--plane", "1e308 1e308 0 0"]])
def test_non_finite_or_out_of_range_options_are_usage_errors(tmp_path: Path, args: list[str]) -> None:
    bad = write_orca_3mf(tmp_path / "bad.3mf", pair_objects("T", half_mesh(1), half_mesh(2, outline=TREE_LONG_TIER)))
    assert hc.main([str(bad), *args]) == 2


def test_png_name_cannot_escape_the_png_directory(tmp_path: Path) -> None:
    objs = pair_objects("T", half_mesh(1), half_mesh(2))
    for o in objs:
        cast(dict, o["meta"])["halves_pair"] = "../../outside"
    path = write_orca_3mf(tmp_path / "evil.3mf", objs)
    png_dir = tmp_path / "png"

    assert hc.main([str(path), "--png", str(png_dir)]) == 0
    written = list(tmp_path.rglob("*.png"))
    assert written and all(png_dir in p.parents for p in written)


def test_dowel_holes_are_matched_by_the_best_overall_assignment() -> None:
    # two close holes listed in an order where a greedy nearest match pairs them wrongly
    # (greedy from the hole at u=1: 1->4.2, 6->-3 = 3.2 + 9.0; best: 1->-3, 6->4.2 = 4.0 + 1.8)
    a = [(1.0, 12.0), (6.0, 12.0)]
    b = [(4.2, 12.0), (-3.0, 12.0)]
    rep = hc.check_pair(_pair_holes(a, b), hc.Options())
    assert max(h["offset_mm"] for h in rep["hole_pairs"]) == pytest.approx(4.0, abs=0.1)
    assert sum(h["offset_mm"] for h in rep["hole_pairs"]) == pytest.approx(5.8, abs=0.15)


def _pair_holes(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> hc.Pair:
    big = [(-12.0, 0.0), (12.0, 0.0), (12.0, 24.0), (-12.0, 24.0)]
    p = hc.Pair("T", hc.Half("T1", [half_mesh(1, outline=big, dowels=a)]),
                hc.Half("T2", [half_mesh(2, outline=big, dowels=b)]), CUT[0], CUT[1], None, "test")
    return p
