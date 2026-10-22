"""Moonraker's active spool through `MoonrakerAdapter` (`[spoolman]` in moonraker.conf, the rooted
K1 SE): the status read, the write with its read-back, the refusals. Only the loopback FakeMoonraker."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator

import pytest
from printer_fakes import FakeMoonraker, free_port

from printer_ops.base import Adapter, PrinterCommError, UnsupportedOperation
from printer_ops.moonraker import MoonrakerAdapter

STATUS = "/server/spoolman/status"
SPOOL_ID = "/server/spoolman/spool_id"


@pytest.fixture
def moon() -> Iterator[FakeMoonraker]:
    fake = FakeMoonraker(spoolman=True, active_spool=14)
    with fake.serve() as served:
        yield served


def _adapter(moon: FakeMoonraker) -> MoonrakerAdapter:
    return MoonrakerAdapter("k1se", "127.0.0.1", moon.port)


def _paths(moon: FakeMoonraker) -> list[tuple[str, str]]:
    return [(r.method, r.path) for r in moon.requests]


def test_active_spool_reads_the_link_and_the_selected_spool(moon: FakeMoonraker) -> None:
    moon.spoolman_pending = [{"spool_id": 14, "filament_used": 12.5}, {"spool_id": 14, "filament_used": 3.0}]
    got = _adapter(moon).active_spool()
    assert (got.connected, got.spool_id, got.pending_reports) == (True, 14, 2)
    assert _paths(moon) == [("GET", STATUS)]


def test_set_active_spool_posts_the_id_as_json_and_reads_it_back(moon: FakeMoonraker) -> None:
    got = _adapter(moon).set_active_spool(2)
    assert (got.before, got.after) == (14, 2)
    assert moon.active_spool == 2
    assert _paths(moon) == [("GET", STATUS), ("POST", SPOOL_ID), ("GET", SPOOL_ID)]
    assert json.loads(moon.writes()[0].fields["body"]) == {"spool_id": 2}


def test_set_active_spool_none_clears_it_with_a_json_null(moon: FakeMoonraker) -> None:
    got = _adapter(moon).set_active_spool(None)
    assert (got.before, got.after) == (14, None)
    assert moon.active_spool is None
    assert json.loads(moon.writes()[0].fields["body"]) == {"spool_id": None}


def test_the_write_is_made_while_moonraker_cannot_reach_spoolman_and_says_so(moon: FakeMoonraker) -> None:
    """Moonraker keeps the id in its own database and queues usage per spool id until the link is
    back, so the write is right (and a refusal would leave the old spool collecting the usage)."""
    moon.spoolman_connected = False
    moon.spoolman_pending = [{"spool_id": 14, "filament_used": 800.0}]
    adapter = _adapter(moon)
    assert adapter.active_spool().connected is False
    got = adapter.set_active_spool(2)
    assert (got.before, got.after, got.connected, got.pending_reports) == (14, 2, False, 1)
    assert moon.active_spool == 2


def test_the_write_reports_a_connected_moonraker(moon: FakeMoonraker) -> None:
    got = _adapter(moon).set_active_spool(2)
    assert (got.connected, got.pending_reports) == (True, 0)


@pytest.mark.parametrize(
    ("fault", "needle"),
    [
        (lambda m: setattr(m, "spoolman_error", 500), "HTTP 500"),
        (lambda m: setattr(m, "spoolman_raw", b"<html>proxy error</html>"), "non-JSON reply"),
        (lambda m: setattr(m, "active_spool", "14"), "neither an integer nor null"),
        (lambda m: setattr(m, "active_spool", True), "neither an integer nor null"),
    ],
    ids=["http-500", "not-json", "id-as-string", "id-as-bool"],
)
def test_an_answer_that_cannot_be_read_is_a_comm_error(
    moon: FakeMoonraker, fault: Callable[[FakeMoonraker], None], needle: str
) -> None:
    fault(moon)
    with pytest.raises(PrinterCommError, match=needle):
        _adapter(moon).set_active_spool(2)
    assert moon.writes() == []


def test_a_lost_answer_to_a_write_that_landed_is_not_a_failure(moon: FakeMoonraker) -> None:
    """Moonraker applies the id before it answers: when the answer is lost the read-back decides."""
    moon.spoolman_set_error, moon.spoolman_set_error_after_apply = 500, True
    got = _adapter(moon).set_active_spool(2)
    assert (got.before, got.after) == (14, 2) and moon.active_spool == 2


def test_a_failed_write_that_did_not_land_is_an_error(moon: FakeMoonraker) -> None:
    moon.spoolman_set_error = 500
    with pytest.raises(PrinterCommError, match="HTTP 500"):
        _adapter(moon).set_active_spool(2)
    assert moon.active_spool == 14


def test_an_unreadable_read_back_after_a_good_write_is_an_error(moon: FakeMoonraker) -> None:
    moon.spoolman_read_error = 500
    with pytest.raises(PrinterCommError, match="HTTP 500"):
        _adapter(moon).set_active_spool(2)
    assert moon.active_spool == 2  # the write happened; the caller is told it is unverified


def test_a_404_on_the_write_itself_is_a_comm_error_not_unsupported(moon: FakeMoonraker) -> None:
    """The 404 that means "no [spoolman]" is only read from the first (status) call: the component
    is there when it answered, so a refused write must not be skipped as "unsupported"."""
    moon.spoolman_set_error = 404
    with pytest.raises(PrinterCommError, match="HTTP 404"):
        _adapter(moon).set_active_spool(2)
    assert moon.active_spool == 14


@pytest.mark.parametrize(
    "call",
    [lambda a: a.active_spool(), lambda a: a.set_active_spool(2)],
    ids=["active_spool", "set_active_spool"],
)
def test_a_printer_without_a_spoolman_section_is_unsupported(call: Callable[[MoonrakerAdapter], object]) -> None:
    """Moonraker answers 404 for the component it did not load (the stock U1)."""
    fake = FakeMoonraker()
    with fake.serve() as moon, pytest.raises(UnsupportedOperation, match=r"no \[spoolman\] section"):
        call(_adapter(moon))
    assert moon.writes() == []


def test_a_read_back_that_differs_is_an_error(moon: FakeMoonraker) -> None:
    moon.spoolman_ignores_set = True
    with pytest.raises(PrinterCommError, match=r"after setting the active spool to 2 Moonraker reports 14"):
        _adapter(moon).set_active_spool(2)


def test_an_unreachable_moonraker_is_a_comm_error() -> None:
    with pytest.raises(PrinterCommError):
        MoonrakerAdapter("k1se", "127.0.0.1", free_port(), timeout=1.0).set_active_spool(2)


def test_adapters_without_the_concept_say_so() -> None:
    base = Adapter("x", "127.0.0.1", 1)
    with pytest.raises(UnsupportedOperation, match="active spool"):
        base.active_spool()
    with pytest.raises(UnsupportedOperation, match="active spool"):
        base.set_active_spool(2)
