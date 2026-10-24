"""`3d dimension` --up, --overall, --dims-from and --check --require-dims (#594, #595, #596)."""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest
from dimension_fixtures import GREEN, RED, Box, write_3mf

pytest.importorskip("PIL")
trimesh = pytest.importorskip("trimesh")

from dimension_args import parse_dims_file, parse_overall, parse_up  # noqa: E402
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
        str(path), str(tmp_path / "x.png"), up="X", dims=["along:-25,0,0:25,0,0"]
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


def test_overall_always_and_never_override_the_detection(
    tmp_path: pathlib.Path,
) -> None:
    model = write_3mf(
        tmp_path / "plate.3mf", [Box("left", 0, RED), Box("right", 60, GREEN)]
    )
    always = render(str(model), str(tmp_path / "a.png"), overall="always")
    assert always["overall"] == "drawn" and always["bbox_mm"][0] == 70.0
    assert {"X", "Y", "Z"} <= _labels(always)
    never = render(str(model), str(tmp_path / "n.png"), overall="never")
    assert never["overall"] == "none" and not _labels(never)


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
