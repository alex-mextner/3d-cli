"""`prep.gcode_view`: PNG proof renders from a parsed G-code and the layer histogram data."""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from errors import InvalidArgument
from prep.gcode_gates import parse
from prep.gcode_view import TOP_SCALE, layer_histogram, render_layers
from prep_gcode_fixtures import write_gcode

COLOURS = {0: "#ff0000", 1: "#00ff00", 2: "#0000ff"}
RGB = {0: (255, 0, 0), 1: (0, 255, 0), 2: (0, 0, 255)}


def read_png(path: Path) -> NDArray[np.uint8]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, w, h = 8, b"", 0, 0
    while pos < len(data):
        (n,) = struct.unpack(">I", data[pos : pos + 4])
        tag, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + n]
        assert struct.unpack(">I", data[pos + 8 + n : pos + 12 + n])[0] == zlib.crc32(
            tag + body
        )
        if tag == b"IHDR":
            w, h = struct.unpack(">II", body[:8])
        elif tag == b"IDAT":
            idat += body
        pos += 12 + n
    raw = np.frombuffer(zlib.decompress(idat), dtype=np.uint8).reshape(h, 1 + 3 * w)
    assert (raw[:, 0] == 0).all()
    return raw[:, 1:].reshape(h, w, 3)


def colour_rows(img: NDArray[np.uint8], rgb: tuple[int, int, int]) -> NDArray[np.intp]:
    return np.flatnonzero(
        (img == np.array(rgb, dtype=np.uint8)).all(axis=2).any(axis=1)
    )


# bottom third tool 0, middle tool 1, top tool 2; two tool changes inside layers 7/8 and 15/16
LAYERS = [(0.2, 0)] * 8 + [(0.2, 1)] * 8 + [(0.2, 2)] * 8


@pytest.fixture()
def parsed(tmp_path: Path):  # type: ignore[no-untyped-def]
    return parse(write_gcode(tmp_path / "v.gcode", LAYERS, tower=True))


def test_all_views_write_valid_non_blank_pngs(tmp_path: Path, parsed) -> None:  # type: ignore[no-untyped-def]
    paths = render_layers(parsed, tmp_path / "out", colours=COLOURS)
    assert [p.name for p in paths] == ["v_front.png", "v_top.png", "v_closeup.png"]
    for p in paths:
        img = read_png(p)
        assert (
            len(np.unique(img.reshape(-1, 3), axis=0)) >= 3
        )  # background + at least two more colours


def test_front_view_stacks_tool_colours_bottom_to_top(tmp_path: Path, parsed) -> None:  # type: ignore[no-untyped-def]
    (front,) = render_layers(parsed, tmp_path, views=("front",), colours=COLOURS)
    img = read_png(front)
    r0, r1, r2 = (colour_rows(img, RGB[t]) for t in (0, 1, 2))
    assert len(r0) and len(r1) and len(r2)
    # row 0 is the top of the model: tool 2 (printed last) is highest, tool 0 lowest, no overlap
    assert r2.max() < r1.min() and r1.max() < r0.min()
    # three equal thirds of the height: the bands have about the same thickness
    assert abs(len(r0) - len(r1)) <= 3 and abs(len(r1) - len(r2)) <= 3
    # the model is 40 mm wide: its bands span most of the image width
    cols = np.flatnonzero(
        (img == np.array(RGB[1], dtype=np.uint8)).all(axis=2).any(axis=0)
    )
    assert (cols.max() - cols.min()) / img.shape[1] > 0.8


def test_front_view_changes_when_a_layer_changes_tool(tmp_path: Path) -> None:
    swapped = [*LAYERS[:4], (0.2, 2), *LAYERS[5:]]
    a = read_png(
        render_layers(
            parse(write_gcode(tmp_path / "a.gcode", LAYERS)),
            tmp_path,
            views=("front",),
            colours=COLOURS,
        )[0]
    )
    b = read_png(
        render_layers(
            parse(write_gcode(tmp_path / "b.gcode", swapped)),
            tmp_path,
            views=("front",),
            colours=COLOURS,
        )[0]
    )
    assert a.shape == b.shape and (a != b).any()
    assert len(colour_rows(b, RGB[2])) > len(colour_rows(a, RGB[2]))


def test_top_view_colours_at_expected_pixels(tmp_path: Path, parsed) -> None:  # type: ignore[no-untyped-def]
    (top,) = render_layers(parsed, tmp_path, views=("top",), colours=COLOURS)
    img = read_png(top)
    assert img.shape == (594, 594, 3)  # 270 mm * 2.2 px/mm
    h = img.shape[0]

    def px(x: float, y: float) -> tuple[int, ...]:
        return tuple(
            int(v) for v in img[h - 1 - int(y * TOP_SCALE), int(x * TOP_SCALE)]
        )

    assert (
        px(155.0, 135.0) == RGB[2]
    )  # outer wall at (cx + r, cy), topmost layer printed with tool 2 wins
    assert px(60.0, 60.0) == (235, 235, 235)  # empty plate
    # prime tower square (20..30) is the tool colour darkened to 70 %, the last layer's tool is 2 -> (0,0,178)
    assert px(25.0, 20.0) == (0, 0, 178)
    assert px(0.2, 100.0) == (0, 0, 0)  # bed outline


def test_closeup_is_centred_on_the_zone_with_most_tool_changes(
    tmp_path: Path, parsed
) -> None:  # type: ignore[no-untyped-def]
    (c,) = render_layers(parsed, tmp_path, views=("closeup",), colours=COLOURS)
    img = read_png(c)
    assert img.shape[1] == 900
    # 24 layers fit the auto window: the image shows all three tools
    assert all(len(colour_rows(img, RGB[t])) for t in (0, 1, 2))
    (z,) = render_layers(
        parsed,
        tmp_path / "z",
        views=("closeup",),
        colours=COLOURS,
        zoom=(135.0, 0.8, 2.0),
    )
    zi = read_png(z)
    assert len(colour_rows(zi, RGB[0])) and not len(
        colour_rows(zi, RGB[2])
    )  # window z -0.3..1.9 mm: tools 0 and 1 only (tool 2 starts at 3.2)


def test_unknown_tool_draws_grey_and_bad_input_raises(tmp_path: Path, parsed) -> None:  # type: ignore[no-untyped-def]
    (front,) = render_layers(parsed, tmp_path, views=("front",), colours={0: "#ff0000"})
    img = read_png(front)
    assert len(colour_rows(img, (120, 120, 120)))
    with pytest.raises(InvalidArgument):
        render_layers(parsed, tmp_path, views=("side",), colours=COLOURS)
    with pytest.raises(InvalidArgument):
        render_layers(parsed, tmp_path, colours={0: "red"})
    empty = tmp_path / "e.gcode"
    empty.write_text(";LAYER_CHANGE\n;Z:0.2\n;HEIGHT:0.2\n")
    with pytest.raises(InvalidArgument):
        render_layers(parse(empty), tmp_path, views=("front",), colours=COLOURS)


def test_layer_histogram_counts_heights_and_tool_changes(tmp_path: Path) -> None:
    layers = [(0.2, 0), (0.08, 0), (0.08, 1), (0.12, 1), (0.12, 0), (0.32, 0)]
    hist = layer_histogram(parse(write_gcode(tmp_path / "h.gcode", layers)))
    assert json.loads(json.dumps(hist)) == hist  # plain JSON types
    assert hist["layers"] == 6 and hist["first_layer_h"] == 0.2
    assert hist["by_height"] == {
        "0.08": 2,
        "0.12": 2,
        "0.32": 1,
    }  # first layer excluded
    assert hist["height_mm_by_bin"]["0.08"] == pytest.approx(0.16)
    assert (hist["min"], hist["max"], hist["median"]) == (0.08, 0.32, 0.12)
    assert hist["layers_per_tool"] == {"0": 4, "1": 2}
    # the Tn line of a layer only counts when it switches tool is NOT assumed: every layer writes its Tn
    assert hist["tool_changes_per_layer"] == [1, 1, 1, 1, 1, 1]
    assert hist["tool_changes"] == 2
    assert hist["z"][-1] == pytest.approx(0.2 + 0.08 + 0.08 + 0.12 + 0.12 + 0.32)
