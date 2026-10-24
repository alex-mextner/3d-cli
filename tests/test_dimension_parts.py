"""Parts, colours and the legend of `3d dimension` (alex-mextner/3d-cli#580).

Every part of a model is drawn in its own colour: authored colours from 3MF / OBJ+MTL / OpenSCAD
`color()` when they are distinct, else the shared palette (`lib/parts.py`); stand-ins are muted.
Small hand-written models (tests/dimension_fixtures.py) pin the structures the readers must handle.
"""

from __future__ import annotations

import colorsys
import json
import pathlib
import random
import shutil
import sys
from typing import Any

import numpy as np
import pytest
from dimension_fixtures import (
    BLUE,
    GREEN,
    RED,
    THREE_BOXES,
    Box,
    write_3mf,
    write_3mf_per_triangle,
    write_obj,
)

pytest.importorskip("PIL")
trimesh = pytest.importorskip("trimesh")

from dimension_args import parse_define, parse_part  # noqa: E402
from dimension_check import check_image, sidecar_path  # noqa: E402
from dimension_model import RenderError, load_raw, read_3mf  # noqa: E402
from dimension_parts import (  # noqa: E402
    MIN_DELTA_E,
    MUTE_MIX,
    MUTED_PAIR_SCALE,
    PartInfo,
    build_model,
    delta_e,
)
from dimension_render import render  # noqa: E402


def _parts(
    path: pathlib.Path, specs: list[str] | None = None, title: str = "t"
) -> dict[str, PartInfo]:
    model = build_model(load_raw(str(path)), title, specs or [])
    return {p.name: p for p in model.parts}


def test_3mf_objects_become_parts_with_their_authored_colours(
    tmp_path: pathlib.Path,
) -> None:
    parts = _parts(write_3mf(tmp_path / "a.3mf", THREE_BOXES))
    assert {n: p.color for n, p in parts.items()} == {
        "lid": RED,
        "base": GREEN,
        "clip": BLUE,
    }
    assert {p.source for p in parts.values()} == {"authored"}


def test_3mf_reads_without_lxml(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """trimesh's own 3MF loader needs lxml (not a dependency): the reader must not."""
    monkeypatch.setitem(sys.modules, "lxml", None)
    raw = read_3mf(str(write_3mf(tmp_path / "a.3mf", THREE_BOXES)))
    assert len(raw.faces) == 36 and len(raw.groups) == 3


def test_openscad_style_object_splits_by_colour_and_names_generic_materials_by_hex(
    tmp_path: pathlib.Path,
) -> None:
    path = write_3mf_per_triangle(
        tmp_path / "scad.3mf",
        [Box("a", 0, RED), Box("b", 20, GREEN), Box("c", 40)],
        openscad=True,
    )
    parts = _parts(path)
    assert set(parts) == {
        RED,
        GREEN,
        "uncoloured",
    }  # "Color 1" says nothing; the hex does
    assert (
        parts["uncoloured"].authored is None
    )  # slot 0 "Default" is OpenSCAD's "no color()"


def test_a_named_material_names_its_part(tmp_path: pathlib.Path) -> None:
    path = write_3mf_per_triangle(
        tmp_path / "m.3mf", [Box("a", 0, RED, "steel"), Box("b", 20, GREEN, "rubber")]
    )
    assert set(_parts(path)) == {"steel", "rubber"}


def test_obj_mtl_gives_each_object_its_kd(tmp_path: pathlib.Path) -> None:
    parts = _parts(write_obj(tmp_path / "a.obj", THREE_BOXES))
    assert {n: p.color for n, p in parts.items()} == {
        "lid": RED,
        "base": GREEN,
        "clip": BLUE,
    }


def test_obj_with_a_missing_mtl_warns_and_falls_back_to_the_palette(
    tmp_path: pathlib.Path,
) -> None:
    path = write_obj(tmp_path / "a.obj", THREE_BOXES)
    path.with_suffix(".mtl").unlink()
    model = build_model(load_raw(str(path)), "t")
    assert any("a.mtl not found" in w for w in model.warnings)
    assert {p.source for p in model.parts} == {"palette"}
    assert len({p.color for p in model.parts}) == 3


def test_a_part_that_clashes_with_another_takes_a_palette_colour(
    tmp_path: pathlib.Path,
) -> None:
    near_red = "#CD2323"
    parts = _parts(
        write_3mf(tmp_path / "a.3mf", [Box("a", 0, RED), Box("b", 20, near_red)])
    )
    assert (
        parts["a"].color == RED and parts["a"].source == "authored"
    )  # first by name keeps its colour
    assert parts["b"].source == "palette" and parts["b"].authored == near_red
    assert delta_e(parts["a"].color, parts["b"].color) >= MIN_DELTA_E


def test_same_name_parts_get_distinct_names_and_colours(tmp_path: pathlib.Path) -> None:
    parts = _parts(
        write_3mf(
            tmp_path / "a.3mf", [Box("screw", 0), Box("screw", 20), Box("screw", 40)]
        )
    )
    assert set(parts) == {"screw", "screw (2)", "screw (3)"}
    assert len({p.color for p in parts.values()}) == 3


def test_a_generated_suffix_never_takes_a_name_the_file_gave(
    tmp_path: pathlib.Path,
) -> None:
    boxes = [Box("screw", 0), Box("screw", 20), Box("screw (2)", 40)]
    parts = _parts(write_3mf(tmp_path / "a.3mf", boxes))
    assert set(parts) == {"screw", "screw (2)", "screw (3)"}
    assert parts["screw (2)"].faces == 12  # the file's own "screw (2)" kept its name


@pytest.mark.parametrize("how", ["spec", "metadata"])
def test_a_lone_stand_in_is_muted_like_one_in_an_assembly(
    tmp_path: pathlib.Path, how: str
) -> None:
    meta = {"3d:role": "stand_in"} if how == "metadata" else {}
    path = write_3mf(tmp_path / "a.3mf", [Box("board", 0, BLUE, meta=meta)])
    parts = _parts(path, ["board=:stand_in"] if how == "spec" else None)
    board = parts["board"]
    assert board.role == "stand_in" and board.muted and board.source == "palette"
    assert board.color != BLUE


@pytest.mark.parametrize("seed", range(4))
def test_colours_do_not_depend_on_the_order_of_the_objects(
    tmp_path: pathlib.Path, seed: int
) -> None:
    boxes = [Box(f"part {i}", 20 * i, None if i % 2 else "#336699") for i in range(7)]
    want = {n: p.color for n, p in _parts(write_3mf(tmp_path / "a.3mf", boxes)).items()}
    shuffled = boxes[:]
    random.Random(seed).shuffle(shuffled)
    got = _parts(write_3mf(tmp_path / "b.3mf", shuffled))
    assert {n: p.color for n, p in got.items()} == want
    assert len(set(want.values())) == 7


def test_many_uncoloured_parts_all_get_different_colours(
    tmp_path: pathlib.Path,
) -> None:
    boxes = [Box(f"p{i}", 15 * i) for i in range(20)]
    colors = [p.color for p in _parts(write_3mf(tmp_path / "a.3mf", boxes)).values()]
    assert len(set(colors)) == 20
    assert (
        min(delta_e(a, b) for i, a in enumerate(colors) for b in colors[i + 1 :]) > 15
    )


def test_stand_in_role_from_the_command_line_mutes_the_colour(
    tmp_path: pathlib.Path,
) -> None:
    path = write_3mf(tmp_path / "a.3mf", THREE_BOXES)
    parts = _parts(path, [f"{BLUE}=PCB:stand_in"])
    assert set(parts) == {"lid", "base", "PCB"}
    pcb = parts["PCB"]
    assert pcb.role == "stand_in" and pcb.muted and not pcb.printable
    assert (
        pcb.authored == BLUE and pcb.source == "palette"
    )  # an authored colour never overrides muting
    channels = [int(pcb.color[i : i + 2], 16) for i in (1, 3, 5)]
    assert min(channels) >= MUTE_MIX * 255 - 1  # washed toward white
    assert not parts["lid"].muted


def test_role_tag_of_a_3mf_object_is_read(tmp_path: pathlib.Path) -> None:
    boxes = [Box("lid", 0, RED), Box("board", 20, GREEN, meta={"3d:role": "stand_in"})]
    parts = _parts(write_3mf(tmp_path / "a.3mf", boxes))
    assert parts["board"].role == "stand_in" and parts["board"].muted
    assert parts["lid"].role == "printed"


def test_two_stand_ins_stay_distinguishable(tmp_path: pathlib.Path) -> None:
    boxes = [Box("a", 0, RED), Box("b", 20, GREEN), Box("c", 40, BLUE)]
    parts = _parts(
        write_3mf(tmp_path / "a.3mf", boxes),
        [f"{c}=:stand_in" for c in (RED, GREEN, BLUE)],
    )
    shown = [p.color for p in parts.values()]
    assert all(p.muted for p in parts.values())
    assert (
        min(delta_e(a, b) for i, a in enumerate(shown) for b in shown[i + 1 :])
        >= MIN_DELTA_E * MUTED_PAIR_SCALE  # muting washes every difference toward white
    )


def test_part_selector_that_matches_nothing_lists_what_exists(
    tmp_path: pathlib.Path,
) -> None:
    path = write_3mf(tmp_path / "a.3mf", THREE_BOXES)
    with pytest.raises(RenderError) as excinfo:
        _parts(path, ["nope=x"])
    assert "lid" in str(excinfo.value) and excinfo.value.exit_code == 2


def test_uncoloured_stl_is_split_into_its_bodies(tmp_path: pathlib.Path) -> None:
    a = trimesh.creation.box(extents=(10, 10, 10))
    b = trimesh.creation.box(extents=(10, 10, 10))
    b.apply_translation((30, 0, 0))
    path = tmp_path / "two.stl"
    trimesh.util.concatenate([a, b]).export(path)
    assert sorted(_parts(path, title="two")) == ["body 1", "body 2"]


def test_a_single_body_is_one_part_named_after_the_file(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "one.stl"
    trimesh.creation.box(extents=(10, 10, 10)).export(path)
    parts = _parts(path, title="one")
    assert list(parts) == ["one"] and parts["one"].source == "default"


def test_a_tiny_fragment_joins_the_big_body_instead_of_becoming_a_part(
    tmp_path: pathlib.Path,
) -> None:
    big = trimesh.creation.box(extents=(50, 50, 50))
    speck = trimesh.creation.box(extents=(0.2, 0.2, 0.2))
    speck.apply_translation((60, 0, 0))
    path = tmp_path / "speck.stl"
    trimesh.util.concatenate([big, speck]).export(path)
    assert list(_parts(path, title="speck")) == ["speck"]


def test_named_objects_are_not_split_into_bodies(tmp_path: pathlib.Path) -> None:
    """Two disjoint cubes inside ONE named object of a multi-object file stay that object's part."""
    parts = _parts(write_3mf(tmp_path / "a.3mf", [Box("a", 0), Box("b", 20)]))
    assert set(parts) == {"a", "b"}


@pytest.mark.parametrize(
    ("spec", "want"),
    [
        ("#2a62c9=PCB:stand_in", ("#2A62C9", "PCB", "stand_in")),
        ("#abc=:vitamin", ("#AABBCC", None, "vitamin")),
        ("lid=top cover", ("lid", "top cover", None)),
        ("lid=top cover:printed", ("lid", "top cover", "printed")),
        ("body 1=board:stand-in", ("body 1", "board", "stand_in")),
    ],
)
def test_part_spec_grammar(spec: str, want: tuple[str, str | None, str | None]) -> None:
    assert parse_part(spec) == want


@pytest.mark.parametrize(
    "spec", ["nonsense", "=x", "#12=x", "#gggggg=x", "lid=", "lid=x:nonrole", "#abc="]
)
def test_bad_part_specs_are_rejected(spec: str) -> None:
    with pytest.raises(ValueError, match="--part"):
        parse_part(spec)


def test_define_grammar() -> None:
    assert parse_define("standin=true") == "standin=true"
    for bad in ("standin", "=1", "1x=2", "a b=1", "x="):
        with pytest.raises(ValueError, match="-D"):
            parse_define(bad)


def test_define_needs_a_scad_input(tmp_path: pathlib.Path) -> None:
    path = write_3mf(tmp_path / "a.3mf", THREE_BOXES)
    with pytest.raises(RenderError, match=r"\.scad"):
        load_raw(str(path), ["x=1"])


def test_broken_3mf_is_a_render_error_not_a_traceback(tmp_path: pathlib.Path) -> None:
    bad = tmp_path / "bad.3mf"
    bad.write_bytes(b"not a zip")
    with pytest.raises(RenderError, match="not a readable 3MF"):
        load_raw(str(bad))


def test_3mf_build_item_transform_and_unit_are_applied(tmp_path: pathlib.Path) -> None:
    path = write_3mf(
        tmp_path / "a.3mf",
        [Box("a", 0, RED)],
        unit="centimeter",
        transform="1 0 0 0 1 0 0 0 1 5 0 0",
    )
    verts = read_3mf(str(path)).vertices
    assert verts.min(axis=0)[0] == pytest.approx(50.0)  # (0 + 5) cm
    assert verts.max(axis=0)[0] == pytest.approx(150.0)  # (10 + 5) cm


# --- the rendered picture ---------------------------------------------------------------------


def _hue_pixels(image: Any, color: str, rect: list[int]) -> int:
    """Pixels of `rect` with the hue/saturation of `color` (shading only scales brightness)."""
    want_h, want_s, _ = colorsys.rgb_to_hsv(
        *(int(color[i : i + 2], 16) / 255 for i in (1, 3, 5))
    )
    hsv = (
        np.asarray(image.crop(tuple(rect)).convert("RGB").convert("HSV"), dtype=float)
        / 255.0
    )
    hue, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    near = (np.abs(((hue - want_h + 0.5) % 1.0) - 0.5) < 0.03) & (
        np.abs(sat - want_s) < 0.15
    )
    return int((near & (val > 0.25)).sum())


def test_each_part_shows_in_its_own_colour_with_a_legend(
    tmp_path: pathlib.Path,
) -> None:
    from PIL import Image

    model = write_3mf(tmp_path / "a.3mf", THREE_BOXES)
    out = str(tmp_path / "a.png")
    data = render(str(model), out)
    assert [p["name"] for p in data["parts"]] == ["lid", "base", "clip"]
    assert data["legend"]["rows"] >= 1
    image = Image.open(out)
    front = data["views"]["front"]["rect"]
    for part in data["parts"]:
        assert _hue_pixels(image, part["color"], front) > 200, part["name"]
        # the legend strip holds this exact swatch colour
        strip = np.asarray(image.convert("RGB"))[data["legend"]["box"][1] :]
        want = np.array([int(part["color"][i : i + 2], 16) for i in (1, 3, 5)])
        assert (strip == want).all(axis=2).sum() > 100, part["name"]
    assert check_image(out) == []


def test_legend_marks_stand_ins_and_the_sidecar_records_the_role(
    tmp_path: pathlib.Path,
) -> None:
    model = write_3mf(tmp_path / "a.3mf", THREE_BOXES)
    data = render(str(model), str(tmp_path / "a.png"), parts=[f"{BLUE}=PCB:stand_in"])
    pcb = next(p for p in data["parts"] if p["name"] == "PCB")
    assert pcb["role"] == "stand_in" and pcb["muted"] and pcb["printable"] is False
    assert check_image(str(tmp_path / "a.png")) == []


def test_single_part_model_keeps_one_colour_and_no_legend(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "one.stl"
    trimesh.creation.box(extents=(30, 20, 10)).export(path)
    out = str(tmp_path / "one.png")
    data = render(str(path), out)
    assert len(data["parts"]) == 1 and "legend" not in data
    assert data["image"]["height"] == 64 + 2 * 560
    assert check_image(out) == []


def test_sidecar_keeps_its_old_fields_and_gains_parts(tmp_path: pathlib.Path) -> None:
    data = render(
        str(write_3mf(tmp_path / "a.3mf", THREE_BOXES)), str(tmp_path / "a.png")
    )
    for key in ("part", "source", "bbox_mm", "views", "image", "dimensions"):
        assert key in data
    on_disk = json.loads(
        pathlib.Path(sidecar_path(str(tmp_path / "a.png"))).read_text()
    )
    assert on_disk["parts"] == data["parts"]
    assert on_disk["legend"]["box"][3] == data["image"]["height"]


def test_many_parts_wrap_the_legend_and_the_gate_still_passes(
    tmp_path: pathlib.Path,
) -> None:
    boxes = [Box(f"long part name number {i}", 15 * i) for i in range(30)]
    out = str(tmp_path / "many.png")
    data = render(str(write_3mf(tmp_path / "many.3mf", boxes)), out)
    assert data["legend"]["rows"] > 2
    assert len({p["color"] for p in data["parts"]}) == 30
    assert check_image(out) == []


@pytest.mark.skipif(shutil.which("openscad") is None, reason="openscad not installed")
def test_scad_color_calls_become_coloured_parts(tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "asm.scad"
    scad.write_text(
        'color("red") cube(10);\n'
        'color("#2222CC") translate([20, 0, 0]) cube(10);\n'
        "translate([40, 0, 0]) cube(10);\n"
        'extra = false;\nif (extra) translate([60, 0, 0]) color("lime") cube(10);\n'
    )
    out = str(tmp_path / "asm.png")
    data = render(str(scad), out)
    by_name = {p["name"]: p for p in data["parts"]}
    assert set(by_name) == {"#FF0000", "#2222CC", "uncoloured"}
    assert (
        by_name["#FF0000"]["color"] == "#FF0000"
        and by_name["uncoloured"]["authored_color"] is None
    )
    assert check_image(out) == []
    with_extra = render(str(scad), out, defines=["extra=true"])
    assert "#00FF00" in {p["name"] for p in with_extra["parts"]}  # -D reached openscad


@pytest.mark.skipif(shutil.which("openscad") is None, reason="openscad not installed")
def test_uncoloured_scad_assembly_splits_into_bodies(tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "plain.scad"
    scad.write_text("cube(10); translate([30, 0, 0]) cube(10);\n")
    data = render(str(scad), str(tmp_path / "plain.png"))
    assert [p["name"] for p in data["parts"]] == ["body 1", "body 2"]
