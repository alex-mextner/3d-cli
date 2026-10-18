"""labels.py — printable location labels (QR + text) as PDF/SVG sheets, and the NTAG213 payload.

WHAT: `render_pdf(labels, sheet)` / `render_svg_pages(labels, sheet)` lay labels out on a grid
  sheet (default A4, 3 × 8 = 70 × 37 mm, the common sticker sheet). Each label: a QR code of
  the location's payload (decision D8: `cell:<path>`, or the path itself for `dryer:` and
  `slot:` locations), the path (wrapped at `/`), its kind and `loc-<id>`.
  `ntag213_ndef(label)` is the byte image for the cell's NTAG213 (writing tags is #175):
    TLV 0x03 · NDEF message:
      record 1  URI  https://www.home-assistant.io/tag/loc-<id>  (HA Companion fires
                tag_scanned with tag_id `loc-<id>`; the id survives moves/renames)
      record 2  Text (en, UTF-8)  <QR payload>  (what the ESPHome station / our reader parses)
    · 0xFE terminator; ≤ 144 bytes (NTAG213 user memory) for any 64-char path.

WHY: stdlib only (PrintOps contract): the PDF is written by hand (Courier/Helvetica base-14
  fonts, filled rectangles for QR modules), QR codes come from printops.qr.

INVARIANTS: paths are ASCII by grammar (printops.locations), so base-14 fonts cover all text.
  Geometry is in millimetres; PDF converts to points (72/25.4).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from printops import qr

PT_PER_MM = 72 / 25.4
COURIER_ADVANCE = 0.6  # em; every Courier glyph is 600/1000 wide
PAGES = {"a4": (210.0, 297.0), "letter": (215.9, 279.4)}
HA_TAG_URL = "www.home-assistant.io/tag/"  # after the NDEF URI prefix code 0x04 = "https://"
NTAG213_USER_BYTES = 144


@dataclass(frozen=True)
class Label:
    loc_id: int
    path: str
    kind: str
    payload: str  # QR / NFC text record

    @property
    def tag_id(self) -> str:
        return f"loc-{self.loc_id}"


@dataclass(frozen=True)
class Sheet:
    page: str = "a4"
    cols: int = 3
    rows: int = 8
    margin_mm: float = 0.0  # page margin around the grid
    start: int = 0  # cells to skip on the first page (a partly used sheet)
    border: bool = True  # thin grey cut guide around every label

    @property
    def page_mm(self) -> tuple[float, float]:
        return PAGES[self.page]

    @property
    def cell_mm(self) -> tuple[float, float]:
        w, h = self.page_mm
        return (w - 2 * self.margin_mm) / self.cols, (h - 2 * self.margin_mm) / self.rows

    def validate(self) -> None:
        if self.page not in PAGES:
            raise ValueError(f"page {self.page!r}: one of {', '.join(PAGES)}")
        if not (1 <= self.cols <= 10 and 1 <= self.rows <= 30):
            raise ValueError(f"grid {self.cols}x{self.rows}: 1–10 columns, 1–30 rows")
        if not 0 <= self.margin_mm < min(self.page_mm) / 4:
            raise ValueError(f"margin {self.margin_mm} mm is out of range")
        if not 0 <= self.start < self.cols * self.rows:
            raise ValueError(f"--start {self.start}: 0..{self.cols * self.rows - 1} (cells on one sheet)")
        w, h = self.cell_mm
        if w < 25 or h < 12:
            raise ValueError(f"labels of {w:.1f} × {h:.1f} mm are too small (min 25 × 12 mm)")


# -- layout (shared by PDF and SVG) ---------------------------------------------------------
@dataclass(frozen=True)
class _Text:
    x: float
    y: float  # baseline, mm from the TOP of the page
    size_pt: float
    mono: bool
    text: str


@dataclass(frozen=True)
class _Placed:
    x: float  # top-left of the label cell, mm from the page's top-left
    y: float
    w: float
    h: float
    qr_x: float
    qr_y: float
    module: float  # mm per QR module
    code: qr.QrCode
    texts: tuple[_Text, ...]


def _wrap_path(path: str, max_chars: int) -> list[str]:
    """Break after `/` (kept at line end) or hard-split a too-long segment."""
    pieces = [p + "/" for p in path.split("/")]
    pieces[-1] = pieces[-1][:-1]
    lines: list[str] = []
    cur = ""
    for piece in pieces:
        while len(piece) > max_chars:
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(piece[:max_chars])
            piece = piece[max_chars:]
        if len(cur) + len(piece) > max_chars:
            lines.append(cur)
            cur = piece
        else:
            cur += piece
    if cur:
        lines.append(cur)
    return lines


def _layout(label: Label, x: float, y: float, w: float, h: float) -> _Placed:
    pad = min(2.5, h * 0.08)
    code = qr.encode(label.payload, ecc="M")
    quiet = 4  # modules of quiet zone (ISO/IEC 18004 minimum); scanners miss codes with less
    side = min(h - 2 * pad, w * 0.5)
    module = side / (code.size + 2 * quiet)
    qr_x, qr_y = x + pad + quiet * module, y + (h - side) / 2 + quiet * module
    tx = x + pad + side + 1.0
    tw = x + w - pad - tx
    footer = f"{label.kind}  {label.tag_id}"
    small = 6.0
    texts: list[_Text] = []
    avail_h = h - 2 * pad - small / PT_PER_MM * 1.6
    for size in (11.0, 10.0, 9.0, 8.0, 7.0, 6.0, 5.0):
        max_chars = max(1, int(tw / (size * COURIER_ADVANCE / PT_PER_MM)))
        lines = _wrap_path(label.path, max_chars)
        line_h = size / PT_PER_MM * 1.15
        if len(lines) * line_h <= avail_h:
            break
    else:
        raise ValueError(f"path {label.path!r} does not fit a {w:.1f} × {h:.1f} mm label even at 5 pt; "
                         "use fewer rows/columns (--grid) or a smaller --margin")
    by = y + pad + size / PT_PER_MM
    for line in lines:
        texts.append(_Text(tx, by, size, True, line))
        by += line_h
    small_chars = max(1, int(tw / (small * 0.55 / PT_PER_MM)))
    texts.append(_Text(tx, y + h - pad, small, False, footer[:small_chars]))
    return _Placed(x, y, w, h, qr_x, qr_y, module, code, tuple(texts))


def layout(labels: Sequence[Label], sheet: Sheet) -> list[list[_Placed]]:
    """Labels placed on pages (list of pages, each a list of placed labels)."""
    sheet.validate()
    if not labels:
        raise ValueError("no labels to lay out")
    cw, ch = sheet.cell_mm
    per_page = sheet.cols * sheet.rows
    pages: list[list[_Placed]] = []
    for n, label in enumerate(labels):
        slot = n + sheet.start
        page, cell = divmod(slot, per_page)
        while len(pages) <= page:
            pages.append([])
        r, c = divmod(cell, sheet.cols)
        pages[page].append(_layout(label, sheet.margin_mm + c * cw, sheet.margin_mm + r * ch, cw, ch))
    return pages


def _runs(code: qr.QrCode) -> list[tuple[int, int, int]]:
    """Dark modules as horizontal runs (row, col, length): far fewer rectangles to draw."""
    out: list[tuple[int, int, int]] = []
    for r, row in enumerate(code.modules):
        c = 0
        while c < len(row):
            if row[c]:
                start = c
                while c < len(row) and row[c]:
                    c += 1
                out.append((r, start, c - start))
            else:
                c += 1
    return out


# -- PDF ------------------------------------------------------------------------------------
def _pdf_str(text: str) -> str:
    return "(" + text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"


def _num(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".")


def render_pdf(labels: Sequence[Label], sheet: Sheet | None = None) -> bytes:
    sheet = sheet or Sheet()
    pages = layout(labels, sheet)
    pw, ph = sheet.page_mm
    W, H = pw * PT_PER_MM, ph * PT_PER_MM

    def px(mm: float) -> float:
        return mm * PT_PER_MM

    streams: list[bytes] = []
    for placed in pages:
        ops: list[str] = []
        if sheet.border:
            ops.append("0.8 G 0.3 w")
            ops.extend(f"{_num(px(p.x))} {_num(H - px(p.y + p.h))} {_num(px(p.w))} {_num(px(p.h))} re S"
                       for p in placed)
        ops.append("0 g")
        for p in placed:
            m = px(p.module)
            for r, c, n in _runs(p.code):
                x0 = px(p.qr_x) + c * m
                y0 = H - px(p.qr_y) - (r + 1) * m
                ops.append(f"{_num(x0)} {_num(y0)} {_num(n * m + 0.01)} {_num(m + 0.01)} re")
            ops.append("f")
            for t in p.texts:
                font = "/F1" if t.mono else "/F2"
                ops.append(f"BT {font} {_num(t.size_pt)} Tf {_num(px(t.x))} {_num(H - px(t.y))} Td {_pdf_str(t.text)} Tj ET")
        streams.append("\n".join(ops).encode("ascii"))

    objs: list[bytes] = []
    n_pages = len(pages)
    # 1 catalog, 2 pages, 3 F1, 4 F2, then (page, content) pairs
    kids = " ".join(f"{5 + 2 * i} 0 R" for i in range(n_pages))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier-Bold /Encoding /WinAnsiEncoding >>")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    for i, stream in enumerate(streams):
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_num(W)} {_num(H)}] "
                    f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {6 + 2 * i} 0 R >>".encode())
        objs.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


# -- SVG ------------------------------------------------------------------------------------
def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def render_svg_pages(labels: Sequence[Label], sheet: Sheet | None = None) -> list[str]:
    """One SVG document per page, sized in mm (prints 1:1 from a browser)."""
    sheet = sheet or Sheet()
    pw, ph = sheet.page_mm
    docs: list[str] = []
    for placed in layout(labels, sheet):
        parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{_num(pw)}mm" height="{_num(ph)}mm" '
                 f'viewBox="0 0 {_num(pw)} {_num(ph)}">', '<rect width="100%" height="100%" fill="#fff"/>']
        for p in placed:
            if sheet.border:
                parts.append(f'<rect x="{_num(p.x)}" y="{_num(p.y)}" width="{_num(p.w)}" height="{_num(p.h)}" '
                             'fill="none" stroke="#ccc" stroke-width="0.1"/>')
            d = "".join(f"M{_num(p.qr_x + c * p.module)} {_num(p.qr_y + r * p.module)}h{_num(n * p.module)}"
                        f"v{_num(p.module)}h-{_num(n * p.module)}z" for r, c, n in _runs(p.code))
            parts.append(f'<path d="{d}" fill="#000" shape-rendering="crispEdges"/>')
            for t in p.texts:
                family = "Courier New,Courier,monospace" if t.mono else "Helvetica,Arial,sans-serif"
                weight = ' font-weight="bold"' if t.mono else ""
                parts.append(f'<text x="{_num(t.x)}" y="{_num(t.y)}" font-family="{family}"{weight} '
                             f'font-size="{_num(t.size_pt / PT_PER_MM)}">{_xml(t.text)}</text>')
        parts.append("</svg>")
        docs.append("\n".join(parts) + "\n")
    return docs


# -- NTAG213 --------------------------------------------------------------------------------
def _short_record(first: bool, last: bool, rtype: bytes, payload: bytes) -> bytes:
    header = 0x10 | 0x01  # SR, TNF well-known
    header |= 0x80 if first else 0
    header |= 0x40 if last else 0
    if len(payload) > 255:
        raise ValueError("NDEF short record payload > 255 bytes")
    return bytes([header, len(rtype), len(payload)]) + rtype + payload


def ntag213_ndef(label: Label) -> bytes:
    """The NDEF TLV image for the location's NTAG213 (see module docstring)."""
    uri = _short_record(True, False, b"U", b"\x04" + (HA_TAG_URL + label.tag_id).encode("ascii"))
    text = _short_record(False, True, b"T", b"\x02en" + label.payload.encode("utf-8"))
    message = uri + text
    tlv = bytes([0x03, len(message)]) + message + b"\xfe"
    if len(tlv) > NTAG213_USER_BYTES:
        raise ValueError(f"NDEF image is {len(tlv)} bytes; NTAG213 holds {NTAG213_USER_BYTES}")
    return tlv

