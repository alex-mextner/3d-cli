"""Unit tests for printops.spool_label — the rules that read a spool label's OCR text (#379)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from printops.spool_label import parse_label

ESUN = """\
eSUN PLA+
Color: Black
Net Weight: 1KG
Diameter: 1.75mm +0.03mm
Printing Temp: 205-225°C
Bed Temp: 60-80°C
Batch No: ES2408A12
"""


def test_an_english_label_gives_every_field_with_its_line() -> None:
    p = parse_label(ESUN, source="k1.jpg")

    assert p.vendor is not None and (p.vendor.value, p.vendor.line) == (
        "eSUN",
        "eSUN PLA+",
    )
    assert p.material is not None and p.material.value == "PLA+"
    assert p.colour is not None and p.colour.value == {
        "name": "Black",
        "hex": "#000000",
        "ru": "чёрный",
    }
    assert p.colour.line == "Color: Black"
    assert p.net_g is not None and p.net_g.value == 1000.0
    assert p.diameter is not None and p.diameter.value == 1.75
    assert p.nozzle_c is not None and p.nozzle_c.value == (205, 225)
    assert p.bed_c is not None and p.bed_c.value == (60, 80)
    assert p.lot is not None and p.lot.value == "ES2408A12"
    assert p.missing == []
    assert p.receive_body() == {
        "vendor": "eSUN",
        "material": "PLA+",
        "name": "Black",
        "color_hex": "#000000",
        "net_g": 1000.0,
        "diameter": 1.75,
        "lot_nr": "ES2408A12",
        "comment": "from label k1.jpg; nozzle 205–225 °C; bed 60–80 °C",
    }


def test_a_russian_label_reads_the_same_fields() -> None:
    text = """\
Пластик для 3D печати PLA
Цвет: чёрный
Вес нетто 1 кг
Диаметр 1,75 мм
Температура печати 190-220 °С
Температура стола 50-60 °С
Партия 2310A7
"""
    p = parse_label(text)

    assert p.material is not None and p.material.value == "PLA"
    assert p.colour is not None and p.colour.value["hex"] == "#000000"
    assert p.colour.value["ru"] == "чёрный"
    assert p.net_g is not None and p.net_g.value == 1000.0
    assert p.diameter is not None and p.diameter.value == 1.75
    assert p.nozzle_c is not None and p.nozzle_c.value == (190, 220)
    assert p.bed_c is not None and p.bed_c.value == (50, 60)
    assert p.lot is not None and p.lot.value == "2310A7"
    assert p.missing == ["vendor"]


def test_gross_weight_is_never_the_net() -> None:
    p = parse_label("G.W.: 1.25kg\nN.W.: 1kg\nPETG Blue")

    assert p.net_g is not None and p.net_g.value == 1000.0
    assert p.net_g.line == "N.W.: 1kg"


@pytest.mark.parametrize(
    "text", ["Net weight: 1.000 g", "Net weight: 1,000 g", "NW 1000g"]
)
def test_a_thousands_separator_in_grams_reads_as_a_thousand(text: str) -> None:
    p = parse_label(text)

    assert p.net_g is not None and p.net_g.value == 1000.0


def test_control_characters_never_reach_the_proposal() -> None:
    """An OCR'd or pasted label must not carry terminal escapes into the output or Spoolman."""
    p = parse_label(
        "\x1b]52;c;ZXZpbA==\x07Color: Black\x9b\nPLA", source="a\x1b[2Jb.txt"
    )

    assert p.colour is not None and p.colour.line == "]52;c;ZXZpbA==Color: Black"
    assert p.source == "a[2Jb.txt"
    assert "\x1b" not in p.text and "\x9b" not in p.text


def test_the_net_is_never_guessed() -> None:
    p = parse_label("SUNLU PLA White 1.75mm")

    assert p.net_g is None
    assert "net" not in p.receive_body()
    assert p.missing == ["net"]


def test_a_colour_line_wins_over_colour_words_elsewhere() -> None:
    p = parse_label("Available in red, blue, white\nPLA\nColour: Grey")

    assert p.colour is not None and p.colour.value["name"] == "Grey"


def test_without_a_colour_line_the_first_colour_word_wins() -> None:
    p = parse_label("PLA\nAvailable in red, blue, white")

    assert p.colour is not None and p.colour.value["name"] == "Red"


def test_a_printed_hex_is_kept_over_the_nominal_one() -> None:
    p = parse_label("Color: Black #1A1A1A")

    assert p.colour is not None and p.colour.value["hex"] == "#1A1A1A"


@pytest.mark.parametrize(
    ("text", "material"),
    [
        ("PLA PRO", "PLA+"),
        ("PLA Plus", "PLA+"),
        ("PLA +", "PLA+"),
        ("Hyper PLA", "PLA"),
        ("PETG", "PETG"),
        ("TPU 95A", "TPU"),
        ("ABS", "ABS"),
        ("SPLASH", None),  # a word that only contains PLA letters is no material
    ],
)
def test_material_forms(text: str, material: str | None) -> None:
    p = parse_label(text)

    assert (p.material.value if p.material else None) == material


def test_nozzle_and_bed_on_one_line_are_told_apart() -> None:
    p = parse_label("Nozzle 200-230°C Bed 60-70°C")

    assert p.nozzle_c is not None and p.nozzle_c.value == (200, 230)
    assert p.bed_c is not None and p.bed_c.value == (60, 70)


def test_temperatures_without_words_go_by_value() -> None:
    p = parse_label("190~220℃\n45-60°C")

    assert p.nozzle_c is not None and p.nozzle_c.value == (190, 220)
    assert p.bed_c is not None and p.bed_c.value == (45, 60)


def test_the_longest_known_vendor_wins_and_extra_vendors_count() -> None:
    p = parse_label(
        "Das Filament PLA Black 800 g", vendors=["Das Filament", "Bambu Lab"]
    )
    q = parse_label("Bambu Lab PLA Basic Black 1 kg")

    assert p.vendor is not None and p.vendor.value == "Das Filament"
    assert p.net_g is not None and p.net_g.value == 800.0
    assert q.vendor is not None and q.vendor.value == "Bambu Lab"
    assert q.name == "Basic Black"


def test_product_line_words_make_the_name() -> None:
    p = parse_label("Elegoo Matte PLA\nColor: Dark Grey\n1kg")

    assert p.name == "Matte Dark Grey"
    assert p.colour is not None and p.colour.value["ru"] == "тёмно-серый"


def test_nothing_readable_gives_an_empty_proposal() -> None:
    p = parse_label("")

    assert p.missing == ["vendor", "material", "colour", "net"]
    assert p.receive_body() == {}


def test_label_reading_needs_no_llm() -> None:
    """#363: the deterministic path imports no AI backend."""
    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys; sys.path.insert(0, 'lib');"
        "import printops.cli.spool, printops.ocr, printops.spool_label;"
        "assert 'ai.backends' not in sys.modules and 'ai' not in sys.modules"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], cwd=root, capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr
