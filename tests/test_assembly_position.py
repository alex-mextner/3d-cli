"""`assembly.position` + the role guardrail: parts laid on plates, non-printable parts kept off, tags and mates survive."""

from __future__ import annotations

import io
import json
import logging
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import arrange_pack
import numpy as np
import parts_3mf
import pytest
from assembly import position as P
from assembly import scene as SC
from assembly import spec as S
from assembly import threemf as T
from assembly import viz
from assembly_fixtures import board_in_case, box, peg_pocket, write_stl
from errors import GateFailure
from parts import NonPrintableParts, assign_styles


def load(doc: dict[str, Any], base: Path) -> SC.Scene:
    return SC.load_scene(S.parse_spec(doc, base_dir=base))


def positioned(
    tmp: Path, doc: dict[str, Any] | None = None, **kw: Any
) -> tuple[SC.Scene, P.Layout, Path]:
    scene = load(doc or board_in_case(tmp), tmp)
    layout = P.plan_layout(scene, **kw)
    out = tmp / "plates.3mf"
    P.write_project(scene, layout, out)
    return scene, layout, out


def test_non_printable_parts_never_reach_a_plate(tmp_path: Path) -> None:
    scene, layout, out = positioned(tmp_path)
    assert [p.name for p in layout.placed] == ["case"]
    assert [d.name for d in layout.dropped] == ["board"] and layout.dropped[
        0
    ].role == "stand_in"
    summary = parts_3mf.summary(out)
    assert [s["name"] for s in summary] == ["case"]
    assert summary[0]["3d:role"] == "printed" and summary[0]["3d:printable"] == "true"
    assert (
        "NOT on any plate" not in json.dumps(layout.to_dict())
        and layout.to_dict()["dropped_non_printable"][0]["name"] == "board"
    )


def test_the_explicit_override_places_them_logs_it_and_keeps_the_tag(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="3d.parts"):
        _, layout, out = positioned(
            tmp_path, include_non_printable=True, reason="fit test"
        )
    assert sorted(p.name for p in layout.placed) == ["board", "case"]
    tags = {s["name"]: s["3d:role"] for s in parts_3mf.summary(out)}
    assert tags == {"case": "printed", "board": "stand_in"}, (
        "the role tag survives the override"
    )
    assert (
        "non-printable parts included by override" in caplog.text
        and "fit test" in caplog.text
    )
    assert [o.name for o in layout.overridden] == ["board"]


def test_a_spec_with_nothing_printable_is_refused(tmp_path: Path) -> None:
    doc = board_in_case(tmp_path)
    doc["parts"]["case"]["role"] = "reference"
    doc["parts"]["case"]["reference_source"] = {
        "url": "https://example.org/case",
        "license": "CC0",
    }
    with pytest.raises(GateFailure, match="no printable part"):
        P.plan_layout(load(doc, tmp_path))


def test_the_3mf_carries_distinct_colours_settings_and_the_assembly(
    tmp_path: Path,
) -> None:
    doc = board_in_case(tmp_path)
    doc["parts"]["case"]["print"] = {
        "face": "-Z",
        "settings": {"layer_height": "0.2", "wall_loops": "3"},
    }
    _, layout, out = positioned(tmp_path, doc, include_non_printable=True)
    with zipfile.ZipFile(out) as zf:
        model = zf.read("3D/3dmodel.model").decode()
        settings = zf.read("Metadata/model_settings.config").decode()
        embedded = json.loads(zf.read(T.ENTRY))
    styles = assign_styles(S.parse_spec(doc, base_dir=tmp_path).part_list())
    assert len({s.color for s in styles.values()}) == 2
    for style in styles.values():
        assert f'displaycolor="{style.color}"' in model
    assert (
        'key="layer_height" value="0.2"' in settings
        and 'key="wall_loops" value="3"' in settings
    )
    assert embedded["schema"] == T.SCHEMA_3MF
    assert [m["name"] for m in embedded["spec"]["mates"]] == ["board_ribs"], (
        "mates travel with the 3MF"
    )
    assert set(embedded["layout"]) == {"case", "board"}


def test_orientation_auto_keeps_a_pocket_open_upwards_and_explicit_choices_win(
    tmp_path: Path,
) -> None:
    scene = load(peg_pocket(tmp_path), tmp_path)
    rot, info = P.choose_orientation(scene, scene.body("socket"))
    assert (
        info["how"] == "default"
        and info["face"] == "-Z"
        and info["fit_surface_overhang_mm2"] == 0
    )
    doc = peg_pocket(tmp_path)
    doc["parts"]["socket"]["print"] = {"face": "auto"}
    assert (
        P.choose_orientation(load(doc, tmp_path), load(doc, tmp_path).body("socket"))[
            1
        ]["face"]
        == "-Z"
    )
    doc["parts"]["socket"]["print"] = {"face": "+X"}
    sc = load(doc, tmp_path)
    assert P.choose_orientation(sc, sc.body("socket"))[1] == {
        "how": "face",
        "face": "+X",
    }
    doc["parts"]["socket"]["print"] = {"rotate": [90, 0, 0]}
    sc = load(doc, tmp_path)
    assert P.choose_orientation(sc, sc.body("socket"))[1]["how"] == "rotate"


def test_an_orientation_that_must_overhang_a_fit_surface_is_warned_about(
    tmp_path: Path,
) -> None:
    # a ceiling the peg must touch, printable only with supports in every orientation of this part
    write_stl(
        tmp_path / "arch.stl",
        box((-10, -10, 0), (10, 10, 10))
        - box((-6, -6, -1), (6, 6, 6))
        - box((-12, -2, -1), (12, 2, 3)),
    )
    write_stl(tmp_path / "ball.stl", box((-5, -5, 2), (5, 5, 8)))
    doc = {
        "parts": {"arch": {"mesh": "arch.stl"}, "ball": {"mesh": "ball.stl"}},
        "mates": [
            {
                "name": "m",
                "a": "arch",
                "b": "ball",
                "kind": "sliding",
                "zone": {"min": [-6, -6, 0], "max": [6, 6, 6.5]},
            }
        ],
    }
    scene = load(doc, tmp_path)
    _, info = P.choose_orientation(scene, scene.body("arch"))
    assert info["fit_surface_overhang_mm2"] >= 0 and "how" in info


def test_the_positioned_3mf_round_trips_spec_geometry_and_passes_its_own_check(
    tmp_path: Path,
) -> None:
    from assembly import check

    scene, _, out = positioned(tmp_path)
    pos = T.read_positioned(out)
    assert [m.name for m in pos.spec.mates] == ["board_ribs"] and not pos.drift
    printed = pos.meshes["case"][0]
    assert printed.bounds == pytest.approx(scene.body("case").mesh.bounds, abs=1e-4), (
        "mapped back into the assembled frame"
    )
    again = SC.load_scene(pos.spec, meshes=pos.meshes)
    assert check.run_check(again)["ok"], (
        "the check of the printed file passes like the spec's"
    )


def test_a_3mf_without_the_assembly_entry_says_how_to_make_one(tmp_path: Path) -> None:
    plain = tmp_path / "plain.3mf"
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
    with pytest.raises(Exception, match="assembly position"):
        T.read_positioned(plain)


def test_teeth_an_edited_object_is_reported_as_drift(tmp_path: Path) -> None:
    _, _, out = positioned(tmp_path)
    edited = tmp_path / "edited.3mf"
    with zipfile.ZipFile(out) as src, zipfile.ZipFile(edited, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "3D/3dmodel.model":
                data = (
                    data.decode().replace('z="12.0"', 'z="13.0"').encode()
                )  # one corner pulled up
            dst.writestr(item, data)
    pos = T.read_positioned(edited)
    assert pos.drift and pos.drift[0]["part"] == "case" and "case" not in pos.meshes


def test_the_guard_refuses_a_tagged_3mf_and_the_override_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _, _, out = positioned(tmp_path, include_non_printable=True)
    with pytest.raises(NonPrintableParts, match="board"):
        parts_3mf.guard(out, command="prep")
    with caplog.at_level(logging.WARNING, logger="3d.parts"):
        guard = parts_3mf.guard(
            out, command="prep", include_non_printable=True, reason="fit test"
        )
    assert [p.name for p in guard.overridden] == ["board"] and "fit test" in caplog.text


def test_arrange_keeps_printed_tags_and_leaves_the_stand_in_off_the_plates(
    tmp_path: Path,
) -> None:
    pytest.importorskip(
        "lxml",
        reason="arrange re-reads its output with trimesh, whose 3MF loader needs lxml",
    )
    _, _, out = positioned(tmp_path, include_non_printable=True)
    assert len(parts_3mf.parts_of(out)) == 2
    plates = tmp_path / "arranged.3mf"
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert arrange_pack.main([str(out), "-o", str(plates), "--bed", "220"]) == 0
    assert "NOT placed (non-printable part, role tag): board" in buf.getvalue()
    assert [(s["name"], s["3d:role"]) for s in parts_3mf.summary(plates)] == [
        ("case", "printed")
    ]
    forced = tmp_path / "forced.3mf"
    with redirect_stdout(io.StringIO()):
        assert (
            arrange_pack.main(
                [str(out), "-o", str(forced), "--bed", "220", "--include-non-printable"]
            )
            == 0
        )
    assert {(s["name"], s["3d:role"]) for s in parts_3mf.summary(forced)} == {
        ("case", "printed"),
        ("board", "stand_in"),
    }


def test_arrange_pieces_drop_tagged_objects_and_carry_the_tags_into_the_project(tmp_path: Path) -> None:
    """The lxml-free part of the arrange round trip: load tagged -> guard -> write with the tags."""
    from orca_project_3mf import MeshPart, write_project_3mf

    _, _, out = positioned(tmp_path, include_non_printable=True)
    named, tags = arrange_pack._load_tagged(str(out), min_volume=1.0)
    assert sorted(n for n, _ in named) == ["board", "case"] and tags["board"]["3d:role"] == "stand_in"
    kept, dropped, overridden = arrange_pack._apply_print_guard(named, tags, include_non_printable=False, reason="")
    assert [n for n, _ in kept] == ["case"] and dropped == ["board"] and overridden == []
    kept, dropped, overridden = arrange_pack._apply_print_guard(named, tags, include_non_printable=True, reason="x")
    assert len(kept) == 2 and dropped == [] and overridden == ["board"]
    written = tmp_path / "carried.3mf"
    parts = [
        MeshPart(
            n,
            [(float(v[0]), float(v[1]), float(v[2])) for v in m.vertices],
            [(int(f[0]), int(f[1]), int(f[2])) for f in m.faces],
            0,
            tags[n],
        )
        for n, m in kept
    ]
    write_project_3mf(str(written), parts, bed=220)
    assert {(t["name"], t["3d:role"]) for t in parts_3mf.summary(written)} == {("case", "printed"), ("board", "stand_in")}


def test_arrange_has_nothing_to_print_when_every_object_is_non_printable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from orca_project_3mf import MeshPart, write_project_3mf

    only = tmp_path / "only.3mf"
    tri = [(0, 1, 2), (0, 2, 3), (0, 3, 1), (1, 3, 2)]
    verts = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0), (0.0, 0.0, 10.0)]
    write_project_3mf(
        str(only),
        [
            MeshPart(
                "pcb", verts, tri, 0, {"3d:role": "vitamin", "3d:printable": "false"}
            )
        ],
        bed=220,
    )
    assert arrange_pack.main([str(only), "-o", str(tmp_path / "x.3mf")]) == 1
    assert "nothing printable is left" in capsys.readouterr().err


def test_arrange_refuses_per_plate_output_for_a_tagged_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, _, out = positioned(tmp_path)
    assert arrange_pack.main([str(out), "--per-plate", "-o", str(tmp_path / "pp")]) == 2
    assert "role tags only survive" in capsys.readouterr().err


# --- colours: stable, distinct, with a legend --------------------------------------------------------------------
def test_every_picture_draws_each_part_in_its_own_colour_with_a_legend_and_is_reproducible(
    tmp_path: Path,
) -> None:
    from PIL import Image

    scene = load(board_in_case(tmp_path), tmp_path)
    styles = assign_styles(scene.spec.part_list())
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    viz.render_view(scene, a, styles=styles, size=(500, 380))
    viz.render_view(scene, b, styles=styles, size=(500, 380))
    assert a.read_bytes() == b.read_bytes(), "two runs are identical"
    img = Image.open(a).convert("RGB")
    legend_row = img.crop((0, img.height - 60, img.width, img.height))
    colours = {tuple(int(c) for c in px) for px in np.asarray(legend_row).reshape(-1, 3)}
    for style in styles.values():
        rgb = viz.hex_rgb(style.color)
        assert rgb in colours, f"legend swatch of {style.name} {style.color}"
    assert len({s.color for s in styles.values()}) == len(styles)


def test_the_section_highlight_marks_the_overlap_in_red(tmp_path: Path) -> None:
    from assembly import check
    from PIL import Image

    scene = load(board_in_case(tmp_path, board_dx=1.0), tmp_path)
    shown: list[Any] = []

    def on_region(sc: SC.Scene, rep: dict[str, Any]) -> None:
        out = viz.render_section(
            sc, rep, tmp_path / f"r{len(shown)}.png", region_mesh=rep["_region"].mesh
        )
        shown.append(out)

    rep = check.run_check(scene, on_region=on_region)
    assert not rep["ok"] and shown
    pixels = np.asarray(Image.open(shown[0]).convert("RGB")).reshape(-1, 3)
    red = int((pixels == np.array(viz.RED)).all(axis=1).sum())
    assert red > 200, "the overlap region is drawn red"
    exploded = viz.explode_offsets(load(peg_pocket(tmp_path), tmp_path))
    assert exploded["peg"] == pytest.approx((0.0, 0.0, 10.0)) and exploded[
        "socket"
    ] == (0.0, 0.0, 0.0)
