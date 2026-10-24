"""mix_card.py — the white readout card of a mix tester: where its swatches lie, and the card as a PNG.

WHAT: a printed card (paper, 1:1) the printed swatches are laid on, face up, for a photo. It carries
  four ArUco fiducials (DICT_4X4_50, ids 0-3, 12 mm, the corners of the card), a QR code naming the
  tester manifest (`3dmix:<id>`), blank paper regions for the white balance, and one frame per
  swatch with its number. `layout()` is the geometry of all of it in card millimetres (origin lower
  left, +Y up, as calib_rectify.py takes plate fiducials), stored in the manifest as `readout`;
  `render()` draws the card at `PX_PER_MM` px/mm with the print resolution set in the PNG.
WHY: epic #410 / issue #416: the top camera sees only the top of a standing swatch (one layer), so
  the swatches are taken off the bed and laid on a card whose layout the manifest dictates; the
  fiducials rectify any photo of the card (top camera or phone) to millimetres, and the QR refuses a
  photo of another tester. The reader (slicing/mix_readout.py) takes every box from the manifest.
INVARIANTS: stdlib only; the layout and the PNG depend only on (swatch count, swatch height,
  manifest id), so the same tester always gives the same card; the manifest id is the first 12 hex of
  the sha256 of the manifest without `id`, `readout` and `files`.
"""

from __future__ import annotations

import hashlib
import json
import struct
import zlib
from typing import Any

from printops import qr

SCHEMA = "mix-card/1"
DICTIONARY = "DICT_4X4_50"
PX_PER_MM = 10
PAYLOAD_PREFIX = "3dmix:"

MARKER_MM = 12.0  # outer edge of a fiducial, black border included (6 cells of 2 mm)
MARGIN = 6.0  # paper edge → fiducial
FACE_W = 20.0  # the swatch's wide face
FACE_PAD = 5.0  # block edge → face
CARD_PITCH_X = 30.0
GAP_Y = 2.0  # between blocks
NUMBER_H = 8.0  # seven-segment digit height (the tester's labels)
PATCH_INSET = 1.5  # the reader skips this much of the face's border (at most a quarter of the height)
QR_MODULE = 1.2
QR_QUIET = 4  # modules
HEADER = 50.0  # top of the card → top of the swatch grid
FOOTER = 20.0  # bottom of the card → bottom of the swatch grid
COLUMNS = 3
FRAME_OUT = 1.0  # frame outline around the face
LINE = 0.3  # frame line width

# 6x6 cells of DICT_4X4_50 ids 0-3 (1 = white), border included
MARKERS: dict[int, tuple[str, ...]] = {
    0: ("000000", "010110", "001010", "000110", "000100", "000000"),
    1: ("000000", "000000", "011110", "010010", "010100", "000000"),
    2: ("000000", "000110", "000110", "000100", "011010", "000000"),
    3: ("000000", "010010", "010010", "001000", "001100", "000000"),
}

Box = list[float]


class MixCardError(ValueError):
    """A card cannot be laid out for this tester."""


def manifest_id(manifest: dict[str, Any]) -> str:
    """12 hex naming a tester: its content without `id`, `readout` and `files`."""
    body = {k: v for k, v in manifest.items() if k not in ("id", "readout", "files")}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def payload(mid: str) -> str:
    return PAYLOAD_PREFIX + mid


def _box(x0: float, y0: float, x1: float, y1: float) -> Box:
    return [round(v, 3) for v in (x0, y0, x1, y1)]


def layout(count: int, face_h: float, mid: str) -> dict[str, Any]:
    """The card of `count` swatches whose faces are FACE_W × `face_h` mm, for the tester `mid`."""
    if count < 1:
        raise MixCardError("a card needs at least one swatch")
    if not 1.0 <= face_h <= 100.0:
        raise MixCardError(f"swatch height {face_h!r} mm is outside 1..100")
    from slicing.mix_tester import DIGIT_H

    rows = (count + COLUMNS - 1) // COLUMNS
    pitch_y = DIGIT_H + 3.0 + face_h + 3.0 + GAP_Y
    width = (
        COLUMNS * CARD_PITCH_X + 2 * MARGIN
    )  # always three columns wide: the QR and the paper need the room
    height = HEADER + rows * pitch_y + FOOTER
    half = MARKER_MM / 2
    fid_xy = [
        (MARGIN + half, MARGIN + half),
        (width - MARGIN - half, MARGIN + half),
        (width - MARGIN - half, height - MARGIN - half),
        (MARGIN + half, height - MARGIN - half),
    ]
    fiducials = [
        {"id": i, "center_mm": [round(x, 3), round(y, 3)], "size_mm": MARKER_MM}
        for i, (x, y) in enumerate(fid_xy)
    ]
    code = qr.encode(payload(mid), ecc="L")
    qr_mm = code.size * QR_MODULE
    qr_x0 = MARGIN + MARKER_MM + 3.0 + QR_QUIET * QR_MODULE
    qr_y1 = height - MARGIN - QR_QUIET * QR_MODULE
    qr_box = _box(qr_x0, qr_y1 - qr_mm, qr_x0 + qr_mm, qr_y1)
    right = qr_x0 + qr_mm + QR_QUIET * QR_MODULE + 4.0
    white = [
        # beside the QR, under the top right fiducial, between the bottom fiducials
        _box(right, qr_y1 - qr_mm + 2, width - MARGIN - MARKER_MM - 3.0, qr_y1 - 4),
        _box(
            MARGIN + MARKER_MM + 4.0,
            MARGIN + 1.5,
            width / 2 - 3.0,
            MARGIN + MARKER_MM - 1.5,
        ),
        _box(
            width / 2 + 3.0,
            MARGIN + 1.5,
            width - MARGIN - MARKER_MM - 4.0,
            MARGIN + MARKER_MM - 1.5,
        ),
    ]
    cells = []
    for i in range(count):
        r, c = divmod(i, COLUMNS)
        bx = MARGIN + c * CARD_PITCH_X
        top = height - HEADER - r * pitch_y
        fy1 = top - DIGIT_H - 3.0
        face = _box(bx + FACE_PAD, fy1 - face_h, bx + FACE_PAD + FACE_W, fy1)
        inset = min(
            PATCH_INSET, face_h / 4
        )  # a short face keeps a patch half its height
        patch = _box(face[0] + inset, face[1] + inset, face[2] - inset, face[3] - inset)
        cells.append(
            {
                "number": i + 1,
                "face_box": face,
                "patch_box": patch,
                "number_origin": [round(bx + 2.0, 3), round(top - DIGIT_H - 1.0, 3)],
            }
        )
    return {
        "schema": SCHEMA,
        "card_mm": [round(width, 3), round(height, 3)],
        "px_per_mm": PX_PER_MM,
        "patch_inset_mm": round(min(PATCH_INSET, face_h / 4), 3),
        "dictionary": DICTIONARY,
        "fiducials": fiducials,
        "qr": {"payload": payload(mid), "box_mm": qr_box, "modules": code.size},
        "white_boxes": white,
        "cells": cells,
    }


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_box(b: Any) -> bool:
    return (
        isinstance(b, list)
        and len(b) == 4
        and all(_num(v) for v in b)
        and b[0] < b[2]
        and b[1] < b[3]
    )


def _inside(b: list[float], w: float, h: float) -> bool:
    return b[0] >= 0 and b[2] <= w and b[1] >= 0 and b[3] <= h


def validate_readout(r: Any, count: int, mid: str) -> list[str]:
    """Problems with a manifest's `readout` section ([] = valid)."""
    if not isinstance(r, dict):
        return ["readout is not an object"]
    errs: list[str] = []
    if r.get("schema") != SCHEMA:
        errs.append(f"readout.schema must be {SCHEMA}")
    size = r.get("card_mm")
    if not (
        isinstance(size, list)
        and len(size) == 2
        and all(_num(v) and v > 0 for v in size)
    ):
        errs.append("readout.card_mm must be [width, height]")
        return errs
    w, h = size
    if not (_num(r.get("px_per_mm")) and r["px_per_mm"] > 0):
        errs.append("readout.px_per_mm must be a positive number")
    if r.get("dictionary") != DICTIONARY:
        errs.append(f"readout.dictionary must be {DICTIONARY}")
    fids = r.get("fiducials")
    if not (isinstance(fids, list) and len(fids) >= 3):
        errs.append("readout.fiducials needs at least 3 markers")
    else:
        for f in fids:
            ok = (
                isinstance(f, dict)
                and f.get("id") in MARKERS
                and isinstance(f.get("center_mm"), list)
                and len(f["center_mm"]) == 2
                and all(_num(v) for v in f["center_mm"])
                and _num(f.get("size_mm"))
                and f["size_mm"] > 0
            )
            if not ok:
                errs.append(
                    f"readout.fiducials entry {f!r} must be {{id (0-3), center_mm, size_mm}}"
                )
            elif not _inside(
                [
                    f["center_mm"][0] - f["size_mm"] / 2,
                    f["center_mm"][1] - f["size_mm"] / 2,
                    f["center_mm"][0] + f["size_mm"] / 2,
                    f["center_mm"][1] + f["size_mm"] / 2,
                ],
                w,
                h,
            ):
                errs.append(f"readout.fiducials entry {f['id']} lies outside the card")
    q = r.get("qr")
    if not (
        isinstance(q, dict)
        and q.get("payload") == payload(mid)
        and _is_box(q.get("box_mm"))
    ):
        errs.append(f"readout.qr must carry the payload {payload(mid)!r} and a box_mm")
    elif not _inside(q["box_mm"], w, h):
        errs.append("readout.qr lies outside the card")
    white = r.get("white_boxes")
    if not (isinstance(white, list) and white and all(_is_box(b) for b in white)):
        errs.append("readout.white_boxes must be a list of [x0, y0, x1, y1]")
    elif not all(_inside(b, w, h) for b in white):
        errs.append("readout.white_boxes lie outside the card")
    cells = r.get("cells")
    if not (isinstance(cells, list) and len(cells) == count):
        errs.append(f"readout.cells must list the {count} swatches")
    else:
        for i, c in enumerate(cells):
            if not (
                isinstance(c, dict)
                and c.get("number") == i + 1
                and _is_box(c.get("face_box"))
                and _is_box(c.get("patch_box"))
            ):
                errs.append(
                    f"readout.cells[{i}] must be {{number: {i + 1}, face_box, patch_box}}"
                )
            else:
                f, p = c["face_box"], c["patch_box"]
                if not (
                    _inside(f, w, h)
                    and f[0] <= p[0]
                    and p[2] <= f[2]
                    and f[1] <= p[1]
                    and p[3] <= f[3]
                ):
                    errs.append(
                        f"readout.cells[{i}]: the patch must lie inside the face inside the card"
                    )
    return errs


# -- the PNG ----------------------------------------------------------------------------------


def _png(width: int, height: int, rgb: bytearray, dpm: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    stride = width * 3
    raw = b"".join(
        b"\x00" + bytes(rgb[y * stride : (y + 1) * stride]) for y in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(
            b"pHYs", struct.pack(">IIB", dpm, dpm, 1)
        )  # pixels per metre: printed at 1:1
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def render(lay: dict[str, Any]) -> bytes:
    """The card as a PNG: 10 px/mm, 1 px = 0.1 mm, print it at 100 % (no fit-to-page)."""
    from slicing.mix_tester import digit_rects

    ppm = int(lay["px_per_mm"])
    w_mm, h_mm = lay["card_mm"]
    width, height = round(w_mm * ppm), round(h_mm * ppm)
    img = bytearray(b"\xff" * (width * height * 3))

    def fill(x0: float, y0: float, x1: float, y1: float, grey: int) -> None:
        """Paint a card-mm box (Y up) in a grey level."""
        px0, px1 = max(0, round(x0 * ppm)), min(width, round(x1 * ppm))
        py0, py1 = (
            max(0, round((h_mm - y1) * ppm)),
            min(height, round((h_mm - y0) * ppm)),
        )
        if px1 <= px0:
            return
        row = bytes([grey]) * (3 * (px1 - px0))
        for y in range(py0, py1):
            start = (y * width + px0) * 3
            img[start : start + len(row)] = row

    cell = MARKER_MM / 6
    for f in lay["fiducials"]:
        cx, cy = f["center_mm"]
        x0, y0 = cx - MARKER_MM / 2, cy - MARKER_MM / 2
        bits = MARKERS[f["id"]]
        for r, row in enumerate(bits):  # r = 0 is the top row of the marker
            for c, bit in enumerate(row):
                fill(
                    x0 + c * cell,
                    y0 + (5 - r) * cell,
                    x0 + (c + 1) * cell,
                    y0 + (6 - r) * cell,
                    255 if bit == "1" else 0,
                )
    code = qr.encode(lay["qr"]["payload"], ecc="L")
    qx0, _, _, qy1 = lay["qr"]["box_mm"]
    for r, modules in enumerate(code.modules):
        for c, dark in enumerate(modules):
            if dark:
                fill(
                    qx0 + c * QR_MODULE,
                    qy1 - (r + 1) * QR_MODULE,
                    qx0 + (c + 1) * QR_MODULE,
                    qy1 - r * QR_MODULE,
                    0,
                )
    for cl in lay["cells"]:
        x0, y0, x1, y1 = cl["face_box"]
        o = FRAME_OUT
        for a, b, c, d in (
            (x0 - o, y1 + o - LINE, x1 + o, y1 + o),
            (x0 - o, y0 - o, x1 + o, y0 - o + LINE),
            (x0 - o, y0 - o, x0 - o + LINE, y1 + o),
            (x1 + o - LINE, y0 - o, x1 + o, y1 + o),
        ):
            fill(a, b, c, d, 90)
        ox, oy = cl["number_origin"]
        for sx0, sy0, sx1, sy1 in digit_rects(str(cl["number"])):
            fill(ox + sx0, oy + sy0, ox + sx1, oy + sy1, 0)
    return _png(width, height, img, round(ppm * 1000))
