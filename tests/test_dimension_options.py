"""`3d dimension` --up, --overall, --dims-from and --check --require-dims (#594, #595, #596)."""

from __future__ import annotations

import json
import pathlib
import shutil
from typing import Any

import numpy as np
import pytest
from dimension_fixtures import BLUE, GREEN, RED, Box, write_3mf

pytest.importorskip("PIL")
trimesh = pytest.importorskip("trimesh")

from dimension_args import (  # noqa: E402
    parse_dims_file,
    parse_dims_gaps,
    parse_overall,
    parse_up,
)
from dimension_check import check_image, sidecar_path  # noqa: E402
from dimension_model import RenderError  # noqa: E402
from dimension_render import render  # noqa: E402


def _silhouette(data: dict[str, Any], view: str) -> tuple[int, int]:
    """(width, height) in grid cells of the part's silhouette in one view."""
    rows = data["views"][view]["silhouette_grid"]["rows"]
    cols = [i for r in rows for i, c in enumerate(r) if c == "#"]
    lines = [j for j, r in enumerate(rows) if "#" in r]
    return max(cols) - min(cols) + 1, max(lines) - min(lines) + 1


@pytest.fixture
def tall_y(tmp_path: pathlib.Path) -> str:
    """A 10 x 40 x 10 post: it stands along Y, as a model drawn Y-up does."""
    path = tmp_path / "post.stl"
    trimesh.creation.box(extents=(10, 40, 10)).export(path)
    return str(path)


def test_up_y_makes_the_top_view_a_true_top_view(
    tall_y: str, tmp_path: pathlib.Path
) -> None:
    z_up = render(tall_y, str(tmp_path / "z.png"))
    w, h = _silhouette(z_up, "top")
    assert h > 3 * w  # as modelled, the TOP view of the standing post is its long side

    y_up = render(tall_y, str(tmp_path / "y.png"), up="Y")
    w, h = _silhouette(y_up, "top")
    assert abs(w - h) <= 2  # the true top view of a post is its square end
    fw, fh = _silhouette(y_up, "front")
    assert fh > 3 * fw  # and the front view shows it standing
    assert y_up["up"] == "Y" and y_up["bbox_mm"] == pytest.approx([10, 40, 10])


def test_up_keeps_the_models_own_axis_letters(
    tall_y: str, tmp_path: pathlib.Path
) -> None:
    data = render(tall_y, str(tmp_path / "y.png"), up="Y")
    by_view = {
        v: {d["label"]: d["value_mm"] for d in data["dimensions"] if d["view"] == v}
        for v in ("top", "front")
    }
    assert by_view["top"] == {"X": 10.0, "Z": 10.0}
    assert (
        by_view["front"]["Y"] == 40.0
    )  # the vertical edge of the front view is the model's Y
    assert check_image(str(tmp_path / "y.png")) == []


def test_up_x_and_custom_dims_follow_the_model_axes(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "beam.stl"
    trimesh.creation.box(extents=(50, 10, 10)).export(path)
    data = render(
        str(path), str(tmp_path / "x.png"), up="X", dims=["along:-25,6,6:25,6,6"]
    )
    assert any(
        d["label"] == "along" and d["value_mm"] == 50.0 for d in data["dimensions"]
    )
    assert _silhouette(data, "front")[1] > _silhouette(data, "front")[0]


def test_up_with_a_bed_is_refused(tall_y: str, tmp_path: pathlib.Path) -> None:
    with pytest.raises(RenderError, match="--bed"):
        render(tall_y, str(tmp_path / "b.png"), up="Y", bed=(220.0, 220.0))


def test_up_and_overall_grammar() -> None:
    assert [parse_up(v) for v in ("z", "Y", "x")] == ["Z", "Y", "X"]
    assert parse_overall(" Auto ") == "auto"
    with pytest.raises(ValueError, match="--up"):
        parse_up("W")
    with pytest.raises(ValueError, match="--overall"):
        parse_overall("sometimes")


def _labels(data: dict[str, Any]) -> set[str]:
    return {d["label"] for d in data["dimensions"]}


def test_plate_of_separate_parts_gets_per_part_dims_not_one_overall(
    tmp_path: pathlib.Path,
) -> None:
    model = write_3mf(
        tmp_path / "plate.3mf", [Box("left", 0, RED), Box("right", 60, GREEN)]
    )
    out = str(tmp_path / "plate.png")
    data = render(str(model), out)
    assert data["overall"] == "per-part"
    assert not {"X", "Y", "Z"} & _labels(data)  # no 70 mm "X" across the gap
    assert {"X left", "X right"} <= _labels(data)
    assert all(d["value_mm"] == 10.0 for d in data["dimensions"])
    assert check_image(out) == []


def test_touching_parts_keep_the_overall_dims(tmp_path: pathlib.Path) -> None:
    model = write_3mf(
        tmp_path / "pair.3mf", [Box("left", 0, RED), Box("right", 10, GREEN)]
    )
    data = render(str(model), str(tmp_path / "pair.png"))
    assert data["overall"] == "drawn" and {"X", "Y", "Z"} <= _labels(data)
    assert next(d for d in data["dimensions"] if d["label"] == "X")["value_mm"] == 20.0


def test_overall_never_draws_none_and_always_is_refused_across_separate_parts(
    tmp_path: pathlib.Path,
) -> None:
    model = write_3mf(
        tmp_path / "plate.3mf", [Box("left", 0, RED), Box("right", 60, GREEN)]
    )
    with pytest.raises(RenderError, match="separate parts"):
        render(str(model), str(tmp_path / "a.png"), overall="always")
    never = render(str(model), str(tmp_path / "n.png"), overall="never")
    assert never["overall"] == "none" and not _labels(never)
    touching = write_3mf(
        tmp_path / "pair.3mf", [Box("left", 0, RED), Box("right", 10, GREEN)]
    )
    always = render(str(touching), str(tmp_path / "t.png"), overall="always")
    assert always["overall"] == "drawn" and always["bbox_mm"][0] == 20.0


def _spec(
    tmp_path: pathlib.Path,
    *entries: tuple[str, list[float], list[float]],
    as_yaml: bool = False,
) -> str:
    items = [{"name": n, "from": a, "to": b} for n, a, b in entries]
    path = tmp_path / ("dims.yaml" if as_yaml else "dims.json")
    if as_yaml:
        import yaml

        path.write_text(yaml.safe_dump({"dims": items}), encoding="utf-8")
    else:
        path.write_text(json.dumps(items), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("as_yaml", [False, True])
def test_dims_from_reads_json_and_yaml(tmp_path: pathlib.Path, as_yaml: bool) -> None:
    pytest.importorskip("yaml") if as_yaml else None
    spec = _spec(tmp_path, ("slot", [0, 0, 0], [10, 0, 0]), as_yaml=as_yaml)
    assert parse_dims_file(spec) == ["slot:0.0,0.0,0.0:10.0,0.0,0.0"]


@pytest.mark.parametrize(
    "body",
    [
        "not json [",
        "[]",
        '[{"name": "a", "from": [0, 0], "to": [1, 1, 1]}]',
        '[{"name": "a:b", "from": [0,0,0], "to": [1,1,1]}]',
        '[{"name": "a", "from": [0,0,0], "to": [1,1,1]}, {"name": "a", "from": [0,0,0], "to": [2,2,2]}]',
        '[{"name": "a", "from": [0,0,"inf"], "to": [1,1,1]}]',
    ],
)
def test_bad_dims_files_are_refused(tmp_path: pathlib.Path, body: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match="--dims-from"):
        parse_dims_file(str(path))


def test_declared_measurements_are_drawn_and_recorded_for_the_gate(
    tmp_path: pathlib.Path,
) -> None:
    model = tmp_path / "box.stl"
    trimesh.creation.box(extents=(30, 20, 10)).export(model)
    spec = parse_dims_file(
        _spec(
            tmp_path,
            ("width", [-15, -10, -5], [15, -10, -5]),
            ("depth", [15, -10, -5], [15, 10, -5]),
        )
    )
    out = str(tmp_path / "b.png")
    data = render(str(model), out, dims=spec, required=spec)
    assert data["required_dims"] == ["width", "depth"] and {
        "width",
        "depth",
    } <= _labels(data)
    assert check_image(out, require_dims=True) == []


def test_a_measurement_that_fits_no_view_fails_the_render_naming_it(
    tmp_path: pathlib.Path,
) -> None:
    model = tmp_path / "box.stl"
    trimesh.creation.box(extents=(30, 20, 10)).export(model)
    # Along the iso camera direction: it projects to a point in iso and is foreshortened everywhere else.
    spec = parse_dims_file(
        _spec(tmp_path, ("along the eye", [0, 0, 0], [5, -5, 7.0711]))
    )
    with pytest.raises(RenderError, match="along the eye"):
        render(str(model), str(tmp_path / "x.png"), dims=spec, required=spec)


def test_require_dims_fails_when_a_declared_measurement_is_missing_or_none_was_declared(
    tmp_path: pathlib.Path,
) -> None:
    model = tmp_path / "box.stl"
    trimesh.creation.box(extents=(30, 20, 10)).export(model)
    plain = str(tmp_path / "plain.png")
    render(str(model), plain)
    assert check_image(plain) == []  # without the flag nothing changes
    assert [o.rule for o in check_image(plain, require_dims=True)] == [
        "no-required-dims"
    ]

    spec = parse_dims_file(_spec(tmp_path, ("width", [-15, 0, 5], [15, 0, 5])))
    declared = str(tmp_path / "declared.png")
    render(str(model), declared, dims=spec, required=spec)
    side = sidecar_path(declared)
    data = json.loads(pathlib.Path(side).read_text(encoding="utf-8"))
    data["required_dims"].append("ghost")
    pathlib.Path(side).write_text(json.dumps(data), encoding="utf-8")
    offenders = check_image(declared, require_dims=True)
    assert [(o.label, o.rule) for o in offenders] == [("ghost", "missing-required-dim")]


# --- no dimension across separate parts (#602) -------------------------------------------------


@pytest.fixture
def plate(tmp_path: pathlib.Path) -> str:
    """Two 10 mm cubes 50 mm apart: `left` at x 0..10, `right` at x 60..70."""
    return str(
        write_3mf(
            tmp_path / "plate.3mf", [Box("left", 0, RED), Box("right", 60, GREEN)]
        )
    )


def test_a_dim_between_separate_parts_is_rejected_naming_both_and_the_allowed_form(
    plate: str, tmp_path: pathlib.Path
) -> None:
    with pytest.raises(RenderError) as excinfo:
        render(plate, str(tmp_path / "x.png"), dims=["across:5,5,5:65,5,5"])
    message = str(excinfo.value)
    assert "left" in message and "right" in message and "kind: gap" in message
    assert excinfo.value.exit_code == 2


def test_a_dim_inside_one_part_is_drawn_on_a_separated_layout(
    plate: str, tmp_path: pathlib.Path
) -> None:
    out = str(tmp_path / "ok.png")
    data = render(plate, out, dims=["edge:0,5,10:10,5,10"])
    item = next(d for d in data["dimensions"] if d["label"] == "edge")
    assert item["value_mm"] == 10.0 and item["spans_separated"] is False
    assert item["parts"] == ["left"] and data["separated_parts"] is True
    assert check_image(out) == []


def test_an_endpoint_on_no_part_is_rejected(plate: str, tmp_path: pathlib.Path) -> None:
    with pytest.raises(RenderError, match="no part"):
        render(plate, str(tmp_path / "x.png"), dims=["air:5,5,5:30,5,5"])


def test_the_tolerance_is_half_a_millimetre(plate: str, tmp_path: pathlib.Path) -> None:
    render(
        plate, str(tmp_path / "in.png"), dims=["near:0,5,5:10.4,5,5"]
    )  # 0.4 mm past the face
    with pytest.raises(RenderError, match="no part"):
        render(plate, str(tmp_path / "out.png"), dims=["far:0,5,5:10.6,5,5"])


def _gap_spec(tmp_path: pathlib.Path, parts: list[str]) -> list[str]:
    path = tmp_path / "gap.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "mate",
                    "kind": "gap",
                    "parts": parts,
                    "from": [10, 5, 10],
                    "to": [60, 5, 10],
                }
            ]
        ),
        encoding="utf-8",
    )
    return [str(path)]


def test_a_declared_gap_between_two_named_parts_is_allowed_and_marked(
    plate: str, tmp_path: pathlib.Path
) -> None:
    (path,) = _gap_spec(tmp_path, ["left", "right"])
    out = str(tmp_path / "gap.png")
    data = render(
        plate,
        out,
        dims=parse_dims_file(path),
        required=parse_dims_file(path),
        gaps=parse_dims_gaps(path),
    )
    item = next(d for d in data["dimensions"] if d["label"] == "mate")
    assert item["kind"] == "gap" and item["parts"] == ["left", "right"]
    assert item["value_mm"] == 50.0
    assert check_image(out, require_dims=True) == []  # a declared gap passes the gate


def test_a_gap_between_the_wrong_or_unknown_parts_is_rejected(
    plate: str, tmp_path: pathlib.Path
) -> None:
    for parts, text in (
        (["left", "nope"], "no part named"),
        (["left", "left2"], "no part named"),
    ):
        (path,) = _gap_spec(tmp_path, parts)
        with pytest.raises(RenderError, match=text):
            render(
                plate,
                str(tmp_path / "x.png"),
                dims=parse_dims_file(path),
                gaps=parse_dims_gaps(path),
            )
    swapped = write_3mf(
        tmp_path / "three.3mf",
        [Box("left", 0, RED), Box("right", 60, GREEN), Box("far", 120, BLUE)],
    )
    (path,) = _gap_spec(
        tmp_path, ["left", "far"]
    )  # the points sit on left and right, not far
    with pytest.raises(RenderError, match="must run from part 'left' to part 'far'"):
        render(
            str(swapped),
            str(tmp_path / "y.png"),
            dims=parse_dims_file(path),
            gaps=parse_dims_gaps(path),
        )


def test_touching_parts_are_not_a_separated_layout(tmp_path: pathlib.Path) -> None:
    pair = write_3mf(
        tmp_path / "pair.3mf", [Box("left", 0, RED), Box("right", 10, GREEN)]
    )
    data = render(str(pair), str(tmp_path / "p.png"), dims=["across:5,5,5:15,5,5"])
    assert (
        data["separated_parts"] is False
    )  # nothing to refuse: the parts are assembled


def test_the_gate_fails_an_image_whose_sidecar_has_a_dimension_across_separate_parts(
    plate: str, tmp_path: pathlib.Path
) -> None:
    out = str(tmp_path / "ok.png")
    render(plate, out, dims=["edge:0,5,10:10,5,10"])
    assert check_image(out) == []
    side = pathlib.Path(sidecar_path(out))
    data = json.loads(side.read_text(encoding="utf-8"))
    data["dimensions"][0]["spans_separated"] = True
    data["dimensions"][0]["parts"] = ["left", "right"]
    side.write_text(json.dumps(data), encoding="utf-8")
    offenders = check_image(out)
    assert [o.rule for o in offenders] == ["spans-separated-parts"]
    assert "left / right" in offenders[0].detail
    # the same dimension declared as a gap is the allowed form
    data["dimensions"][0]["kind"] = "gap"
    side.write_text(json.dumps(data), encoding="utf-8")
    assert check_image(out) == []


def test_an_overall_dimension_in_a_separated_sidecar_fails_the_gate(
    tmp_path: pathlib.Path,
) -> None:
    box = tmp_path / "box.stl"
    trimesh.creation.box(extents=(30, 20, 10)).export(box)
    out = str(tmp_path / "b.png")
    render(str(box), out)
    side = pathlib.Path(sidecar_path(out))
    data = json.loads(side.read_text(encoding="utf-8"))
    for item in data["dimensions"]:
        if item["kind"] == "bbox":
            item["spans_separated"] = (
                True  # what an overall dim of an exploded layout would say
            )
    side.write_text(json.dumps(data), encoding="utf-8")
    rules = {o.rule for o in check_image(out)}
    assert rules == {"spans-separated-parts"}


def test_dims_file_gap_grammar(tmp_path: pathlib.Path) -> None:
    for entry in (
        {"name": "g", "kind": "gap", "from": [0, 0, 0], "to": [1, 1, 1]},
        {
            "name": "g",
            "kind": "gap",
            "parts": ["a"],
            "from": [0, 0, 0],
            "to": [1, 1, 1],
        },
        {
            "name": "g",
            "kind": "gap",
            "parts": ["a", "a"],
            "from": [0, 0, 0],
            "to": [1, 1, 1],
        },
        {"name": "g", "kind": "wide", "from": [0, 0, 0], "to": [1, 1, 1]},
    ):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps([entry]), encoding="utf-8")
        with pytest.raises(ValueError, match="--dims-from"):
            parse_dims_file(str(path))


@pytest.mark.skipif(shutil.which("openscad") is None, reason="openscad not installed")
def test_an_exploded_scad_assembly_has_no_dimension_across_its_halves(
    tmp_path: pathlib.Path,
) -> None:
    scad = tmp_path / "exploded.scad"
    scad.write_text(
        'color("red") cube(10);\ncolor("#2222CC") translate([0, 40, 0]) cube(10);\n'
    )
    out = str(tmp_path / "e.png")
    data = render(str(scad), out)
    assert data["separated_parts"] is True and data["overall"] == "per-part"
    assert not [
        d for d in data["dimensions"] if d["kind"] == "bbox" or d["spans_separated"]
    ]
    assert all(
        d["value_mm"] == 10.0 for d in data["dimensions"]
    )  # none is the 50 mm overall Y
    assert check_image(out) == []


# --- occlusion: only geometry the eye sees can be measured (see the occlusion ticket) ------------------------------


@pytest.fixture
def boxed(tmp_path: pathlib.Path) -> str:
    """A closed 40 mm cube shell (case) with a 10 x 10 x 10 'board' sealed inside it."""
    case = trimesh.creation.box(extents=(40, 40, 40))
    board = trimesh.creation.box(extents=(10, 10, 10))
    path = tmp_path / "boxed.stl"
    trimesh.util.concatenate([case, board]).export(path)
    return str(path)


def test_a_dimension_of_something_inside_the_case_is_hidden_in_every_view_and_named(
    boxed: str, tmp_path: pathlib.Path
) -> None:
    spec = ["head+plug:-5,0,0:5,0,0"]  # the board's width: inside the closed case
    with pytest.raises(RenderError) as excinfo:
        render(boxed, str(tmp_path / "x.png"), dims=spec, required=spec)
    assert "head+plug" in str(excinfo.value) and "inside the model" in str(
        excinfo.value
    )
    data = render(boxed, str(tmp_path / "h.png"), dims=spec)
    assert data["hidden_in_view"] == {"head+plug": ["top", "front", "side", "iso"]}
    assert "head+plug" not in _labels(
        data
    )  # no arrow floating in the air next to the case


def test_a_dimension_on_the_visible_surface_is_drawn_and_not_reported_hidden(
    boxed: str, tmp_path: pathlib.Path
) -> None:
    out = str(tmp_path / "v.png")
    data = render(boxed, out, dims=["top edge:-20,0,20:20,0,20"])  # the case's top face
    drawn = {d["view"] for d in data["dimensions"] if d["label"] == "top edge"}
    assert "top" in drawn
    assert "top" not in data["hidden_in_view"].get("top edge", [])
    assert check_image(out) == []


def test_a_dimension_is_drawn_only_in_the_views_that_see_both_ends(
    boxed: str, tmp_path: pathlib.Path
) -> None:
    # On the +x face of the case: seen from the side (eye +x) and the iso, not from the top or front.
    data = render(boxed, str(tmp_path / "s.png"), dims=["face:20,-15,0:20,15,0"])
    views = {d["view"] for d in data["dimensions"] if d["label"] == "face"}
    assert "side" in views
    assert not views & set(data["hidden_in_view"].get("face", []))
    assert (
        "front" in data["hidden_in_view"]["face"]
        or "top" in data["hidden_in_view"]["face"]
    )


def test_visible_from_ray_test_has_teeth() -> None:
    from dimension_options import visible_from

    wall = np.array(
        [[[-5, -5, 5], [5, -5, 5], [0, 5, 5]]], dtype=float
    )  # a triangle at z = 5
    up = np.array([0.0, 0.0, 1.0])
    assert not visible_from(wall, np.array([0.0, 0.0, 0.0]), up)  # behind it
    assert visible_from(wall, np.array([0.0, 0.0, 5.0]), up)  # on it
    assert visible_from(
        wall, np.array([0.0, 0.0, 4.7]), up
    )  # within the 0.5 mm tolerance
    assert not visible_from(
        wall, np.array([0.0, 0.0, 4.4]), up
    )  # 0.6 mm behind: hidden
    assert visible_from(wall, np.array([0.0, 0.0, 6.0]), up)  # in front of it
    assert visible_from(
        wall, np.array([20.0, 0.0, 0.0]), up
    )  # beside it: nothing in the way
