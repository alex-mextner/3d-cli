"""E2E stories of the Spoolman ↔ PrintOps sync (issue #704) through `bin/3d`, against a real uvicorn
3d-api talking HTTP to FakeSpoolman (`live_api.spoolman`: an edit made on it is "the Spoolman web UI").

  3d printops spool drift [--spool ID] [--heal] [--json]   3d printops health   3d print-preflight
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fleet_fixtures import write_policy_for_fleet
from printops_fakes import FakeSpoolman
from printops_fixtures import LiveApi, serve_api

from printops import db, locations
from printops.cli import spool_drift
from printops.client import ApiClient

from .test_printops_gates_workflows import FLEET, _load, _plate
from .workflow_helper import run_cli

GREEN = {
    "vendor": "Elegoo",
    "material": "PLA+",
    "name": "RAPID PLA+ Green",
    "color_hex": "#4FE44D",
    "net_g": 1000,
    "location": "WH:default",
}


def _seed_locations(live: LiveApi) -> None:
    conn = db.open_db(live.settings.db_path)
    try:
        locations.add(conn, "WH:default", "warehouse", actor="e2e")
        locations.add(conn, "WH:A", "zone", actor="e2e")
        locations.add(conn, "WH:A/S1", "shelf", actor="e2e")
    finally:
        conn.close()


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "fleet.yaml"
    path.write_text(FLEET, encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(path))  # the in-process 3d-api reads it too
    monkeypatch.setenv(
        "PRINTOPS_POLICY", str(write_policy_for_fleet(tmp_path / "policy.yaml", path))
    )
    return path


@pytest.fixture
def shop(live_api: LiveApi, fleet: Path) -> LiveApi:
    _seed_locations(live_api)
    return live_api


def cli(
    tmp_path: Path,
    live: LiveApi,
    *args: str,
    token: str = "rw",
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    extra = {
        "PRINTOPS_API": live.url,
        "PRINTOPS_TOKEN": live.tokens[token],
        "THREED_FLEET": "",
        **(env or {}),
    }
    return run_cli(tmp_path, *args, env_extra=extra)


def receive(live: LiveApi, **over: Any) -> int:
    got = ApiClient(live.url, live.tokens["rw"]).post("/v1/spools", {**GREEN, **over})
    return int(got["id"])


def ui_edit(live: LiveApi, sid: int, grams: float) -> None:
    live.spoolman.update("spool", sid, {"remaining_weight": grams})


# -- 3d printops spool drift ------------------------------------------------------------------------------


def test_drift_story_detect_heal_converge(tmp_path: Path, shop: LiveApi) -> None:
    """A spool in step is quiet; a Spoolman UI weight edit is listed with both numbers and the fix,
    exits 1, heals into a ledger anchor, and then everything is quiet again."""
    sid, other = receive(shop), receive(shop)
    quiet = cli(tmp_path, shop, "printops", "spool", "drift")
    assert quiet.returncode == 0, quiet.stderr
    assert "in step: 2 spool(s) checked, nothing drifts" in quiet.stdout

    ui_edit(shop, sid, 412)
    loud = cli(tmp_path, shop, "printops", "spool", "drift")
    assert loud.returncode == 1, loud.stdout
    assert "DRIFT: 1 difference(s) in 2 spool(s)" in loud.stdout
    assert "[remaining]" in loud.stdout
    assert "ledger 1000.0 g, Spoolman 412.0 g" in loud.stdout
    assert "`3d printops spool drift --heal`" in loud.stdout

    as_json = cli(tmp_path, shop, "printops", "spool", "drift", "--json")
    assert as_json.returncode == 1
    rep = json.loads(as_json.stdout)
    assert (rep["ok"], rep["drift"], rep["healable"], rep["unhealable"]) == (
        False,
        1,
        1,
        0,
    )
    assert [(d["kind"], d["spool_id"]) for d in rep["drifts"]] == [("remaining", sid)]

    scoped_other = cli(
        tmp_path, shop, "printops", "spool", "drift", "--spool", str(other)
    )
    scoped_sid = cli(tmp_path, shop, "printops", "spool", "drift", "--spool", str(sid))
    assert scoped_other.returncode == 0 and "1 spool(s) checked" in scoped_other.stdout
    assert scoped_sid.returncode == 1 and "[remaining]" in scoped_sid.stdout

    wrote_before = len(shop.spoolman.writes())
    healed = cli(tmp_path, shop, "printops", "spool", "drift", "--heal")
    assert healed.returncode == 0, healed.stdout + healed.stderr
    assert "healed 1 difference(s)" in healed.stdout and "[remaining]" in healed.stdout
    assert "in step: 2 spool(s) checked" in healed.stdout
    # the weight is never written back to Spoolman
    new_writes = shop.spoolman.writes()[wrote_before:]
    assert [w for w in new_writes if w[1].endswith(("/use", "/measure"))] == []
    assert shop.spoolman.get("spool", sid)["remaining_weight"] == 412

    after = cli(tmp_path, shop, "printops", "spool", "drift")
    assert after.returncode == 0 and "nothing drifts" in after.stdout

    listed = cli(tmp_path, shop, "printops", "spool", "ls")
    assert listed.returncode == 0, listed.stderr
    mine = next(ln for ln in listed.stdout.splitlines() if ln.startswith(f"#{sid}"))
    assert (
        "412" in mine and "1000" not in mine
    )  # the ledger converged on Spoolman's value
    shown = cli(tmp_path, shop, "printops", "spool", "show", str(sid))
    assert shown.returncode == 0, shown.stderr
    assert "spool-sync" in shown.stdout and "412.0 g" in shown.stdout

    again = cli(tmp_path, shop, "printops", "spool", "drift", "--heal")
    assert again.returncode == 0 and "healed 0 difference(s)" in again.stdout


def test_an_unhealable_drift_needs_a_person_until_the_location_exists(
    tmp_path: Path, shop: LiveApi
) -> None:
    """Someone moves a spool in the Spoolman UI to a shelf PrintOps has never heard of: `spool drift --heal`
    cannot invent the shelf, so it says a person must decide and exits 1; once the owner adds the shelf
    with `loc add`, the same heal converges and the drift check is quiet."""
    sid = receive(shop)
    shop.spoolman.update(
        "spool", sid, {"location": "WH:A/S2"}
    )  # PrintOps has no such shelf

    stuck = cli(tmp_path, shop, "printops", "spool", "drift", "--heal")
    assert stuck.returncode == 1, stuck.stdout
    assert "healed 0 difference(s)" in stuck.stdout
    assert "[location_unknown]" in stuck.stdout and "WH:A/S2" in stuck.stdout
    assert "needs you:" in stuck.stdout and "need a person" in stuck.stdout
    plain = cli(tmp_path, shop, "printops", "spool", "drift", "--json")
    assert plain.returncode == 1 and json.loads(plain.stdout)["unhealable"] == 1

    added = cli(tmp_path, shop, "printops", "loc", "add", "WH:A/S2", "--kind", "shelf")
    assert added.returncode == 0, added.stderr
    cured = cli(tmp_path, shop, "printops", "spool", "drift", "--heal")
    assert cured.returncode == 0, cured.stdout + cured.stderr
    assert "in step" in cured.stdout
    assert cli(tmp_path, shop, "printops", "spool", "drift").returncode == 0


def test_a_wiped_spoolman_heals_nothing_until_the_owner_confirms_the_deletions(
    tmp_path: Path, shop: LiveApi
) -> None:
    """Spoolman is replaced by an empty one (or SPOOLMAN_URL points elsewhere): `spool drift --heal`
    refuses to touch the survivor and names `--confirm-deleted`; after the owner confirms, the lost
    spools are recorded as deleted and the heal of the survivor works again."""
    ids = [receive(shop) for _ in range(5)]
    for sid in ids[:4]:
        shop.spoolman.delete("spool", sid)
    ui_edit(shop, ids[4], 412)

    stuck = cli(tmp_path, shop, "printops", "spool", "drift", "--heal")
    assert stuck.returncode == 1, stuck.stdout
    assert "healed 0 difference(s)" in stuck.stdout and "[spoolman_empty]" in stuck.stdout
    assert "--confirm-deleted" in stuck.stdout
    assert shop.spoolman.get("spool", ids[4])["remaining_weight"] == 412

    read_only = cli(tmp_path, shop, "printops", "spool", "drift", "--confirm-deleted", token="read")
    assert read_only.returncode == 1 and "403" in read_only.stderr

    confirmed = cli(tmp_path, shop, "printops", "spool", "drift", "--confirm-deleted")
    assert "recorded 4 spool(s) as deleted: " + ", ".join(f"#{i}" for i in ids[:4]) in confirmed.stdout
    assert "[spoolman_empty]" not in confirmed.stdout

    healed = cli(tmp_path, shop, "printops", "spool", "drift", "--heal")
    assert healed.returncode == 0, healed.stdout + healed.stderr
    assert "healed 1 difference(s)" in healed.stdout and "in step" in healed.stdout


def test_confirm_deleted_refuses_to_be_combined_and_lists_ids_that_may_have_changed(
    tmp_path: Path, shop: LiveApi
) -> None:
    """`--confirm-deleted` is a bulk decision: with `--spool` or `--heal` it is a usage error (exit 2) that
    names the conflict and writes nothing; and after it, the known ids Spoolman still lists are named as
    possibly different spools, because a wiped Spoolman numbers its new spools from 1 again."""
    ids = [receive(shop) for _ in range(5)]
    for sid in ids[:4]:
        shop.spoolman.delete("spool", sid)
    ui_edit(shop, ids[4], 412)
    writes = len(shop.spoolman.writes())

    for extra in (["--spool", str(ids[4])], ["--heal"]):
        bad = cli(tmp_path, shop, "printops", "spool", "drift", "--confirm-deleted", *extra)
        assert bad.returncode == 2, (extra, bad.stdout, bad.stderr)
        assert "--confirm-deleted cannot be combined with " + extra[0] in bad.stderr
    assert len(shop.spoolman.writes()) == writes
    assert list(shop.spoolman.items["spool"]) == [ids[4]]  # nothing was deleted or recorded

    done = cli(tmp_path, shop, "printops", "spool", "drift", "--confirm-deleted", "--json")
    assert done.returncode == 1 or done.returncode == 0, done.stderr
    body = json.loads(done.stdout)
    assert sorted(body["confirmed"]) == ids[:4] and body["may_be_other_spools"] == [ids[4]]
    text = cli(tmp_path, shop, "printops", "spool", "drift", "--confirm-deleted")
    assert f"ids that may now belong to different spools: check them in Spoolman: #{ids[4]}" in text.stdout


def test_drift_heal_needs_the_write_scope_and_drift_the_read_scope(
    tmp_path: Path, shop: LiveApi
) -> None:
    """A bot with a read-only token can look at the drift but not heal it, and a write-only token cannot
    even look: each refusal names the missing scope, and the refused heal changes nothing in Spoolman
    or in the drift that is listed afterwards."""
    sid = receive(shop)
    ui_edit(shop, sid, 412)
    read_heal = cli(
        tmp_path, shop, "printops", "spool", "drift", "--heal", token="read"
    )
    assert read_heal.returncode == 1 and "403" in read_heal.stderr
    assert "scope" in read_heal.stderr and "healed" not in read_heal.stdout
    write_only = cli(tmp_path, shop, "printops", "spool", "drift", token="write")
    assert write_only.returncode == 1 and "403" in write_only.stderr
    assert "scope" in write_only.stderr and write_only.stdout == ""
    assert shop.spoolman.get("spool", sid)["remaining_weight"] == 412  # nothing was written back
    still = cli(tmp_path, shop, "printops", "spool", "drift", token="read")
    assert (
        still.returncode == 1 and "[remaining]" in still.stdout
    )  # the refused heal changed nothing


# -- help ---------------------------------------------------------------------------------------------------


def test_spool_help_lists_the_drift_action_and_drift_help_prints_its_usage(
    tmp_path: Path, shop: LiveApi
) -> None:
    """A user asks `3d printops spool --help` what else the spool command can do and finds `drift` listed once under the extra actions; `spool drift --help` then prints exactly the usage text, with examples."""
    spool_help = cli(tmp_path, shop, "printops", "spool", "--help")
    assert spool_help.returncode == 0, spool_help.stderr
    more = spool_help.stdout.split("More actions", 1)
    assert len(more) == 2, spool_help.stdout
    assert re.search(r"^\s+drift\s+do Spoolman and PrintOps agree", more[1], re.M)

    drift_help = cli(tmp_path, shop, "printops", "spool", "drift", "--help")
    assert drift_help.returncode == 0
    assert drift_help.stdout.strip() == spool_drift.USAGE.strip()
    assert "Examples:" in drift_help.stdout
    assert "3d printops spool drift --heal" in drift_help.stdout


def _examples() -> list[list[str]]:
    block = spool_drift.USAGE.split("Examples:", 1)[1]
    out = [shlex.split(ln)[1:] for ln in block.splitlines() if ln.strip()]
    assert out and all(argv[:3] == ["printops", "spool", "drift"] for argv in out)
    return out


def test_every_example_in_the_drift_usage_parses_and_runs(
    tmp_path: Path, shop: LiveApi
) -> None:
    """A user copies each example from `spool drift --help` into a shell: with everything in step every one of them parses and exits 0."""
    receive(shop)  # an in-step world: every example exits 0
    for argv in _examples():
        got = cli(tmp_path, shop, *argv)
        assert got.returncode == 0, (argv, got.stdout, got.stderr)
        assert "unknown option" not in got.stderr + got.stdout, argv


def test_every_option_in_the_drift_usage_exists(tmp_path: Path, shop: LiveApi) -> None:
    """Every option `spool drift --help` documents is accepted by the real command, and an invented one is refused by name."""
    options = re.findall(
        r"^  (--[a-z-]+)", spool_drift.USAGE.split("Options:")[1], re.M
    )
    assert sorted(options) == ["--api", "--confirm-deleted", "--heal", "--json", "--spool", "--token"]
    values = {"--spool": "1", "--api": shop.url, "--token": shop.tokens["rw"]}
    for opt in options:
        argv = [
            "printops",
            "spool",
            "drift",
            opt,
            *([values[opt]] if opt in values else []),
        ]
        got = cli(tmp_path, shop, *argv)
        assert "unknown option" not in got.stderr, (opt, got.stderr)
        assert got.returncode in (0, 1) and "Traceback" not in got.stderr, (
            opt,
            got.stderr,
        )
    bogus = cli(tmp_path, shop, "printops", "spool", "drift", "--nope")
    assert bogus.returncode != 0 and "unknown option --nope" in bogus.stderr  # teeth


# -- 3d printops health ----------------------------------------------------------------------------------------


def test_health_says_the_sync_is_off_when_nothing_schedules_it(
    tmp_path: Path, shop: LiveApi
) -> None:
    """An operator runs `3d printops health` against a 3d-api whose spool sync schedule is off: it says so in text and in --json, and the overall status stays ok."""
    got = cli(tmp_path, shop, "printops", "health", token="read")
    assert got.returncode == 0, got.stdout + got.stderr
    assert "spool sync  OFF" in got.stdout
    as_json = cli(tmp_path, shop, "printops", "health", "--json", token="read")
    assert as_json.returncode == 0
    body = json.loads(as_json.stdout)
    assert 0 <= body["spool_sync"].pop("age_s") < 60  # wall clock since start: not compared exactly
    assert body["spool_sync"] == {
        "enabled": False,
        "interval_s": 0,
        "last_pass": None,
        "stale": False,
        "anomalies_open": 0,
    }
    assert body["status"] == "ok"


@pytest.fixture
def scheduled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fleet: Path
) -> Iterator[tuple[FakeSpoolman, Path]]:
    """The seam for an API whose schedule is ON: the env is read when the app starts, so the API of
    these tests is started by the test itself (a `live_api` fixture has the schedule off)."""
    monkeypatch.setenv("PRINTOPS_SPOOL_SYNC_S", "10")
    yield FakeSpoolman(), tmp_path


def _wait_for_a_pass(live: LiveApi, deadline_s: float = 10.0) -> dict[str, Any]:
    api = ApiClient(live.url, live.tokens["read"])
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        st: dict[str, Any] = api.get("/v1/spool-sync/status")
        if st["last_pass"] and st["last_pass"]["at"]:
            return st
        time.sleep(0.05)
    raise AssertionError("the scheduled pass never ran")


def test_health_lists_leftover_legacy_fields_and_exits_1_while_the_api_stays_ok(
    tmp_path: Path, shop: LiveApi
) -> None:
    """An operator upgrades PrintOps and runs `3d printops health`: the stack answers "ok" (a deploy must
    not wait on it), but the command names the old field definitions still in Spoolman, says to run
    `fields migrate-native`, and exits 1 until they are gone."""
    shop.spoolman.set_field("spool", "printops_slot", {"name": "Slot", "field_type": "text"})
    shop.spoolman.set_field("vendor", "spool_tare_g", {"name": "Tare", "field_type": "float", "unit": "g"})

    got = cli(tmp_path, shop, "printops", "health", token="read")
    assert got.returncode == 1, got.stdout + got.stderr
    assert "legacy    vendor.spool_tare_g, spool.printops_slot still defined" in got.stdout
    assert "3d printops fields migrate-native" in got.stdout
    assert got.stdout.splitlines()[0].split()[2] == "ok"  # the API's own status is not degraded

    applied = cli(tmp_path, shop, "printops", "fields", "migrate-native", "--apply")
    assert applied.returncode == 0, applied.stdout + applied.stderr
    after = cli(tmp_path, shop, "printops", "health", token="read")
    assert after.returncode == 0 and "legacy" not in after.stdout


def test_health_is_ok_when_the_scheduled_pass_found_nothing(
    scheduled: tuple[FakeSpoolman, Path],
) -> None:
    """An operator runs `3d printops health` against a 3d-api whose scheduled spool sync pass found nothing to fix: health reports the sync as ok and exits 0."""
    fake, tmp_path = scheduled
    with serve_api(tmp_path, fake) as live:
        _seed_locations(live)
        status = _wait_for_a_pass(live)
        assert status["enabled"] is True and status["last_pass"]["drift"] == 0
        got = cli(tmp_path, live, "printops", "health", token="read")
        assert got.returncode == 0, got.stdout + got.stderr
        assert "spool sync  ok" in got.stdout and "0 drift" in got.stdout


def test_health_exits_1_and_points_at_drift_when_the_pass_could_not_fix_something(
    scheduled: tuple[FakeSpoolman, Path],
) -> None:
    """A spool created in the Spoolman UI on a shelf PrintOps has never heard of: the scheduled pass
    adopts what it can, cannot fix the location, opens an anomaly — and health says so."""
    fake, tmp_path = scheduled
    vendor = fake.create("vendor", {"name": "Elegoo"})
    filament = fake.create(
        "filament",
        {
            "name": "PLA",
            "vendor_id": vendor["id"],
            "material": "PLA",
            "density": 1.24,
            "diameter": 1.75,
            "weight": 1000,
            "spool_weight": 150,
        },
    )
    fake.create(
        "spool",
        {"filament_id": filament["id"], "remaining_weight": 900, "location": "WH:Z/S9"},
    )
    with serve_api(tmp_path, fake) as live:
        _seed_locations(live)
        status = _wait_for_a_pass(live)
        assert status["last_pass"]["drift"] >= 1 and status["anomalies_open"] == 1

        got = cli(tmp_path, live, "printops", "health", token="read")
        assert got.returncode == 1, got.stdout
        assert "spool sync  DRIFT" in got.stdout
        assert "1 anomaly(ies) it could not fix" in got.stdout
        assert "3d printops spool drift" in got.stdout

        as_json = cli(tmp_path, live, "printops", "health", "--json", token="read")
        assert as_json.returncode == 1
        body = json.loads(as_json.stdout)
        assert body["status"] == "ok"  # db and Spoolman are fine: only the sync is not
        assert body["spool_sync"]["anomalies_open"] == 1
        assert body["spool_sync"]["last_pass"]["drift"] >= 1

        anomalies = cli(
            tmp_path, live, "printops", "anomalies", "--printer", "spools", "--json"
        )
        assert anomalies.returncode == 0, anomalies.stderr
        assert "location_unknown" in anomalies.stdout and "WH:Z/S9" in anomalies.stdout


# -- 3d print-preflight ------------------------------------------------------------------------------------------


def _preflight(
    tmp_path: Path, live: LiveApi, fleet: Path, plate: Path
) -> dict[str, dict[str, Any]]:
    got = cli(
        tmp_path,
        live,
        "print-preflight",
        plate.name,
        "--axis-limits",
        "u1",
        "--json",
        token="read",
        env={"THREED_FLEET": str(fleet)},
    )
    checks: dict[str, dict[str, Any]] = {
        c["name"]: c for c in json.loads(got.stdout)["checks"]
    }
    return checks


def test_preflight_fails_a_drifted_spool_and_passes_after_the_heal(
    tmp_path: Path, shop: LiveApi, fleet: Path
) -> None:
    """A user loads two spools, edits one weight in the Spoolman UI and runs print-preflight: the loaded drifted spool FAILs the plate with the fix line; after `spool drift --heal` the same preflight passes."""
    black, green = _load(shop, "T1", "#000000"), _load(shop, "T3", "#1B5E20")
    plate = _plate(tmp_path / "plate.gcode")  # 20 g from T1, 50 g from T3

    clean = _preflight(tmp_path, shop, fleet, plate)
    assert clean["spool_in_sync"]["status"] == "PASS", clean["spool_in_sync"]
    assert clean["filament_sufficient"]["status"] == "PASS"

    ui_edit(
        shop, green, 30
    )  # the UI says 30 g are left on the spool T3 prints 50 g from
    drift = _preflight(tmp_path, shop, fleet, plate)
    assert drift["spool_in_sync"]["status"] == "FAIL", drift["spool_in_sync"]
    detail = str(drift["spool_in_sync"]["detail"])
    assert "T3" in detail and "Spoolman and PrintOps disagree" in detail
    assert "`3d printops spool drift --heal`" in detail
    # the other spool is untouched, and no weight is trusted while the two numbers differ
    assert drift["filament_sufficient"]["status"] == "UNVERIFIED"
    assert drift["loaded_filament_match"]["status"] == "PASS"

    healed = cli(
        tmp_path, shop, "printops", "spool", "drift", "--heal", "--spool", str(green)
    )
    assert healed.returncode == 0, healed.stdout + healed.stderr
    after = _preflight(tmp_path, shop, fleet, plate)
    assert after["spool_in_sync"]["status"] == "PASS"
    # with one trusted number again the gate judges it: 30 g cannot print 50 g
    assert after["filament_sufficient"]["status"] == "FAIL"
    assert black != green
