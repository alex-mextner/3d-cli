"""Tests for `3d arrange`: pure shelf packing + the command's argv parsing/validation.

The shelf packer and the lay-flat / centering helpers are stdlib-only, so they are tested
directly (no trimesh/numpy needed). The mesh I/O and 3MF round-trip are exercised
end-to-end manually (see the handoff); here the command is tested with run_tool stubbed so
the suite stays fast and dependency-free.
"""
from __future__ import annotations

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


def test_cmd_passes_through_tool_exit_code(monkeypatch: Any, tmp_path: Any) -> None:
    src = tmp_path / "asm.3mf"
    src.write_text("")
    # Oversize -> tool exits 1; the command must propagate that.
    monkeypatch.setattr("commands.arrange.run_tool", lambda d, s, a: 1)
    assert arrange_cmd.run([str(src)]) == 1
