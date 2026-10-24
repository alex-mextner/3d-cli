"""`3d mix target` (#415): the target colour of a region of a mockup photo or render."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from printops.color import hex_delta_e, parse_hex, srgb_to_lab
from slicing.mix_target import (
    Box,
    MixTargetError,
    decode_srgb,
    lab_of_linear,
    lab_to_hex,
    parse_box,
    SOURCE_KEYS,
    read_target,
    source_of,
    validate_source,
)

from mix_target_fixtures import PATCH, WHITE, photo

PATCH_BOX = Box(*PATCH)
WHITE_BOX = Box(*WHITE)
PATCHES = ["#B5654F", "#1B5E20", "#F6C700", "#B0284F", "#808080", "#3A4F7A"]
CASTS = [
    (1.0, 0.82, 0.55),  # tungsten
    (0.70, 0.90, 1.0),  # cold daylight in shade
    (0.9, 0.9, 0.9),  # exposure only
    (1.0, 1.0, 1.0),
]


@pytest.mark.parametrize("patch", PATCHES)
@pytest.mark.parametrize("cast", CASTS)
def test_known_patch_under_a_colour_cast_is_within_de1_after_white_balance(
    tmp_path: Path, patch: str, cast: tuple[float, float, float]
) -> None:
    img = photo(tmp_path / "scene.png", patch, cast)
    reading = read_target(img, PATCH_BOX, WHITE_BOX)
    assert hex_delta_e(reading.target, patch) < 1.0, (patch, cast, reading.target)


def test_without_white_balance_the_same_cast_is_far_off(tmp_path: Path) -> None:
    """The balance is what brings the target in: a render read on a cast photo misses by ΔE 4 and more."""
    img = photo(tmp_path / "scene.png", "#B5654F", (1.0, 0.82, 0.55))
    assert hex_delta_e(read_target(img, PATCH_BOX, None).target, "#B5654F") > 4


def test_jpeg_compression_stays_within_de1(tmp_path: Path) -> None:
    img = photo(tmp_path / "scene.jpg", "#B5654F", (1.0, 0.82, 0.55))
    assert hex_delta_e(read_target(img, PATCH_BOX, WHITE_BOX).target, "#B5654F") < 1.0


def test_the_median_ignores_a_shadow_band_and_a_glint_inside_the_box(
    tmp_path: Path,
) -> None:
    """The fixture puts a dark band over 7 % of the box and a white glint over 1 %."""
    img = photo(tmp_path / "scene.png", "#1B5E20", (1.0, 0.9, 0.7))
    inner = read_target(img, Box(150, 100, 210, 160), WHITE_BOX)
    whole = read_target(img, PATCH_BOX, WHITE_BOX)
    assert hex_delta_e(whole.target, inner.target) < 1.0


def test_render_without_balance_reads_the_pixels(tmp_path: Path) -> None:
    path = tmp_path / "render.png"
    Image.new("RGB", (40, 40), (0xB5, 0x65, 0x4F)).save(path)
    r = read_target(path, Box(5, 5, 30, 30), None)
    assert r.target == "#B5654F"
    assert r.kind == "render"
    assert r.white_box is None and r.gain is None and r.white_level is None
    assert r.samples == 25 * 25


def test_reading_records_the_image_the_boxes_and_the_sha256(tmp_path: Path) -> None:
    img = photo(tmp_path / "scene.png", "#B5654F", (1.0, 0.82, 0.55))
    r = read_target(img, PATCH_BOX, WHITE_BOX)
    d = r.to_dict()
    assert d["schema"] == "mix-target/1"
    assert d["sha256"] == hashlib.sha256(img.read_bytes()).hexdigest()
    assert d["box"] == list(PATCH) and d["white_box"] == list(WHITE)
    assert d["image"] == str(img) and d["image_size"] == [320, 240]
    assert d["white_balance"] is True and d["kind"] == "photo"
    assert d["white_rgb"] is not None and d["gain"] is not None
    assert validate_source(source_of(d)) == []
    assert "target" not in source_of(d)


def test_exif_orientation_is_applied_before_the_box(tmp_path: Path) -> None:
    """A phone stores the sensor's rows and an orientation tag: boxes are in the upright image."""
    path = tmp_path / "rot.jpg"
    im = Image.new("RGB", (60, 40), (0, 0, 0))
    im.paste((0xB5, 0x65, 0x4F), (0, 0, 20, 20))  # top-left of the stored image
    exif = Image.Exif()
    exif[0x0112] = (
        6  # stored rotated: shown turned 90° clockwise, the top-left lands top-right
    )
    im.save(path, quality=100, subsampling=0, exif=exif)
    # upright image is 40 x 60; the coloured square is now at x 20..40, y 0..20
    r = read_target(path, Box(22, 2, 38, 18), None)
    assert hex_delta_e(r.target, "#B5654F") < 2


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("1,2,3", "four"),
        ("a,b,c,d", "four"),
        ("10,10,5,20", "x0 < x1"),
        ("-1,0,5,5", "0 <= x0"),
        ("0,0,0,5", "x0 < x1"),
    ],
)
def test_box_syntax_is_checked(text: str, why: str) -> None:
    with pytest.raises(MixTargetError, match=why):
        parse_box(text, "--box")


def test_a_box_outside_the_image_is_refused_not_clipped(tmp_path: Path) -> None:
    img = photo(tmp_path / "scene.png", "#B5654F")
    with pytest.raises(MixTargetError, match="outside the 320x240 image"):
        read_target(img, Box(300, 100, 400, 200), WHITE_BOX)
    with pytest.raises(MixTargetError, match="--white .* outside"):
        read_target(img, PATCH_BOX, Box(0, 0, 500, 10))


def test_a_dark_box_is_not_a_white_reference(tmp_path: Path) -> None:
    path = tmp_path / "dark.png"
    Image.new("RGB", (40, 40), (20, 30, 30)).save(path)
    with pytest.raises(MixTargetError, match="white reference"):
        read_target(path, Box(0, 0, 10, 10), Box(20, 20, 30, 30))


def test_not_an_image_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "x.png"
    path.write_text("not a picture")
    with pytest.raises(MixTargetError, match="cannot be read as an image"):
        read_target(path, Box(0, 0, 1, 1), None)


def test_a_huge_box_is_read_with_a_fixed_stride(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import slicing.mix_target as mt

    monkeypatch.setattr(mt, "MAX_SAMPLES", 100)
    path = tmp_path / "flat.png"
    Image.new("RGB", (40, 40), (0x80, 0x80, 0x80)).save(path)
    r = read_target(path, Box(0, 0, 40, 40), None)
    assert r.samples <= 100 and r.target == "#808080"


def test_lab_helpers_agree_with_the_colour_module() -> None:
    for h in ("#000000", "#FFFFFF", "#B5654F", "#1B5E20", "#F6C700", "#3A4F7A"):
        rgb = np.array([[int(h[i : i + 2], 16) / 255 for i in (1, 3, 5)]])
        assert tuple(lab_of_linear(decode_srgb(rgb))[0]) == pytest.approx(
            srgb_to_lab(h), abs=1e-6
        )
        assert lab_to_hex(srgb_to_lab(h)) == parse_hex(h)


def test_validate_source_reports_each_broken_part() -> None:
    photo_src = {
        "kind": "photo",
        "image": "m.png",
        "sha256": "a" * 64,
        "image_size": [320, 240],
        "box": [0, 0, 5, 5],
        "white_balance": True,
        "white_box": [1, 1, 4, 4],
        "white_level": 235,
    }
    render_src = {
        **photo_src,
        "kind": "render",
        "white_balance": False,
        "white_box": None,
        "white_level": None,
    }
    assert validate_source(photo_src) == []
    assert validate_source(render_src) == []
    assert validate_source("x") == ["target_source is not an object"]
    assert validate_source({}) == [f"target_source.{k} is missing" for k in SOURCE_KEYS]
    broken = {
        "sha256": "zz",
        "image": 3,
        "kind": "scan",
        "image_size": [320],
        "box": [1, 2],
        "white_box": [1],
        "white_balance": False,
        "white_level": 235,
    }
    errors = validate_source({**photo_src, **broken})
    for fragment in (
        "sha256",
        "image is not a path",
        "kind must be",
        "image_size",
        ".box",
        "white_box must be",
        "exactly when",
    ):
        assert any(fragment in e for e in errors), (fragment, errors)
    assert validate_source({**photo_src, "white_balance": False}) != []
    assert validate_source({**photo_src, "kind": "render"}) != []


def test_the_recorded_sha256_is_of_the_bytes_that_were_decoded(tmp_path: Path) -> None:
    """One read feeds both the decoder and the hash: a file changed after the read cannot split them."""
    path = tmp_path / "flat.png"
    Image.new("RGB", (40, 40), (0x80, 0x80, 0x80)).save(path)
    first = path.read_bytes()
    r = read_target(path, Box(0, 0, 10, 10), None)
    assert r.sha256 == hashlib.sha256(first).hexdigest()
    Image.new("RGB", (40, 40), (0x10, 0x20, 0x30)).save(path)
    again = read_target(path, Box(0, 0, 10, 10), None)
    assert again.sha256 != r.sha256 and again.target == "#102030"


def test_an_embedded_icc_profile_is_converted_to_srgb(tmp_path: Path) -> None:
    """A tagged photo is read in sRGB: an sRGB profile changes nothing, a profile that is not valid is refused."""
    from PIL import ImageCms

    path = tmp_path / "tagged.png"
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    Image.new("RGB", (30, 30), (0xB5, 0x65, 0x4F)).save(path, icc_profile=profile)
    assert read_target(path, Box(0, 0, 20, 20), None).target == "#B5654F"
    path.write_bytes(b"")
    Image.new("RGB", (30, 30), (0xB5, 0x65, 0x4F)).save(
        path, icc_profile=b"not a profile"
    )
    with pytest.raises(MixTargetError, match="colour profile"):
        read_target(path, Box(0, 0, 20, 20), None)


def test_dash_leading_values_reach_the_reader_intact(tmp_path: Path) -> None:
    """`--box -1,0,5,5` and an image called -x.png go through the reader's argparse as values."""
    import subprocess

    from cli.pyrun import tool_argv

    path = tmp_path / "-x.png"
    Image.new("RGB", (30, 30), (0x80, 0x80, 0x80)).save(path)
    argv = tool_argv(
        "numpy,pillow",
        "mix_target_cli.py",
        ["--box=-1,0,5,5", "--white-level=235", "--", str(path)],
    )
    run = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert run.returncode == 2 and "0 <= x0" in run.stdout
    ok = subprocess.run(
        tool_argv("numpy,pillow", "mix_target_cli.py", ["--box=0,0,5,5", "--", str(path)]),
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert ok.returncode == 0 and '"target": "#808080"' in ok.stdout
