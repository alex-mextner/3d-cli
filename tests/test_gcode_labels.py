"""Object labels in both G-code dialects (gcode_labels) and the gates that read them: head
sequence, overhangs, visible faces and the print-area plate section. The Bambu cases run on
tests/data/bambu_a1mini_path.gcode, an excerpt of a real Bambu Studio A1 mini plate (issue
https://github.com/alex-mextner/3d-cli/issues/184): 5 objects, a raft, a tool change through
the waste chute, a timelapse block inside an object, the end G-code."""

from __future__ import annotations

from pathlib import Path

import pytest

import gcode_overhangs as go
import visible_faces as vf
from gcode_labels import ObjectLabels, feature, line_width, undecorated
from head_sequence_fixtures import project_3mf
from printer_ops.move_bounds import check_bounds
from slicing.head_sequence import check, heads_by_layer

BAMBU = Path(__file__).parent / "data" / "bambu_a1mini_path.gcode"
NAMES = {
    "PTA — PTA id:72340172838076673 copy 0",
    "PTB — PTB id:23 copy 0",
    "SW — SW id:23 copy 0",
    "SWF — SWF id:23 copy 0",
    "SWK — SWK id:23 copy 0",
}
# the real project's part extruders (a1_path.3mf model_settings.config)
PROJECT: dict[str, int | list[int]] = {
    "PTA — PTA": [1, 2],
    "PTB — PTB": [1, 2],
    "SW — SW": [4, 1, 2, 3],
    "SWF — SWF": 1,
    "SWK — SWK": 1,
}


def _trace(lines: list[str]) -> list[str | None]:
    labels = ObjectLabels()
    out = []
    for line in lines:
        labels.feed(line)
        out.append(labels.current)
    return out


def test_klipper_labels_win_over_the_comments_the_same_slicer_writes() -> None:
    labels = ObjectLabels()
    for line in [
        "EXCLUDE_OBJECT_DEFINE NAME=A_id_0_copy_0 CENTER=1,1",
        "; printing object A id:0 copy 0",
    ]:
        labels.feed(line)
    # a DEFINE opens no block: until the first START the file still reads as one plate
    assert (
        labels.defined == ["A_id_0_copy_0"]
        and labels.current is None
        and not labels.labeled
    )
    assert _trace(
        [
            "EXCLUDE_OBJECT_START NAME=A_id_0_copy_0",
            "; stop printing object A id:0 copy 0",
            "G1 X1 Y1 E1",
            "EXCLUDE_OBJECT_END NAME=A_id_0_copy_0",
        ]
    ) == ["A_id_0_copy_0", "A_id_0_copy_0", "A_id_0_copy_0", None]


def test_bambu_block_is_named_by_the_comment_before_it_and_spans_the_layer_change() -> (
    None
):
    lines = [
        "; model label id: 8,12",
        "; printing object DR — DR id:0 copy 0",  # before the block: a name, not an object yet
        "; start printing object, unique label id: 8",
        "M624 AQAAAAAAAAA=",
        "; stop printing object DR — DR id:0 copy 0",  # inner comment: the block goes on
        "; CHANGE_LAYER",
        "; printing object WIN — WIN id:1 copy 0",
        "; stop printing object, unique label id: 8",
        "M625",
        "; start printing object, unique label id: 12",
        "; stop printing object, unique label id: 12",
        "; start printing object, unique label id: 8",  # no comment this time: the known name
    ]
    dr, win = "DR — DR id:0 copy 0", "WIN — WIN id:1 copy 0"
    assert _trace(lines) == [None, None, dr, dr, dr, dr, dr, None, None, win, None, dr]


def test_bambu_m624_mask_alone_names_the_header_label() -> None:
    lines = [
        "; model label id: 8,12,16",
        "M624 BAAAAAAAAAA=",
        "G1 X1 Y1 E1",
        "M625",
        "M624 bogus",
        "M624 AwAAAAAAAAA=",
    ]
    assert _trace(lines) == [None, "object 16", "object 16", None, None, None]


def test_prusa_comments_and_m486() -> None:
    assert _trace(
        [
            "; printing object A",
            "G1 X1",
            "; stop printing object A",
            "M486 S2",
            "M486 T3",
            "M486 S-1",
        ]
    ) == ["A", "A", None, "object 2", "object 2", None]
    assert _trace(["G1 X1 Y1 E1"]) == [None]


def test_tags_widths_and_undecorated_names() -> None:
    assert (
        feature(";TYPE:Support") == "Support"
        and feature("; FEATURE: Prime tower") == "Prime tower"
    )
    assert feature("; LINE_WIDTH: 0.5") is None
    assert (
        line_width("; LINE_WIDTH: 0.5") == "0.5" and line_width(";WIDTH:0.45") == "0.45"
    )
    assert undecorated("PTA — PTA id:72340172838076673 copy 0") == "PTA — PTA"
    assert undecorated("PTA_—_PTA_id_0_copy_0") == "PTA_—_PTA"


def test_head_sequence_attributes_a_bambu_plate_per_object(tmp_path: Path) -> None:
    with BAMBU.open(encoding="utf-8") as fh:
        seen = heads_by_layer(fh)
    assert set(seen) == NAMES
    # the excerpt's third layer is SWF's raft, printed by the support filament on head 3
    assert seen["SWF — SWF id:23 copy 0"][2] == {3}
    ok = check(
        str(project_3mf(tmp_path / "p.3mf", PROJECT, mixed="", raft_layers=4)),
        str(BAMBU),
    )
    assert {o.name: o.status for o in ok.objects} == {
        undecorated(n): "ok" for n in NAMES
    }
    # without the raft skip the head-3 raft layer is off SWF's recipe: the per-object check bites
    raw = check(str(project_3mf(tmp_path / "r.3mf", PROJECT, mixed="")), str(BAMBU))
    assert {o.name for o in raw.objects if o.status == "FAIL"} >= {"SWF — SWF"}


def test_overhangs_reads_bambu_objects_raft_and_widths() -> None:
    plate = go.parse(str(BAMBU))
    assert (
        set(plate.objects) == NAMES
    )  # no unlabeled "plate": the prime tower is not an object
    swf = plate.objects["SWF — SWF id:23 copy 0"]
    assert (
        swf.zs(go.SUPPORT)[0] == 0.2 and swf.zs(go.OWN)[0] == 1.34
    )  # `; FEATURE: Support` is the raft
    widths = {
        round(float(w), 3)
        for o in plate.objects.values()
        for a in o.lines.values()
        for w in a[4::5]
    }
    assert 0.5 in widths and 0.42 in widths  # `; LINE_WIDTH:`, not the 0.45 default
    assert go.run_check(str(BAMBU), png=None)["status"] != "FAIL"


def test_visible_faces_names_bambu_objects() -> None:
    _, seen = vf.parse_gcode(str(BAMBU), vf.build_specs(["SW:all"], [], []))
    assert set(seen) == NAMES
    assert vf.object_names("SW — SW id:23 copy 0") == [
        "SW — SW id:23 copy 0",
        "SW",
        "SW — SW",
    ]


@pytest.mark.parametrize(
    "tag", [";TYPE:Support", "; TYPE:Support", ";FEATURE:Support", "; FEATURE: Support"]
)
def test_every_feature_tag_spelling_marks_support(tag: str, tmp_path: Path) -> None:
    """Slicers differ in the space after `;`; visible faces must still see the support."""
    assert feature(tag) == "Support"
    path = tmp_path / "s.gcode"
    path.write_text(
        f";Z:0.2\n;TYPE:Outer wall\nG1 X1 Y1 E1\n{tag}\nG1 X2 Y1 E1\n", encoding="utf-8"
    )
    objs, _ = vf.parse_gcode(str(path), vf.build_specs(["plate:all"], [], []))
    assert len(objs["plate"].support) == 1


def test_print_area_skips_bambu_vendor_moves_but_not_the_plate(tmp_path: Path) -> None:
    rep = check_bounds(BAMBU)
    assert (
        rep.all_extent is not None and rep.all_extent[0] == -13.5
    )  # the waste chute trips are there
    assert (
        rep.area_out_count == 0
        and rep.plate_extent is not None
        and rep.plate_extent[0] > 0
    )

    bad = tmp_path / "bad.gcode"
    bad.write_text(
        "; model label id: 8\n; printable_area = 0x0,180x0,180x180,0x180\nG90\nM83\n"
        "; CHANGE_LAYER\n; FEATURE: Prime tower\nG1 X-2 Y10\nG1 X5 Y10 E1\n"  # tower: plate
        "M622 J1\nG1 X-13 F3000\nM623\n"  # timelapse: vendor
        "; printing object A id:0 copy 0\n; start printing object, unique label id: 8\nM624 AQAAAAAAAAA=\n"
        "; FEATURE: Outer wall\nG1 X10 Y10\nG1 X-4 Y10 E1\n"  # an object leaving the bed: plate
        "; stop printing object, unique label id: 8\nM625\nG1 X-13 Y10\n"  # after the object: vendor
        "; FEATURE: Custom\nG1 X-13 Y180\n",
        encoding="utf-8",
    )
    rep = check_bounds(bad)
    # the tower travel from X-2 and the extrusion leaving it (both plate), the object's last line
    assert sorted(v.line for v in rep.area_out) == [7, 8, 17]


def test_overhangs_without_an_object_block_still_check_the_plate(
    tmp_path: Path,
) -> None:
    """A DEFINE or a cancel without any START block must not leave every line ownerless."""
    path = tmp_path / "p.gcode"
    path.write_text(
        "EXCLUDE_OBJECT_DEFINE NAME=A\nEXCLUDE_OBJECT NAME=A\n;LAYER_CHANGE\n;Z:0.2\n"
        "G90\nM83\nG1 X10 Y10\nG1 X20 Y10 E1\n",
        encoding="utf-8",
    )
    assert set(go.parse(str(path)).objects) == {"plate"}


@pytest.mark.parametrize("opener", ["M622 J1", "; CP TOOLCHANGE START"])
def test_print_area_does_not_trust_an_unclosed_vendor_block(
    opener: str, tmp_path: Path
) -> None:
    """A truncated M622 / tool change block ends at the next object label: the object's moves
    are checked again and the open block is reported unverified."""
    path = tmp_path / "open.gcode"
    path.write_text(
        "; model label id: 8\n; printable_area = 0x0,180x0,180x180,0x180\nG90\nM83\n"
        f"; CHANGE_LAYER\n{opener}\nG1 X-13 F3000\n"  # line 6: never closed
        "; printing object A id:0 copy 0\n; start printing object, unique label id: 8\nM624 AQAAAAAAAAA=\n"
        "; FEATURE: Outer wall\nG1 X10 Y10\nG1 X-4 Y10 E1\n"  # line 13: off the bed
        "; stop printing object, unique label id: 8\nM625\n",
        encoding="utf-8",
    )
    rep = check_bounds(path)
    assert [v.line for v in rep.area_out] == [13]
    assert rep.unverified_count == 1 and "left open" in rep.unverified[0]
