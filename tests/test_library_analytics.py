"""Unit tests for lib/library/analytics.py: deterministic file facts from names/sizes, archive listing, mesh
measurement of generated STL fixtures (no network, no recorded models)."""

from __future__ import annotations

import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
from search_fixtures import cube_stl

from library import analytics

LIB = Path(__file__).resolve().parents[1] / "lib"


# -- extension / class -----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,ext,klass",
    [
        ("Part.STL", "stl", "mesh"),
        ("case.step", "step", "cad"),
        ("case.STP", "stp", "cad"),
        ("model.f3d", "f3d", "cad"),
        ("param.scad", "scad", "cad"),
        ("scene.blend", "blend", "cad"),
        ("box.3mf", "3mf", "mesh"),
        ("a.tar.gz", "gz", "archive"),
        ("pack.zip", "zip", "archive"),
        ("outline.dxf", "dxf", "drawing"),
        ("plate.gcode", "gcode", "gcode"),
        ("photo.JPG", "jpg", "image"),
        ("README", "", "other"),
        (".hidden", "", "other"),
        ("dir/sub/Lid.obj", "obj", "mesh"),
    ],
)
def test_extension_and_class(name: str, ext: str, klass: str) -> None:
    assert analytics.extension(name) == ext
    assert analytics.file_class(name) == klass


# -- cad flag, table-driven ------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "files,cad,formats",
    [
        ([("a.stl", 10), ("b.3mf", 10)], False, []),
        ([("a.stl", 10), ("b.step", 10)], True, ["step"]),
        ([("a.STP", 10)], True, ["stp"]),
        ([("a.f3d", 1), ("a.f3z", 1), ("a.scad", 1)], True, ["f3d", "f3z", "scad"]),
        ([("scene.blend", 1)], True, ["blend"]),
        (
            [("drawing.dxf", 1), ("drawing.dwg", 1), ("logo.svg", 1)],
            False,
            [],
        ),  # 2-D drawings are not 3-D CAD
        ([("pack.zip", 100)], False, []),  # an archive nobody opened proves nothing
        ([], False, []),
    ],
)
def test_cad_is_true_exactly_when_a_cad_format_is_listed(
    files: list[tuple[str, int]], cad: bool, formats: list[str]
) -> None:
    facts = analytics.file_facts(files)
    assert facts["cad"] is cad and facts["cad_formats"] == formats


def test_archive_members_decide_cad_once_the_archive_is_listed() -> None:
    files = [("pack.zip", 100), ("readme.txt", 5)]
    assert analytics.file_facts(files)["archives_unlisted"] == 1
    expanded = analytics.expand_archives(
        files, {"pack.zip": [("src/case.step", 50), ("src/case.stl", 40)]}
    )
    facts = analytics.file_facts(expanded)
    assert facts["cad"] and facts["cad_formats"] == ["step"] and facts["has_stl"]
    assert facts["archives_unlisted"] == 0
    assert ("pack.zip/src/case.step", 50) in expanded


def test_unknown_file_list_is_not_reported_as_no_cad() -> None:
    facts = analytics.file_facts([], files_known=False)
    assert facts == {"schema": analytics.SCHEMA, "files_known": False}


# -- flags and ratios ------------------------------------------------------------------------------------
def test_facts_for_a_cad_plus_mesh_listing() -> None:
    facts = analytics.file_facts(
        [
            ("Case.step", 900_000),
            ("Case.stl", 300_000),
            ("Case.scad", 12_000),
            ("photo.jpg", 88_000),
        ]
    )
    assert facts["file_count"] == 4 and facts["total_bytes"] == 1_300_000
    assert facts["formats"] == {"jpg": 1, "scad": 1, "step": 1, "stl": 1}
    assert (
        facts["cad"] and facts["has_stl"] and facts["has_step"] and facts["parametric"]
    )
    assert facts["mesh_bytes"] == 300_000 and facts["cad_bytes"] == 912_000
    assert facts["mesh_ratio"] == pytest.approx(0.2308, abs=1e-4)
    assert facts["cad_ratio"] == pytest.approx(0.7015, abs=1e-4)
    assert (
        facts["part_count"] == 1
        and not facts["is_multi_part"]
        and not facts["is_assembly"]
    )
    assert facts["size_class"] == "small"  # 300 kB of mesh


def test_multi_part_counts_distinct_part_names_not_formats_or_versions() -> None:
    same_part = analytics.file_facts(
        [("Lid.stl", 1), ("lid.3mf", 1), ("Lid (1).stl", 1), ("lid_v2.stl", 1)]
    )
    assert same_part["part_count"] == 1 and not same_part["is_multi_part"]
    two = analytics.file_facts([("Base.stl", 1), ("Lid.stl", 1)])
    assert two["part_count"] == 2 and two["is_multi_part"]


@pytest.mark.parametrize(
    "name,assembly",
    [
        ("case_assembled.stl", True),
        ("AssemblyView.3mf", True),
        ("All_Parts_complete.stl", True),
        ("assembly.sldasm", True),
        ("Lid.stl", False),
        ("plate_1.stl", False),
        ("reassemblyfree.stl", False),
    ],
)
def test_assembly_hint_comes_from_name_tokens_or_assembly_formats(
    name: str, assembly: bool
) -> None:
    facts = analytics.file_facts([(name, 1000)])
    assert facts["is_assembly"] is assembly
    assert (name in facts["assembly_files"]) is assembly


@pytest.mark.parametrize(
    "total,klass",
    [
        (None, "unknown"),
        (0, "unknown"),
        (99_999, "tiny"),
        (100_000, "small"),
        (1_999_999, "small"),
        (2_000_000, "medium"),
        (19_999_999, "medium"),
        (20_000_000, "large"),
        (100_000_000, "huge"),
    ],
)
def test_size_class_thresholds(total: int | None, klass: str) -> None:
    assert analytics.size_class(total) == klass


def test_binary_stl_triangle_estimate() -> None:
    facts = analytics.file_facts([("a.stl", 84 + 50 * 12)])
    assert facts["stl_triangles_est"] == 12
    assert analytics.file_facts([("a.obj", 5000)])["stl_triangles_est"] is None


def test_facts_are_deterministic_and_order_independent() -> None:
    a = analytics.file_facts([("b.stl", 2), ("a.step", 1), ("c.zip", 3)])
    b = analytics.file_facts([("c.zip", 3), ("a.step", 1), ("b.stl", 2)])
    assert a == b and list(a) == list(b)


# -- pick_mesh / reference suitability -------------------------------------------------------------------
def test_pick_mesh_takes_the_smallest_listed_mesh_within_the_cap() -> None:
    files = [
        ("big.stl", 9_000_000),
        ("small.stl", 1_000),
        ("tiny.step", 10),
        ("mid.3mf", 5_000),
        ("unknown.stl", None),
        ("empty.stl", 0),
    ]
    assert analytics.pick_mesh(files, max_bytes=10_000_000) == ("small.stl", 1_000)
    assert analytics.pick_mesh(files, max_bytes=500) is None
    assert analytics.pick_mesh([("a.step", 5)], max_bytes=10) is None


def test_reference_suitability_reports_every_failed_test() -> None:
    good = analytics.file_facts([("board.step", 200_000), ("board.stl", 50_000)])
    assert analytics.reference_suitability(good, paid=False, license_name="CC-BY") == {
        "suitable": True,
        "dims_known": False,
        "reasons": [],
    }
    bad = analytics.reference_suitability(
        analytics.file_facts([("big.zip", 120_000_000)]), paid=True, license_name=""
    )
    assert not bad["suitable"]
    assert bad["reasons"] == [
        "paid",
        "no licence listed",
        "no STEP or mesh file",
        "size class huge (needs small or smaller)",
    ]
    measured = {"status": "ok", "bbox_mm": [300.0, 10.0, 5.0]}
    too_big = analytics.reference_suitability(
        good, paid=False, license_name="MIT", mesh=measured
    )
    assert (
        not too_big["suitable"]
        and too_big["dims_known"]
        and "largest edge 300 mm" in too_big["reasons"][0]
    )
    known = analytics.reference_suitability(
        good,
        paid=False,
        license_name="MIT",
        mesh={"status": "ok", "bbox_mm": [60.0, 30.0, 2.0]},
    )
    assert known == {"suitable": True, "dims_known": True, "reasons": []}
    assert (
        analytics.reference_suitability(
            {"files_known": False}, paid=False, license_name="MIT"
        )["suitable"]
        is False
    )


# -- mesh measurement on generated fixtures --------------------------------------------------------------
def test_analyze_mesh_of_a_closed_box(tmp_path: Path) -> None:
    path = tmp_path / "box.stl"
    path.write_bytes(cube_stl(10, 20, 30))
    m = analytics.analyze_mesh(path)
    assert m["status"] == "ok"
    assert (
        m["bbox_mm"] == [10.0, 20.0, 30.0]
        and m["triangles"] == 12
        and m["body_count"] == 1
    )
    assert m["watertight"] and m["winding_consistent"] and m["manifold"]
    assert m["volume_mm3"] == pytest.approx(6000.0) and m[
        "surface_area_mm2"
    ] == pytest.approx(2200.0)
    assert m["fill_ratio"] == pytest.approx(1.0)
    assert (
        m["est_print_footprint_mm"] == [30.0, 20.0] and m["est_print_height_mm"] == 10.0
    )
    assert (
        m["dimension_class"] == "small"
        and not m["units_suspect"]
        and m["degenerate_faces"] == 0
    )


def test_analyze_mesh_of_an_open_box_has_no_volume(tmp_path: Path) -> None:
    path = tmp_path / "open.stl"
    path.write_bytes(cube_stl(10, 20, 30, open_top=True))
    m = analytics.analyze_mesh(path)
    assert m["status"] == "ok" and not m["watertight"] and not m["manifold"]
    assert m["volume_mm3"] is None and m["fill_ratio"] is None and m["triangles"] == 10


def test_analyze_mesh_counts_separate_bodies(tmp_path: Path) -> None:
    first, second = cube_stl(5, 5, 5), cube_stl(5, 5, 5, origin=(20.0, 0.0, 0.0))
    count = int.from_bytes(first[80:84], "little") + int.from_bytes(
        second[80:84], "little"
    )
    path = tmp_path / "two.stl"
    path.write_bytes(
        first[:80] + count.to_bytes(4, "little") + first[84:] + second[84:]
    )
    m = analytics.analyze_mesh(path)
    assert (
        m["body_count"] == 2
        and m["bbox_mm"] == [25.0, 5.0, 5.0]
        and m["volume_mm3"] == pytest.approx(250.0)
    )


def test_analyze_mesh_flags_suspect_units(tmp_path: Path) -> None:
    path = tmp_path / "metres.stl"
    path.write_bytes(cube_stl(0.05, 0.04, 0.03))
    assert analytics.analyze_mesh(path)["units_suspect"] is True


def test_analyze_mesh_reports_an_unreadable_file_instead_of_raising(
    tmp_path: Path,
) -> None:
    path = tmp_path / "broken.stl"
    path.write_bytes(b"this is not a mesh")
    m = analytics.analyze_mesh(path)
    assert m["status"] == "error" and m["reason"]


# -- archives --------------------------------------------------------------------------------------------
def test_list_archive_reads_zip_and_tar_member_tables(tmp_path: Path) -> None:
    zpath = tmp_path / "pack.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("src/", "")
        zf.writestr("src/case.step", b"x" * 40)
        zf.writestr("case.stl", b"y" * 10)
    tpath = tmp_path / "pack.tar.gz"
    with tarfile.open(tpath, "w:gz") as tf:
        data = tmp_path / "member.scad"
        data.write_bytes(b"cube(1);")
        tf.add(data, arcname="member.scad")
    assert analytics.list_archive(zpath) == [("src/case.step", 40), ("case.stl", 10)]
    assert analytics.list_archive(tpath) == [("member.scad", 8)]
    (tmp_path / "junk.zip").write_bytes(b"not an archive")
    assert analytics.list_archive(tmp_path / "junk.zip") == []
    assert analytics.analyze_path(zpath)["members"]["cad"] is True


def test_analytics_module_imports_without_trimesh_or_numpy() -> None:
    code = (
        "import sys; sys.modules['trimesh'] = None; sys.modules['numpy'] = None\n"
        "import library.analytics as a\n"
        "assert a.file_facts([('a.step', 1)])['cad']\n"
        "print('OK')\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=LIB,
        env={"PYTHONPATH": str(LIB), "PATH": ""},
    )
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr


# -- 3MF (read with the standard library: trimesh's own reader needs lxml) -------------------------------
def _write_3mf(path: Path, *, unit: str = "millimeter", transform: str = "", doctype: bool = False) -> None:
    cube = [(0, 0, 0), (10, 0, 0), (10, 20, 0), (0, 20, 0), (0, 0, 30), (10, 0, 30), (10, 20, 30), (0, 20, 30)]
    tris = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (2, 3, 7), (2, 7, 6),
            (0, 4, 7), (0, 7, 3), (1, 2, 6), (1, 6, 5)]
    vertices = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in cube)
    triangles = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
    xml = (("<!DOCTYPE x [<!ENTITY a 'b'>]>" if doctype else "") + f'<model unit="{unit}" '
           'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>'
           f'<object id="1" type="model"><mesh><vertices>{vertices}</vertices><triangles>{triangles}</triangles>'
           '</mesh></object><object id="2"><components><component objectid="1" transform="1 0 0 0 1 0 0 0 1 5 5 5"/>'
           '</components></object></resources>'
           f'<build><item objectid="2"{f" transform={chr(34)}{transform}{chr(34)}" if transform else ""}/></build></model>')
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", xml)


def test_3mf_is_measured_through_components_units_and_transforms(tmp_path: Path) -> None:
    plain = tmp_path / "a.3mf"
    _write_3mf(plain)
    m = analytics.analyze_mesh(plain)
    assert m["status"] == "ok" and m["bbox_mm"] == [10.0, 20.0, 30.0] and m["watertight"]
    assert m["volume_mm3"] == pytest.approx(6000.0) and m["triangles"] == 12
    inches = tmp_path / "inch.3mf"
    _write_3mf(inches, unit="inch")
    assert analytics.analyze_mesh(inches)["bbox_mm"] == [254.0, 508.0, 762.0]
    scaled = tmp_path / "scaled.3mf"
    _write_3mf(scaled, transform="2 0 0 0 2 0 0 0 2 0 0 0")
    assert analytics.analyze_mesh(scaled)["bbox_mm"] == [20.0, 40.0, 60.0]


def test_3mf_that_is_not_a_3mf_or_declares_entities_is_an_error_not_a_crash(tmp_path: Path) -> None:
    junk = tmp_path / "junk.3mf"
    junk.write_bytes(cube_stl(1, 1, 1))  # an STL renamed to .3mf
    assert analytics.analyze_mesh(junk) == {"status": "error", "reason": "ValueError: not a 3MF (zip) archive"}
    hostile = tmp_path / "hostile.3mf"
    _write_3mf(hostile, doctype=True)
    assert "DOCTYPE" in analytics.analyze_mesh(hostile)["reason"]
