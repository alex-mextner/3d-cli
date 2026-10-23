"""E2E stories for `3d printers spools|load|swap|unload|filament` through `bin/3d` against a real
uvicorn 3d-api, a FakeSpoolman over HTTP and loopback fake printers (never a real one)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fleet_fixtures import k1se_moonraker_entry, u1_entry, write_fleet
from printer_fakes import FakeMoonraker
from printops_fakes import FakeSpoolman
from printops_fixtures import LiveApi, serve_api

from printops.spoolman import ensure_extra_fields

from .workflow_helper import run_cli


@pytest.fixture
def k1() -> Iterator[FakeMoonraker]:
    fake = FakeMoonraker(spoolman=True, active_spool=14)
    fake.status.pop("print_task_config")
    with fake.serve() as served:
        yield served


@pytest.fixture
def u1() -> Iterator[FakeMoonraker]:
    with FakeMoonraker().serve() as served:
        yield served


@pytest.fixture
def live(
    tmp_path: Path,
    k1: FakeMoonraker,
    u1: FakeMoonraker,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[LiveApi]:
    monkeypatch.setenv(
        "THREED_FLEET",
        str(
            write_fleet(tmp_path / "fleet.yaml", k1se_moonraker_entry(k1), u1_entry(u1))
        ),
    )
    monkeypatch.setenv("PRINTOPS_POLL_S", "60")
    fake = FakeSpoolman()
    ensure_extra_fields(fake)
    with serve_api(tmp_path, fake) as api:
        yield api


def _env(live: LiveApi) -> dict[str, str]:
    return {"PRINTOPS_API": live.url, "PRINTOPS_TOKEN": live.tokens["rw"]}


def _cli(tmp_path: Path, live: LiveApi, *argv: str) -> tuple[int, str, str]:
    r = run_cli(tmp_path, "printers", *argv, env_extra=_env(live))
    return r.returncode, r.stdout, r.stderr


def _printops(tmp_path: Path, live: LiveApi, *argv: str) -> dict[str, object]:
    r = run_cli(tmp_path, "printops", *argv, "--json", env_extra=_env(live))
    assert r.returncode == 0, r.stderr
    return dict(json.loads(r.stdout))


def _receive(tmp_path: Path, live: LiveApi, name: str, color: str) -> int:
    r = run_cli(
        tmp_path,
        "printops",
        "spool",
        "receive",
        "--vendor",
        "Elegoo",
        "--material",
        "PLA+",
        "--name",
        name,
        "--color",
        color,
        "--net",
        "1000",
        "--location",
        "WH:default",
        "--json",
        env_extra=_env(live),
    )
    assert r.returncode == 0, r.stderr
    return int(json.loads(r.stdout)["id"])


def test_user_loads_swaps_and_unloads_a_spool_and_sees_the_active_spool_agree(
    tmp_path: Path, live: LiveApi, k1: FakeMoonraker
) -> None:
    r = run_cli(
        tmp_path,
        "printops",
        "loc",
        "add",
        "WH:default",
        "--kind",
        "warehouse",
        env_extra=_env(live),
    )
    assert r.returncode == 0, r.stderr
    black, white = (
        _receive(tmp_path, live, "Black", "#000000"),
        _receive(tmp_path, live, "White", "#FFFFFF"),
    )

    code, out, err = _cli(tmp_path, live, "load", str(black), "k1se")
    assert code == 0, err
    assert f"#{black}" in out and k1.active_spool == black

    code, out, err = _cli(tmp_path, live, "spools", "k1se", "--strict")
    assert code == 0, err
    assert "active spool: ok" in out

    code, out, err = _cli(
        tmp_path, live, "swap", str(white), "k1se", "--to", "WH:default", "--json"
    )
    assert code == 0, err
    assert json.loads(out)["slot"] == "k1se/T1" and k1.active_spool == white

    k1.active_spool = black  # drift: Fluidd's dialog picked the other spool
    code, out, err = _cli(tmp_path, live, "spools", "k1se", "--strict")
    assert code == 1 and "MISMATCH" in out
    code, out, err = _cli(tmp_path, live, "spools", "k1se", "--json")
    assert (
        code == 0
        and json.loads(out)["printers"][0]["active_spool"]["status"] == "mismatch"
    )

    code, out, err = _cli(tmp_path, live, "unload", "k1se", "--to", "WH:default")
    assert code == 0, err
    assert k1.active_spool == black  # it was not the unloaded (white) spool: left alone


def test_dry_run_load_into_a_broken_head_is_refused_and_writes_nothing(
    tmp_path: Path, live: LiveApi, u1: FakeMoonraker
) -> None:
    r = run_cli(
        tmp_path,
        "printops",
        "loc",
        "add",
        "WH:default",
        "--kind",
        "warehouse",
        env_extra=_env(live),
    )
    assert r.returncode == 0, r.stderr
    sid = _receive(tmp_path, live, "Black", "#000000")
    writes = len(u1.writes())

    code, out, err = _cli(tmp_path, live, "load", str(sid), "u1/T2", "--dry-run")
    assert code == 1 and "WOULD BE REFUSED" in out and "unusable" in out
    code, out, err = _cli(
        tmp_path, live, "load", str(sid), "u1/T1", "--dry-run", "--json"
    )
    assert code == 0 and json.loads(out)["refusals"] == []
    assert len(u1.writes()) == writes
    assert _printops(tmp_path, live, "spool", "show", str(sid))["slot"] is None


def test_filament_set_writes_the_u1_head_and_show_reads_it(
    tmp_path: Path, live: LiveApi, u1: FakeMoonraker
) -> None:
    code, out, err = _cli(
        tmp_path,
        live,
        "filament",
        "set",
        "u1/T3",
        "--color",
        "#C62828",
        "--material",
        "PETG",
        "--vendor",
        "Elegoo",
    )
    assert code == 0, err
    assert "now shows PETG #C62828" in out
    code, out, err = _cli(tmp_path, live, "filament", "show", "u1", "--json")
    assert code == 0, err
    t3 = json.loads(out)["printers"][0]["slots"][2]
    assert (t3["material"], t3["color"]) == ("PETG", "#C62828")


def test_help_and_usage_errors_exit_as_documented(
    tmp_path: Path, live: LiveApi
) -> None:
    for sub in ("spools", "load", "unload", "swap", "filament"):
        code, out, err = _cli(tmp_path, live, sub, "--help")
        assert code == 0 and "Examples:" in out, sub
    code, out, err = _cli(tmp_path, live, "swap", "1", "k1se")
    assert code == 2 and "--to" in err
    code, out, err = _cli(tmp_path, live, "load", "1", "nope/T1")
    assert code == 2
    code, out, err = _cli(tmp_path, live, "--help")
    assert code == 0 and "spools" in out and "swap" in out
    code, out, err = _cli(tmp_path, live, "slots", "--help")  # alias of spools
    assert code == 0 and "3d printers spools" in out
