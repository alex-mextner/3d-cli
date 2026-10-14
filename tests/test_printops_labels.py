"""PrintOps location labels (#159): the stdlib QR encoder, PDF/SVG sheets, NTAG213 NDEF image."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import pytest

from printops import labels, qr
from printops.labels import Label, Sheet


# ------------------------------------------------------------------------------------ QR
def test_reed_solomon_matches_the_iso_18004_annex_example() -> None:
    """ISO/IEC 18004 Annex I: "01234567" at 1-M → these 16 data and 10 ECC codewords."""
    data = [0x10, 0x20, 0x0C, 0x56, 0x61, 0x80, 0xEC, 0x11, 0xEC, 0x11, 0xEC, 0x11, 0xEC, 0x11, 0xEC, 0x11]
    assert qr.rs_remainder(data, qr._rs_divisor(10)) == [0xA5, 0x24, 0xD4, 0xC1, 0xED, 0x36, 0xC7, 0x87, 0x2C, 0x55]


@pytest.mark.parametrize(("ver", "ecc", "cap"), [(1, "M", 16), (1, "L", 19), (5, "M", 86), (7, "H", 66), (10, "M", 216)])
def test_data_capacity_matches_the_spec_tables(ver: int, ecc: str, cap: int) -> None:
    assert qr.data_capacity(ver, ecc) == cap


def test_format_bits_and_fixed_patterns() -> None:
    code = qr.encode("cell:WH:A/S2", ecc="M")
    m, s = code.modules, code.size
    assert s == 17 + 4 * code.version
    for r0, c0 in ((0, 0), (0, s - 7), (s - 7, 0)):  # finder patterns: dark ring, light ring, 3×3 core
        assert all(m[r0][c0 + i] and m[r0 + 6][c0 + i] for i in range(7))
        assert not m[r0 + 1][c0 + 1] and m[r0 + 3][c0 + 3]
    assert m[s - 8][8]  # the always-dark module
    data = 0b00 << 3 | code.mask  # M = 00
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    bits = (data << 10 | rem) ^ 0x5412
    assert [m[i][8] for i in range(6)] == [(bits >> i) & 1 == 1 for i in range(6)]


def test_encode_picks_the_smallest_version_and_refuses_overflow() -> None:
    assert qr.encode("dryer:1").version == 1
    assert qr.encode("cell:WH:" + "A" * 61).version == 5  # the longest label payload: 69 bytes
    with pytest.raises(qr.QrError, match="do not fit"):
        qr.encode("x" * 300)
    with pytest.raises(qr.QrError, match="ECC level"):
        qr.encode("x", ecc="Z")


@pytest.mark.parametrize("payload", ["cell:WH:default", "slot:u1/T3", "dryer:1", "cell:WH:" + "A" * 61,
                                     "cell:W:abcdefg/abcdefg/abcdefg/abcdefg/abcdefg/abcdefg/abcdefg/a"])
def test_codes_decode_with_an_independent_reader(payload: str) -> None:
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    for ecc in "LMQH":
        m = np.array(qr.encode(payload, ecc=ecc).modules, dtype=np.uint8)
        img = np.pad((1 - m) * 255, 4, constant_values=255).astype(np.uint8)
        img = cv2.resize(img, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
        text, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
        assert text == payload, ecc


# --------------------------------------------------------------------------------- sheets
def _labels(n: int) -> list[Label]:
    return [Label(i + 1, f"WH:A/S{i // 10}/B{i % 10}", "box", f"cell:WH:A/S{i // 10}/B{i % 10}") for i in range(n)]


def test_wrap_breaks_after_slashes_and_hard_splits_long_segments() -> None:
    assert labels._wrap_path("WH:A/S2/B3/04", 8) == ["WH:A/S2/", "B3/04"]
    assert labels._wrap_path("WH:" + "x" * 20, 8) == ["WH:xxxxx", "xxxxxxxx", "xxxxxxx"]


def test_pdf_is_well_formed_with_one_page_per_sheet() -> None:
    pdf = labels.render_pdf(_labels(30), Sheet(start=2))  # 2 skipped + 30 labels = 32 cells → 2 pages
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF")
    assert b"/Count 2" in pdf
    xref = int(re.search(rb"startxref\n(\d+)", pdf).group(1))  # type: ignore[union-attr]
    assert pdf[xref:xref + 4] == b"xref"
    offsets = [int(x) for x in re.findall(rb"(\d{10}) 00000 n", pdf)]
    for n, off in enumerate(offsets, start=1):
        assert pdf[off:].startswith(f"{n} 0 obj".encode())
    for m in re.finditer(rb"<< /Length (\d+) >>\nstream\n", pdf):
        end = m.end() + int(m.group(1))
        assert pdf[end:end + 10] == b"\nendstream"
    assert b"(WH:A/S2/B9) Tj" in pdf and b"(box  loc-30) Tj" in pdf


def test_svg_pages_parse_and_carry_text_and_qr() -> None:
    pages = labels.render_svg_pages(_labels(25), Sheet(cols=2, rows=4, page="letter", margin_mm=5, border=False))
    assert len(pages) == 4
    root = ET.fromstring(pages[0])
    assert root.get("width") == "215.9mm"
    texts = [t.text for t in root.iter("{http://www.w3.org/2000/svg}text")]
    assert "WH:A/S0/B0" in texts and "box  loc-1" in texts
    assert len(list(root.iter("{http://www.w3.org/2000/svg}path"))) == 8
    assert "stroke=\"#ccc\"" not in pages[0]


@pytest.mark.parametrize(("sheet", "match"), [
    (Sheet(page="a3"), "page"), (Sheet(cols=0), "grid"), (Sheet(start=24), "start"),
    (Sheet(cols=10, rows=30), "too small"), (Sheet(margin_mm=80), "margin"),
])
def test_bad_sheets_are_refused(sheet: Sheet, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        sheet.validate()


def test_long_paths_fit_their_label() -> None:
    path = "W:" + "/".join(["abcdefg"] * 7) + "/a"
    (page,) = labels.layout([Label(1, path, "slot", "cell:" + path)], Sheet())
    (placed,) = page
    path_lines = [t for t in placed.texts if t.mono]
    assert "".join(t.text for t in path_lines) == path
    for t in path_lines:
        assert t.x + len(t.text) * t.size_pt * labels.COURIER_ADVANCE / labels.PT_PER_MM <= placed.x + placed.w
        assert t.y <= placed.y + placed.h
    assert placed.qr_x > placed.x and placed.module * placed.code.size < placed.h


# ---------------------------------------------------------------------------------- NTAG
def test_ntag213_image_holds_ha_tag_uri_and_payload_text() -> None:
    path = "W:" + "/".join(["abcdefg"] * 7) + "/a"
    lb = Label(123456, path, "slot", "cell:" + path)
    image = labels.ntag213_ndef(lb)
    assert len(image) <= labels.NTAG213_USER_BYTES
    assert image[0] == 0x03 and image[1] == len(image) - 3 and image[-1] == 0xFE
    msg = image[2:-1]
    # record 1: MB, SR, TNF=1, type "U", prefix 0x04 = https://
    assert msg[0] == 0x91 and msg[3:4] == b"U" and msg[4] == 0x04
    uri_len = msg[2]
    assert msg[5:4 + uri_len] == b"www.home-assistant.io/tag/loc-123456"
    rec2 = msg[4 + uri_len:]
    assert rec2[0] == 0x51 and rec2[3:4] == b"T" and rec2[4:7] == b"\x02en"
    assert rec2[7:].decode() == "cell:" + path
