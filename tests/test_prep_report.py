"""Tests of the markdown proof report (lib/prep/report.py): ordering, links that resolve, FAIL flagging, determinism."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from errors import InputNotFound, InvalidArgument
from prep.raster import write_png
from prep.report import PROOF_DIR, REPORT_NAME, ProofReport


def _png(path: Path, value: int = 128) -> Path:
    write_png(path, np.full((8, 8, 3), value, dtype=np.uint8))
    return path


def _build(tmp_path: Path) -> ProofReport:
    img = _png(tmp_path / "src" / "heat.png")
    rep = ProofReport()
    rep.add_section(
        "Mesh repair",
        "PASS",
        "watertight, 0 defects",
        [("faces", "411000")],
        [img],
        ["note one"],
        seconds=1.5,
    )
    rep.add_section(
        "Overhangs",
        "WARN",
        "3% of area over 45 deg",
        None,
        [np.zeros((8, 8, 3), dtype=np.uint8)],
        seconds=0.25,
    )
    rep.add_section(
        "Phone fit", "FAIL", "Pixel 9 collides | wall", [("clearance", "-1.2 mm")]
    )
    rep.add_section("Colours", "SKIP", "not requested")
    rep.add_limit("Overhang map assumes 0.2 mm layers")
    return rep


def _write(tmp_path: Path, name: str = "out") -> tuple[ProofReport, Path]:
    rep = _build(tmp_path)
    path = rep.write(
        tmp_path / name,
        title="Proof",
        subtitle="cat stand",
        meta={"Model": "cat.stl", "Digest": "abc123"},
    )
    return rep, path


def test_table_rows_are_in_step_order_with_plain_text_verdicts(tmp_path: Path) -> None:
    _, path = _write(tmp_path)
    assert path.name == REPORT_NAME
    text = path.read_text()
    rows = [ln for ln in text.splitlines() if re.match(r"\| \d+ \|", ln)]
    assert [r.split("|")[2].strip() for r in rows] == [
        "Mesh repair",
        "Overhangs",
        "Phone fit",
        "Colours",
    ]
    assert [r.split("|")[3].strip() for r in rows] == ["PASS", "WARN", "FAIL", "SKIP"]
    assert rows[0].split("|")[5].strip() == "1.50"
    assert rows[2].rsplit("|", 2)[1].strip() == "-"  # no timing given
    assert (
        "collides \\| wall" in rows[2]
    )  # the pipe in a summary cannot break the table
    assert not re.search(r"[^\x00-\x7f]", text)  # no emoji or symbol verdicts


def test_header_carries_meta_and_overall_verdict(tmp_path: Path) -> None:
    rep, path = _write(tmp_path)
    text = path.read_text()
    assert text.startswith("# Proof\n")
    assert "- **Model**: cat.stl" in text and "- **Digest**: abc123" in text
    assert "**Overall: FAIL**" in text
    assert rep.overall == "FAIL"


def test_overall_is_the_worst_verdict(tmp_path: Path) -> None:
    rep = ProofReport()
    assert rep.overall == "SKIP"
    rep.add_section("a", "PASS", "ok")
    assert rep.overall == "PASS"
    rep.add_section("b", "WARN", "hmm")
    assert rep.overall == "WARN"
    rep.add_section("c", "FAIL", "bad")
    assert rep.overall == "FAIL"


def test_image_links_are_relative_and_resolve(tmp_path: Path) -> None:
    _, path = _write(tmp_path)
    links = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", path.read_text())
    assert len(links) == 2
    for link in links:
        assert not link.startswith("/") and link.startswith(f"{PROOF_DIR}/")
        assert (path.parent / link).is_file()
        assert (path.parent / link).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    # the file copied from outside is a faithful copy, the array one was rendered
    assert (path.parent / links[0]).read_bytes() == (
        tmp_path / "src" / "heat.png"
    ).read_bytes()


def test_fail_section_is_flagged_and_pass_is_not(tmp_path: Path) -> None:
    _, path = _write(tmp_path)
    text = path.read_text()
    assert "## 3. Phone fit - FAIL" in text
    assert "> **FAIL**: Pixel 9 collides | wall" in text
    assert text.count("> **FAIL**") == 1
    assert "## 1. Mesh repair - PASS" in text


def test_sections_carry_rows_notes_and_images_in_place(tmp_path: Path) -> None:
    _, path = _write(tmp_path)
    text = path.read_text()
    first = text.index("## 1. Mesh repair")
    second = text.index("## 2. Overhangs")
    block = text[first:second]
    assert "| faces | 411000 |" in block and "- note one" in block and "![" in block
    assert "![" not in text[text.index("## 3. Phone fit") :]


def test_honest_limits_close_the_report(tmp_path: Path) -> None:
    _, path = _write(tmp_path)
    text = path.read_text()
    assert text.rstrip().endswith("- Overhang map assumes 0.2 mm layers")
    assert text.index("## Honest limits") > text.index("## 4. Colours")
    empty = ProofReport()
    empty.add_section("a", "PASS", "ok")
    out = empty.write(tmp_path / "e", title="T").read_text()
    assert "- None recorded." in out


def test_output_is_deterministic_and_self_contained(tmp_path: Path) -> None:
    _, a = _write(tmp_path, "one")
    _, b = _write(tmp_path, "two")
    assert a.read_text() == b.read_text()
    assert sorted(p.name for p in (a.parent / PROOF_DIR).iterdir()) == sorted(
        p.name for p in (b.parent / PROOF_DIR).iterdir()
    )
    # writing again into the same directory is idempotent
    _, again = _write(tmp_path, "one")
    assert again.read_text() == a.read_text()


def test_bad_inputs_are_rejected(tmp_path: Path) -> None:
    rep = ProofReport()
    with pytest.raises(InvalidArgument):
        rep.add_section("x", "GREAT", "?")
    rep.add_section("x", "PASS", "ok", images=[tmp_path / "missing.png"])
    with pytest.raises(InputNotFound):
        rep.write(tmp_path / "o", title="T")
