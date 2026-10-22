"""`3d dimension` — dimensioned renders and the legibility gate (alex-mextner/3d-cli#477).

The gate must be able to FAIL: each rule gets a case that produces exactly that offender,
either from a real render (tiny font) or from a tampered sidecar / image.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
from typing import Any

import pytest

pytest.importorskip("PIL")
trimesh = pytest.importorskip("trimesh")

from commands.dimension import run as dimension_run  # noqa: E402
from dimension_check import check_image, sha256_file, sidecar_path  # noqa: E402
from dimension_render import render  # noqa: E402
from errors import InputNotFound, InvalidArgument, UsageError  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def part(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A 60 x 30 x 20 plate with a 10 mm boss standing on it, in print pose on a 220 bed."""
    plate = trimesh.creation.box(extents=(60, 30, 20))
    boss = trimesh.creation.cylinder(radius=5, height=25, sections=48)
    boss.apply_translation((15, 0, 22))
    mesh = trimesh.util.concatenate([plate, boss])
    mesh.apply_translation((110, 110, 10))
    path = tmp_path_factory.mktemp("dimpart") / "plate.stl"
    mesh.export(path)
    return str(path)


@pytest.fixture(scope="module")
def rendered(part: str, tmp_path_factory: pytest.TempPathFactory) -> str:
    out = str(tmp_path_factory.mktemp("dimout") / "plate_dims.png")
    render(part, out, bed=(220.0, 220.0), dims=["boss height:125,110,20:125,110,45"])
    return out


def _sidecar(image: str) -> dict[str, Any]:
    with open(sidecar_path(image), encoding="utf-8") as handle:
        data: dict[str, Any] = json.load(handle)
    return data


def _write_sidecar(image: str, data: dict[str, Any]) -> None:
    with open(sidecar_path(image), "w", encoding="utf-8") as handle:
        json.dump(data, handle)


@pytest.fixture
def copy(rendered: str, tmp_path: pathlib.Path) -> str:
    """A private copy of the good render (+ sidecar) a test may tamper with."""
    dst = tmp_path / "copy.png"
    dst.write_bytes(pathlib.Path(rendered).read_bytes())
    data = _sidecar(rendered)
    data["image"]["path"] = str(dst)
    _write_sidecar(str(dst), data)
    return str(dst)


def test_render_writes_png_and_sidecar_with_the_real_sizes(rendered: str) -> None:
    data = _sidecar(rendered)
    values = {(d["label"], d["view"]): d["value_mm"] for d in data["dimensions"]}
    assert values[("X", "top")] == pytest.approx(60.0, abs=0.01)
    assert values[("Y", "top")] == pytest.approx(30.0, abs=0.01)
    assert values[("Z", "front")] == pytest.approx(44.5, abs=0.01)
    assert values[("boss height", "front")] == pytest.approx(25.0, abs=0.01)
    assert data["bed"]["part_inside"] is True
    assert data["image"]["sha256"] == sha256_file(rendered)
    assert data["image"]["width"] == 1280
    for dim in data["dimensions"]:
        assert dim["text"] == f"{dim['label']} {dim['value_mm']:.1f} mm"


def test_good_render_is_legible(rendered: str) -> None:
    assert check_image(rendered) == []


def test_part_outside_the_bed_is_flagged(part: str, tmp_path: pathlib.Path) -> None:
    out = str(tmp_path / "small_bed.png")
    data = render(part, out, bed=(100.0, 100.0))
    assert data["bed"]["part_inside"] is False


def test_tiny_labels_fail_the_gate(part: str, tmp_path: pathlib.Path) -> None:
    out = str(tmp_path / "tiny.png")
    render(part, out, label_px=8.0)
    rules = {o.rule for o in check_image(out)}
    assert "font-too-small" in rules
    assert "text-too-short" in rules


def test_scaled_up_image_keeps_legibility_proportional(
    part: str, tmp_path: pathlib.Path
) -> None:
    out = str(tmp_path / "wide.png")
    render(part, out, width=1920)
    assert check_image(out) == []


def test_missing_sidecar_fails(rendered: str, tmp_path: pathlib.Path) -> None:
    lone = tmp_path / "lone.png"
    lone.write_bytes(pathlib.Path(rendered).read_bytes())
    assert [o.rule for o in check_image(str(lone))] == ["no-sidecar"]


def test_edited_image_no_longer_matches_its_sidecar(copy: str) -> None:
    with open(copy, "ab") as handle:
        handle.write(b"\0")
    assert [o.rule for o in check_image(copy)] == ["sidecar-mismatch"]


def test_overlapping_labels_fail(copy: str) -> None:
    data = _sidecar(copy)
    first = data["dimensions"][0]
    second = next(
        d for d in data["dimensions"] if d["view"] == first["view"] and d is not first
    )
    second["box"] = list(first["box"])
    _write_sidecar(copy, data)
    assert any(o.rule == "overlaps-label" for o in check_image(copy))


def test_label_outside_the_image_fails(copy: str) -> None:
    data = _sidecar(copy)
    data["dimensions"][0]["box"] = [-30.0, 5.0, 100.0, 30.0]
    _write_sidecar(copy, data)
    assert any(o.rule == "outside-image" for o in check_image(copy))


def test_label_sitting_on_the_part_fails(copy: str) -> None:
    data = _sidecar(copy)
    dim = next(d for d in data["dimensions"] if d["view"] == "front")
    grid = data["views"]["front"]["silhouette_grid"]
    cell = grid["cell"]
    gy = next(y for y, row in enumerate(grid["rows"]) if row.count("#") > 12)
    gx = grid["rows"][gy].index("#")
    x, y = grid["x0"] + gx * cell, grid["y0"] + gy * cell
    dim["box"] = [x, y, x + 40, y + 12]
    _write_sidecar(copy, data)
    assert any(o.rule == "covers-geometry" for o in check_image(copy))


def test_label_leaving_its_panel_fails(copy: str) -> None:
    data = _sidecar(copy)
    dim = next(d for d in data["dimensions"] if d["view"] == "top")
    dim["box"] = [
        630.0,
        100.0,
        700.0,
        125.0,
    ]  # inside the image, across into the front panel
    _write_sidecar(copy, data)
    assert any(o.rule == "outside-panel" for o in check_image(copy))


def test_wiped_label_text_fails(copy: str) -> None:
    from PIL import Image, ImageDraw

    data = _sidecar(copy)
    box = [round(v) for v in data["dimensions"][0]["text_box"]]
    img = Image.open(copy).convert("RGB")
    ImageDraw.Draw(img).rectangle(box, fill=(255, 255, 255))
    img.save(copy)
    data["image"]["sha256"] = sha256_file(copy)
    _write_sidecar(copy, data)
    assert any(o.rule == "no-text" for o in check_image(copy))


def test_low_contrast_text_fails(copy: str) -> None:
    from PIL import Image

    data = _sidecar(copy)
    x0, y0, x1, y1 = (round(v) for v in data["dimensions"][0]["text_box"])
    img = Image.open(copy).convert("RGB")
    px = img.load()
    assert px is not None
    for y in range(y0, y1):
        for x in range(x0, x1):
            if px[x, y] != (255, 255, 255):
                px[x, y] = (
                    150,
                    150,
                    150,
                )  # every inked pixel becomes mid grey on white (~3:1)
    img.save(copy)
    data["image"]["sha256"] = sha256_file(copy)
    _write_sidecar(copy, data)
    assert any(o.rule == "low-contrast" for o in check_image(copy))


def test_wrong_value_in_label_text_fails(copy: str) -> None:
    data = _sidecar(copy)
    data["dimensions"][0]["text"] = "X 99.9 mm"
    _write_sidecar(copy, data)
    assert any(o.rule == "value-not-shown" for o in check_image(copy))


def test_too_few_dimensions_fails(copy: str) -> None:
    data = _sidecar(copy)
    data["dimensions"] = data["dimensions"][:1]
    _write_sidecar(copy, data)
    assert any(o.rule == "too-few-dimensions" for o in check_image(copy))


def test_cli_bad_arguments_raise_structured_errors(part: str) -> None:
    with pytest.raises(InvalidArgument):
        dimension_run([part, "-o", "x.png", "--bed", "wide"])
    with pytest.raises(InvalidArgument):
        dimension_run([part, "-o", "x.png", "--dim", "only:1,2,3"])
    with pytest.raises(UsageError):
        dimension_run([part])
    with pytest.raises(InputNotFound):
        dimension_run(["nope.stl", "-o", "x.png"])
    with pytest.raises(InputNotFound):
        dimension_run(["--check", "nope.png"])
    assert dimension_run([]) == 1
    assert dimension_run(["--help"]) == 0


def test_cli_check_exit_codes(rendered: str, copy: str) -> None:
    env = {**os.environ, "PY3D_NO_UV": "1"}
    three_d = str(REPO / "bin" / "3d")
    good = subprocess.run(
        [three_d, "dimension", "--check", rendered],
        capture_output=True,
        text=True,
        env=env,
    )
    assert good.returncode == 0, good.stderr
    assert "PASS" in good.stdout
    data = _sidecar(copy)
    data["dimensions"] = data["dimensions"][:1]
    _write_sidecar(copy, data)
    bad = subprocess.run(
        [three_d, "dimension", "--check", copy, "--json"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert bad.returncode == 1
    assert json.loads(bad.stdout)["legible"] is False


def test_lying_font_claim_is_caught_at_a_wide_width(
    part: str, tmp_path: pathlib.Path
) -> None:
    """At 1920 px the text must be 1.5x taller: a sidecar claiming a big font over small text fails."""
    out = str(tmp_path / "wide_tiny.png")
    render(part, out, width=1920, label_px=8.0)
    data = _sidecar(out)
    for dim in data["dimensions"]:
        dim["font_px"] = 30.0  # claims the default 20 px @1280 scaled to 1920
    _write_sidecar(out, data)
    assert any(o.rule == "text-too-short" for o in check_image(out))


def test_default_render_is_legible_below_the_reference_width(
    part: str, tmp_path: pathlib.Path
) -> None:
    out = str(tmp_path / "narrow.png")
    render(part, out, width=960)
    assert check_image(out) == []


def _segment_hits_box(a: list[float], b: list[float], box: list[float]) -> bool:
    return any(
        box[0] <= a[0] + (b[0] - a[0]) * t / 80 <= box[2]
        and box[1] <= a[1] + (b[1] - a[1]) * t / 80 <= box[3]
        for t in range(81)
    )


def test_no_arrow_strikes_through_another_label(rendered: str) -> None:
    dims = _sidecar(rendered)["dimensions"]
    for dim in dims:
        for other in dims:
            if other is not dim and other["view"] == dim["view"]:
                assert not _segment_hits_box(
                    dim["arrow"][0], dim["arrow"][1], other["box"]
                ), (dim["label"], other["label"])


def test_dimension_label_never_covers_the_bed_caption(
    part: str, tmp_path: pathlib.Path
) -> None:
    out = str(tmp_path / "bed.png")
    data = render(part, out, bed=(220.0, 220.0))
    bed = data["bed"]["box"]
    for dim in data["dimensions"]:
        if dim["view"] == "top":
            box = dim["box"]
            assert not (
                box[0] < bed[2]
                and bed[0] < box[2]
                and box[1] < bed[3]
                and bed[1] < box[3]
            )


def test_small_values_round_consistently(tmp_path: pathlib.Path) -> None:
    """A 4.4496 mm edge prints 4.4 and its sidecar value 4.45 must still count as shown."""
    tiny = trimesh.creation.box(extents=(4.4496, 30.0, 20.0))
    tiny.apply_translation((110, 110, 10))
    src = tmp_path / "tiny.stl"
    tiny.export(src)
    out = str(tmp_path / "tiny.png")
    render(str(src), out, bed=(220.0, 220.0))
    assert check_image(out) == []


def test_text_must_end_with_the_value_not_merely_contain_it(copy: str) -> None:
    data = _sidecar(copy)
    data["dimensions"][0]["value_mm"] = 0.5
    data["dimensions"][0]["text"] = "X 10.5 mm"
    _write_sidecar(copy, data)
    assert any(o.rule == "value-not-shown" for o in check_image(copy))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["dimensions"][0].pop("box"),
        lambda d: d["dimensions"][0].__setitem__("box", [1, 2, 3]),
        lambda d: d["dimensions"].__setitem__(0, "not a dict"),
        lambda d: d.__setitem__("views", []),
        lambda d: d["views"]["top"]["silhouette_grid"].__setitem__("cell", "x"),
    ],
)
def test_malformed_sidecar_is_reported_not_raised(copy: str, mutate: Any) -> None:
    data = _sidecar(copy)
    mutate(data)
    _write_sidecar(copy, data)
    assert [o.rule for o in check_image(copy)] == ["bad-sidecar"]


def test_unknown_view_is_an_offender(copy: str) -> None:
    data = _sidecar(copy)
    data["dimensions"][0]["view"] = "nowhere"
    _write_sidecar(copy, data)
    assert any(o.rule == "unknown-view" for o in check_image(copy))


def test_non_image_file_with_a_sidecar_is_reported(
    tmp_path: pathlib.Path, rendered: str
) -> None:
    fake = tmp_path / "fake.png"
    fake.write_text("not a png")
    pathlib.Path(sidecar_path(str(fake))).write_text(
        pathlib.Path(sidecar_path(rendered)).read_text()
    )
    assert [o.rule for o in check_image(str(fake))] == ["bad-image"]


def test_non_finite_numbers_are_refused_at_the_command(part: str) -> None:
    for flag, value in (
        ("--label-px", "inf"),
        ("--bed", "infx220"),
        ("--dim", "a:nan,0,0:1,1,1"),
        ("--width", "-5"),
    ):
        with pytest.raises(InvalidArgument):
            dimension_run([part, "-o", "x.png", flag, value])


def test_check_rejects_render_flags_and_render_rejects_json(
    part: str, rendered: str
) -> None:
    with pytest.raises(UsageError):
        dimension_run(["--check", rendered, "--bed", "220x220"])
    with pytest.raises(UsageError):
        dimension_run([part, "-o", "x.png", "--json"])


def test_scad_input_renders_through_openscad(tmp_path: pathlib.Path) -> None:
    import shutil

    if shutil.which("openscad") is None:
        pytest.skip("openscad not installed")
    scad = tmp_path / "block.scad"
    scad.write_text("cube([40, 20, 10]);\n")
    out = str(tmp_path / "block.png")
    data = render(str(scad), out)
    assert data["bbox_mm"] == pytest.approx([40.0, 20.0, 10.0], abs=0.01)


def test_missing_openscad_is_a_structured_failure_without_leaking_a_temp_file(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dimension_render import RenderError

    scad = tmp_path / "block.scad"
    scad.write_text("cube(10);\n")
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp_root))
    monkeypatch.setattr("tempfile.tempdir", None)
    monkeypatch.setenv("PATH", str(tmp_path))  # no openscad here
    with pytest.raises(RenderError) as excinfo:
        render(str(scad), str(tmp_path / "x.png"))
    assert excinfo.value.exit_code == 127
    assert list(tmp_root.iterdir()) == []


def test_span_dimension_appears_for_a_slanted_part_and_foreshortened_extras_are_omitted(
    tmp_path: pathlib.Path,
) -> None:
    slab = trimesh.creation.box(extents=(60, 10, 6))
    slab.apply_transform(trimesh.transformations.rotation_matrix(0.6, [0, 0, 1]))
    slab.apply_translation((110, 110, 3))
    src = tmp_path / "slab.stl"
    slab.export(src)
    out = str(tmp_path / "slab.png")
    data = render(str(src), out, dims=["tilted edge:100,100,3:110,100,40"])
    spans = {
        d["view"]: d["value_mm"] for d in data["dimensions"] if d["label"] == "span"
    }
    assert spans["top"] == pytest.approx(60.8, abs=0.5)
    assert "front" not in spans  # the span is foreshortened there
    assert all(
        d["view"] != "top" for d in data["dimensions"] if d["label"] == "tilted edge"
    )
