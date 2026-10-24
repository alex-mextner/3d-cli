from __future__ import annotations

import logging
import random

import pytest

from errors import GateFailure, InvalidArgument
from parts import (
    META_PRINTABLE,
    META_ROLE,
    NonPrintableParts,
    Part,
    ReferenceSource,
    assert_printable,
    assign_styles,
    filter_printable,
    legend,
    legend_text,
    part_from_dict,
    part_from_metadata,
)

SRC = ReferenceSource("https://www.printables.com/model/1", "CC-BY-4.0")


def sample() -> list[Part]:
    return [
        Part("lid"),
        Part("base"),
        Part("pcb", "vitamin", SRC),
        Part("usb_c", "reference", SRC),
        Part("battery_box", "stand_in"),
    ]


def test_printable_is_derived_from_role() -> None:
    assert [p.printable for p in sample()] == [True, True, False, False, False]


def test_unknown_role_and_missing_reference_source_are_rejected() -> None:
    with pytest.raises(InvalidArgument):
        Part("x", "bolt")
    with pytest.raises(InvalidArgument, match="reference_source"):
        Part("screw", "vitamin")
    with pytest.raises(InvalidArgument):
        Part("screw", "vitamin", ReferenceSource("https://x", ""))


def test_part_from_dict_rejects_contradicting_printable() -> None:
    assert part_from_dict({"name": "lid"}).printable
    with pytest.raises(InvalidArgument) as exc:
        part_from_dict({"name": "pcb", "role": "stand_in", "printable": True})
    assert "derived from `role`" in " ".join(exc.value.remediation)
    p = part_from_dict({"name": "pcb", "role": "vitamin", "printable": False,
                        "reference_source": {"url": "https://x", "license": "MIT"}})
    assert p.reference_source == ReferenceSource("https://x", "MIT")


def test_assert_printable_refuses_non_printable_parts() -> None:
    with pytest.raises(NonPrintableParts) as exc:
        assert_printable(sample(), command="arrange")
    assert isinstance(exc.value, GateFailure) and exc.value.exit_code == 1
    assert {p.name for p in exc.value.parts} == {"pcb", "usb_c", "battery_box"}
    assert "--include-non-printable" in " ".join(exc.value.remediation)


def test_assert_printable_override_is_logged_and_reported(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="3d.parts"):
        guard = assert_printable(sample(), include_non_printable=True, command="export")
    assert [p.name for p in guard.overridden] == ["pcb", "usb_c", "battery_box"]
    assert len(guard.parts) == 5
    assert "pcb:vitamin" in caplog.text and "export" in caplog.text


def test_assert_printable_passes_all_printed_without_log(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="3d.parts"):
        guard = assert_printable([Part("a"), Part("b")])
    assert guard.overridden == () and caplog.text == ""


def test_filter_printable_drops_and_reports() -> None:
    kept, dropped = filter_printable(sample())
    assert [p.name for p in kept] == ["lid", "base"]
    assert [p.name for p in dropped] == ["pcb", "usb_c", "battery_box"]


def test_metadata_tag_round_trips_the_role() -> None:
    for p in sample():
        meta = p.to_metadata()
        assert meta[META_PRINTABLE] == ("true" if p.printable else "false")
        back = part_from_metadata(p.name, meta)
        assert back.role == p.role and back.printable == p.printable


def test_untagged_object_is_printed_and_false_tag_is_never_read_as_printed() -> None:
    assert part_from_metadata("lid", {}).role == "printed"
    assert not part_from_metadata("x", {META_PRINTABLE: "false"}).printable
    assert META_ROLE == "3d:role"


def test_palette_is_distinct_stable_and_order_independent() -> None:
    parts = [Part(f"part_{i}") for i in range(40)] + sample()
    first = assign_styles(parts)
    shuffled = parts[:]
    random.Random(7).shuffle(shuffled)
    assert assign_styles(shuffled) == first
    colors = [s.color for s in first.values()]
    assert len(set(colors)) == len(colors) == len(parts)
    assert all(len(c) == 7 and c[0] == "#" for c in colors)


def test_palette_color_is_the_same_for_the_same_name_across_calls() -> None:
    assert assign_styles([Part("lid")])["lid"] == assign_styles([Part("lid")])["lid"]


def test_stand_ins_are_always_ghost_and_others_solid() -> None:
    styles = assign_styles(sample())
    assert styles["battery_box"].style == "ghost" and styles["battery_box"].alpha < 0.5
    assert all(styles[n].style == "solid" and styles[n].alpha == 1.0 for n in ("lid", "base", "pcb", "usb_c"))


def test_legend_lists_each_part_once_with_source() -> None:
    rows = legend([*sample(), Part("lid")])
    assert [r["name"] for r in rows] == ["base", "battery_box", "lid", "pcb", "usb_c"]
    pcb = next(r for r in rows if r["name"] == "pcb")
    assert pcb["reference_source"]["license"] == "CC-BY-4.0" and pcb["printable"] is False
    text = legend_text(sample())
    assert "battery_box" in text and "(ghost)" in text
