"""Constants parser/writer + project scanner tests."""
from __future__ import annotations

import pathlib

from web import constants_io, scan

SAMPLE = """\
// model contract
co2_od = 22;        // [10:40] cartridge OD
body_od = 28;
carr_h = 3.5;       // carrier height
use_cap = true;     // toggle
name = "demo";
module thing() {
    inner = 5;      // must be ignored (inside braces)
}
"""


def test_parse_constants(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "constants.scad"
    f.write_text(SAMPLE)
    rows = constants_io.parse_constants(f)
    names = {r["name"]: r for r in rows}
    assert "co2_od" in names and names["co2_od"]["type"] == "integer"
    assert names["co2_od"]["range"] == "10:40"
    assert names["carr_h"]["type"] == "number"
    assert names["use_cap"]["type"] == "boolean"
    assert "inner" not in names  # brace-scoped assignment skipped


def test_apply_changes_surgical(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "constants.scad"
    f.write_text(SAMPLE)
    applied = constants_io.apply_changes(f, {"co2_od": "25", "carr_h": "4.2", "missing": "9"})
    assert applied == {"co2_od": "25", "carr_h": "4.2"}
    after = {r["name"]: r["value"] for r in constants_io.parse_constants(f)}
    assert after["co2_od"] == "25" and after["carr_h"] == "4.2"
    # untouched lines + comments preserved
    text = f.read_text()
    assert "// [10:40] cartridge OD" in text
    assert 'name = "demo";' in text
    assert "body_od = 28;" in text


def test_scan_projects(tmp_path: pathlib.Path) -> None:
    (tmp_path / "p1").mkdir()
    (tmp_path / "p1" / "model.scad").write_text("cube(1);")
    (tmp_path / "p1" / "SPEC.md").write_text("# spec")
    (tmp_path / "p1" / "constants.scad").write_text("x = 1;")
    (tmp_path / "p2").mkdir()
    (tmp_path / "p2" / "assembly.scad").write_text("sphere(1);")
    (tmp_path / "p2" / "parts").mkdir()
    (tmp_path / "p2" / "parts" / "a.scad").write_text("circle(1);")
    (tmp_path / "notaproject").mkdir()
    (tmp_path / "notaproject" / "readme.txt").write_text("hi")

    projs = scan.scan_projects(tmp_path)
    by_name = {p.name: p for p in projs}
    assert set(by_name) == {"p1", "p2"}
    assert by_name["p1"].spec is not None
    assert by_name["p1"].constants is not None
    assert by_name["p1"].primary_scad.endswith("model.scad")  # type: ignore[union-attr]
    # p2 gathers the part file but stays a single project (leaf)
    assert len(by_name["p2"].scad_files) == 2
    assert by_name["p2"].primary_scad.endswith("assembly.scad")  # type: ignore[union-attr]


def test_scan_missing_root(tmp_path: pathlib.Path) -> None:
    assert scan.scan_projects(tmp_path / "nope") == []
