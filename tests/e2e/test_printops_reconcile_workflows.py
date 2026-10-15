"""E2E stories for slot reconciliation (#161) through `bin/3d printops slots|anomalies` against a
real uvicorn 3d-api whose poller reads an in-process fake U1 (never a real printer)."""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fleet_fixtures import u1_entry, write_fleet
from printer_fakes import FakeMoonraker
from printops_fakes import FakeSpoolman
from printops_fixtures import LiveApi, serve_api

from printops.spoolman import encode_extra, ensure_extra_fields

from .workflow_helper import run_cli


@pytest.fixture
def u1() -> Iterator[FakeMoonraker]:
    fake = FakeMoonraker()
    cfg = copy.deepcopy(fake.status["print_task_config"])
    cfg["filament_type"][0], cfg["filament_color_rgba"][0] = (
        "PETG",
        "C62828FF",
    )  # red PETG in T1
    fake.status["print_task_config"] = cfg
    with fake.serve() as served:
        yield served


@pytest.fixture
def live(
    tmp_path: Path, u1: FakeMoonraker, monkeypatch: pytest.MonkeyPatch
) -> Iterator[LiveApi]:
    monkeypatch.setenv(
        "THREED_FLEET", str(write_fleet(tmp_path / "fleet.yaml", u1_entry(u1)))
    )
    monkeypatch.setenv("PRINTOPS_POLL_S", "10")
    fake = FakeSpoolman()
    ensure_extra_fields(fake)
    vendor = fake.create("vendor", {"name": "Elegoo"})
    fil = fake.create(
        "filament",
        {
            "name": "PLA Black",
            "vendor_id": vendor["id"],
            "material": "PLA",
            "color_hex": "1A1A1A",
            "density": 1.24,
            "diameter": 1.75,
            "weight": 1000,
        },
    )
    fake.create(
        "spool",
        {
            "filament_id": fil["id"],
            "location": "slot:u1/T1",
            "extra": {"printops_slot": encode_extra("u1/T1")},
        },
    )
    with serve_api(tmp_path, fake) as api:
        yield api


def _env(live: LiveApi, token: str = "rw") -> dict[str, str]:
    return {"PRINTOPS_API": live.url, "PRINTOPS_TOKEN": live.tokens[token]}


def _wait_for_anomalies(tmp_path: Path, live: LiveApi) -> list[dict[str, object]]:
    deadline = time.monotonic() + 15
    while True:
        r = run_cli(tmp_path, "printops", "anomalies", "--json", env_extra=_env(live))
        rows = json.loads(r.stdout)["anomalies"] if r.returncode == 0 else []
        if rows or time.monotonic() > deadline:
            return rows
        time.sleep(0.2)


def test_user_sees_the_swapped_spool_in_slots_and_acks_the_anomaly(
    tmp_path: Path, live: LiveApi, u1: FakeMoonraker
) -> None:
    """The black PLA spool is bound to U1 T1, but someone loaded red PETG. The user lists the
    slots, sees both anomalies, acks the type one with a reason and resolves the colour one."""
    rows = _wait_for_anomalies(tmp_path, live)
    kinds = {str(r["kind"]): r for r in rows}
    assert {"type_mismatch", "color_mismatch"} <= set(kinds)

    slots = run_cli(tmp_path, "printops", "slots", env_extra=_env(live))
    assert slots.returncode == 0, slots.stderr
    t1 = next(ln for ln in slots.stdout.splitlines() if ln.strip().startswith("T1"))
    assert "printer: PETG #C62828 Snapmaker" in t1 and "Elegoo PLA Black" in t1
    assert "!type_mismatch" in t1 and "!color_mismatch" in t1
    assert "T2 (unusable: T2 is broken)" in slots.stdout
    assert "p1s  Bambu Lab P1S  [offline]  offline (never contacted)" in slots.stdout

    one = run_cli(tmp_path, "printops", "slots", "u1", "--json", env_extra=_env(live))
    assert json.loads(one.stdout)["slots"][0]["printer"]["material"] == "PETG"
    missing = run_cli(tmp_path, "printops", "slots", "nope", env_extra=_env(live))
    assert missing.returncode == 1 and "unknown printer 'nope'" in missing.stderr

    listed = run_cli(tmp_path, "printops", "anomalies", env_extra=_env(live))
    assert (
        "type_mismatch  slot:u1/T1  printer: PETG #C62828; spool 1: PLA #1A1A1A  [open]"
        in listed.stdout
    )
    assert "ΔE2000" in listed.stdout

    type_id = str(kinds["type_mismatch"]["id"])
    acked = run_cli(
        tmp_path,
        "printops",
        "anomalies",
        "ack",
        type_id,
        "--note",
        "testing PETG",
        env_extra=_env(live),
    )
    assert acked.returncode == 0, acked.stderr
    assert "[ack by test-rw]" in acked.stdout
    again = run_cli(
        tmp_path, "printops", "anomalies", "ack", type_id, env_extra=_env(live)
    )
    assert again.returncode == 1 and "HTTP 409" in again.stderr
    read_only = run_cli(
        tmp_path,
        "printops",
        "anomalies",
        "resolve",
        str(kinds["color_mismatch"]["id"]),
        env_extra=_env(live, "read"),
    )
    assert read_only.returncode == 1 and "HTTP 403" in read_only.stderr
    done = run_cli(
        tmp_path,
        "printops",
        "anomalies",
        "resolve",
        str(kinds["color_mismatch"]["id"]),
        env_extra=_env(live),
    )
    assert done.returncode == 0 and "[resolved by test-rw]" in done.stdout

    events = run_cli(
        tmp_path, "printops", "events", "--last", "50", "--json", env_extra=_env(live)
    )
    seen = [json.loads(ln)["kind"] for ln in events.stdout.splitlines()]
    assert (
        "slot.mismatch" in seen
        and "anomaly.acked" in seen
        and "anomaly.resolved" in seen
    )
    assert u1.writes() == []


def test_anomalies_usage_errors(tmp_path: Path, live: LiveApi) -> None:
    bad_id = run_cli(
        tmp_path, "printops", "anomalies", "ack", "x", env_extra=_env(live)
    )
    assert bad_id.returncode == 2 and "anomaly id must be a number" in bad_id.stderr
    bad_status = run_cli(
        tmp_path, "printops", "anomalies", "--status", "nope", env_extra=_env(live)
    )
    assert bad_status.returncode == 1 and "HTTP 422" in bad_status.stderr
    help_ = run_cli(tmp_path, "printops", "anomalies", "--help")
    assert help_.returncode == 0 and "color_mismatch" in help_.stdout
    assert "slots" in run_cli(tmp_path, "printops", "--help").stdout
