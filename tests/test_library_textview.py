"""Unit tests for lib/library/textview.py: the terminal lines of a search result and `--columns`."""

from __future__ import annotations

from typing import Any

import pytest

from errors import InvalidArgument
from library import analytics, textview


def _record(**over: Any) -> dict[str, Any]:
    files = [("case.step", 900_000), ("case.stl", 300_000), ("assembled.3mf", 100_000)]
    facts = analytics.file_facts(files)
    facts["reference"] = analytics.reference_suitability(
        facts, paid=False, license_name="CC-BY"
    )
    rec: dict[str, Any] = {
        "site": "printables",
        "id": "5",
        "title": "Capacitive soil moisture sensor case",
        "url": "https://x/model/5",
        "author": "maker",
        "likes": 41,
        "downloads": 493,
        "makes": 7,
        "views": None,
        "license": "CC-BY",
        "price": "",
        "paid": False,
        "summary": "Case for capacitive soil moisture sensor v1.2 and v2.0 " * 6,
        "tags": ["arduino", "moisture"],
        "image": "https://i/1.png",
        "date": "2026-01-31T09:18:54+00:00",
        "category": "Gadgets",
        "cad": True,
        "cad_formats": ["step"],
        "analytics": facts,
    }
    rec.update(over)
    return rec


def test_default_view_has_title_stats_facts_description_and_url() -> None:
    lines = textview.render_result(_record(), 3, textview.DEFAULT_COLUMNS, 100)
    assert lines[0] == "  3. Capacitive soil moisture sensor case"
    assert lines[1] == "     by maker  likes 41  downloads 493  CC-BY  free"
    assert lines[2] == (
        "     CAD: yes (step)  size ?  files 3 (1 3mf, 1 step, 1 stl, 1.3 MB) [multi-part, assembly]  [img]"
    )
    assert lines[3].startswith(
        "     Case for capacitive soil moisture sensor v1.2"
    ) and lines[3].endswith("…")
    assert len(lines[3]) <= 100
    assert lines[4] == "     https://x/model/5"
    assert len(lines) == 5


def test_size_is_printed_only_when_a_mesh_was_measured() -> None:
    assert (
        "size 62x40.5x21 mm"
        in textview.render_result(
            _record(size_mm=[62.0, 40.5, 21.0]), 1, ("size",), 100
        )[1]
    )
    assert "size ?" in textview.render_result(_record(), 1, ("size",), 100)[1]


def test_unknown_file_list_prints_question_marks_not_no() -> None:
    rec = _record(
        cad=False,
        cad_formats=[],
        analytics={"schema": analytics.SCHEMA, "files_known": False},
    )
    line = textview.render_result(rec, 1, ("cad", "files"), 100)[1]
    assert line == "     CAD: ?  files ?"
    assert (
        "CAD: no"
        in textview.render_result(_record(cad=False, cad_formats=[]), 1, ("cad",), 100)[
            1
        ]
    )


def test_columns_choose_and_order_the_shown_fields() -> None:
    cols = textview.parse_columns("url, cad ,author,site")
    assert cols == ("url", "cad", "author", "site")
    lines = textview.render_result(_record(), 1, cols, 100)
    assert lines == [
        "  1. Capacitive soil moisture sensor case  [printables]",
        "     by maker",
        "     CAD: yes (step)",
        "     https://x/model/5",
    ]


def test_missing_counts_print_a_question_mark_and_empty_price_is_free() -> None:
    line = textview.render_result(
        _record(views=None, makes=None), 1, ("views", "makes", "price", "license"), 100
    )[1]
    assert line == "     views ?  makes ?  free  CC-BY"
    assert (
        "license ?"
        in textview.render_result(_record(license=""), 1, ("license",), 100)[1]
    )


def test_lines_are_clipped_to_the_terminal_width_except_the_url() -> None:
    rec = _record(title="T" * 200, url="https://example.com/" + "u" * 150)
    lines = textview.render_result(rec, 1, textview.DEFAULT_COLUMNS, 60)
    assert all(len(line) <= 60 for line in lines[:-1]) and lines[0].endswith("…")
    assert lines[-1] == "     " + rec["url"]


def test_tags_and_reference_columns() -> None:
    lines = textview.render_result(
        _record(), 1, ("tags", "ref", "date", "category"), 100
    )
    assert "     tags arduino, moisture" in lines
    assert "     2026-01-31  Gadgets" in lines
    assert any(line.startswith("     ref: yes") for line in lines)
    rec = _record()
    rec["analytics"]["reference"] = {
        "suitable": False,
        "dims_known": False,
        "reasons": ["paid", "no licence listed"],
    }
    assert (
        "ref: no (paid; no licence listed)"
        in textview.render_result(rec, 1, ("ref",), 100)[1]
    )


def test_unknown_column_is_an_invalid_argument_listing_the_accepted_ones() -> None:
    with pytest.raises(InvalidArgument) as exc:
        textview.parse_columns("author,colour")
    shown = exc.value.render(color=False)
    assert "colour" in shown and "description" in shown
    with pytest.raises(InvalidArgument):
        textview.parse_columns(" , ")


@pytest.mark.parametrize(
    "n,text",
    [
        (0, "0 B"),
        (999, "999 B"),
        (1000, "1 KB"),
        (347_500, "348 KB"),
        (12_500_000, "12.5 MB"),
        (3_200_000_000, "3.2 GB"),
    ],
)
def test_human_bytes(n: int, text: str) -> None:
    assert textview.human_bytes(n) == text
