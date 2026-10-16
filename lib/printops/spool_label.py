"""spool_label.py — read a filament spool label (its OCR text) into a proposed `receive` body.

WHAT: `parse_label(text, vendors)` applies fixed rules to the label text and returns a
  `Proposal`: vendor, material, colour (name, nominal #hex, Russian name), product name, net
  weight, diameter, nozzle and bed temperature ranges, lot — each with the label line it came
  from. `Proposal.receive_body()` turns it into the body of `POST /v1/spools`.

WHY: the inventory is filled from photos of spool labels (`3d printops spool scan`, issue
  #379). The rules are deterministic and need no LLM (epic #356, #363): the user confirms the
  proposal and corrects any field with a flag before anything is written.

INVARIANTS: stdlib only. A field the rules cannot read stays None and is listed in `missing`;
  nothing is guessed from defaults (the net weight is never assumed to be 1 kg). Colour hexes
  are nominal (a colour WORD on the label, not a measurement). Temperatures and the source photo
  go into the spool comment: 3d-api's receive has no temperature fields.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from printops.tares import vendor_key

# Vendors that print their name on the label; 3d-api's tare DB (`GET /v1/tares`) adds the
# SpoolmanDB vendors at run time.
BUILTIN_VENDORS = (
    "Anycubic",
    "Bambu Lab",
    "Creality",
    "Elegoo",
    "Eryone",
    "eSUN",
    "Flashforge",
    "Geeetech",
    "Hatchbox",
    "JAYO",
    "Kingroon",
    "Overture",
    "Polymaker",
    "Prusament",
    "Snapmaker",
    "Sovol",
    "SUNLU",
    "Tinmorry",
    "ZIRO",
)

# (pattern, material) — first match wins, so the specific forms come first. Materials are
# the names 3d-api knows a density for (printops.spools.DENSITY).
_MATERIALS: tuple[tuple[str, str], ...] = (
    (r"PLA\s*(?:\+|PLUS\b|PRO\b)", "PLA+"),
    (r"PETG", "PETG"),
    (r"PCTG", "PCTG"),
    (r"ABS", "ABS"),
    (r"ASA", "ASA"),
    (r"TPU", "TPU"),
    (r"HIPS", "HIPS"),
    (r"PVA", "PVA"),
    (r"PA(?:6|12)?", "PA"),
    (r"PC", "PC"),
    (r"PET", "PET"),
    (r"PLA", "PLA"),
)
_MATERIAL_RES = tuple(
    (re.compile(rf"(?<![A-Z0-9]){pat}(?![A-Z])", re.IGNORECASE), mat)
    for pat, mat in _MATERIALS
)

# Product-line words kept in the proposed filament name ("Matte PLA Black" → "Matte Black").
_LINE_WORDS = (
    "Silk",
    "Matte",
    "Rapid",
    "High Speed",
    "Hyper",
    "Basic",
    "Glow",
    "Marble",
    "Wood",
    "Galaxy",
    "Sparkle",
    "Rainbow",
    "Translucent",
    "Carbon Fiber",
    "CF",
)

# colour word → (English name, nominal #hex, Russian name). Multi-word names first.
_COLOURS: tuple[tuple[str, str, str, str], ...] = (
    (r"(?:space|dark)\s+gr[ae]y", "Dark Grey", "#4D4D4F", "тёмно-серый"),
    (r"light\s+gr[ae]y", "Light Grey", "#BFC1C2", "светло-серый"),
    (r"(?:cold|cool|bone|ivory)\s+white", "White", "#FFFFFF", "белый"),
    (r"(?:jet|deep)\s+black", "Black", "#000000", "чёрный"),
    (r"sky\s+blue", "Sky Blue", "#56B7E6", "голубой"),
    (r"black|ч[её]рн\w*", "Black", "#000000", "чёрный"),
    (r"white|бел\w*", "White", "#FFFFFF", "белый"),
    (r"gr[ae]y|сер(?:ый|ая|ое)", "Grey", "#8E9089", "серый"),
    (r"silver|серебр\w*", "Silver", "#A6A9AA", "серебристый"),
    (r"red|красн\w*", "Red", "#C12E1F", "красный"),
    (r"blue|син\w*", "Blue", "#0A2989", "синий"),
    (r"голуб\w*", "Sky Blue", "#56B7E6", "голубой"),
    (r"green|з[её]л[её]н\w*", "Green", "#1B5E20", "зелёный"),
    (r"yellow|ж[её]лт\w*", "Yellow", "#F4EE2A", "жёлтый"),
    (r"orange|оранж\w*", "Orange", "#E8732A", "оранжевый"),
    (r"purple|violet|фиолет\w*", "Purple", "#5E43B7", "фиолетовый"),
    (r"pink|розов\w*", "Pink", "#E4A6C8", "розовый"),
    (r"brown|коричн\w*", "Brown", "#6F4E37", "коричневый"),
    (r"beige|бежев\w*", "Beige", "#D7C9A7", "бежевый"),
    (r"gold|золот\w*", "Gold", "#D4AF37", "золотой"),
    (r"transparent|clear|прозрачн\w*", "Transparent", "#F0F0F0", "прозрачный"),
    (r"natural|натурал\w*", "Natural", "#F2EDDF", "натуральный"),
)
_COLOUR_RES = tuple(
    (re.compile(rf"(?<![\w]){pat}(?![\w])", re.IGNORECASE), en, hex_, ru)
    for pat, en, hex_, ru in _COLOURS
)
_COLOUR_KEY = re.compile(
    r"\b(?:colou?r|цвет)\b\s*[:：.]?\s*(?P<rest>.+)", re.IGNORECASE
)
_HEX_ON_LABEL = re.compile(r"#([0-9A-F]{6})\b", re.IGNORECASE)

_WEIGHT = re.compile(
    r"(?<![\d.,])(?P<num>\d{1,4}(?:[.,]\d{1,3})?)\s*(?P<unit>kg|кг|g|г|gr)(?![a-zа-я])",
    re.IGNORECASE,
)
_NET_WORDS = re.compile(
    r"net|n\.?\s?w\b|нетто|вес|weight|peso|poids|gewicht", re.IGNORECASE
)
_GROSS_WORDS = re.compile(r"gross|g\.?\s?w\b|брутто|spool\s+weight", re.IGNORECASE)

_DIAMETER = re.compile(
    r"(?<![\d.,])(?P<d>1[.,]75|2[.,]85|3[.,]00?)\s*(?:±\s*0[.,]0\d+\s*)?(?:mm|мм)?",
    re.IGNORECASE,
)
_DIAMETER_WORDS = re.compile(r"diam|ø|⌀|диаметр|mm|мм", re.IGNORECASE)

_DEG = r"\s*(?:[°º*o]\s*)?(?:[CС]\b|℃)"
_TEMP_RANGE = re.compile(
    rf"(?<!\d)(?P<lo>\d{{2,3}})(?:{_DEG})?\s*(?:-|~|–|—|to|до)\s*(?P<hi>\d{{2,3}}){_DEG}",
    re.IGNORECASE,
)
_TEMP_ONE = re.compile(rf"(?<![\d\-~–])(?P<t>\d{{2,3}}){_DEG}", re.IGNORECASE)
_NOZZLE_WORDS = re.compile(
    r"nozzle|print|extru|hot\s*end|печат|сопл|экструд", re.IGNORECASE
)
_BED_WORDS = re.compile(r"bed|plate|platform|стол|подлож|платформ", re.IGNORECASE)

_LOT = re.compile(
    r"\b(?:lot(?:\s*no)?|batch(?:\s*no)?|b/?n|s/?n|партия)\b\s*[.:#№]?\s*(?P<lot>[A-Z0-9][A-Z0-9\-/]{3,})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Found:
    """A recognised value and the label line it was read from."""

    value: Any
    line: str


@dataclass
class Proposal:
    """What a label says, field by field; None = not readable on the label."""

    source: str
    text: str
    vendor: Found | None = None
    material: Found | None = None
    colour: Found | None = None  # value: {"name", "hex", "ru"}
    line_words: list[str] = field(default_factory=list)
    net_g: Found | None = None
    diameter: Found | None = None
    nozzle_c: Found | None = None  # value: (lo, hi)
    bed_c: Found | None = None
    lot: Found | None = None

    @property
    def name(self) -> str | None:
        """The filament name: product-line words + the colour as printed ("Matte Black")."""
        parts = [*self.line_words]
        if self.colour is not None:
            parts.append(self.colour.value["name"])
        return " ".join(parts) or None

    @property
    def missing(self) -> list[str]:
        """Fields a receive needs or should have that the label did not give."""
        need = {
            "vendor": self.vendor,
            "material": self.material,
            "colour": self.colour,
            "net": self.net_g,
        }
        return [k for k, v in need.items() if v is None]

    def comment(self) -> str:
        """Spool comment: where the record came from + what Spoolman has no field for."""
        bits = [f"from label {self.source}"] if self.source else []
        if self.nozzle_c is not None:
            bits.append(f"nozzle {_range(self.nozzle_c.value)} °C")
        if self.bed_c is not None:
            bits.append(f"bed {_range(self.bed_c.value)} °C")
        return "; ".join(bits)

    def receive_body(self) -> dict[str, Any]:
        """The `POST /v1/spools` body the label supports (no location: the caller adds it)."""
        body: dict[str, Any] = {}
        if self.vendor is not None:
            body["vendor"] = self.vendor.value
        if self.material is not None:
            body["material"] = self.material.value
        if self.name is not None:
            body["name"] = self.name
        if self.colour is not None:
            body["color_hex"] = self.colour.value["hex"]
        if self.net_g is not None:
            body["net_g"] = self.net_g.value
        if self.diameter is not None:
            body["diameter"] = self.diameter.value
        if self.lot is not None:
            body["lot_nr"] = self.lot.value
        if self.comment():
            body["comment"] = self.comment()
        return body

    def to_dict(self) -> dict[str, Any]:
        def f(x: Found | None) -> dict[str, Any] | None:
            if x is None:
                return None
            v = list(x.value) if isinstance(x.value, tuple) else x.value
            return {"value": v, "line": x.line}

        return {
            "source": self.source,
            "fields": {
                "vendor": f(self.vendor),
                "material": f(self.material),
                "colour": f(self.colour),
                "name": self.name,
                "net_g": f(self.net_g),
                "diameter": f(self.diameter),
                "nozzle_c": f(self.nozzle_c),
                "bed_c": f(self.bed_c),
                "lot": f(self.lot),
            },
            "missing": self.missing,
            "receive": self.receive_body(),
            "text": self.text,
        }


def _range(v: tuple[int, int]) -> str:
    lo, hi = v
    return f"{lo}" if lo == hi else f"{lo}–{hi}"


# C0/C1 control characters (but tab and newline): an OCR'd or pasted label must not carry
# terminal escapes into the printed proposal or the Spoolman comment.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean(text: str) -> str:
    """`text` without control characters."""
    return _CONTROL.sub("", text)


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def parse_label(
    text: str, vendors: Iterable[str] = (), *, source: str = ""
) -> Proposal:
    """Apply the label rules to OCR text. `vendors`: names known besides BUILTIN_VENDORS."""
    text = clean(text)
    lines = _lines(text)
    p = Proposal(source=clean(source), text=text)
    p.vendor = _vendor(lines, [*BUILTIN_VENDORS, *vendors])
    p.material = _first(lines, _material)
    p.colour = _colour(lines)
    p.line_words = _line_words(lines)
    p.net_g = _net(lines)
    p.diameter = _first(lines, _diameter)
    p.nozzle_c, p.bed_c = _temps(lines)
    p.lot = _first(lines, _lot)
    return p


def _first(lines: list[str], read: Any) -> Found | None:
    for ln in lines:
        v = read(ln)
        if v is not None:
            return Found(v, ln)
    return None


def _vendor(lines: list[str], vendors: list[str]) -> Found | None:
    """The longest known vendor name that appears as whole words (case and spacing free)."""
    by_key: dict[str, str] = {}
    for v in vendors:
        k = vendor_key(v)
        if len(k) >= 3:
            by_key.setdefault(k, v)
    best: tuple[int, int, Found] | None = None
    for i, ln in enumerate(lines):
        words = re.findall(r"[\w+&:.'-]+", ln)
        for n in range(len(words)):
            for m in range(n + 1, min(n + 4, len(words)) + 1):
                name = by_key.get(vendor_key(" ".join(words[n:m])))
                if name is None:
                    continue
                cand = (len(vendor_key(name)), -i, Found(name, ln))
                if best is None or cand[:2] > best[:2]:
                    best = cand
    return best[2] if best else None


def _material(line: str) -> str | None:
    for rx, mat in _MATERIAL_RES:
        if rx.search(line):
            return mat
    return None


def _colour_in(s: str) -> dict[str, str] | None:
    """The first colour word of `s` (by position; at one position the longer name, which
    _COLOURS lists first)."""
    hit = _HEX_ON_LABEL.search(s)
    best: tuple[int, int] | None = None
    for i, (rx, *_) in enumerate(_COLOUR_RES):
        m = rx.search(s)
        if m is not None and (best is None or m.start() < best[0]):
            best = (m.start(), i)
    if best is None:
        return None
    _, en, hex_, ru = _COLOUR_RES[best[1]]
    return {"name": en, "hex": f"#{hit[1].upper()}" if hit else hex_, "ru": ru}


def _colour(lines: list[str]) -> Found | None:
    """A 'Colour: …' line wins over a colour word anywhere (a label can list other colours)."""
    for ln in lines:
        m = _COLOUR_KEY.search(ln)
        if m is not None:
            c = _colour_in(m["rest"])
            if c is not None:
                return Found(c, ln)
    return _first(lines, _colour_in)


def _line_words(lines: list[str]) -> list[str]:
    text = " ".join(lines)
    return [
        w
        for w in _LINE_WORDS
        if re.search(rf"(?<![\w]){re.escape(w)}(?![\w])", text, re.IGNORECASE)
    ]


def _grams(num: str, unit: str) -> float:
    if unit.lower() in ("kg", "кг"):
        return float(num.replace(",", ".")) * 1000
    if re.fullmatch(
        r"\d{1,2}[.,]\d{3}", num
    ):  # "1.000 g" / "1,000 g": a thousands separator
        return float(num[:-4] + num[-3:])
    return float(num.replace(",", "."))


def _net(lines: list[str]) -> Found | None:
    """The net weight: a weight on a 'net'/'weight' line beats a bare one; gross never counts."""
    best: tuple[int, int, Found] | None = None
    for i, ln in enumerate(lines):
        if _GROSS_WORDS.search(ln) and not re.search(r"net|нетто", ln, re.IGNORECASE):
            continue
        for m in _WEIGHT.finditer(ln):
            g = _grams(m["num"], m["unit"])
            if not 100 <= g <= 10000:
                continue
            score = 2 if _NET_WORDS.search(ln) else 1
            cand = (score, -i, Found(g, ln))
            if best is None or cand[:2] > best[:2]:
                best = cand
    return best[2] if best else None


def _diameter(line: str) -> float | None:
    m = _DIAMETER.search(line)
    if m is None or not _DIAMETER_WORDS.search(line):
        return None
    return float(m["d"].replace(",", "."))


def _temps(lines: list[str]) -> tuple[Found | None, Found | None]:
    """(nozzle, bed) °C ranges. The words before a temperature (back to the previous one on
    the line) say which it is, else the previous line's words; without words, a range from
    150 °C up is the nozzle and one up to 120 °C the bed."""
    nozzle: Found | None = None
    bed: Found | None = None
    for i, ln in enumerate(lines):
        matches = list(_TEMP_RANGE.finditer(ln))
        temps = [(int(m["lo"]), int(m["hi"]), m.end()) for m in matches] or [
            (int(m["t"]), int(m["t"]), m.end()) for m in _TEMP_ONE.finditer(ln)
        ]
        start = 0
        for a, b, end in temps:
            lo, hi = min(a, b), max(a, b)
            kind = _temp_kind(ln[start:end], lines[i - 1] if i else "", lo, hi)
            start = end
            if kind == "nozzle" and nozzle is None and 150 <= lo <= hi <= 450:
                nozzle = Found((lo, hi), ln)
            elif kind == "bed" and bed is None and 0 <= lo <= hi <= 150:
                bed = Found((lo, hi), ln)
    return nozzle, bed


def _temp_kind(segment: str, prev_line: str, lo: int, hi: int) -> str | None:
    for ctx in (segment, prev_line):
        bed, nozzle = _BED_WORDS.search(ctx), _NOZZLE_WORDS.search(ctx)
        if bed and not nozzle:
            return "bed"
        if nozzle and not bed:
            return "nozzle"
    if lo >= 150:
        return "nozzle"
    return "bed" if hi <= 120 else None


def _lot(line: str) -> str | None:
    m = _LOT.search(line)
    return m["lot"] if m else None
