"""qr.py — a small stdlib QR Code encoder (ISO/IEC 18004, byte mode, versions 1–10).

WHY: location labels (`3d printops loc label`) need QR codes; the 3d-api image and the CLI stay
  stdlib + pyyaml (PrintOps contract), and no QR library is a dependency. Label payloads are
  short ASCII (`cell:<path ≤ 64>` ≤ 69 bytes), so byte mode up to version 10 (≤ 213 bytes at
  level M) is all that is needed.

ALGORITHM: the reference construction (as in Project Nayuki's QR-Code-generator, MIT): data
  bits → pad → Reed–Solomon ECC per block (GF(2^8), poly 0x11D) → interleave → zig-zag
  placement → the mask with the lowest penalty score → format/version bits.

USAGE: `encode("cell:WH:A/S2", ecc="M").modules` → list of rows of bool (True = dark), without
  the quiet zone (renderers add 4 modules of margin).
"""
from __future__ import annotations

from dataclasses import dataclass

MAX_VERSION = 10
_ECL_FORMAT = {"L": 1, "M": 0, "Q": 3, "H": 2}
# Index = version (0 unused). From ISO/IEC 18004 table 9.
_ECC_PER_BLOCK = {
    "L": (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18),
    "M": (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26),
    "Q": (-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24),
    "H": (-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28),
}
_NUM_BLOCKS = {
    "L": (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4),
    "M": (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5),
    "Q": (-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8),
    "H": (-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8),
}


class QrError(ValueError):
    """The payload does not fit a version ≤ MAX_VERSION at the requested ECC level."""


@dataclass(frozen=True)
class QrCode:
    version: int
    ecc: str
    mask: int
    modules: tuple[tuple[bool, ...], ...]  # [row][col], True = dark

    @property
    def size(self) -> int:
        return len(self.modules)


def _raw_modules(ver: int) -> int:
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        result -= (25 * n - 10) * n - 55
        if ver >= 7:
            result -= 36
    return result


def data_capacity(ver: int, ecc: str) -> int:
    """Data codewords (bytes incl. mode/count overhead) of a version at an ECC level."""
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK[ecc][ver] * _NUM_BLOCKS[ecc][ver]


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _alignment_positions(ver: int) -> list[int]:
    if ver == 1:
        return []
    n = ver // 7 + 2
    step = (ver * 8 + n * 3 + 5) // (n * 4 - 4) * 2
    size = ver * 4 + 17
    return [6] + sorted(size - 7 - i * step for i in range(n - 1))


def _codewords(payload: bytes, ver: int, ecc: str) -> list[int]:
    bits: list[int] = []

    def put(value: int, n: int) -> None:
        bits.extend((value >> i) & 1 for i in reversed(range(n)))

    put(0b0100, 4)
    put(len(payload), 8 if ver <= 9 else 16)
    for b in payload:
        put(b, 8)
    cap_bits = data_capacity(ver, ecc) * 8
    put(0, min(4, cap_bits - len(bits)))
    put(0, (-len(bits)) % 8)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < cap_bits // 8:
        data.append(pad)
        pad ^= 0xEC ^ 0x11
    # split into blocks, add ECC, interleave
    n_blocks = _NUM_BLOCKS[ecc][ver]
    ecc_len = _ECC_PER_BLOCK[ecc][ver]
    raw = _raw_modules(ver) // 8
    n_short = n_blocks - raw % n_blocks
    short_len = raw // n_blocks
    divisor = _rs_divisor(ecc_len)
    blocks: list[list[int]] = []
    k = 0
    for i in range(n_blocks):
        dat = data[k:k + short_len - ecc_len + (0 if i < n_short else 1)]
        k += len(dat)
        rem = rs_remainder(dat, divisor)
        if i < n_short:
            dat = dat + [0]
        blocks.append(dat + rem)
    out: list[int] = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != short_len - ecc_len or j >= n_short:
                out.append(blk[i])
    return out


class _Grid:
    def __init__(self, ver: int) -> None:
        self.ver = ver
        self.size = ver * 4 + 17
        self.mod = [[False] * self.size for _ in range(self.size)]
        self.fn = [[False] * self.size for _ in range(self.size)]

    def set_fn(self, x: int, y: int, dark: bool) -> None:
        self.mod[y][x] = dark
        self.fn[y][x] = True

    def function_patterns(self) -> None:
        s = self.size
        for i in range(s):
            self.set_fn(6, i, i % 2 == 0)
            self.set_fn(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (s - 4, 3), (3, s - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < s and 0 <= y < s:
                        self.set_fn(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _alignment_positions(self.ver)
        last = len(pos) - 1
        for i, ax in enumerate(pos):
            for j, ay in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_fn(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.format_bits("M", 0)  # reserve; real bits drawn after masking
        if self.ver >= 7:
            rem = self.ver
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.ver << 12 | rem
            for i in range(18):
                bit = (bits >> i) & 1 == 1
                a, b = s - 11 + i % 3, i // 3
                self.set_fn(a, b, bit)
                self.set_fn(b, a, bit)

    def format_bits(self, ecc: str, mask: int) -> None:
        data = _ECL_FORMAT[ecc] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        s = self.size

        def bit(i: int) -> bool:
            return (bits >> i) & 1 == 1

        for i in range(6):
            self.set_fn(8, i, bit(i))
        self.set_fn(8, 7, bit(6))
        self.set_fn(8, 8, bit(7))
        self.set_fn(7, 8, bit(8))
        for i in range(9, 15):
            self.set_fn(14 - i, 8, bit(i))
        for i in range(8):
            self.set_fn(s - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_fn(8, s - 15 + i, bit(i))
        self.set_fn(8, s - 8, True)

    def place(self, codewords: list[int]) -> None:
        s = self.size
        i = 0
        total = len(codewords) * 8
        right = s - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(s):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = s - 1 - vert if upward else vert
                    if not self.fn[y][x] and i < total:
                        self.mod[y][x] = (codewords[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        for y in range(self.size):
            for x in range(self.size):
                if self.fn[y][x]:
                    continue
                if mask == 0:
                    flip = (x + y) % 2 == 0
                elif mask == 1:
                    flip = y % 2 == 0
                elif mask == 2:
                    flip = x % 3 == 0
                elif mask == 3:
                    flip = (x + y) % 3 == 0
                elif mask == 4:
                    flip = (x // 3 + y // 2) % 2 == 0
                elif mask == 5:
                    flip = x * y % 2 + x * y % 3 == 0
                elif mask == 6:
                    flip = (x * y % 2 + x * y % 3) % 2 == 0
                else:
                    flip = ((x + y) % 2 + x * y % 3) % 2 == 0
                if flip:
                    self.mod[y][x] = not self.mod[y][x]


_FINDER_LIKE = ((True, False, True, True, True, False, True, False, False, False, False),
                (False, False, False, False, True, False, True, True, True, False, True))


def penalty(mod: list[list[bool]]) -> int:
    """ISO/IEC 18004 §7.8.3 mask penalty (N1=3, N2=3, N3=40, N4=10)."""
    s = len(mod)
    score = 0
    lines = list(mod) + [[mod[y][x] for y in range(s)] for x in range(s)]
    for line in lines:
        run, prev = 0, None
        for v in line:
            if v == prev:
                run += 1
            else:
                if run >= 5:
                    score += 3 + run - 5
                run, prev = 1, v
        if run >= 5:
            score += 3 + run - 5
        padded = [False] * 4 + list(line) + [False] * 4
        for i in range(len(padded) - 10):
            if tuple(padded[i:i + 11]) in _FINDER_LIKE:
                score += 40
    for y in range(s - 1):
        for x in range(s - 1):
            c = mod[y][x]
            if c == mod[y][x + 1] == mod[y + 1][x] == mod[y + 1][x + 1]:
                score += 3
    dark = sum(sum(row) for row in mod)
    total = s * s
    k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
    return score + max(k, 0) * 10


def encode(payload: str | bytes, *, ecc: str = "M") -> QrCode:
    """The smallest QR code (version ≤ 10) holding `payload` in byte mode at `ecc` (L/M/Q/H)."""
    if ecc not in _ECL_FORMAT:
        raise QrError(f"ECC level {ecc!r}: one of L, M, Q, H")
    data = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    for ver in range(1, MAX_VERSION + 1):
        overhead_bits = 4 + (8 if ver <= 9 else 16)
        if overhead_bits + len(data) * 8 <= data_capacity(ver, ecc) * 8:
            break
    else:
        raise QrError(f"{len(data)} bytes do not fit a version-{MAX_VERSION} QR code at level {ecc}")
    cw = _codewords(data, ver, ecc)
    best: tuple[int, int, list[list[bool]]] | None = None
    for mask in range(8):
        g = _Grid(ver)
        g.function_patterns()
        g.place(cw)
        g.apply_mask(mask)
        g.format_bits(ecc, mask)
        p = penalty(g.mod)
        if best is None or p < best[0]:
            best = (p, mask, g.mod)
    assert best is not None
    return QrCode(ver, ecc, best[1], tuple(tuple(row) for row in best[2]))
