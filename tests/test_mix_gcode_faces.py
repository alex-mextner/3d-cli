"""The `mix face` rule on a G-code alone (#419): top-surface vs outer-wall area per object printed by
more than one head, no project 3MF. Synthetic G-code for the rule; real Snapmaker Orca U1 slices of a
flat and a standing mix tile (tests/data/mix_gcode_faces) for agreement with the 3MF check."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from mix_fixtures import orca_project, tile

from printer_ops.preflight import check_mix_faces
from slicing import mix_faces, mix_gcode_faces

DATA = Path(__file__).parent / "data" / "mix_gcode_faces"
LAYER_H = 0.12


class Plate:
    """Builds an Orca-dialect G-code: EXCLUDE_OBJECT labels, per-layer ;Z:/;HEIGHT:, T changes, ;TYPE: runs."""

    def __init__(self, *, labels: bool = True, relative_e: bool = True) -> None:
        self.lines = ["; HEADER_BLOCK_START", "G90", "M83" if relative_e else "M82"]
        self.labels, self.layer, self.relative_e, self.e = labels, 0, relative_e, 0.0
        self.tool = -1

    def layer_change(self, z: float, h: float = LAYER_H) -> None:
        self.layer += 1
        self.lines += [";LAYER_CHANGE", f";Z:{z:.3f}", f";HEIGHT:{h}"]

    def obj(self, name: str) -> Plate:
        self.lines.append(f"EXCLUDE_OBJECT_START NAME={name}_id_0_copy_0")
        self.current = name
        return self

    def end(self) -> None:
        self.lines.append(f"EXCLUDE_OBJECT_END NAME={self.current}_id_0_copy_0")

    def run(self, tool: int, kind: str, length: float, width: float = 0.42) -> None:
        """One extruding run of `length` mm along X, preceded by a travel to its start."""
        if tool != self.tool:
            self.lines.append(f"T{tool - 1}")
            self.tool = tool
        self.lines += [f";TYPE:{kind}", f";WIDTH:{width}", "G1 X10 Y10 F9000"]
        remaining, x = length, 10.0
        while remaining > 1e-9:
            seg = min(remaining, 100.0)
            x += seg
            de = seg * 0.04
            if self.relative_e:
                self.lines.append(f"G1 X{x:.3f} Y10 E{de:.5f}")
            else:
                self.e += de
                self.lines.append(f"G1 X{x:.3f} Y10 E{self.e:.5f}")
            remaining -= seg
            if remaining > 1e-9:
                self.lines.append("G1 X10 Y10 F9000")
                x = 10.0

    def write(self, path: Path, header: list[str] | None = None) -> Path:
        path.write_text("\n".join((header or []) + self.lines) + "\n", encoding="utf-8")
        return path


def two_head_part(
    plate: Plate, name: str, *, top: float, wall: float, layers: int = 10
) -> None:
    """A part whose heads alternate layer by layer (T1, T2, T1…), `top` mm of top-surface line on
    its last layer and `wall` mm of outer wall spread over all layers."""
    plate.obj(name)
    for i in range(layers):
        plate.layer_change(0.12 * (i + 1))
        head = 1 + i % 2
        plate.run(head, "Outer wall", wall / layers)
        if i == layers - 1:
            plate.run(head, "Top surface", top)
    plate.end()


def test_two_head_object_with_mostly_top_surface_fails(tmp_path: Path) -> None:
    p = Plate()
    two_head_part(
        p, "tile", top=400.0, wall=120.0
    )  # 400 × 0.42 = 168 mm² top vs 120 × 0.12 = 14.4 mm²
    report = mix_gcode_faces.check(str(p.write(tmp_path / "flat.gcode")))
    (obj,) = report.objects
    assert obj.heads == {1, 2} and obj.mixed
    assert obj.top_mm2 == pytest.approx(400 * 0.42, rel=1e-6)
    assert obj.wall_mm2 == pytest.approx(120 * LAYER_H, rel=1e-6)
    assert (
        obj.verdict()[0] == "FAIL"
        and "top 168 mm² > half the walls" in obj.verdict()[1]
    )
    assert report.status == "FAIL"


def test_two_head_object_with_mostly_outer_wall_passes(tmp_path: Path) -> None:
    p = Plate()
    two_head_part(p, "tile", top=20.0, wall=3000.0)
    report = mix_gcode_faces.check(str(p.write(tmp_path / "standing.gcode")))
    (obj,) = report.objects
    assert obj.verdict()[0] == "PASS" and report.status == "PASS"
    assert obj.top_mm2 == pytest.approx(8.4) and obj.wall_mm2 == pytest.approx(360.0)


def test_single_head_object_is_not_checked(tmp_path: Path) -> None:
    p = Plate()
    p.obj("lid")
    for i in range(6):
        p.layer_change(0.12 * (i + 1))
        p.run(1, "Top surface", 500.0)  # all top, no walls: would fail if it were mixed
    p.end()
    report = mix_gcode_faces.check(str(p.write(tmp_path / "lid.gcode")))
    assert [o.mixed for o in report.objects] == [False] and report.mixed == []
    assert report.status == "SKIP"
    got = mix_gcode_faces.preflight_check(str(tmp_path / "lid.gcode"))
    assert got["status"] == "SKIP" and "no object printed on more than one head" in str(
        got["detail"]
    )


def test_local_z_object_printing_both_heads_every_layer_is_mixed(
    tmp_path: Path,
) -> None:
    """Local-Z splits every layer between the heads: head_sequence skips such objects, this does not."""
    p = Plate()
    p.obj("tile")
    for i in range(8):
        p.layer_change(0.12 * (i + 1))
        p.run(1, "Top surface", 150.0)
        p.run(2, "Top surface", 150.0)
        p.run(1, "Outer wall", 40.0)
        p.run(2, "Outer wall", 40.0)
    p.end()
    report = mix_gcode_faces.check(str(p.write(tmp_path / "lz.gcode")))
    (obj,) = report.objects
    assert obj.heads == {1, 2} and obj.verdict()[0] == "FAIL"


def test_only_the_mixed_object_of_a_plate_is_reported(tmp_path: Path) -> None:
    p = Plate()
    two_head_part(p, "flat_tile", top=300.0, wall=100.0)
    p.layer_change(1.5)
    p.obj("plain")
    p.run(3, "Top surface", 900.0)
    p.end()
    report = mix_gcode_faces.check(str(p.write(tmp_path / "two.gcode")))
    assert {o.name for o in report.objects} == {
        "flat_tile_id_0_copy_0",
        "plain_id_0_copy_0",
    }
    assert [o.name for o in report.mixed] == ["flat_tile_id_0_copy_0"]
    got = mix_gcode_faces.preflight_check(str(tmp_path / "two.gcode"))
    assert got["status"] == "WARN" and "1 of 1 object(s) on several heads" in str(
        got["detail"]
    )
    lines = got["lines"]
    assert isinstance(lines, list)
    assert [str(line).split(":")[0] for line in lines] == ["FAIL flat_tile"]


def test_prime_tower_outside_any_object_does_not_make_an_object_mixed(
    tmp_path: Path,
) -> None:
    p = Plate()
    p.obj("lid")
    p.layer_change(0.12)
    p.run(1, "Top surface", 300.0)
    p.end()
    p.run(2, "Prime tower", 500.0)  # outside every object block
    report = mix_gcode_faces.check(str(p.write(tmp_path / "tower.gcode")))
    assert [o.heads for o in report.objects] == [{1}]


def test_absolute_extrusion_and_arcs_are_measured(tmp_path: Path) -> None:
    p = Plate(relative_e=False)
    two_head_part(p, "tile", top=0.0, wall=600.0, layers=6)
    arc = tmp_path / "arc.gcode"
    arc.write_text(
        "\n".join(
            [
                "G90",
                "M83",
                "EXCLUDE_OBJECT_START NAME=ring_id_0_copy_0",
                ";LAYER_CHANGE",
                ";Z:0.12",
                ";HEIGHT:0.12",
                "T0",
                ";TYPE:Outer wall",
                ";WIDTH:0.42",
                "G1 X20 Y10 F9000",
                "G2 X10 Y10 I-5 J0 E1.0",  # clockwise half circle of radius 5, from the right: π·5
                "T1",
                "G3 X20 Y10 I5 J0 E1.0",  # counter-clockwise half circle, back
                "EXCLUDE_OBJECT_END NAME=ring_id_0_copy_0",
            ]
        ),
        encoding="utf-8",
    )
    report = mix_gcode_faces.check(str(p.write(tmp_path / "abs.gcode")))
    assert report.objects[0].wall_mm2 == pytest.approx(600 * LAYER_H, rel=1e-6)
    ring = mix_gcode_faces.check(str(arc)).objects[0]
    assert ring.wall_mm2 == pytest.approx(2 * math.pi * 5 * LAYER_H, rel=1e-6)


def test_wall_area_follows_the_layer_height_of_each_layer(tmp_path: Path) -> None:
    p = Plate()
    p.obj("tile")
    p.layer_change(0.2, 0.2)
    p.run(1, "Outer wall", 100.0)
    p.layer_change(0.32, 0.12)
    p.run(2, "Outer wall", 100.0)
    p.end()
    (obj,) = mix_gcode_faces.check(str(p.write(tmp_path / "vlh.gcode"))).objects
    assert obj.wall_mm2 == pytest.approx(100 * 0.2 + 100 * 0.12)


def test_g_code_without_object_labels_or_feature_tags_is_skipped_with_the_reason(
    tmp_path: Path,
) -> None:
    bare = tmp_path / "bare.gcode"
    bare.write_text("G90\nM83\nT0\nG1 X1 Y1 F100\nG1 X5 Y5 E1\n", encoding="utf-8")
    got = mix_gcode_faces.preflight_check(str(bare))
    assert got["status"] == "SKIP" and "no object labels" in str(got["detail"])
    p = Plate()
    p.obj("tile")
    for i in range(4):
        p.layer_change(0.12 * (i + 1))
        p.run(
            1 + i % 2, "Internal solid infill", 100.0
        )  # a feature that is neither top nor wall
    p.end()
    untagged = mix_gcode_faces.preflight_check(str(p.write(tmp_path / "infill.gcode")))
    assert untagged["status"] == "SKIP" and "no `;TYPE:Top surface`" in str(
        untagged["detail"]
    )


# -- print-preflight line: 3MF when there is one, G-code otherwise -----------------------------


def test_preflight_line_uses_the_project_3mf_when_present_and_says_so(
    tmp_path: Path,
) -> None:
    p = Plate()
    two_head_part(p, "PTA", top=400.0, wall=120.0)  # the G-code alone would WARN
    gcode = p.write(tmp_path / "plate.gcode")
    project = orca_project(
        tmp_path / "plate.3mf", [tile("PTA", 5, standing=True)]
    )  # the project is standing
    line = check_mix_faces(project, "plate.3mf next to the G-code", gcode)
    assert line.status == "PASS" and "(plate.3mf next to the G-code)" in line.detail
    assert "G-code only" not in line.detail


def test_preflight_line_without_a_project_uses_the_gcode_and_warns_never_fails(
    tmp_path: Path,
) -> None:
    p = Plate()
    two_head_part(p, "PTA", top=400.0, wall=120.0)
    line = check_mix_faces(None, "", p.write(tmp_path / "plate.gcode"))
    assert line.status == "WARN" and "G-code only: no project 3MF" in line.detail
    assert any("top 168 mm² > half the walls" in x for x in line.lines)


# -- real U1 slices (Snapmaker Orca 2.4, 12 × 12 × 2.4 mm tile, mix 431, U1 system presets) ----


@pytest.mark.parametrize(("name", "verdict"), [("flat", "FAIL"), ("standing", "PASS")])
def test_real_sliced_tile_gets_the_verdict_of_the_3mf_check(
    name: str, verdict: str
) -> None:
    from_3mf = mix_faces.check(str(DATA / f"{name}.3mf"))
    (part,) = from_3mf.parts
    from_gcode = mix_gcode_faces.check(str(DATA / f"{name}.gcode"))
    (obj,) = from_gcode.objects
    assert part.status == verdict and obj.verdict()[0] == verdict
    assert obj.heads == {1, 3, 4}  # mix 431 prints on heads T4, T3, T1 (tools 0, 2, 3)
    # the two measurements see the same tile: the walls land within 10 % of the mesh area; the skin
    # stops at the inner wall, so a small top reads up to a third lower than the mesh face
    assert obj.top_mm2 == pytest.approx(part.top, rel=0.35)
    assert obj.wall_mm2 == pytest.approx(part.side, rel=0.10)


def test_real_sliced_tile_preflight_lines() -> None:
    flat = mix_gcode_faces.preflight_check(str(DATA / "flat.gcode"))
    standing = mix_gcode_faces.preflight_check(str(DATA / "standing.gcode"))
    assert flat["status"] == "WARN" and standing["status"] == "PASS"
    assert json.dumps(flat)  # plain JSON types: the report prints with --json
