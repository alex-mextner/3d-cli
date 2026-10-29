"""fit_coupon / fit_coupon_export: the ladders, the layout, the estimate and the rendered meshes (issue #573)."""

from __future__ import annotations

import math
import shutil
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

import fit_coupon as fc
import fit_coupon_export as export
from registries.printers import load_printers

BED = {name: load_printers()[model].bed for name, model in (("k1se", "Creality K1 SE"), ("u1", "Snapmaker U1"))}
needs_openscad = pytest.mark.skipif(shutil.which("openscad") is None, reason="OpenSCAD not installed")


def test_every_ladder_has_seven_steps_stepped_around_the_table_value() -> None:
    ladders = fc.ladders()
    assert [(x.fit_type, x.shape) for x in ladders] == list(fc.LADDER_SPECS)
    assert [x.number for x in ladders] == [1, 2, 3, 4, 5, 6]
    for ladder in ladders:
        gaps = ladder.gaps
        assert len(gaps) == fc.STEPS and list(gaps) == sorted(gaps)
        # the table value sits inside the ladder, with steps on both sides of it
        assert gaps[0] < ladder.table_mm < gaps[-1] or ladder.table_mm in gaps
        assert any(g < ladder.table_mm for g in gaps) or ladder.fit_type == "tight"
        assert any(g > ladder.table_mm for g in gaps)
        for step in ladder.steps:
            assert step.hole_mm == pytest.approx(ladder.mating_mm + 2 * step.gap_mm)


@pytest.mark.parametrize(
    ("fit", "shape", "expected"),
    [
        ("sliding", "round", (0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35)),
        ("tight", "round", (0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3)),
        ("snap", "slot", (0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45)),
        ("press", "round", (-0.15, -0.125, -0.1, -0.075, -0.05, -0.025, 0.0)),
    ],
)
def test_gaps_follow_the_table_values(fit: str, shape: str, expected: tuple[float, ...]) -> None:
    (ladder,) = fc.ladders((fit,), (shape,))
    assert ladder.gaps == expected


def test_a_press_ladder_is_an_undersized_hole() -> None:
    (ladder,) = fc.ladders(("press",), ("round",))
    assert ladder.steps[0].hole_mm < fc.PIN_D < ladder.steps[-1].hole_mm + 1e-9
    assert ladder.steps[2].hole_mm == pytest.approx(fc.PIN_D - 0.2)  # step 3 = the table's 0.10 interference per side


def test_filters_pick_ladders_and_renumber() -> None:
    only = fc.ladders(("sliding", "snap"), ("slot",))
    assert [(x.number, x.fit_type) for x in only] == [(1, "sliding"), (2, "snap")]


def test_the_default_coupon_prints_in_about_ten_minutes_and_fits_both_beds() -> None:
    lay = fc.layout(fc.ladders())
    est = fc.estimate(lay.objects)
    assert est["minutes"] <= 10.0, est
    assert 3 <= est["grams"] <= 8
    for printer, bed in BED.items():
        assert fc.fits_bed(lay.size, tuple(bed)), (printer, lay.size, bed)
    assert fc.fits_bed(lay.size, (220.0, 220.0, 250.0)) and not fc.fits_bed(lay.size, (100.0, 60.0))


def test_objects_never_touch_and_stay_inside_the_footprint() -> None:
    lay = fc.layout(fc.ladders(), (10.0, 20.0))
    boxes = [(o.name, o.bbox) for o in lay.objects]
    for i, (_, a) in enumerate(boxes):
        assert a[0] >= 10.0 and a[1] >= 20.0
        assert a[3] <= 10.0 + lay.size[0] + 1e-6 and a[4] <= 20.0 + lay.size[1] + 1e-6
        for _, b in boxes[i + 1 :]:
            overlap = a[0] < b[3] + 1.0 and b[0] < a[3] + 1.0 and a[1] < b[4] + 1.0 and b[1] < a[4] + 1.0
            assert not overlap, (a, b)
    assert lay.size[2] == max(fc.PIN_H, fc.TAB_H, fc.BLOCK_H)


def test_a_coupon_is_centred_on_the_bed() -> None:
    lay = fc.layout(fc.ladders())
    x, y = fc.centred(lay.size, (220.0, 220.0))
    assert x + lay.size[0] / 2 == pytest.approx(110.0) and y + lay.size[1] / 2 == pytest.approx(110.0)


def test_labels_are_raised_one_mm_strokes_on_the_lip() -> None:
    from slicing.calibration_gen import digit_boxes

    w, h, gap, stroke = fc.DIGIT
    assert stroke >= 1.0 and fc.DIGIT_EMBOSS >= 0.4
    boxes = digit_boxes("8", 0.0, 0.0, fc.LIP_H, fc.DIGIT, fc.DIGIT_EMBOSS)
    assert min(b[5] - b[2] for b in boxes) == pytest.approx(fc.DIGIT_EMBOSS)
    assert all(min(b[3] - b[0], b[4] - b[1]) >= 1.0 - 1e-9 for b in boxes)  # every stroke is >= 1 mm wide
    # the digits stand on the lip (z = LIP_H) and fit its depth
    assert max(b[4] for b in boxes) - min(b[1] for b in boxes) <= fc.LIP_D


def test_gap_offset_shifts_every_step_and_refuses_a_negative_clearance() -> None:
    import calibrate_fit as cf
    from errors import UsageError

    shifted = cf.select_ladders(("sliding",), ("round",), 0.1)[0]
    assert shifted.gaps[0] == pytest.approx(0.15) and shifted.steps[0].hole_mm == pytest.approx(4.3)
    with pytest.raises(UsageError, match="below a zero gap"):
        cf.select_ladders(("tight",), ("round",), -0.05)
    assert cf.select_ladders(("press",), ("round",), -0.05)[0].gaps[0] == pytest.approx(-0.2)  # press may go negative


# --------------------------------------------------------------------------- rendered meshes
@needs_openscad
def test_the_rendered_coupon_is_watertight_manifold_and_matches_the_analytic_volume(tmp_path: Path) -> None:
    trimesh = pytest.importorskip("trimesh")
    lay = fc.layout(fc.ladders(), fc.centred(fc.layout(fc.ladders()).size, (220.0, 220.0)))
    paths, meshes = export.export(lay, tmp_path / "coupon.3mf", title="t", metadata={}, doc={"x": 1})
    stl = next(p for p in paths if p.suffix == ".stl")
    mesh = trimesh.load(stl)
    assert mesh.is_watertight and mesh.is_winding_consistent
    assert mesh.volume == pytest.approx(sum(o.volume_mm3 for o in lay.objects), rel=0.02)
    assert len(mesh.split(only_watertight=False)) == 6 + 2 * fc.SPARE_COUNT  # 6 ladders + pins and tabs
    x0, y0, z0 = mesh.bounds[0]
    x1, y1, z1 = mesh.bounds[1]
    assert (x1 - x0, y1 - y0) == pytest.approx(lay.size[:2], abs=0.01) and z0 == 0 and z1 == pytest.approx(fc.PIN_H)
    for name, (vertices, tris) in meshes.items():  # every object alone is closed, too
        m = trimesh.Trimesh(vertices=vertices, faces=tris)
        assert m.is_watertight and m.volume > 0, name


@needs_openscad
def test_the_holes_measure_pin_plus_twice_the_gap() -> None:
    trimesh = pytest.importorskip("trimesh")
    geometry = pytest.importorskip("shapely.geometry")  # mesh sections need shapely
    lay = fc.layout(fc.ladders(("sliding",), ("round",)))
    placed = next(o for o in lay.objects if o.ladder is not None)
    vertices, faces = export.parse_stl(export.render(placed.scad))
    section = trimesh.Trimesh(vertices=vertices, faces=faces).section(
        plane_origin=[0, 0, fc.BLOCK_H - 0.5], plane_normal=[0, 0, 1]  # above the raised digits
    )
    planar, _ = section.to_2D()
    holes = [ring for poly in planar.polygons_full for ring in poly.interiors]
    # area-equivalent diameter of each hole (a 96-gon is within 0.03 % of the circle)
    diameters = sorted(2 * (geometry.Polygon(ring).area / math.pi) ** 0.5 for ring in holes)
    expected = sorted(s.hole_mm for s in placed.ladder.steps)  # type: ignore[union-attr]
    assert diameters == pytest.approx(expected, abs=0.01)



@needs_openscad
def test_the_3mf_is_a_valid_package_with_one_item_per_object_in_bed_coordinates(tmp_path: Path) -> None:
    lay = fc.layout(fc.ladders(("sliding",), ("round",)), (40.0, 50.0))
    paths, meshes = export.export(
        lay, tmp_path / "c.3mf", title="fit coupon", metadata={"Printer": "Creality K1 SE"}, doc={}
    )
    assert sorted(p.suffix for p in paths) == [".3mf", ".json", ".scad", ".stl"]
    with zipfile.ZipFile(next(p for p in paths if p.suffix == ".3mf")) as z:
        assert set(z.namelist()) == {"[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model"}
        root = ElementTree.fromstring(z.read("3D/3dmodel.model"))
    ns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
    assert root.get("unit") == "millimeter"
    objects = root.findall("m:resources/m:object", ns)
    items = root.findall("m:build/m:item", ns)
    assert [o.get("name") for o in objects] == list(meshes) and len(items) == len(objects) == 2
    xs = [float(v.attrib["x"]) for v in root.iter(f"{{{ns['m']}}}vertex")]
    assert min(xs) >= 40.0 - 1e-6  # absolute bed coordinates, not re-centred
    assert {m.get("name"): m.text for m in root.findall("m:metadata", ns)}["Printer"] == "Creality K1 SE"
    for obj in objects:  # every triangle indexes an existing vertex
        n = len(obj.findall("m:mesh/m:vertices/m:vertex", ns))
        assert all(
            0 <= int(t.get(k)) < n  # type: ignore[arg-type]
            for t in obj.findall("m:mesh/m:triangles/m:triangle", ns)
            for k in ("v1", "v2", "v3")
        )


def test_export_refuses_an_unknown_extension(tmp_path: Path) -> None:
    from errors import ThreeDError

    with pytest.raises(ThreeDError, match="written as"):
        export.export(fc.layout(fc.ladders()), tmp_path / "c.obj", title="t", metadata={}, doc={})
