"""color.py — filament colour comparison: hex parsing, sRGB → CIELAB (D65) and CIEDE2000 ΔE.

WHAT: `parse_hex` accepts what printers and Spoolman report ("#1A2B3C", "1a2b3c", U1 RGBA
  "000000FF", Creality's padded "#0ffa800"); `srgb_to_lab`; `delta_e2000` (Sharma, Wu, Dalal 2005,
  "The CIEDE2000 Color-Difference Formula: Implementation Notes, Supplementary Test Data, and
  Mathematical Observations", Color Res. Appl. 30(1) — tests check all 34 pairs of its table);
  `hex_delta_e` for two hex colours; `linear_rgb` / `hex_from_linear` / `linear_to_lab` for
  colours averaged in linear light (the layer-mix model, lib/slicing/mix_model.py).

WHY: a printer reports the colour of what is loaded only approximately (U1 touchscreen presets,
  slicer sync), so "same colour" is a perceptual distance under a threshold, not string equality
  (plan §5.3 item 2: ΔE2000 > threshold → anomaly).

INVARIANTS: stdlib only; kL = kC = kH = 1; sRGB companding per IEC 61966-2-1, D65 white
  (Xn, Yn, Zn) = (95.047, 100.000, 108.883).
"""

from __future__ import annotations

import math
import re

_HEX = re.compile(r"[0-9A-Fa-f]+")
_D65 = (95.047, 100.0, 108.883)


def parse_hex(value: object) -> str | None:
    """Colour text -> "#RRGGBB" (upper case), or None when it is not a colour.

    8 hex digits are RGBA as the U1 reports them (alpha dropped); 7 digits are Creality's
    "pad digit + RRGGBB"."""
    if not isinstance(value, str):
        return None
    s = value.strip().lstrip("#")
    if not _HEX.fullmatch(s):
        return None
    if len(s) == 8:
        s = s[:6]
    elif len(s) == 7:
        s = s[1:]
    elif len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return None
    return "#" + s.upper()


def _linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _f(t: float) -> float:
    return t ** (1 / 3) if t > (6 / 29) ** 3 else t / (3 * (6 / 29) ** 2) + 4 / 29


def linear_rgb(hex_color: str) -> tuple[float, float, float]:
    """ "#RRGGBB" -> linear-light RGB in 0..1 (IEC 61966-2-1 decoding)."""
    h = parse_hex(hex_color)
    if h is None:
        raise ValueError(f"not a colour: {hex_color!r}")
    r, g, b = (_linear(int(h[i : i + 2], 16) / 255.0) for i in (1, 3, 5))
    return r, g, b


def _encode(c: float) -> float:
    c = min(1.0, max(0.0, c))
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def hex_from_linear(rgb: tuple[float, float, float]) -> str:
    """Linear-light RGB (clipped to 0..1) -> "#RRGGBB"."""
    return "#" + "".join(f"{round(_encode(c) * 255):02X}" for c in rgb)


def linear_to_lab(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    """Linear-light sRGB -> CIELAB (L*, a*, b*) under D65 (mixes average in linear light)."""
    r, g, b = rgb
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) * 100
    y = (0.2126729 * r + 0.7151522 * g + 0.0721750 * b) * 100
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) * 100
    fx, fy, fz = _f(x / _D65[0]), _f(y / _D65[1]), _f(z / _D65[2])
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def srgb_to_lab(hex_color: str) -> tuple[float, float, float]:
    """ "#RRGGBB" -> CIELAB (L*, a*, b*) under D65."""
    return linear_to_lab(linear_rgb(hex_color))


def delta_e2000(
    lab1: tuple[float, float, float], lab2: tuple[float, float, float]
) -> float:
    """CIEDE2000 colour difference between two CIELAB colours (kL = kC = kH = 1)."""
    l1, a1, b1 = lab1
    l2, a2, b2 = lab2
    c1, c2 = math.hypot(a1, b1), math.hypot(a2, b2)
    c_bar7 = ((c1 + c2) / 2) ** 7
    g = 0.5 * (1 - math.sqrt(c_bar7 / (c_bar7 + 25**7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if c1p else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if c2p else 0.0

    dl = l2 - l1
    dc = c2p - c1p
    if c1p * c2p == 0:
        dh = 0.0
    elif abs(h2p - h1p) <= 180:
        dh = h2p - h1p
    elif h2p - h1p > 180:
        dh = h2p - h1p - 360
    else:
        dh = h2p - h1p + 360
    dH = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dh / 2))

    l_bar = (l1 + l2) / 2
    c_bar_p = (c1p + c2p) / 2
    if c1p * c2p == 0:
        h_bar = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        h_bar = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        h_bar = (h1p + h2p + 360) / 2
    else:
        h_bar = (h1p + h2p - 360) / 2

    t = (
        1
        - 0.17 * math.cos(math.radians(h_bar - 30))
        + 0.24 * math.cos(math.radians(2 * h_bar))
        + 0.32 * math.cos(math.radians(3 * h_bar + 6))
        - 0.20 * math.cos(math.radians(4 * h_bar - 63))
    )
    d_theta = 30 * math.exp(-(((h_bar - 275) / 25) ** 2))
    c_bar_p7 = c_bar_p**7
    r_c = 2 * math.sqrt(c_bar_p7 / (c_bar_p7 + 25**7))
    s_l = 1 + 0.015 * (l_bar - 50) ** 2 / math.sqrt(20 + (l_bar - 50) ** 2)
    s_c = 1 + 0.045 * c_bar_p
    s_h = 1 + 0.015 * c_bar_p * t
    r_t = -math.sin(math.radians(2 * d_theta)) * r_c
    return math.sqrt(
        (dl / s_l) ** 2
        + (dc / s_c) ** 2
        + (dH / s_h) ** 2
        + r_t * (dc / s_c) * (dH / s_h)
    )


def hex_delta_e(a: str, b: str) -> float:
    """CIEDE2000 between two hex colours (see parse_hex for accepted forms)."""
    return delta_e2000(srgb_to_lab(a), srgb_to_lab(b))
