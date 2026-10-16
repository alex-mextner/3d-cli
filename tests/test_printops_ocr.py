"""Unit tests for printops.ocr — tesseract over the four turns of a label photo (#379)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.env import find_magick, find_tesseract
from errors import InputNotFound, MissingDependency
from printops import ocr
from printops.spool_label import parse_label

LABELS = Path(__file__).resolve().parent / "data" / "spool_labels"
HAVE_OCR = find_tesseract() is not None and find_magick() is not None

TSV_HEAD = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"


def _word(block: int, line: int, conf: float, text: str) -> str:
    return f"5\t1\t{block}\t1\t{line}\t1\t0\t0\t10\t10\t{conf}\t{text}"


def test_tsv_words_are_grouped_into_lines_and_noise_is_dropped() -> None:
    tsv = "\n".join(
        [
            TSV_HEAD,
            "4\t1\t1\t1\t1\t0\t0\t0\t10\t10\t-1\t",
            _word(1, 1, 96.0, "eSUN"),
            _word(1, 1, 91.0, "PLA+"),
            _word(1, 2, 12.0, "~%"),
            _word(1, 2, 88.0, "1KG"),
            _word(2, 1, 40.0, "Black"),
        ]
    )

    text, score = ocr.parse_tsv(tsv)

    assert text == "eSUN PLA+\n1KG\nBlack"
    # confident words only: eSUN 4×0.96 + PLA 3×0.91 + 1KG 3×0.88; Black (40) is kept but unscored
    assert score == round(4 * 0.96 + 3 * 0.91 + 3 * 0.88, 1)


def test_missing_tesseract_is_a_missing_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    photo = tmp_path / "label.jpg"
    photo.write_bytes(b"\xff\xd8")
    monkeypatch.setattr(ocr, "require_tesseract", _no_tesseract)

    with pytest.raises(MissingDependency) as e:
        ocr.read_label(photo)

    assert e.value.exit_code == 127
    assert "tesseract" in str(e.value)


def _no_tesseract(command: str | None = None) -> str:
    raise MissingDependency(
        "tesseract (OCR)", install="apt-get install -y tesseract-ocr", command=command
    )


def test_a_missing_photo_is_input_not_found(tmp_path: Path) -> None:
    with pytest.raises(InputNotFound):
        ocr.read_label(tmp_path / "nope.jpg")


@pytest.mark.skipif(not HAVE_OCR, reason="tesseract and ImageMagick are not installed")
def test_an_upside_down_label_is_read_from_its_best_turn() -> None:
    got = ocr.read_label(LABELS / "synthetic-esun-upside-down.png")
    p = parse_label(got.text)

    assert got.rotation == 180
    assert p.vendor is not None and p.vendor.value == "eSUN"
    assert p.material is not None and p.material.value == "PLA+"
    assert p.net_g is not None and p.net_g.value == 1000.0
    assert p.lot is not None and p.lot.value == "ES2408A12"
