"""`3d printers spools | load | unload | swap | filament` against the in-process 3d-api (TestClient
transport), a FakeSpoolman and loopback fake printers. Never a real printer or Spoolman."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from errors import GateFailure, InvalidArgument, UsageError
from fleet_fixtures import (
    k1_entry,
    k1se_moonraker_entry,
    u1_entry,
    write_fleet,
    write_policy_for_fleet,
)
from printer_fakes import FakeCreality, FakeMoonraker, free_port
from printers_cli import _spool_view as sv
from printers_cli._common import known_printers
from printers_cli.filament import run as filament
from printers_cli.load import run_load, run_swap, run_unload
from printers_cli.spools import run as spools_cmd

from printops import locations
from printops.client import ApiClient

BLACK = {
    "vendor": "Elegoo",
    "material": "PLA+",
    "name": "RAPID PLA+ Black",
    "color_hex": "#000000",
    "net_g": 1000,
    "location": "WH:default",
}
WHITE = BLACK | {"name": "RAPID PLA+ White", "color_hex": "#FFFFFF"}


@pytest.fixture
def k1() -> Iterator[FakeMoonraker]:
    """The rooted K1 SE: one head, Moonraker with [spoolman], spool #14 selected."""
    fake = FakeMoonraker(spoolman=True, active_spool=14)
    fake.status.pop("print_task_config")
    with fake.serve() as served:
        yield served


@pytest.fixture
def u1() -> Iterator[FakeMoonraker]:
    fake = FakeMoonraker(spoolman=True, active_spool=14)
    with fake.serve() as served:
        yield served


@pytest.fixture
def use_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    monkeypatch.setenv(
        "XDG_CONFIG_HOME", str(tmp_path / "xdg")
    )  # no real printer-devices.yaml
    monkeypatch.chdir(tmp_path)

    def apply(*entries: str) -> None:
        fleet = write_fleet(tmp_path / "fleet.yaml", *entries)
        monkeypatch.setenv("THREED_FLEET", str(fleet))
        monkeypatch.setenv(
            "PRINTOPS_POLICY",
            str(write_policy_for_fleet(tmp_path / "policy.yaml", fleet)),
        )

    monkeypatch.delenv("PRINTOPS_CALIBRATIONS", raising=False)
    return apply


@pytest.fixture
def fleet(use_fleet: Callable[..., None], k1: FakeMoonraker, u1: FakeMoonraker) -> None:
    """The fleet file in place BEFORE the 3d-api app starts (its reconciler reads the fleet once)."""
    use_fleet(k1se_moonraker_entry(k1), u1_entry(u1))


@pytest.fixture
def po(
    fleet: None,
    printops_db: sqlite3.Connection,
    api_client: ApiClient,
    monkeypatch: pytest.MonkeyPatch,
) -> ApiClient:
    """3d-api over a fleet of a U1 (T2 marked broken) and a single-head Moonraker K1 SE; the CLI
    subs get this very client from `ApiClient.from_config`."""
    locations.add(printops_db, "WH:default", "warehouse", actor="test")
    monkeypatch.setattr(ApiClient, "from_config", classmethod(lambda cls, **kw: api_client))
    return api_client


def _out(capsys: Any) -> str:
    return str(capsys.readouterr().out)


def _json(capsys: Any) -> Any:
    return json.loads(_out(capsys))


# ---- slot addressing ---------------------------------------------------------------------------


def test_resolve_slot_matches_heads_case_insensitively_and_defaults_a_single_slot(
    use_fleet: Callable[..., None], k1: FakeMoonraker, u1: FakeMoonraker
) -> None:
    use_fleet(k1se_moonraker_entry(k1), u1_entry(u1))
    assert sv.resolve_slot("u1/t3", "load").key == "u1/T3"
    assert sv.resolve_slot("u1/T3", "load").head_index == 2
    assert sv.resolve_slot("slot:u1/T1", "load").key == "u1/T1"
    only = sv.resolve_slot("k1se", "load")
    assert (only.key, only.head_index) == ("k1se/T1", 0)


def test_resolve_slot_refuses_what_the_fleet_does_not_declare(
    use_fleet: Callable[..., None], k1: FakeMoonraker, u1: FakeMoonraker
) -> None:
    use_fleet(k1se_moonraker_entry(k1), u1_entry(u1))
    with pytest.raises(UsageError) as many:
        sv.resolve_slot("u1", "load")  # four slots: which one?
    assert "T1, T2, T3, T4" in many.value.remediation[0]
    with pytest.raises(InvalidArgument) as head:
        sv.resolve_slot("u1/T9", "load")
    assert head.value.accepted == ["T1", "T2", "T3", "T4"]
    with pytest.raises(InvalidArgument) as printer:
        sv.resolve_slot("nope/T1", "load")
    assert "u1" in printer.value.accepted and "k1se" in printer.value.accepted


def test_resolve_slot_knows_ams_slots(
    use_fleet: Callable[..., None], k1: FakeMoonraker
) -> None:
    use_fleet(
        k1se_moonraker_entry(k1)
    )  # the fixture fleet also holds an offline printer with an AMS lite
    target = sv.resolve_slot("boris/LITE/2", "load")
    assert (target.key, target.head_index) == ("boris/lite/2", None)


# ---- spools ------------------------------------------------------------------------------------


def test_spools_shows_the_bound_spool_with_its_remaining_interval_and_agreement(
    po: ApiClient, k1: FakeMoonraker, capsys: Any
) -> None:
    sid = po.post("/v1/spools", BLACK)["id"]
    po.post("/v1/printers/k1se/slots/T1/load", {"spool_id": sid})
    capsys.readouterr()

    assert spools_cmd(["k1se", "--json"]) == 0
    rows = _json(capsys)["printers"]
    k1se = rows[0]
    assert k1se["name"] == "k1se"
    bound = k1se["slots"][0]["spools"][0]
    assert (
        bound["spool_id"] == sid and bound["remaining_text"]
    )  # the interval, not a bare number
    assert k1se["active_spool"]["status"] == "agrees"
    assert (
        k1se["active_spool"]["spool_id"],
        k1se["active_spool"]["bound_spool_id"],
    ) == (sid, sid)

    assert spools_cmd(["k1se"]) == 0
    text = _out(capsys)
    assert f"#{sid}" in text and "active spool: ok" in text


def test_spools_flags_an_active_spool_that_is_not_the_bound_one(
    po: ApiClient, k1: FakeMoonraker, capsys: Any
) -> None:
    sid = po.post("/v1/spools", BLACK)["id"]
    po.post("/v1/printers/k1se/slots/T1/load", {"spool_id": sid})
    k1.active_spool = 14  # someone picked another spool in Fluidd
    capsys.readouterr()

    assert spools_cmd(["k1se"]) == 0  # a report, not a gate, unless --strict
    text = _out(capsys)
    assert (
        "active spool: MISMATCH" in text
        and "usage is booked to the wrong spool" in text
    )
    with pytest.raises(GateFailure) as err:
        spools_cmd(["k1se", "--strict"])
    assert "k1se" in err.value.message
    capsys.readouterr()
    k1.active_spool = None  # none selected while a spool is bound is the same drift
    spools_cmd(["k1se", "--json"])
    assert _json(capsys)["printers"][0]["active_spool"]["status"] == "mismatch"


def test_spools_passes_strict_when_everything_agrees(
    po: ApiClient, k1: FakeMoonraker
) -> None:
    sid = po.post("/v1/spools", BLACK)["id"]
    po.post("/v1/printers/k1se/slots/T1/load", {"spool_id": sid})
    assert spools_cmd(["k1se", "--strict"]) == 0


def test_spools_says_why_a_multi_head_printer_has_no_agreement_and_never_asks_it(
    po: ApiClient, u1: FakeMoonraker, capsys: Any
) -> None:
    assert spools_cmd(["u1", "--json"]) == 0
    active = _json(capsys)["printers"][0]["active_spool"]
    assert active["status"] == "not-applicable" and "u1 has 4 heads" in active["note"]
    assert all(not r.path.startswith("/server/spoolman") for r in u1.requests)


def test_spools_no_live_asks_no_printer(
    po: ApiClient, k1: FakeMoonraker, u1: FakeMoonraker, capsys: Any
) -> None:
    seen = (len(k1.requests), len(u1.requests))  # 3d-api's own poller may read them; the CLI must not
    assert spools_cmd(["--no-live", "--json"]) == 0
    printers = {p["name"]: p for p in _json(capsys)["printers"]}
    assert printers["k1se"]["active_spool"]["status"] == "skipped"
    assert not [r for r in k1.requests[seen[0] :] if r.path.startswith("/server/spoolman")]
    assert not [r for r in u1.requests[seen[1] :] if r.path.startswith("/server/spoolman")]


def test_spools_never_contacts_an_offline_printer(po: ApiClient, capsys: Any) -> None:
    assert spools_cmd(["--json"]) == 0
    printers = {p["name"]: p for p in _json(capsys)["printers"]}
    assert {"u1", "k1se", "p1s", "boris"} <= set(printers)
    p1s = printers["p1s"]
    assert p1s["access"] == "offline"
    assert p1s["active_spool"]["status"] == "skipped" and "never contacted" in p1s["active_spool"]["note"]


@pytest.fixture
def dead_k1_fleet(use_fleet: Callable[..., None]) -> None:
    dead = FakeMoonraker(spoolman=True)
    dead.port = free_port()  # nothing listens here
    use_fleet(k1se_moonraker_entry(dead))


def test_spools_reports_an_unreachable_printer_without_failing(
    dead_k1_fleet: None, printops_db: sqlite3.Connection, api_client: ApiClient,
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr(ApiClient, "from_config", classmethod(lambda cls, **kw: api_client))
    assert spools_cmd(["k1se", "--json", "--secs", "1"]) == 0
    active = _json(capsys)["printers"][0]["active_spool"]
    assert active["status"] == "unreachable" and active["note"]


def test_spools_rejects_an_unknown_printer_and_a_bad_secs(po: ApiClient) -> None:
    with pytest.raises(InvalidArgument):
        spools_cmd(["nope"])
    with pytest.raises(InvalidArgument):
        spools_cmd(["k1se", "--secs", "0"])
    with pytest.raises(UsageError):
        spools_cmd(["--bogus"])


def test_a_printer_that_exists_in_the_registry_is_listed(po: ApiClient) -> None:
    assert {"u1", "k1se"} <= set(known_printers())


# ---- load --------------------------------------------------------------------------------------


def test_load_binds_the_spool_and_sets_the_active_spool(
    po: ApiClient, k1: FakeMoonraker, capsys: Any
) -> None:
    sid = po.post("/v1/spools", BLACK)["id"]
    assert run_load([f"#{sid}", "k1se", "--json"]) == 0
    got = _json(capsys)
    assert (
        got["slot"] == "k1se/T1"
        and got["printer_write"]["active_spool"]["after"] == sid
    )
    assert k1.active_spool == sid


def test_load_dry_run_writes_nothing_anywhere(
    po: ApiClient, k1: FakeMoonraker, capsys: Any
) -> None:
    sid = po.post("/v1/spools", BLACK)["id"]
    assert run_load([str(sid), "k1se", "--dry-run", "--json"]) == 0
    plan = _json(capsys)
    assert plan["dry_run"] is True and plan["refusals"] == []
    assert any("bind spool" in s for s in plan["steps"]) and any(
        "active spool" in s for s in plan["steps"]
    )
    assert k1.writes() == [] and po.get(f"/v1/spools/{sid}")["slot"] is None


def test_load_dry_run_predicts_the_refusals_of_the_real_load(
    po: ApiClient, u1: FakeMoonraker, capsys: Any
) -> None:
    black, white = (
        po.post("/v1/spools", BLACK)["id"],
        po.post("/v1/spools", WHITE)["id"],
    )
    po.post("/v1/printers/u1/slots/T3/load", {"spool_id": black})
    capsys.readouterr()
    writes_before = len(u1.writes())

    with pytest.raises(GateFailure):  # occupied, no --replace-to
        run_load([str(white), "u1/T3", "--dry-run"])
    assert "already holds" in _out(capsys)
    with pytest.raises(GateFailure):  # the fleet marks T2 unusable
        run_load([str(white), "u1/T2", "--dry-run"])
    assert "unusable" in _out(capsys)
    assert (
        run_load([str(white), "u1/T3", "--replace-to", "WH:default", "--dry-run"]) == 0
    )
    assert len(u1.writes()) == writes_before


def test_load_dry_run_names_an_open_fault_on_the_head(
    po: ApiClient, capsys: Any
) -> None:
    sid = po.post("/v1/spools", BLACK)["id"]
    po.post("/v1/printers/u1/faults", {"symptom": "clog", "head": "T4"})
    capsys.readouterr()
    with pytest.raises(GateFailure):
        run_load([str(sid), "u1/T4", "--dry-run"])
    assert "open fault" in _out(capsys)


def test_load_real_refusal_is_the_api_rule(po: ApiClient) -> None:
    black, white = (
        po.post("/v1/spools", BLACK)["id"],
        po.post("/v1/spools", WHITE)["id"],
    )
    po.post("/v1/printers/u1/slots/T3/load", {"spool_id": black})
    from errors import ThreeDError

    with pytest.raises(ThreeDError) as err:
        run_load([str(white), "u1/T3"])
    assert "already holds" in err.value.message


def test_load_rejects_a_bad_spool_id(po: ApiClient) -> None:
    with pytest.raises(UsageError):
        run_load(["seven", "k1se"])


# ---- unload ------------------------------------------------------------------------------------


def test_unload_dry_run_and_real(po: ApiClient, k1: FakeMoonraker, capsys: Any) -> None:
    sid = po.post("/v1/spools", BLACK)["id"]
    po.post("/v1/printers/k1se/slots/T1/load", {"spool_id": sid})
    capsys.readouterr()
    writes = len(k1.writes())

    assert run_unload(["k1se", "--to", "WH:default", "--dry-run", "--json"]) == 0
    plan = _json(capsys)
    assert (
        plan["steps"][0].startswith(f"unload spool #{sid}")
        and len(k1.writes()) == writes
    )
    assert run_unload(["k1se", "--to", "WH:default"]) == 0
    assert k1.active_spool is None  # cleared: it was the unloaded spool
    assert po.get(f"/v1/spools/{sid}")["slot"] is None
    with pytest.raises(GateFailure):  # now empty
        run_unload(["k1se", "--dry-run"])


# ---- swap --------------------------------------------------------------------------------------


def test_swap_needs_a_place_for_the_old_spool(po: ApiClient) -> None:
    sid = po.post("/v1/spools", BLACK)["id"]
    with pytest.raises(UsageError) as err:
        run_swap([str(sid), "k1se"])
    assert "--to" in err.value.message


def test_swap_exchanges_the_spool_in_one_call(
    po: ApiClient, k1: FakeMoonraker, capsys: Any
) -> None:
    black, white = (
        po.post("/v1/spools", BLACK)["id"],
        po.post("/v1/spools", WHITE)["id"],
    )
    po.post("/v1/printers/k1se/slots/T1/load", {"spool_id": black})
    capsys.readouterr()

    assert run_swap([str(white), "k1se", "--to", "WH:default", "--dry-run"]) == 0
    assert (
        po.get(f"/v1/spools/{black}")["slot"] == "k1se/T1"
    )  # untouched by the dry run
    capsys.readouterr()

    assert run_swap([str(white), "k1se", "--to", "WH:default", "--json"]) == 0
    assert _json(capsys)["slot"] == "k1se/T1"
    assert po.get(f"/v1/spools/{black}")["slot"] is None
    assert po.get(f"/v1/spools/{black}")["location"] == "WH:default"
    assert k1.active_spool == white


def test_swap_into_an_empty_slot_is_a_plain_load(po: ApiClient, capsys: Any) -> None:
    sid = po.post("/v1/spools", BLACK)["id"]
    assert run_swap([str(sid), "k1se", "--to", "WH:default"]) == 0
    assert "plain load" in _out(capsys)
    assert po.get(f"/v1/spools/{sid}")["slot"] == "k1se/T1"


# ---- filament ----------------------------------------------------------------------------------


def test_filament_show_reads_every_head_and_skips_offline_printers(
    po: ApiClient, capsys: Any
) -> None:
    assert filament(["show", "--json"]) == 0
    printers = {p["name"]: p for p in _json(capsys)["printers"]}
    assert set(printers) == {
        "u1",
        "k1se",
    }  # offline printers are not contacted, so not listed by default
    assert [s["label"] for s in printers["u1"]["slots"]] == ["T1", "T2", "T3", "T4"]
    assert printers["u1"]["slots"][0]["material"] == "PLA"
    assert filament(["show", "p1s"]) == 0
    assert "never contacted" in _out(capsys)


def test_filament_show_reports_an_unreachable_printer(
    use_fleet: Callable[..., None], capsys: Any
) -> None:
    dead = FakeMoonraker()
    dead.port = free_port()
    use_fleet(u1_entry(dead))
    assert filament(["show", "u1", "--secs", "1", "--json"]) == 0
    row = _json(capsys)["printers"][0]
    assert row["status"] == "unreachable" and row["error"]


def test_filament_set_writes_one_head_and_reads_it_back(
    po: ApiClient, u1: FakeMoonraker, capsys: Any
) -> None:
    assert (
        filament(
            [
                "set",
                "u1/T3",
                "--color",
                "#c62828",
                "--material",
                "PETG",
                "--vendor",
                "Elegoo",
                "--json",
            ]
        )
        == 0
    )
    got = _json(capsys)
    assert got["after"]["color"] == "#C62828" and got["after"]["material"] == "PETG"
    assert got["head"] == "T3" and got["pressure_advance"] is not None
    assert any(
        "SET_PRINT_FILAMENT_CONFIG" in s and "CONFIG_EXTRUDER=2" in s
        for s in u1.scripts
    )


def test_filament_set_dry_run_sends_nothing_and_predicts_a_busy_refusal(
    po: ApiClient, u1: FakeMoonraker, capsys: Any
) -> None:
    assert (
        filament(
            ["set", "u1/T3", "--color", "#C62828", "--material", "PETG", "--dry-run"]
        )
        == 0
    )
    assert u1.writes() == []
    u1.status["print_stats"]["state"] = "printing"
    capsys.readouterr()
    with pytest.raises(GateFailure):
        filament(
            ["set", "u1/T3", "--color", "#C62828", "--material", "PETG", "--dry-run"]
        )
    assert "WOULD BE REFUSED" in _out(capsys) and u1.writes() == []


def test_filament_set_refuses_a_printer_without_full_access(
    use_fleet: Callable[..., None], u1: FakeMoonraker
) -> None:
    from printer_ops.base import PrinterRefused

    use_fleet(u1_entry(u1, access="read-only"))
    with pytest.raises(PrinterRefused):
        filament(["set", "u1/T1", "--color", "#FFFFFF", "--material", "PLA"])
    assert u1.writes() == []


def test_filament_set_on_an_adapter_without_head_writes_is_unsupported(
    use_fleet: Callable[..., None],
) -> None:
    from printer_ops.base import UnsupportedOperation

    with FakeCreality().serve() as k1:
        use_fleet(k1_entry(k1))
        with pytest.raises(UnsupportedOperation) as err:
            filament(["set", "k1se", "--color", "#FFFFFF", "--material", "PLA"])
    assert err.value.exit_code == 2


def test_filament_set_validates_its_arguments(po: ApiClient) -> None:
    with pytest.raises(UsageError):
        filament(["set", "u1/T1", "--material", "PLA"])  # no colour
    with pytest.raises(InvalidArgument):
        filament(["set", "u1/T1", "--color", "red", "--material", "PLA"])
    with pytest.raises(UsageError):
        filament(["bogus"])


def test_every_sub_prints_help(capsys: Any) -> None:
    for run in (spools_cmd, run_load, run_unload, run_swap, filament):
        assert run(["--help"]) == 0
        assert "Examples:" in _out(capsys)
