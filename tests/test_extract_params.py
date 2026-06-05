"""Unit tests for extract_params.py — Customizer parameter extraction."""
from __future__ import annotations

import pathlib
from typing import Any

from extract_params import extract, main


def test_extract_basic(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "params.scad"
    f.write_text("""
width = 20;     // [10:40] width
height = 10;
use_cap = true; // toggle
name = "demo";  // name
arr = [1,2,3];
expr = 1 + 2;
""")
    rows = extract(str(f))
    by_name = {r["name"]: r for r in rows}
    assert "width" in by_name
    assert by_name["width"]["type"] == "integer"
    assert by_name["width"]["range"] == "10:40"
    assert by_name["height"]["type"] == "integer"
    assert by_name["use_cap"]["type"] == "boolean"
    assert by_name["name"]["type"] == "string"
    assert by_name["arr"]["type"] == "array"
    assert by_name["expr"]["type"] == "expression"


def test_extract_skips_braced(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "params.scad"
    f.write_text("""
outer = 5;
module thing() {
    inner = 3;
}
""")
    rows = extract(str(f))
    names = [r["name"] for r in rows]
    assert "outer" in names
    assert "inner" not in names


def test_extract_dropdown(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "params.scad"
    f.write_text('shape = "circle"; // [circle, square] pick one\n')
    rows = extract(str(f))
    assert rows[0]["options"] == "circle, square"
    assert rows[0]["description"] == "pick one"


def test_extract_no_comment(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "params.scad"
    f.write_text("x = 1;\n")
    rows = extract(str(f))
    assert rows[0]["description"] == ""
    assert rows[0]["range"] == ""


def test_main_json(tmp_path: pathlib.Path, capsys: Any) -> None:
    f = tmp_path / "params.scad"
    f.write_text("width = 20; // [10:40]\n")
    rc = main(["extract_params", str(f), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"name": "width"' in out


def test_main_table(tmp_path: pathlib.Path, capsys: Any) -> None:
    f = tmp_path / "params.scad"
    f.write_text("width = 20; // [10:40]\n")
    rc = main(["extract_params", str(f)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "width" in out


def test_main_no_args(capsys: Any) -> None:
    rc = main(["extract_params"])
    assert rc == 1
    assert "usage" in capsys.readouterr().err.lower()
