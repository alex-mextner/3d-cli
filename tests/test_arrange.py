"""Tests for `3d arrange`: pure shelf packing + the command's argv parsing/validation.

The shelf packer and the lay-flat / centering helpers are stdlib-only, so they are tested
directly (no trimesh/numpy needed). The mesh I/O and 3MF round-trip are exercised
end-to-end manually (see the handoff); here the command is tested with run_tool stubbed so
the suite stays fast and dependency-free.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from typing import Any

import pytest

from arrange_pack import (
    OversizePart,
    PartBox,
    Plate,
    Placement,
    centering_offset,
    plate_extent,
    shelf_pack,
)
from commands import arrange as arrange_cmd
from errors import InputNotFound, InvalidArgument, UsageError
from orca_project_3mf import (
    MeshPart,
    plate_columns,
    plate_world_origin,
    write_project_3mf,
)

_CORE_NS = {"c": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}

# --- pure shelf packing -------------------------------------------------------


def test_two_small_parts_pack_onto_one_plate() -> None:
    parts = [PartBox("a", 0, 50, 40), PartBox("b", 1, 60, 30)]
    plates = shelf_pack(parts, usable=254, gap=6)
    assert len(plates) == 1
    assert {pl.name for pl in plates[0].placements} == {"a", "b"}
    # Both on the same bottom shelf, side by side, not overlapping.
    by_name = {pl.name: pl for pl in plates[0].placements}
    assert by_name["a"].x + by_name["a"].width + 6 <= by_name["b"].x + 1e-6 or (
        by_name["b"].x + by_name["b"].width + 6 <= by_name["a"].x + 1e-6
    )


def test_three_big_parts_need_multiple_plates() -> None:
    # 200x200 on usable 254: two won't share a row (200+6+200 > 254) nor stack (same), so
    # next-fit shelf packing puts each on its own plate.
    parts = [PartBox(f"big{i}", i, 200, 200) for i in range(3)]
    plates = shelf_pack(parts, usable=254, gap=6)
    assert len(plates) == 3
    for plate in plates:
        assert len(plate.placements) == 1


def test_two_parts_share_a_plate_when_room_allows() -> None:
    # 100x200 each: 100+6+100 = 206 <= 254 -> both fit on one row -> one plate.
    parts = [PartBox("l", 0, 100, 200), PartBox("r", 1, 100, 200)]
    plates = shelf_pack(parts, usable=254, gap=6)
    assert len(plates) == 1
    assert len(plates[0].placements) == 2


def test_oversize_part_raises() -> None:
    parts = [PartBox("huge", 0, 300, 50)]
    with pytest.raises(OversizePart) as exc:
        shelf_pack(parts, usable=254, gap=6)
    assert exc.value.name == "huge"
    assert "exceeds" in str(exc.value)


def test_placements_do_not_overlap() -> None:
    parts = [PartBox(f"p{i}", i, 60, 40) for i in range(8)]
    plates = shelf_pack(parts, usable=254, gap=6)
    for plate in plates:
        boxes = [
            (pl.x, pl.y, pl.x + pl.width, pl.y + pl.depth) for pl in plate.placements
        ]
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                ax0, ay0, ax1, ay1 = boxes[i]
                bx0, by0, bx1, by1 = boxes[j]
                disjoint = ax1 <= bx0 + 1e-9 or bx1 <= ax0 + 1e-9 or ay1 <= by0 + 1e-9 or by1 <= ay0 + 1e-9
                assert disjoint, f"parts {i} and {j} overlap on plate {plate.index}"


def test_centering_offset_centers_the_used_box() -> None:
    plate = Plate(index=1, placements=[Placement("a", 0, 50, 40, 0.0, 0.0)])
    ox, oy = centering_offset(plate, bed=270)
    assert ox == pytest.approx((270 - 50) / 2)
    assert oy == pytest.approx((270 - 40) / 2)


def test_plate_extent_uses_far_corners() -> None:
    plate = Plate(
        index=1,
        placements=[Placement("a", 0, 50, 40, 0.0, 0.0), Placement("b", 1, 30, 60, 56.0, 0.0)],
    )
    w, d = plate_extent(plate)
    assert w == pytest.approx(86.0)
    assert d == pytest.approx(60.0)


# --- command argv parsing / validation ----------------------------------------


def test_cmd_no_args_prints_usage() -> None:
    assert arrange_cmd.run([]) == 1


def test_cmd_help() -> None:
    assert arrange_cmd.run(["--help"]) == 0


def test_cmd_missing_input_file() -> None:
    with pytest.raises(InputNotFound):
        arrange_cmd.run(["nope.3mf"])


def test_cmd_rejects_bad_extension(monkeypatch: Any, tmp_path: Any) -> None:
    bad = tmp_path / "model.scad"
    bad.write_text("cube(1);")
    with pytest.raises(InvalidArgument):
        arrange_cmd.run([str(bad)])


def test_cmd_rejects_unknown_option() -> None:
    with pytest.raises(UsageError):
        arrange_cmd.run(["x.3mf", "--bogus"])


def test_cmd_rejects_nonpositive_bed() -> None:
    with pytest.raises(InvalidArgument):
        arrange_cmd.run(["x.3mf", "--bed", "0"])


def test_cmd_rejects_margin_swallowing_bed() -> None:
    with pytest.raises(UsageError):
        arrange_cmd.run(["x.3mf", "--margin", "200", "--bed", "270"])


def test_cmd_rejects_mixed_3mf_and_stl(tmp_path: Any) -> None:
    a = tmp_path / "a.3mf"
    a.write_text("")
    b = tmp_path / "b.stl"
    b.write_text("")
    with pytest.raises(UsageError):
        arrange_cmd.run([str(a), str(b)])


def test_cmd_forwards_to_tool(monkeypatch: Any, tmp_path: Any) -> None:
    src = tmp_path / "asm.3mf"
    src.write_text("")
    seen: dict[str, Any] = {}

    def fake_run_tool(deps: str, script: str, args: list[str]) -> int:
        seen["deps"] = deps
        seen["script"] = script
        seen["args"] = args
        return 0

    monkeypatch.setattr("commands.arrange.run_tool", fake_run_tool)
    rc = arrange_cmd.run([str(src), "--bed", "220", "--gap", "5", "--margin", "10", "-o", "out/tray"])
    assert rc == 0
    assert seen["script"] == "arrange_pack.py"
    assert "trimesh" in seen["deps"] and "networkx" in seen["deps"]
    args = seen["args"]
    assert str(src) in args
    assert args[args.index("--bed") + 1] == repr(220.0)
    assert args[args.index("--gap") + 1] == repr(5.0)
    assert args[args.index("--margin") + 1] == repr(10.0)
    assert args[args.index("-o") + 1] == "out/tray"
    # Single mode is the default and is always forwarded explicitly.
    assert "--single" in args and "--per-plate" not in args


def test_cmd_forwards_per_plate(monkeypatch: Any, tmp_path: Any) -> None:
    src = tmp_path / "asm.3mf"
    src.write_text("")
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        "commands.arrange.run_tool",
        lambda d, s, a: seen.update(args=a) or 0,
    )
    assert arrange_cmd.run([str(src), "--per-plate"]) == 0
    assert "--per-plate" in seen["args"] and "--single" not in seen["args"]


def test_cmd_passes_through_tool_exit_code(monkeypatch: Any, tmp_path: Any) -> None:
    src = tmp_path / "asm.3mf"
    src.write_text("")
    # Oversize -> tool exits 1; the command must propagate that.
    monkeypatch.setattr("commands.arrange.run_tool", lambda d, s, a: 1)
    assert arrange_cmd.run([str(src)]) == 1


# --- single multi-plate Orca project 3MF writer (stdlib only) -----------------


def test_plate_columns_matches_orca_ceil_sqrt() -> None:
    # compute_colum_count in PartPlate.hpp: round(sqrt(n)), bumped by 1 if sqrt > round.
    assert plate_columns(1) == 1
    assert plate_columns(2) == 2  # sqrt=1.41 -> round 1 -> bump to 2
    assert plate_columns(3) == 2
    assert plate_columns(4) == 2
    assert plate_columns(5) == 3
    assert plate_columns(9) == 3
    assert plate_columns(10) == 4


def test_plate_world_origin_grid_stride() -> None:
    # stride = bed * (1 + 1/5). cols=2 for 4 plates: p0=(0,0), p1=(+stride,0),
    # p2=(0,-stride), p3=(+stride,-stride).
    bed = 270.0
    stride = bed * 1.2
    assert plate_world_origin(0, bed=bed, cols=2) == (0.0, 0.0)
    assert plate_world_origin(1, bed=bed, cols=2) == pytest.approx((stride, 0.0))
    assert plate_world_origin(2, bed=bed, cols=2) == pytest.approx((0.0, -stride))
    assert plate_world_origin(3, bed=bed, cols=2) == pytest.approx((stride, -stride))


def _tetra() -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    verts = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0), (0.0, 0.0, 10.0)]
    tris = [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)]
    return verts, tris


def test_write_project_3mf_valid_zip_with_required_entries(tmp_path: Any) -> None:
    verts, tris = _tetra()
    parts = [
        MeshPart("alpha", verts, tris, 0),
        MeshPart("beta", verts, tris, 1),
        MeshPart("gamma", verts, tris, 1),
    ]
    out = str(tmp_path / "proj.3mf")
    num_plates = write_project_3mf(out, parts, bed=270.0)
    assert num_plates == 2

    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert zf.testzip() is None  # no CRC errors -> valid zip
        # [Content_Types].xml must be the first OPC entry.
        assert names[0] == "[Content_Types].xml"
        assert {
            "[Content_Types].xml",
            "_rels/.rels",
            "3D/3dmodel.model",
            "3D/_rels/3dmodel.model.rels",
            "Metadata/model_settings.config",
        } <= set(names)


def test_write_project_3mf_geometry_and_build_items_round_trip(tmp_path: Any) -> None:
    verts, tris = _tetra()
    parts = [MeshPart("a", verts, tris, 0), MeshPart("b", verts, tris, 1)]
    out = str(tmp_path / "proj.3mf")
    write_project_3mf(out, parts, bed=270.0)

    with zipfile.ZipFile(out) as zf:
        model = ET.fromstring(zf.read("3D/3dmodel.model"))
    objects = model.findall(".//c:object", _CORE_NS)
    items = model.findall(".//c:item", _CORE_NS)
    assert len(objects) == 2 and len(items) == 2
    for obj in objects:
        assert obj.get("type") == "model"
        assert len(obj.findall(".//c:vertex", _CORE_NS)) == 4
        assert len(obj.findall(".//c:triangle", _CORE_NS)) == 4
    # Build items reference objects 1..N and carry a 12-number transform; plate 1's item
    # is offset by one stride in +X (column-major translation = last 3 numbers).
    stride = 270.0 * 1.2
    tails = {}
    for it in items:
        nums = [float(x) for x in (it.get("transform") or "").split()]
        assert len(nums) == 12
        assert it.get("printable") == "1"
        tails[it.get("objectid")] = nums[-3:]
    assert tails["1"] == pytest.approx([0.0, 0.0, 0.0])
    assert tails["2"] == pytest.approx([stride, 0.0, 0.0])


def test_write_project_3mf_plate_assignment(tmp_path: Any) -> None:
    verts, tris = _tetra()
    parts = [
        MeshPart("p0a", verts, tris, 0),
        MeshPart("p1a", verts, tris, 1),
        MeshPart("p1b", verts, tris, 1),
        MeshPart("p2a", verts, tris, 2),
    ]
    out = str(tmp_path / "proj.3mf")
    num_plates = write_project_3mf(out, parts, bed=200.0)
    assert num_plates == 3

    with zipfile.ZipFile(out) as zf:
        settings = ET.fromstring(zf.read("Metadata/model_settings.config"))
    plates = settings.findall("plate")
    assert len(plates) == 3
    assignment: dict[int, list[str]] = {}
    for plate in plates:
        plater_id = next(
            m.get("value") for m in plate.findall("metadata") if m.get("key") == "plater_id"
        )
        objs: list[str] = []
        for inst in plate.findall("model_instance"):
            md = inst.find("metadata")
            assert md is not None
            value = md.get("value")
            assert value is not None
            objs.append(value)
        assignment[int(plater_id or "0")] = objs
    # plater_id is 1-indexed; objects 1,(2,3),4 land on plates 1,2,3.
    assert assignment == {1: ["1"], 2: ["2", "3"], 3: ["4"]}
    # Every object has a <object id=> name entry too.
    obj_ids = {o.get("id") for o in settings.findall("object")}
    assert obj_ids == {"1", "2", "3", "4"}


def test_write_project_3mf_xml_escapes_part_names(tmp_path: Any) -> None:
    verts, tris = _tetra()
    parts = [MeshPart('a & b <"x">', verts, tris, 0)]
    out = str(tmp_path / "proj.3mf")
    write_project_3mf(out, parts, bed=270.0)
    # Must still parse (escaping correct), and the raw text must not contain a bare & .
    with zipfile.ZipFile(out) as zf:
        raw = zf.read("Metadata/model_settings.config").decode("utf-8")
        settings = ET.fromstring(raw)
    metadata = settings.find("object/metadata")
    assert metadata is not None
    assert metadata.get("value") == 'a & b <"x">'


def test_write_project_3mf_rejects_empty(tmp_path: Any) -> None:
    with pytest.raises(ValueError):
        write_project_3mf(str(tmp_path / "x.3mf"), [], bed=270.0)
