"""`gcode_walls`: the outer-wall lines of ONE object of a sliced G-code, read from the slicer's own labels.

Each test writes a tiny G-code whose answer is readable off the text; the teeth tests (wrong object, copies, other feature
types, absolute / relative extrusion, G91, G92, arcs, mid-layer heights) fail if the reader attributes a line to the
wrong object, feature or height.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from gcode_walls import read_walls

DATA = Path(__file__).parent / "data"

TWO_OBJECTS = """\
; outer_wall_line_width = 0.45
EXCLUDE_OBJECT_DEFINE NAME=Pot_Pepper.stl_id_0_copy_0 CENTER=10,10
EXCLUDE_OBJECT_DEFINE NAME=Plug_x2.stl_id_1_copy_0 CENTER=50,10
M83
G1 X0 Y0 F6000
;LAYER_CHANGE
;Z:0.2
;HEIGHT:0.2
EXCLUDE_OBJECT_START NAME=Pot_Pepper.stl_id_0_copy_0
;TYPE:Outer wall
;WIDTH:0.42
G1 X10 Y10 F3000
G1 X20 Y10 E0.5
G1 X20 Y20 E0.5
;TYPE:Inner wall
;WIDTH:0.45
G1 X15 Y15 E0.5
EXCLUDE_OBJECT_END NAME=Pot_Pepper.stl_id_0_copy_0
EXCLUDE_OBJECT_START NAME=Plug_x2.stl_id_1_copy_0
;TYPE:Outer wall
;WIDTH:0.40
G1 X50 Y10 F3000
G1 X60 Y10 E0.5
EXCLUDE_OBJECT_END NAME=Plug_x2.stl_id_1_copy_0
;LAYER_CHANGE
;Z:0.4
;HEIGHT:0.2
EXCLUDE_OBJECT_START NAME=Pot_Pepper.stl_id_0_copy_0
;TYPE:Outer wall
;WIDTH:0.42
G1 X10 Y10 F3000
G1 X20 Y10 E0.5
G1 X20 Y10 E0.5
G1 X30 Y10 E0
EXCLUDE_OBJECT_END NAME=Pot_Pepper.stl_id_0_copy_0
"""


def write(tmp: Path, text: str, name: str = "t.gcode") -> Path:
    p = tmp / name
    p.write_text(text)
    return p


def layer_gcode(body: str, head: str = ";Z:0.2\n;HEIGHT:0.2\n") -> str:
    return f";LAYER_CHANGE\n{head};TYPE:Outer wall\n;WIDTH:0.4\n{body}"


@pytest.fixture
def gcode(tmp_path: Path) -> Path:
    return write(tmp_path, TWO_OBJECTS)


def test_only_the_named_objects_outer_wall_is_returned(gcode: Path) -> None:
    walls = read_walls(
        gcode, "Pot Pepper.stl"
    )  # project name with a space matches the EXCLUDE_OBJECT name with `_`
    assert [lyr.z for lyr in walls.layers] == [0.2, 0.4]
    first = walls.layers[0]
    # two outer-wall pieces (10,10 -> 20,10 and 20,10 -> 20,20); the inner wall and the other object are not here
    assert first.a.tolist() == [[10.0, 10.0], [20.0, 10.0]]
    assert first.b.tolist() == [[20.0, 10.0], [20.0, 20.0]]
    assert first.width.tolist() == [0.42, 0.42]
    assert first.mid_z == pytest.approx(0.1)
    assert walls.objects == ["Pot_Pepper.stl_id_0_copy_0", "Plug_x2.stl_id_1_copy_0"]


def test_the_other_object_has_its_own_width_and_pieces(gcode: Path) -> None:
    walls = read_walls(gcode, "plug x2.stl")
    assert len(walls.layers) == 1  # it prints in the first layer only
    assert walls.layers[0].a.tolist() == [[50.0, 10.0]]
    assert walls.layers[0].width.tolist() == [0.40]


def test_a_non_extruding_or_zero_length_move_is_not_a_wall(gcode: Path) -> None:
    second = read_walls(gcode, "Pot_Pepper.stl").layers[1]
    # `E0` travel to X30 and the zero-length E move add nothing
    assert second.a.tolist() == [[10.0, 10.0]]
    assert second.b.tolist() == [[20.0, 10.0]]


def test_an_unknown_object_names_the_ones_the_file_has(gcode: Path) -> None:
    with pytest.raises(
        KeyError, match=r"Pot_Pepper\.stl_id_0_copy_0, Plug_x2\.stl_id_1_copy_0"
    ):
        read_walls(gcode, "Nothing.stl")


def test_a_plate_with_several_objects_needs_a_name(gcode: Path) -> None:
    with pytest.raises(ValueError, match="several objects"):
        read_walls(gcode)


def test_other_feature_kinds_can_be_asked_for(gcode: Path) -> None:
    walls = read_walls(gcode, "Pot Pepper.stl", kinds=("Inner wall",))
    assert walls.layers[0].a.tolist() == [[20.0, 20.0]]
    assert walls.layers[0].width.tolist() == [0.45]


def test_two_copies_of_one_object_are_not_merged(tmp_path: Path) -> None:
    text = "M83\n" + "".join(
        f";LAYER_CHANGE\n;Z:0.2\n;HEIGHT:0.2\n; printing object Cup.stl id:0 copy {c}\n;TYPE:Outer wall\n;WIDTH:0.4\n"
        f"G1 X{10 * c} Y0 F3000\nG1 X{10 * c + 5} Y0 E0.5\n; stop printing object Cup.stl id:0 copy {c}\n"
        for c in (0, 1)
    )
    p = write(tmp_path, text)
    with pytest.raises(ValueError, match="several copies"):
        read_walls(p, "Cup.stl")
    one = read_walls(p, "Cup.stl id:0 copy 1")
    assert one.layers[0].a.tolist() == [[10.0, 0.0]]


def test_prusaslicer_comment_labels_and_m486_name_the_object(tmp_path: Path) -> None:
    prusa = write(
        tmp_path,
        "M83\n;LAYER_CHANGE\n;Z:0.2\n;HEIGHT:0.2\n; printing object A id:0 copy 0\n;TYPE:Outer wall\n;WIDTH:0.4\n"
        "G1 X1 Y1 F3000\nG1 X5 Y1 E0.5\n; stop printing object A id:0 copy 0\n; printing object B id:1 copy 0\n"
        "G1 X30 Y1 F3000\nG1 X35 Y1 E0.5\n; stop printing object B id:1 copy 0\n",
        "prusa.gcode",
    )
    assert read_walls(prusa, "B").layers[0].a.tolist() == [[30.0, 1.0]]
    marlin = write(
        tmp_path,
        "M83\nM486 S0\n;LAYER_CHANGE\n;Z:0.2\n;HEIGHT:0.2\n;TYPE:Outer wall\n;WIDTH:0.4\nG1 X1 Y1 F3000\nG1 X5 Y1 E0.5\n"
        "M486 S1\nG1 X30 Y1 F3000\nG1 X35 Y1 E0.5\nM486 S-1\n",
        "marlin.gcode",
    )
    assert read_walls(marlin, "object 1").layers[0].a.tolist() == [[30.0, 1.0]]


def test_a_bambu_plate_is_read_by_object_name_with_its_z_height_and_layer_height() -> (
    None
):
    """`; Z_HEIGHT:` / `; LAYER_HEIGHT:` carry the layer in Bambu files; the name is written `NAME — NAME id:N copy 0`."""
    walls = read_walls(DATA / "bambu_a1mini_path.gcode", "SW")
    assert len(walls.layers) == 1
    layer = walls.layers[0]
    # the layer marker's step is 0.3, but the object prints at its own path height 0.12 (`; LAYER_HEIGHT: 0.12`)
    assert (layer.z, layer.h) == (0.8, 0.12)
    assert layer.mid_z == pytest.approx(0.74)
    assert layer.width[0] == pytest.approx(
        0.42, abs=1e-3
    )  # `; LINE_WIDTH:` of the outer wall
    assert "SW — SW id:23 copy 0" in walls.objects


def test_the_first_height_of_a_layer_is_its_thickness_not_the_last(
    tmp_path: Path,
) -> None:
    """Orca repeats `;HEIGHT:` when a path's height changes (a bridge at the end of the layer): mid z must not move."""
    p = write(
        tmp_path,
        layer_gcode(
            "G1 X1 Y1 F3000\nG1 X5 Y1 E0.5\n;TYPE:Internal bridge\n;HEIGHT:0.4\nG1 X9 Y1 E0.5\n",
            ";Z:1.88\n;HEIGHT:0.12\n",
        ),
    )
    layer = read_walls(p).layers[0]
    assert (layer.z, layer.h) == (1.88, 0.12)
    assert layer.mid_z == pytest.approx(1.82)


def test_absolute_extrusion_draws_only_where_e_grows(tmp_path: Path) -> None:
    p = write(
        tmp_path,
        "M82\nG92 E0\n"
        + layer_gcode(
            "G1 X1 Y0 F3000\nG1 X2 Y0 E1.0\nG1 X3 Y0 E1.0\nG1 X4 Y0 E0.5\nG92 E0\nG1 X5 Y0 E0.5\n"
        ),
    )
    # E1.0 draws; the same E again draws nothing; a LOWER E (retraction) draws nothing; after G92 E0 the 0.5 draws
    assert read_walls(p).layers[0].b.tolist() == [[2.0, 0.0], [5.0, 0.0]]


def test_relative_positioning_g91_moves_from_the_current_point(tmp_path: Path) -> None:
    p = write(
        tmp_path,
        "M83\nG90\n"
        + layer_gcode(
            "G1 X10 Y10 F3000\nG91\nG1 X5 Y0 E0.5\nG1 X0 Y5 E0.5\nG90\nG1 X0 Y0 E0.5\n"
        ),
    )
    walls = read_walls(p).layers[0]
    assert walls.b.tolist() == [[15.0, 10.0], [15.0, 15.0], [0.0, 0.0]]


def test_the_position_is_unknown_after_homing(tmp_path: Path) -> None:
    p = write(
        tmp_path,
        "M83\n"
        + layer_gcode("G1 X10 Y10 F3000\nG28\nG1 X20 Y10 E0.5\nG1 X30 Y10 E0.5\n"),
    )
    # the first move after G28 starts from an unknown point: it draws nothing, the next one does
    assert read_walls(p).layers[0].a.tolist() == [[20.0, 10.0]]


def test_arcs_are_followed_as_chords(tmp_path: Path) -> None:
    p = write(
        tmp_path,
        "M83\n"
        + layer_gcode(
            "G1 X10 Y0 F3000\nG3 X-10 Y0 I-10 J0 E1.0\nG3 X10 Y0 I10 J0 E1.0\n"
        ),
    )
    pts, _w = read_walls(p).layers[0].points(0.2)
    r = np.hypot(pts[:, 0], pts[:, 1])
    assert r.min() == pytest.approx(10.0, abs=0.02)
    assert r.max() == pytest.approx(10.0, abs=0.02)
    assert len(pts) > 100  # a full circle of radius 10 densified every ~0.2 mm
    assert float(np.ptp(pts[:, 0])) == pytest.approx(20.0, abs=0.05)


def test_crafted_files_are_refused_not_expanded(tmp_path: Path) -> None:
    huge = "G1 X0 Y0 F3000\nG3 X10 Y0 I-3000 J0 E1\n"
    with pytest.raises(ValueError, match="not a print move"):
        read_walls(write(tmp_path, "M83\n" + layer_gcode(huge)))
    long_piece = "G1 X0 Y0 F3000\nG1 X100000000 Y0 E1\n"
    with pytest.raises(ValueError, match="not a print move"):
        read_walls(write(tmp_path, "M83\n" + layer_gcode(long_piece), "long.gcode"))
    radius_form = "G1 X0 Y0 F3000\nG2 X10 Y0 R5 E1\n"
    with pytest.raises(ValueError, match="radius form"):
        read_walls(write(tmp_path, "M83\n" + layer_gcode(radius_form), "r.gcode"))


def test_g2_is_clockwise_and_g3_counter_clockwise(tmp_path: Path) -> None:
    cw = read_walls(write(tmp_path, "M83\n" + layer_gcode("G1 X10 Y0 F3000\nG2 X0 Y-10 I-10 J0 E1\n"))).layers[0]
    ccw = read_walls(write(tmp_path, "M83\n" + layer_gcode("G1 X10 Y0 F3000\nG3 X0 Y10 I-10 J0 E1\n"), "ccw.gcode")).layers[0]
    assert float(cw.b[:, 1].min()) < -9.9 and float(cw.b[:, 1].max()) <= 0.0  # clockwise from (10,0) goes down
    assert float(ccw.b[:, 1].max()) > 9.9 and float(ccw.b[:, 1].min()) >= 0.0


def test_prusaslicer_external_perimeter_is_a_wall_and_a_file_without_walls_is_refused(tmp_path: Path) -> None:
    prusa = write(
        tmp_path,
        "M83\n;LAYER_CHANGE\n;Z:0.2\n;HEIGHT:0.2\n;TYPE:External perimeter\n;WIDTH:0.45\nG1 X1 Y1 F3000\nG1 X5 Y1 E0.5\n"
        ";TYPE:Overhang wall\nG1 X5 Y5 E0.5\n",
    )
    walls = read_walls(prusa)
    assert walls.layers[0].b.tolist() == [[5.0, 1.0]]
    assert walls.overhang_mm == pytest.approx(4.0)  # the left-out overhang wall is counted, not lost
    assert read_walls(prusa, kinds=("Overhang wall",)).layers[0].b.tolist() == [[5.0, 5.0]]
    with pytest.raises(KeyError, match="prints no"):
        read_walls(write(tmp_path, "G1 X1 Y1\n", "empty.gcode"))


def test_each_pieces_own_height_gives_the_layer_height_and_a_raft_gives_a_base(tmp_path: Path) -> None:
    """The marker says 0.24 (the step above the raft), the object's path says 0.12: mid z and the base follow the path."""
    p = write(tmp_path, "M83\n" + layer_gcode("G1 X1 Y1 F3000\n;HEIGHT:0.12\nG1 X5 Y1 E0.5\n", ";Z:1.34\n;HEIGHT:0.24\n"))
    walls = read_walls(p)
    assert walls.layers[0].mid_z == pytest.approx(1.28)
    assert walls.base_z == pytest.approx(1.22)


def test_points_are_spaced_by_the_requested_step(gcode: Path) -> None:
    layer = read_walls(gcode, "Pot_Pepper.stl").layers[0]
    pts, width = layer.points(0.5)
    assert len(pts) == len(width) == 40  # two 10 mm pieces at 0.5 mm
    assert np.hypot(*(pts[1] - pts[0])) == pytest.approx(0.5)


def test_a_wall_without_a_width_tag_takes_the_footers_line_width(
    tmp_path: Path,
) -> None:
    p = write(
        tmp_path,
        "M83\n;LAYER_CHANGE\n;Z:0.2\n;HEIGHT:0.2\n;TYPE:Outer wall\nG1 X1 Y1 F3000\nG1 X5 Y1 E0.2\n"
        "; outer_wall_line_width = 0.45\n",
    )
    assert read_walls(p).layers[0].width.tolist() == [0.45]


def test_a_nonsense_line_width_is_ignored_not_used(tmp_path: Path) -> None:
    p = write(
        tmp_path,
        "M83\n;LAYER_CHANGE\n;Z:0.2\n;HEIGHT:0.2\n;TYPE:Outer wall\nG1 X1 Y1 F3000\nG1 X5 Y1 E0.2\n"
        "; outer_wall_line_width = 105%\n",
    )
    assert read_walls(p).layers[0].width.tolist() == [0.42]  # the default, not 105 mm
