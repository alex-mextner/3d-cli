"""lib/slicing/project_3mf.py on synthetic Orca-style projects (tests/threemf_fixtures.py)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import numpy as np
import pytest
from threemf_fixtures import Obj, Spec, three_parts, tile, write

from slicing.project_3mf import (
    Bed,
    Project,
    ProjectError,
    SelectorError,
    arrange,
    check,
    largest_flat_face,
    matches,
    rotation_down,
    rotation_xyz,
)

RANGES = (
    '<?xml version="1.0" encoding="utf-8"?>\n<objects>\n'
    ' <object id="1">\n  <range min_z="0" max_z="2">\n   <option opt_key="layer_height">0.1</option>\n'
    "  </range>\n </object>\n"
    ' <object id="3">\n  <range min_z="0" max_z="6">\n   <option opt_key="layer_height">0.24</option>\n'
    "  </range>\n </object>\n</objects>\n"
)
HEIGHTS = "object_id=2|0.0|0.2|10.0|0.1\nobject_id=3|0.0|0.2\n"


def _load(path: Path) -> Project:
    return Project(path.read_bytes(), label=path.name)


def _reload(p: Project) -> Project:
    return Project(p.to_bytes(), label=p.label)


def _size(p: Project, name: str) -> np.ndarray:
    item = next(it for it in p.items() if it.name == name)
    m = p.mesh(item)
    return np.round(m.hi - m.lo, 3)


def test_items_read_names_extruders_plates_and_component_meshes(tmp_path: Path) -> None:
    p = _load(three_parts(tmp_path / "p.3mf"))
    items = p.items()
    assert [
        (it.name, it.object_id, it.index, it.plate, it.extruder) for it in items
    ] == [
        ("CN — CN", "1000", 1, 1, "1"),
        ("SW — SW", "1001", 2, 1, "2"),
        ("ROOF", "1002", 3, 2, "3"),
    ]
    assert list(_size(p, "CN — CN")) == [20, 10, 3]
    assert list(_size(p, "ROOF")) == [
        30,
        30,
        12,
    ]  # two components, from the object file
    cn = p.mesh(items[0])
    assert list(np.round(cn.lo, 3)) == [30, 35, 0]  # build item translation applied
    assert p.printer_model() == "Snapmaker U1"
    bed = p.bed()
    assert bed is not None and (bed.width, bed.depth, bed.height) == (270, 270, 270)


@pytest.mark.parametrize(
    ("selector", "hit"),
    [
        ("CN", True),
        ("CN — CN", True),
        ("C*", True),
        ("1000", True),
        ("cn", False),
        ("CN —", False),
        ("C", False),
    ],
)
def test_selector_matches_id_whole_name_or_first_word_with_globs(
    selector: str, hit: bool
) -> None:
    assert matches("CN — CN", "1000", selector) is hit


def test_pick_keeps_the_matched_objects_and_their_settings_only(tmp_path: Path) -> None:
    src = three_parts(
        tmp_path / "p.3mf",
        extra={
            "Metadata/layer_config_ranges.xml": RANGES,
            "Metadata/layer_heights_profile.txt": HEIGHTS,
        },
    )
    p = _load(src)
    kept = p.pick(["ROOF", "CN"])
    out = _reload(p)
    names = zipfile.ZipFile(src).namelist()
    with zipfile.ZipFile(io.BytesIO(p.to_bytes())) as zf:
        members = zf.namelist()
        model = zf.read("3D/3dmodel.model").decode()
        settings = zf.read("Metadata/model_settings.config").decode()
        rels = zf.read("3D/_rels/3dmodel.model.rels").decode()
        ranges = zf.read("Metadata/layer_config_ranges.xml").decode()
        heights = zf.read("Metadata/layer_heights_profile.txt").decode()
        assert zf.read("Metadata/project_settings.config") == zipfile.ZipFile(src).read(
            "Metadata/project_settings.config"
        )
    assert [it.name for it in kept] == ["CN — CN", "ROOF"]
    assert [(it.name, it.index) for it in out.items()] == [("CN — CN", 1), ("ROOF", 2)]
    assert (
        "3D/Objects/object_2.model" in names
        and "3D/Objects/object_2.model" not in members
    )
    assert 'objectid="1001"' not in model and 'id="1001"' not in model
    assert "object_2.model" not in rels and "object_3.model" in rels
    assert 'value="SW — SW"' not in settings and 'value="1001"' not in settings
    assert 'value="ROOF"' in settings and 'key="extruder" value="3"' in settings
    # Orca keys these files by object index: ROOF was 3, is now 2; CN (1) stays 1; SW's entry goes
    assert (
        '<object id="1">' in ranges
        and '<object id="2">' in ranges
        and '<object id="3">' not in ranges
    )
    assert "0.24" in ranges and heights == "object_id=2|0.0|0.2\n"
    assert list(_size(out, "ROOF")) == [30, 30, 12]


def test_pick_names_every_object_when_a_selector_matches_nothing(
    tmp_path: Path,
) -> None:
    p = _load(three_parts(tmp_path / "p.3mf"))
    with pytest.raises(SelectorError) as err:
        p.pick(["CN", "XX*"])
    assert err.value.selector == "XX*" and err.value.names == [
        "CN — CN",
        "ROOF",
        "SW — SW",
    ]


@pytest.mark.parametrize(
    ("face", "size"),
    [
        ("front", [20, 3, 10]),
        ("back", [20, 3, 10]),
        ("left", [3, 10, 20]),
        ("right", [3, 10, 20]),
        ("top", [20, 10, 3]),
        ("bottom", [20, 10, 3]),
    ],
)
def test_orient_puts_the_named_face_down_and_drops_onto_the_bed(
    tmp_path: Path, face: str, size: list[int]
) -> None:
    from slicing.project_3mf import FACES

    p = _load(three_parts(tmp_path / "p.3mf"))
    cn = p.items()[0]
    before = p.mesh(cn)
    centre = (before.lo[:2] + before.hi[:2]) / 2
    # the vertex that was furthest along the face's direction ends up lowest
    d = np.array(FACES[face])
    far = before.vertices[np.argmax(before.vertices @ d)]
    after = p.orient(cn, rotation_down(FACES[face]))
    out = _reload(p)
    m = out.mesh(out.items()[0])
    assert list(np.round(m.hi - m.lo, 3)) == size
    assert round(float(m.lo[2]), 6) == 0
    assert np.allclose((m.lo[:2] + m.hi[:2]) / 2, centre)
    k = int(np.argmin(np.linalg.norm(before.vertices - far, axis=1)))
    assert after.vertices[k][2] == pytest.approx(0, abs=1e-6)


def test_orient_largest_lays_a_standing_tile_flat_and_rotate_turns_about_z(
    tmp_path: Path,
) -> None:
    path = write(
        tmp_path / "p.3mf", Spec([Obj("T", [tile(20, 3, 10)], at=(80, 80, 0))])
    )
    p = _load(path)
    m = p.mesh(p.items()[0])
    normal = largest_flat_face(m)
    assert abs(abs(normal[1]) - 1) < 1e-9  # the 20 x 10 faces point along Y
    p.orient(p.items()[0], rotation_down(normal))
    assert list(_size(p, "T")) == [20, 10, 3]
    p.orient(p.items()[0], rotation_xyz(0, 0, 90))
    assert list(_size(_reload(p), "T")) == [10, 20, 3]


def test_orient_refuses_objects_with_settings_in_their_own_frame(
    tmp_path: Path,
) -> None:
    p = _load(
        three_parts(
            tmp_path / "p.3mf", extra={"Metadata/layer_config_ranges.xml": RANGES}
        )
    )
    items = p.items()
    assert p.orient_blockers([items[1]]) == []
    assert p.orient_blockers(items) == [
        "CN — CN has height range modifiers (Metadata/layer_config_ranges.xml)",
        "ROOF has height range modifiers (Metadata/layer_config_ranges.xml)",
    ]


def test_arrange_puts_every_object_on_plate_one_inside_the_margin(
    tmp_path: Path,
) -> None:
    p = _load(three_parts(tmp_path / "p.3mf"))
    placed = arrange(p, Bed(220, 220, 250, "test"), gap=5, margin=10)
    out = _reload(p)
    boxes = [(out.mesh(it).lo, out.mesh(it).hi) for it in out.items()]
    assert sorted(pl.name for pl in placed) == ["CN — CN", "ROOF", "SW — SW"]
    assert {it.plate for it in out.items()} == {1}
    assert (
        "Metadata/plate_2.json"
        not in zipfile.ZipFile(io.BytesIO(p.to_bytes())).namelist()
    )
    settings = out.text("Metadata/model_settings.config")
    assert settings.count("<plate>") == 1 and settings.count("<model_instance>") == 3
    for lo, hi in boxes:
        assert (
            lo[0] >= 10 - 1e-6
            and lo[1] >= 10 - 1e-6
            and hi[0] <= 210 + 1e-6
            and hi[1] <= 210 + 1e-6
        )
        assert lo[2] == pytest.approx(0, abs=1e-6)
    for i, (alo, ahi) in enumerate(boxes):
        for blo, bhi in boxes[i + 1 :]:
            apart = (
                ahi[0] + 5 <= blo[0] + 1e-6
                or bhi[0] + 5 <= alo[0] + 1e-6
                or ahi[1] + 5 <= blo[1] + 1e-6
                or bhi[1] + 5 <= alo[1] + 1e-6
            )
            assert apart
    assert check(out, Bed(220, 220, 250, "test"), printer_model=None).overall != "FAIL"


def test_arrange_names_the_parts_that_do_not_fit_one_plate(tmp_path: Path) -> None:
    p = _load(three_parts(tmp_path / "p.3mf"))
    with pytest.raises(ProjectError, match="do not fit on one: "):
        arrange(p, Bed(40, 40, None, "test"), gap=5, margin=2)
    with pytest.raises(ProjectError, match="exceeds the usable plate"):
        arrange(p, Bed(25, 25, None, "test"), gap=1, margin=2)


def test_check_reports_printer_bed_height_floor_and_overhang(tmp_path: Path) -> None:
    spec = Spec(
        [
            Obj("OFF", [tile(20, 10, 3)], at=(265, 40, 0)),
            Obj("SUNK", [tile(10, 10, 10)], at=(100, 100, -2)),
            Obj("TALL", [tile(10, 10, 280)], at=(150, 150, 0)),
            Obj(
                "ROOF",
                [tile(4, 4, 10), (-15.0, -15.0, 10.0, 15.0, 15.0, 12.0)],
                at=(60, 160, 0),
            ),
        ]
    )
    p = _load(write(tmp_path / "p.3mf", spec))
    report = check(p, p.bed(), printer_model="Creality K1 SE")
    lines = {(ln.gate, ln.object): ln for ln in report.lines}
    assert report.overall == "FAIL"
    assert (
        lines[("printer", None)].status == "FAIL"
        and "Snapmaker U1" in lines[("printer", None)].detail
    )
    assert (
        lines[("bed fit", "OFF")].status == "FAIL"
        and "off the bed" in lines[("bed fit", "OFF")].detail
    )
    assert lines[("on bed", "SUNK")].status == "FAIL"
    assert lines[("height", "TALL")].status == "FAIL"
    assert (
        lines[("overhang", "ROOF")].status == "WARN"
        and "900 mm²" in lines[("overhang", "ROOF")].detail
    )
    assert lines[("overhang", "OFF")].status == "PASS"  # its bottom rests on the bed
    assert check(p, None, printer_model=None).lines[1].status == "SKIP"


def test_check_reads_objects_on_other_plates_relative_to_their_plate(
    tmp_path: Path,
) -> None:
    p = _load(three_parts(tmp_path / "p.3mf"))
    report = check(p, p.bed(), printer_model="Snapmaker U1")
    assert report.overall == "WARN"  # the two-plate note and ROOF's overhang
    fit = [ln for ln in report.lines if ln.gate == "bed fit"]
    assert [ln.status for ln in fit] == ["PASS", "PASS", "PASS"]
    assert any(ln.gate == "plates" and ln.status == "WARN" for ln in report.lines)


def test_a_file_that_is_not_a_3mf_is_a_project_error(tmp_path: Path) -> None:
    with pytest.raises(ProjectError, match="not a 3MF"):
        Project(b"G28\n", label="x")
    (tmp_path / "e.zip").write_bytes(b"")
    with zipfile.ZipFile(tmp_path / "e.zip", "w") as zf:
        zf.writestr("a.txt", "x")
    with pytest.raises(ProjectError, match="no 3D/3dmodel.model"):
        _load(tmp_path / "e.zip")
