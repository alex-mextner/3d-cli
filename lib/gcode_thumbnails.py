"""gcode_thumbnails.py — check and add printer-screen thumbnails in G-code.

WHAT: a printer screen (Klipper/Moonraker, Snapmaker U1, Creality K1 stock firmware, Prusa…)
  shows a job's preview from base64 image blocks in the G-code header:

      ; THUMBNAIL_BLOCK_START
      ;
      ; thumbnail begin 300x300 31428
      ; iVBORw0KGgo…   (78 base64 chars per line)
      ; thumbnail end
      ; THUMBNAIL_BLOCK_END

  `check()` compares the blocks present in the first MiB (Moonraker only reads that much
  of the head) with the sizes the slicer profile asks for (`; thumbnails = 48x48/PNG,
  300x300/PNG` in the config block). `ensure()` adds the missing blocks right after
  `; HEADER_BLOCK_END` — where Orca's GUI writes them, before the first move — byte for
  byte in Orca's own block format.

WHY: the Orca-family CLI (Snapmaker Orca 2.4 `--slice`) exports G-code with a null
  thumbnail callback (src/Snapmaker_Orca.cpp: `print_fff->export_gcode(outfile,
  gcode_result, nullptr)`), and GCode::_do_export writes THUMBNAIL_BLOCKs only when that
  callback is set. The `thumbnails` setting is in the config, the image is never written:
  every headless slice reaches the printer screen without a preview.

IMAGE SOURCE: `--image PNG` (a render of the plate) or, by default, a picture drawn from
  the G-code's own extrusion moves: an isometric z-buffered view of walls/surfaces in each
  tool's `filament_colour`, so the preview always shows what this file actually prints.

INVARIANTS: `required`/`present`/`check` and the block writer are stdlib-only (a preflight
  imports them in-process). Rendering imports numpy lazily; decoding `--image` and JPG
  output import Pillow lazily. Files are rewritten atomically; a file that already has
  every required size is left byte-identical.
"""
from __future__ import annotations

import argparse
import base64
import math
import os
import re
import struct
import sys
import tempfile
import zlib
from array import array
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, BinaryIO, Callable, NamedTuple

from errors import InputNotFound

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

    Image = npt.NDArray[np.uint8]

# Moonraker's metadata extractor reads the first and last MiB only (READ_SIZE in
# moonraker/components/file_manager/metadata.py); a thumbnail further in is invisible.
HEAD_BYTES = 1024 * 1024
ROW = 78  # base64 characters per comment line, as Orca writes them (Thumbnails.hpp max_row_length)

# block tag per format, as Orca's compress_thumbnail_* tag() and Moonraker's parser expect
TAGS = {"PNG": "thumbnail", "JPG": "thumbnail_JPG", "QOI": "thumbnail_QOI"}
WRITABLE = ("PNG", "JPG")

# extrusion that is hidden inside the part or is not part of it: left out of the picture
SKIP_FEATURES = {
    "custom", "prime tower", "wipe tower", "skirt", "sparse infill", "internal solid infill",
    "internal infill", "solid infill", "infill", "purge", "flush",
}


class Thumb(NamedTuple):
    width: int
    height: int
    fmt: str  # PNG | JPG | QOI | BTT_TFT | COLPIC

    def __str__(self) -> str:
        return f"{self.width}x{self.height}/{self.fmt}"


@dataclass
class ThumbCheck:
    path: str
    ok: bool
    required: list[Thumb] = field(default_factory=list)
    present: list[Thumb] = field(default_factory=list)
    missing: list[Thumb] = field(default_factory=list)
    reason: str = ""
    added: list[Thumb] = field(default_factory=list)


class ThumbnailError(ValueError):
    """A G-code or option this module cannot handle (unsupported format, no size spec)."""


# ---------------------------------------------------------------- reading


def _head_tail(path: str) -> tuple[str, str]:
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(HEAD_BYTES)
        if size > HEAD_BYTES:
            f.seek(max(HEAD_BYTES, size - HEAD_BYTES))
            tail = f.read()
        else:
            tail = head
    return head.decode("utf-8", "ignore"), tail.decode("utf-8", "ignore")


def _config_value(key: str, head: str, tail: str) -> str | None:
    """Last `; key = value` of the config block (tail for Orca/Prusa, head for BBL G-code)."""
    pat = re.compile(rf"^; {re.escape(key)} = (.*)$", re.M)
    for text in (tail, head):
        found = pat.findall(text)
        if found:
            return str(found[-1]).strip()
    return None


def parse_spec(spec: str, default_fmt: str = "PNG") -> list[Thumb]:
    """`48x48/PNG, 300x300/PNG` (Orca) or `48x48,300x300` + thumbnails_format → [Thumb]."""
    out: list[Thumb] = []
    for item in re.split(r"[,;]", spec):
        item = item.strip()
        if not item:
            continue
        m = re.fullmatch(r"(\d+)\s*[xX*]\s*(\d+)\s*(?:/\s*(\w+))?", item)
        if not m:
            raise ThumbnailError(f"cannot parse thumbnail size {item!r} (expected WxH or WxH/FMT)")
        fmt = (m.group(3) or default_fmt).upper()
        out.append(Thumb(int(m.group(1)), int(m.group(2)), "COLPIC" if fmt == "COLPIC" else fmt))
    return out


def required(path: str) -> list[Thumb]:
    """Thumbnail sizes the slicer profile asks for (`; thumbnails = …`); [] when the G-code has no spec."""
    head, tail = _head_tail(path)
    return _required(head, tail)


def _required(head: str, tail: str) -> list[Thumb]:
    spec = _config_value("thumbnails", head, tail)
    if not spec:
        return []
    return parse_spec(spec, (_config_value("thumbnails_format", head, tail) or "PNG").upper())


_BLOCK = re.compile(r"(thumbnail(?:_[A-Za-z0-9]+)?) begin([;/\+=\w\s]+?); \1 end")


def _present(head: str) -> list[Thumb]:
    """Valid blocks exactly as Moonraker parses them: header `WxH LEN` and LEN base64 chars."""
    out: list[Thumb] = []
    tag_fmt = {v: k for k, v in TAGS.items()}
    for m in _BLOCK.finditer(head):
        lines = re.split(r"\r?\n", m.group(2).replace("; ", ""))
        info = [int(x) for x in re.findall(r"\d+", lines[0])]
        data = "".join(lines[1:-1])
        if len(info) != 3 or len(data) != info[2]:
            continue
        out.append(Thumb(info[0], info[1], tag_fmt.get(m.group(1), m.group(1).partition("_")[2].upper())))
    return out


def present(path: str) -> list[Thumb]:
    """Thumbnail blocks a printer will find in the G-code head."""
    return _present(_head_tail(path)[0])


def check(path: str) -> ThumbCheck:
    """ok = every profile-required size is present; without a spec, ok = at least one thumbnail."""
    if not os.path.isfile(path):
        raise InputNotFound(path, command="gcode-thumbnails")
    head, tail = _head_tail(path)
    req, pres = _required(head, tail), _present(head)
    have = set(pres)
    missing = [t for t in req if t not in have]
    if req:
        ok = not missing
        reason = "all required thumbnails present" if ok else "missing " + ", ".join(map(str, missing))
    else:
        ok = bool(pres)
        reason = "no thumbnails spec; thumbnail present" if ok else "no thumbnails (and no `; thumbnails =` spec)"
    return ThumbCheck(path, ok, req, pres, missing, reason)


# ---------------------------------------------------------------- writing


def block(thumb: Thumb, data: bytes) -> str:
    """One THUMBNAIL_BLOCK in Orca's exact layout (GCode/Thumbnails.hpp export_thumbnails_to_file)."""
    if thumb.fmt not in TAGS:
        raise ThumbnailError(f"thumbnail format {thumb.fmt} is not supported (supported: {', '.join(WRITABLE)})")
    tag = TAGS[thumb.fmt]
    enc = base64.b64encode(data).decode("ascii")
    rows = "".join(f"; {enc[i:i + ROW]}\n" for i in range(0, len(enc), ROW))
    return (f"; THUMBNAIL_BLOCK_START\n\n;\n; {tag} begin {thumb.width}x{thumb.height} {len(enc)}\n"
            f"{rows}; {tag} end\n; THUMBNAIL_BLOCK_END\n\n")


def insert_offset(head: bytes) -> int:
    """Byte offset for new blocks: after existing thumbnail blocks, else after the
    `; HEADER_BLOCK_END` line and its blank line (Orca's place), else the file start.
    Always before the first G/M command."""
    first_cmd = re.search(rb"(?m)^[GMT]\d", head)
    limit = first_cmd.start() if first_cmd else len(head)
    ends = [m.end() for m in re.finditer(rb"; THUMBNAIL_BLOCK_END\r?\n(\r?\n)?", head[:limit])]
    if ends:
        return ends[-1]
    m = re.search(rb"; HEADER_BLOCK_END\r?\n(\r?\n)?", head[:limit])
    return m.end() if m else 0


def splice(path: str, text: str, out: str | None = None) -> None:
    """Write `text` into the G-code at insert_offset(); atomic replace (or write to `out`).
    Mode and mtime of the source are kept: the toolpath is the same slice, and a staleness
    check (G-code older than its project) must not be fooled by adding a picture."""
    dst = out or path
    with open(path, "rb") as src:
        off = insert_offset(src.read(HEAD_BYTES))
        src.seek(0)
        fd, tmp = tempfile.mkstemp(prefix=".thumbs-", dir=os.path.dirname(os.path.abspath(dst)))
        try:
            with os.fdopen(fd, "wb") as w:
                w.write(src.read(off))
                w.write(text.encode("ascii"))
                while chunk := src.read(1 << 22):
                    w.write(chunk)
            st = os.stat(path)
            os.chmod(tmp, st.st_mode & 0o7777)
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
            os.replace(tmp, dst)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


# ---------------------------------------------------------------- images


def encode_png(rgba: Image) -> bytes:
    """RGBA uint8 HxWx4 → PNG (zlib; one filter for all rows — none/Sub/Up, whichever compresses best)."""
    import numpy as np

    h, w = int(rgba.shape[0]), int(rgba.shape[1])
    px = rgba.reshape(h, w * 4).astype(np.int16)
    sub, up = px.copy(), px.copy()
    sub[:, 4:] -= px[:, :-4]
    up[1:] -= px[:-1]
    best = b""
    for ftype, rows in ((0, px), (1, sub), (2, up)):
        body = np.concatenate([np.full((h, 1), ftype, np.int16), rows % 256], axis=1).astype(np.uint8)
        z = zlib.compress(body.tobytes(), 9)
        if not best or len(z) < len(best):
            best = z

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", best) + chunk(b"IEND", b""))


def encode(rgba: Image, fmt: str) -> bytes:
    if fmt == "PNG":
        return encode_png(rgba)
    if fmt == "JPG":
        import io

        import numpy as np
        from PIL import Image as PILImage  # lazy: only JPG output needs Pillow

        a = rgba[..., 3:4].astype(np.float32) / 255.0
        rgb = (rgba[..., :3].astype(np.float32) * a + 255.0 * (1.0 - a)).round().astype(np.uint8)  # on white
        buf = io.BytesIO()
        PILImage.fromarray(rgb, "RGB").save(buf, "JPEG", quality=90)
        return buf.getvalue()
    raise ThumbnailError(f"thumbnail format {fmt} is not supported (supported: {', '.join(WRITABLE)})")


def _area_weights(n_out: int, n_in: int, scale: float, offset: float) -> np.ndarray:
    """Box-filter matrix (n_out × n_in): output pixel i covers input [(i-offset)/scale, (i+1-offset)/scale)."""
    import numpy as np

    lo = (np.arange(n_out) - offset) / scale
    hi = lo + 1.0 / scale
    edges = np.arange(n_in + 1, dtype=np.float64)
    w = np.clip(np.minimum(hi[:, None], edges[None, 1:]) - np.maximum(lo[:, None], edges[None, :-1]), 0.0, None)
    s = w.sum(axis=1, keepdims=True)
    return np.divide(w, s, out=np.zeros_like(w), where=s > 0)


def fit(rgba: Image, width: int, height: int) -> Image:
    """Scale the picture to fit width×height (aspect kept, centred, transparent margins), area-averaged."""
    import numpy as np

    h, w = rgba.shape[:2]
    scale = min(width / w, height / h)
    ox, oy = (width - w * scale) / 2.0, (height - h * scale) / 2.0
    wy, wx = _area_weights(height, h, scale, oy), _area_weights(width, w, scale, ox)
    cover_y = np.clip(np.minimum(np.arange(height) + 1, oy + h * scale) - np.maximum(np.arange(height), oy), 0, 1)
    cover_x = np.clip(np.minimum(np.arange(width) + 1, ox + w * scale) - np.maximum(np.arange(width), ox), 0, 1)
    f = rgba.astype(np.float64) / 255.0
    pre = np.concatenate([f[..., :3] * f[..., 3:4], f[..., 3:4]], axis=2)  # premultiplied alpha
    res = np.tensordot(wy, pre, axes=(1, 0))                    # (height, w, 4)
    res = np.tensordot(res, wx, axes=(1, 1)).transpose(0, 2, 1)  # (height, width, 4)
    res = res * (cover_y[:, None] * cover_x[None, :])[..., None]
    alpha = res[..., 3:4]
    rgb = np.divide(res[..., :3], alpha, out=np.zeros_like(res[..., :3]), where=alpha > 1e-6)
    out = np.concatenate([rgb, alpha], axis=2)
    return np.asarray((np.clip(out, 0.0, 1.0) * 255.0).round(), dtype=np.uint8)


def load_image(path: str) -> Image:
    import numpy as np
    from PIL import Image as PILImage  # lazy: only --image needs Pillow

    with PILImage.open(path) as im:
        rgba = np.asarray(im.convert("RGBA"), dtype=np.uint8)
    ys, xs = np.nonzero(rgba[..., 3] > 0)
    if len(ys):  # trim fully transparent margins so the part fills the thumbnail
        rgba = rgba[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    return rgba


# ---------------------------------------------------------------- G-code preview render


def _hex_rgb(c: str) -> tuple[int, int, int]:
    c = c.strip().lstrip("#")
    if len(c) >= 6:
        try:
            return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
        except ValueError:
            pass
    return 200, 200, 200


_NUM = re.compile(rb"([XYZEIJF])(-?\d*\.?\d+)")


def toolpaths(f: BinaryIO) -> array[float]:
    """Visible extrusion segments as a flat array [x0, y0, x1, y1, z, tool, …] (6 doubles per segment).
    Tracks G90/G91, M82/M83, G92, T<n>, ;TYPE:/; FEATURE:, G2/G3 arcs (I/J)."""
    seg = array("d")
    x = y = z = e = 0.0
    abs_xyz, abs_e, tool, skip = True, False, 0, False
    for line in f:
        c = line[:1]
        if c == b";":
            if line.startswith(b";TYPE:") or line.startswith(b"; FEATURE:"):
                feat = line.split(b":", 1)[1].strip().decode("ascii", "ignore").lower()
                skip = feat in SKIP_FEATURES
            continue
        if c == b"G":
            code = line[1:3]
            if code[:1] in b"0123" and (len(code) == 1 or not code[1:2].isdigit()):
                g = code[:1]
                body = line.split(b";", 1)[0]
                p = {k.decode(): float(v) for k, v in _NUM.findall(body[2:])}
                nx = (p["X"] if abs_xyz else x + p["X"]) if "X" in p else x
                ny = (p["Y"] if abs_xyz else y + p["Y"]) if "Y" in p else y
                nz = (p["Z"] if abs_xyz else z + p["Z"]) if "Z" in p else z
                de = 0.0
                if "E" in p:
                    de = p["E"] - e if abs_e else p["E"]
                    e = p["E"] if abs_e else e + p["E"]
                if de > 0 and not skip and (nx != x or ny != y):
                    if g in b"23" and ("I" in p or "J" in p):
                        cx, cy = x + p.get("I", 0.0), y + p.get("J", 0.0)
                        a0, a1 = math.atan2(y - cy, x - cx), math.atan2(ny - cy, nx - cx)
                        sweep = a1 - a0
                        if g == b"2" and sweep >= 0:
                            sweep -= 2 * math.pi
                        if g == b"3" and sweep <= 0:
                            sweep += 2 * math.pi
                        r = math.hypot(x - cx, y - cy)
                        n = max(2, min(64, int(abs(sweep) * r / 0.5) + 1))
                        px_, py_ = x, y
                        for k in range(1, n + 1):
                            a = a0 + sweep * k / n
                            qx, qy = cx + r * math.cos(a), cy + r * math.sin(a)
                            seg.extend((px_, py_, qx, qy, nz, tool))
                            px_, py_ = qx, qy
                    else:
                        seg.extend((x, y, nx, ny, nz, tool))
                x, y, z = nx, ny, nz
            elif line.startswith(b"G90"):
                abs_xyz = True
            elif line.startswith(b"G91"):
                abs_xyz = False
            elif line.startswith(b"G92"):
                p = {k.decode(): float(v) for k, v in _NUM.findall(line.split(b";", 1)[0][3:])}
                e = p.get("E", e)
            continue
        if c == b"M":
            if line.startswith(b"M82"):
                abs_e = True
            elif line.startswith(b"M83"):
                abs_e = False
            continue
        if c == b"T" and line[1:2].isdigit():
            tool = int(re.match(rb"T(\d+)", line).group(1))  # type: ignore[union-attr]
    return seg


def render_gcode(path: str, size: int = 600) -> Image:
    """Isometric z-buffered preview (front-right, from above) of the G-code's visible extrusion,
    coloured by each tool's filament_colour and lit from screen-space normals. RGBA, transparent bg."""
    import numpy as np

    head, tail = _head_tail(path)
    colours = (_config_value("filament_colour", head, tail) or _config_value("extruder_colour", head, tail) or "")
    palette = np.array([_hex_rgb(c) for c in re.split(r"[;,]", colours) if c.strip()] or [(200, 200, 200)], np.float64)
    width = float(_config_value("outer_wall_line_width", head, tail) or _config_value("line_width", head, tail) or 0.42)
    with open(path, "rb") as f:
        seg = toolpaths(f)
    n = len(seg) // 6
    if n == 0:
        raise ThumbnailError(f"{path}: no visible extrusion moves to draw a preview from")
    s = np.frombuffer(seg, np.float64).reshape(n, 6)
    x0, y0, x1, y1, z, tool = s.T

    az, el = math.radians(-45.0), math.radians(35.264)  # camera at front-right, true isometric elevation
    ca, sa, ce, se = math.cos(az), math.sin(az), math.cos(el), math.sin(el)

    def project(px: np.ndarray, py: np.ndarray, pz: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        rx = px * ca - py * sa
        ry = px * sa + py * ca  # +ry = away from the camera on the bed
        return rx, -(pz * ce + ry * se), ry * ce - pz * se  # screen u, screen v (down), depth (far = big)

    u0, v0, _ = project(np.concatenate([x0, x1]), np.concatenate([y0, y1]), np.concatenate([z, z]))
    umin, umax, vmin, vmax = u0.min(), u0.max(), v0.min(), v0.max()
    extent = max(umax - umin, vmax - vmin, 1.0)
    margin = 0.03 * extent
    # a pixel no finer than ~0.4 line widths: neighbouring lines then touch and surfaces close up
    res = max((extent + 2 * margin) / size, 0.4 * width)
    side = int(math.ceil((extent + 2 * margin) / res))

    seg_len = np.hypot(x1 - x0, y1 - y0)
    steps = np.maximum(1, np.ceil(seg_len / (0.5 * res)).astype(np.int64))
    idx = np.repeat(np.arange(n), steps + 1)
    start = np.repeat(np.cumsum(steps + 1) - (steps + 1), steps + 1)
    t = (np.arange(len(idx)) - start) / steps[idx]
    px, py = x0[idx] + (x1 - x0)[idx] * t, y0[idx] + (y1 - y0)[idx] * t
    u, v, d = project(px, py, z[idx])
    col = palette[np.minimum(tool[idx].astype(np.int64), len(palette) - 1)]

    cu = (u - umin + margin + (extent - (umax - umin)) / 2) / res
    cv = (v - vmin + margin + (extent - (vmax - vmin)) / 2) / res
    zbuf = np.full(side * side, np.inf)
    rgb = np.zeros((side * side, 3))
    r_px = max(0.0, 0.5 * width / res - 0.5)
    offs = [(dx, dy) for dx in range(-2, 3) for dy in range(-2, 3) if dx * dx + dy * dy <= r_px * r_px + 0.25]
    for dx, dy in offs:
        iu, iv = np.floor(cu + dx).astype(np.int64), np.floor(cv + dy).astype(np.int64)
        ok = (iu >= 0) & (iu < side) & (iv >= 0) & (iv < side)
        np.minimum.at(zbuf, iv[ok] * side + iu[ok], d[ok])
    for dx, dy in offs:
        iu, iv = np.floor(cu + dx).astype(np.int64), np.floor(cv + dy).astype(np.int64)
        ok = (iu >= 0) & (iu < side) & (iv >= 0) & (iv < side)
        pix = iv[ok] * side + iu[ok]
        win = d[ok] <= zbuf[pix] + 1e-9
        rgb[pix[win]] = col[ok][win]

    zb = zbuf.reshape(side, side)
    filled = np.isfinite(zb)
    zf = np.where(filled, zb, np.nan)
    gy, gx = np.gradient(zf / res)  # depth change per pixel, in pixels
    nrm = np.stack([-np.nan_to_num(gx), -np.nan_to_num(gy), np.ones_like(zb)], axis=2)
    nrm /= np.linalg.norm(nrm, axis=2, keepdims=True)
    light = np.array([-0.45, -0.55, 0.70])
    light /= np.linalg.norm(light)
    lambert = np.clip((nrm * light).sum(axis=2), 0.0, 1.0)
    shade = 0.45 + 0.55 * lambert
    # silhouette / step edges: a depth jump to a neighbour darkens the pixel
    edge = np.zeros_like(filled)
    for ax in (0, 1):
        for sh in (1, -1):
            nb = np.roll(zf, sh, axis=ax)
            edge |= filled & (~np.isfinite(nb) | (np.abs(nb - zf) > 3 * res))
    shade = np.where(edge, shade * 0.7, shade)
    img = np.zeros((side, side, 4), np.uint8)
    img[..., :3] = np.clip(rgb.reshape(side, side, 3) * shade[..., None], 0, 255).round().astype(np.uint8)
    img[..., 3] = np.where(filled, 255, 0).astype(np.uint8)
    return img


# ---------------------------------------------------------------- ensure


def ensure(path: str, image: str | None = None, sizes: list[Thumb] | None = None, out: str | None = None,
           source: Callable[[str], Image] | None = None) -> ThumbCheck:
    """Add every missing required thumbnail (or `sizes`, when the G-code declares none).
    No-op (file untouched) when check() already passes. Returns the check after writing,
    with `.added` listing what was inserted."""
    before = check(path)
    want = before.required or list(sizes or [])
    if not want and not before.present:
        raise ThumbnailError(f"{path}: no `; thumbnails =` spec in the G-code; pass sizes (e.g. --size 300x300/PNG)")
    have = set(before.present)
    todo = [t for t in want if t not in have]
    if not todo:
        if out and out != path:
            import shutil

            shutil.copyfile(path, out)
        return before
    bad = [t for t in todo if t.fmt not in WRITABLE]
    if bad:
        raise ThumbnailError(f"cannot write {', '.join(map(str, bad))}: supported formats are {', '.join(WRITABLE)}")
    biggest = max(max(t.width, t.height) for t in todo)
    pic = load_image(image) if image else (source or (lambda p: render_gcode(p, size=2 * biggest)))(path)
    text = "".join(block(t, encode(fit(pic, t.width, t.height), t.fmt)) for t in todo)
    splice(path, text, out)
    after = check(out or path)
    after.added = todo
    return after


# ---------------------------------------------------------------- CLI (lib tool entry: `3d gcode-thumbnails`)


def _describe(c: ThumbCheck) -> str:
    pres = ", ".join(map(str, c.present)) or "none"
    req = ", ".join(map(str, c.required)) or "no spec"
    line = f"{'PASS' if c.ok else 'FAIL'} {c.path}: required [{req}] present [{pres}]"
    if c.added:
        line += f" added [{', '.join(map(str, c.added))}]"
    if not c.ok:
        line += f" — {c.reason}"
    return line


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="3d gcode-thumbnails", description="check/add G-code thumbnails")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pc = sub.add_parser("check")
    pc.add_argument("files", nargs="+")
    pc.add_argument("--json", action="store_true")
    pa = sub.add_parser("add")
    pa.add_argument("files", nargs="+")
    pa.add_argument("--image")
    pa.add_argument("--size", action="append", default=[])
    pa.add_argument("--out")
    pa.add_argument("--preview", help="also save the source picture (PNG) here")
    pa.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    results: list[ThumbCheck] = []
    try:
        if a.cmd == "check":
            results = [check(p) for p in a.files]
        else:
            if a.out and len(a.files) != 1:
                raise ThumbnailError("--out takes exactly one input file")
            sizes = [t for s in a.size for t in parse_spec(s)]
            for p in a.files:
                src: Callable[[str], Image] | None = None
                if a.preview:
                    def src(gp: str, keep: str = a.preview) -> Image:
                        want = check(gp).required or sizes
                        pic = render_gcode(gp, size=2 * max([max(t.width, t.height) for t in want] or [300]))
                        with open(keep, "wb") as fh:
                            fh.write(encode_png(pic))
                        return pic
                results.append(ensure(p, image=a.image, sizes=sizes, out=a.out, source=src))
    except (ThumbnailError, InputNotFound) as e:
        print(f"gcode-thumbnails: {e}", file=sys.stderr)
        return 2
    except ImportError as e:  # Pillow for --image / JPG output, numpy for rendering
        print(f"gcode-thumbnails: {e}; install it (e.g. `uv sync --extra preprocess` for Pillow)", file=sys.stderr)
        return 127
    if a.json:
        import json

        print(json.dumps([{"path": c.path, "ok": c.ok, "reason": c.reason, "required": [str(t) for t in c.required],
                           "present": [str(t) for t in c.present], "missing": [str(t) for t in c.missing],
                           "added": [str(t) for t in c.added]} for c in results], indent=2))
    else:
        for c in results:
            print(_describe(c))
    return 0 if all(c.ok for c in results) else 1


if __name__ == "__main__":
    sys.exit(main())
