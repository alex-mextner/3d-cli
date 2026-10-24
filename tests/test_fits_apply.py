"""fits_apply: the `@fit` marker, the snippet and the idempotent correction of declared clearances."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

import fits
import fits_apply
from errors import InvalidArgument
from fits import Correction

STAMP = "2026-10-24T10:00:00Z"


def _resolver(**kw: Any) -> Callable[[str, str], fits.Resolution]:
    def resolve(fit: str, shape: str) -> fits.Resolution:
        return fits.resolve(fit, shape, **kw)

    return resolve


SCAD = """\
// a mate
clearance = 0.2;        // @fit sliding shape=planar mm gap per mating face
  pin_gap = 0.30 ; // @fit snap shape=round
other = 0.2;            // a bare number is not a fit
tight_gap = .1; // @fit tight
"""


def test_every_marked_number_is_replaced_by_the_resolved_value_and_nothing_else_changes() -> (
    None
):
    text, changes = fits_apply.correct_text(
        SCAD, _resolver(printer="k1se", material="PETG")
    )
    assert [(c.name, c.fit, c.shape, c.before, c.after) for c in changes] == [
        ("clearance", "sliding", "planar", 0.2, 0.25),
        ("pin_gap", "snap", "round", 0.3, 0.35),
        ("tight_gap", "tight", "planar", 0.1, 0.15),
    ]
    assert [c.line for c in changes] == [2, 3, 5]
    assert text.splitlines() == [
        "// a mate",
        "clearance = 0.25;        // @fit sliding shape=planar mm gap per mating face",
        "  pin_gap = 0.35 ; // @fit snap shape=round",
        "other = 0.2;            // a bare number is not a fit",
        "tight_gap = 0.15; // @fit tight",
    ]


def test_reapplying_is_a_no_op() -> None:
    once, _ = fits_apply.correct_text(SCAD, _resolver(printer="k1se", material="PETG"))
    twice, changes = fits_apply.correct_text(
        once, _resolver(printer="k1se", material="PETG")
    )
    assert twice == once and not any(c.changed for c in changes)


def test_the_value_is_absolute_so_a_hand_edited_number_is_replaced() -> None:
    text, changes = fits_apply.correct_text("c = 9; // @fit sliding\n", _resolver())
    assert (
        text == "c = 0.2; // @fit sliding\n"
        and changes[0].before == 9
        and changes[0].changed
    )


def test_an_unchanged_marker_keeps_its_original_spelling() -> None:
    text, changes = fits_apply.correct_text("c = 0.20; // @fit sliding\n", _resolver())
    assert text == "c = 0.20; // @fit sliding\n" and not changes[0].changed


def test_a_calibration_row_for_the_printer_is_what_the_file_gets() -> None:
    row = Correction(
        delta_mm=0.12,
        source="calibration",
        printer="k1se",
        material="PLA",
        nozzle_mm=0.4,
        shape="round",
        fit_type="sliding",
        measured_at=STAMP,
    )
    resolve = _resolver(
        printer="k1se", material="PLA", nozzle=0.4, extra_corrections=[row]
    )
    text, changes = fits_apply.correct_text(
        "c = 0.2; // @fit sliding shape=round\n", resolve
    )
    corr = next(
        p for p in changes[0].resolution.provenance if p["kind"] == "correction"
    )
    assert text.startswith("c = 0.32;") and corr["status"] == "calibrated"


def test_unknown_fit_or_shape_in_a_marker_is_the_resolvers_invalid_argument() -> None:
    with pytest.raises(InvalidArgument, match="accepted: snap"):
        fits_apply.correct_text("c = 0.2; // @fit squishy\n", _resolver())
    with pytest.raises(InvalidArgument, match="accepted: planar"):
        fits_apply.correct_text("c = 0.2; // @fit snap shape=cube\n", _resolver())


def test_crlf_files_keep_their_line_endings() -> None:
    text, _ = fits_apply.correct_text(
        "c = 0.5; // @fit snap\r\nx = 1;\r\n", _resolver()
    )
    assert text == "c = 0.3; // @fit snap\r\nx = 1;\r\n"


def test_a_marker_without_a_number_or_in_a_non_assignment_is_not_a_declaration() -> (
    None
):
    text = "c = gap; // @fit snap\necho(0.2); // @fit snap\n/* @fit snap */\n"
    assert fits_apply.correct_text(text, _resolver()) == (text, [])


@pytest.mark.parametrize(
    ("value", "literal"),
    [(0.35, "0.35"), (-0.1, "-0.1"), (0.2, "0.2"), (0.0, "0"), (0.12345, "0.1235")],
)
def test_format_mm_is_a_compact_literal(value: float, literal: str) -> None:
    assert fits_apply.format_mm(value) == literal


def test_snippet_carries_the_marker_the_tolerance_and_the_status() -> None:
    res = fits.resolve("snap", "round", "k1se", "PLA", 0.4)
    snippet = fits_apply.scad_snippet(res, "latch_gap")
    lines = snippet.splitlines()
    assert (
        lines[0]
        == "// 3d fits apply: snap/round for k1se PLA (default, not calibrated)"
    )
    assert lines[1].startswith("latch_gap = 0.3;  // @fit snap shape=round ")
    assert lines[2].startswith("latch_gap_tol = 0.1;")
    assert "scale the model XY by 1.00301" in lines[3]
    # the snippet round-trips: it is a declaration `correct_text` finds again
    _, changes = fits_apply.correct_text(
        snippet, _resolver(printer="k1se", material="PLA")
    )
    assert [(c.name, c.changed) for c in changes] == [("latch_gap", False)]
