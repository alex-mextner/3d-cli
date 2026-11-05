"""The 3d-api side of the Spoolman ↔ PrintOps sync (issue #704): `GET /v1/spool-sync`,
`GET /v1/spool-sync/status`, `POST /v1/spool-sync/heal`, the background schedule and the gateway
wiring of every spool-writing route, plus what `3d printops health` reads from the status.

A "UI edit" is `FakeSpoolman.update` (PATCH: the Spoolman edit form, which does not move
`last_used`), made behind the system's back; every test that detects drift also heals it and proves
the two sides are in step afterwards."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fleet_fixtures import write_policy_for_fleet
from printops_fakes import FakeSpoolman
from printops_fixtures import TestClientTransport, wait_fields
from test_printops_spool_sync import FLEET, GREEN

from printops import ledger, locations, spools
from printops.api import routes_spool_sync
from printops.api.app import create_app
from printops.cli.health import (
    render_spool_sync,
    spool_sync_ok,
    spool_sync_status,
)
from printops.client import ApiClient, ApiError
from printops.settings import Settings
from printops.spool_mirror import SyncedSpoolman
from printops.spoolman import SpoolmanError, ensure_extra_fields


@dataclass
class Shop:
    """A 2-printer fleet, the default warehouse tree and a way to receive spools through the
    same write path 3d-api uses; the DB is the app's own."""

    conn: sqlite3.Connection
    fake: FakeSpoolman
    ctx: spools.Ctx

    def receive(self, location: str = "WH:default", **fields: Any) -> int:
        out = spools.receive(
            self.ctx,
            spools.Receive.from_dict({**GREEN, "location": location, **fields}),
        )
        return int(out["id"])

    def ui_edit(self, sid: int, grams: float) -> None:
        """The Spoolman web UI edit form: sets the remaining weight, ledger never hears of it."""
        self.fake.update("spool", sid, {"remaining_weight": grams})

    def ledger_rows(self, sid: int) -> list[ledger.Row]:
        return ledger.history(self.conn, sid)


def build_shop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
    fake: FakeSpoolman,
) -> Shop:
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(FLEET, encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(fleet))
    monkeypatch.setenv(
        "PRINTOPS_POLICY", str(write_policy_for_fleet(tmp_path / "policy.yaml", fleet))
    )
    ensure_extra_fields(fake)
    sm = SyncedSpoolman(fake, conn)
    for path, kind in (
        ("WH:default", "warehouse"),
        ("WH:A", "zone"),
        ("WH:A/S1", "shelf"),
    ):
        locations.add(conn, path, kind, actor="test")
    locations.ensure_fleet_slots(conn, sm, actor="test", force=True)
    return Shop(conn, fake, spools.Ctx(conn, sm, "test"))


@pytest.fixture
def shop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    printops_client: TestClient,
    printops_db: sqlite3.Connection,
    fake_spoolman: FakeSpoolman,
) -> Shop:
    """printops_client first: the app's extra-field registration has finished before we write."""
    return build_shop(tmp_path, monkeypatch, printops_db, fake_spoolman)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@dataclass
class Http:
    client: TestClient
    tokens: dict[str, str]

    def get(
        self, path: str, token: str | None = "rw", **params: Any
    ) -> Any:  # a Response
        headers = auth(self.tokens[token]) if token else {}
        return self.client.get(path, headers=headers, params=params)

    def post(self, path: str, body: Any = None, token: str | None = "rw") -> Any:
        headers = auth(self.tokens[token]) if token else {}
        return self.client.post(
            path, headers=headers, json=body if body is not None else {}
        )

    def drift(self, **params: Any) -> dict[str, Any]:
        r = self.get("/v1/spool-sync", **params)
        assert r.status_code == 200, r.text
        out: dict[str, Any] = r.json()
        return out

    def ledger(self, sid: int) -> list[dict[str, Any]]:
        r = self.get(f"/v1/spools/{sid}/ledger")
        assert r.status_code == 200, r.text
        rows: list[dict[str, Any]] = r.json()["rows"]
        return rows


@pytest.fixture
def http(printops_client: TestClient, printops_tokens: dict[str, str]) -> Http:
    return Http(printops_client, printops_tokens)


# -- GET /v1/spool-sync ------------------------------------------------------------------------------


def test_drift_is_clean_in_step_then_reports_a_ui_edit_and_scopes_to_a_spool(
    shop: Shop, http: Http
) -> None:
    a, b = shop.receive(), shop.receive()
    clean = http.drift()
    assert (clean["ok"], clean["drift"], clean["drifts"]) == (True, 0, [])
    assert clean["checked"] == 2 and clean["last_pass"] is None

    shop.ui_edit(a, 412)  # behind the system's back
    got = http.drift()
    assert (got["ok"], got["drift"], got["healable"], got["unhealable"]) == (
        False,
        1,
        1,
        0,
    )
    [d] = got["drifts"]
    assert (d["kind"], d["spool_id"], d["healable"], d["blocks"]) == (
        "remaining",
        a,
        True,
        True,
    )
    assert d["detail"]["ledger_g"] == 1000 and d["detail"]["spoolman_g"] == 412

    only_b = http.drift(spool=b)
    assert (only_b["ok"], only_b["checked"], only_b["drifts"]) == (True, 1, [])
    only_a = http.drift(spool=a)
    assert only_a["drift"] == 1 and only_a["drifts"][0]["spool_id"] == a


def test_drift_detection_writes_nothing(shop: Shop, http: Http) -> None:
    """Read-only: listing the drift neither books a ledger row nor writes to Spoolman."""
    sid = shop.receive()
    shop.ui_edit(sid, 412)
    rows, writes = len(shop.ledger_rows(sid)), len(shop.fake.writes())
    for _ in range(2):
        assert http.drift()["drift"] == 1
    assert (len(shop.ledger_rows(sid)), len(shop.fake.writes())) == (rows, writes)


def test_drift_and_heal_need_their_scopes(shop: Shop, http: Http) -> None:
    sid = shop.receive()
    shop.ui_edit(sid, 412)
    assert http.get("/v1/spool-sync", token=None).status_code == 401
    assert http.get("/v1/spool-sync", token="write").status_code == 403  # write ≠ read
    assert http.get("/v1/spool-sync", token="camera").status_code == 403
    assert http.get("/v1/spool-sync", token="read").status_code == 200
    assert http.post("/v1/spool-sync/heal", token=None).status_code == 401
    assert http.post("/v1/spool-sync/heal", token="read").status_code == 403
    assert http.drift()["drift"] == 1  # the refused heals changed nothing
    assert http.post("/v1/spool-sync/heal", token="write").status_code == 200


# -- POST /v1/spool-sync/heal ------------------------------------------------------------------------


def test_heal_anchors_the_ledger_once_and_a_second_heal_changes_nothing(
    shop: Shop, http: Http
) -> None:
    sid = shop.receive()
    shop.ui_edit(sid, 412)

    r = http.post("/v1/spool-sync/heal")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["failed"] == []
    assert [(d["kind"], d["spool_id"]) for d in body["healed"]] == [("remaining", sid)]
    assert body["before"]["drift"] == 1 and body["after"]["drift"] == 0

    anchor = http.ledger(sid)[-1]
    assert (anchor["kind"], anchor["source"], anchor["actor"], anchor["anchor_g"]) == (
        "anchor",
        "manual",
        "spool-sync",
        412.0,
    )
    assert http.drift()["ok"] is True
    # Spoolman was not asked to take the weight back
    assert [w for w in shop.fake.writes() if w[1].endswith(("/use", "/measure"))] == []
    assert shop.fake.get("spool", sid)["remaining_weight"] == 412

    rows, writes = len(http.ledger(sid)), len(shop.fake.writes())
    again = http.post("/v1/spool-sync/heal").json()
    assert again["ok"] is True and again["healed"] == []
    assert (len(http.ledger(sid)), len(shop.fake.writes())) == (rows, writes)


def test_heal_with_a_spool_id_touches_only_that_spool(shop: Shop, http: Http) -> None:
    a, b = shop.receive(), shop.receive()
    shop.ui_edit(a, 400)
    shop.ui_edit(b, 300)

    r = http.post("/v1/spool-sync/heal", {"spool_id": a})
    assert r.status_code == 200, r.text
    assert [d["spool_id"] for d in r.json()["healed"]] == [a]
    left = http.drift()
    assert [d["spool_id"] for d in left["drifts"]] == [b]
    assert http.ledger(a)[-1]["actor"] == "spool-sync"
    assert all(r["actor"] != "spool-sync" for r in http.ledger(b))

    assert http.post("/v1/spool-sync/heal", {"spool_id": b}).json()["ok"] is True
    assert http.drift()["ok"] is True


def test_confirm_deleted_needs_write_and_lifts_the_wiped_spoolman_guard(shop: Shop, http: Http) -> None:
    ids = [shop.receive() for _ in range(5)]
    for sid in ids[:4]:
        shop.fake.delete("spool", sid)
    shop.ui_edit(ids[4], 412)

    assert http.post("/v1/spool-sync/confirm-deleted", token="read").status_code == 403
    assert http.post("/v1/spool-sync/confirm-deleted", token=None).status_code == 401
    stuck = http.post("/v1/spool-sync/heal").json()
    assert stuck["healed"] == [] and "spoolman_empty" in [d["kind"] for d in stuck["after"]["drifts"]]

    done = http.post("/v1/spool-sync/confirm-deleted")
    assert done.status_code == 200, done.text
    body = done.json()
    assert sorted(body["confirmed"]) == ids[:4] and body["still_listed"] == []
    assert "spoolman_empty" not in [d["kind"] for d in body["after"]["drifts"]]
    healed = http.post("/v1/spool-sync/heal").json()
    assert [d["kind"] for d in healed["healed"]] == ["remaining"] and healed["ok"] is True


@pytest.mark.parametrize("bad", ["x", True, 1.5, [1]])
def test_heal_rejects_a_spool_id_that_is_not_an_integer(
    shop: Shop, http: Http, bad: Any
) -> None:
    sid = shop.receive()
    shop.ui_edit(sid, 412)
    r = http.post("/v1/spool-sync/heal", {"spool_id": bad})
    assert r.status_code == 422 and "spool_id" in r.json()["detail"]
    assert http.drift()["drift"] == 1  # nothing was healed by the refused call


def test_heal_reports_what_it_cannot_fix_and_does_not_hide_it(
    shop: Shop, http: Http
) -> None:
    sid = shop.receive()
    shop.fake.update("spool", sid, {"location": "WH:A/S2"})  # not a PrintOps location
    body = http.post("/v1/spool-sync/heal").json()
    assert body["ok"] is False and body["healed"] == []
    assert [(d["kind"], d["healable"]) for d in body["after"]["drifts"]] == [
        ("location_unknown", False)
    ]


# -- the gateway: every spool-writing route keeps the sides in step -------------------------------------


def test_every_spool_route_leaves_the_two_sides_in_step(shop: Shop, http: Http) -> None:
    """The proof that `deps.get_spoolman` hands the routes the SyncedSpoolman: each write through
    the API leaves GET /v1/spool-sync at drift 0."""
    r = http.post("/v1/spools", {**GREEN, "location": "WH:default"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert http.drift()["drift"] == 0, "receive"

    steps: list[tuple[str, str, dict[str, Any]]] = [
        (
            "use",
            f"/v1/spools/{sid}/use",
            {"grams": 12.5, "source": "gcode_estimate", "job_id": "j1"},
        ),
        ("measure", f"/v1/spools/{sid}/measure", {"gross_g": 900.0}),
        ("move", f"/v1/spools/{sid}/move", {"location": "WH:A/S1"}),
        (
            "load",
            "/v1/printers/u1/slots/T3/load",
            {"spool_id": sid, "printer_write": False},
        ),
        (
            "use (correction)",
            f"/v1/spools/{sid}/use",
            {"grams": -2.0, "source": "manual", "note": "correction"},
        ),
        (
            "unload",
            "/v1/printers/u1/slots/T3/unload",
            {"to": "WH:default", "gross_g": 840.0, "printer_write": False},
        ),
        ("empty", f"/v1/spools/{sid}/empty", {"note": "ran out"}),
    ]
    for name, path, body in steps:
        out = http.post(path, body)
        assert out.status_code < 300, (name, out.text)
        got = http.drift()
        assert got["drift"] == 0, (name, got["drifts"])


def test_the_same_use_done_on_spoolman_directly_is_drift_and_heals(
    shop: Shop, http: Http
) -> None:
    """Teeth for the test above: `/use` through the API is in step, the very same `/use` that goes
    around the gateway is not."""
    via_api = http.post("/v1/spools", {**GREEN, "location": "WH:default"}).json()["id"]
    around = http.post("/v1/spools", {**GREEN, "location": "WH:default"}).json()["id"]
    http.post(f"/v1/spools/{via_api}/use", {"grams": 10, "source": "manual"})
    shop.fake.use(
        around, use_weight=10.0
    )  # Moonraker /use, the ledger never hears of it

    got = http.drift()
    assert [(d["spool_id"], d["kind"]) for d in got["drifts"]] == [
        (around, "remaining")
    ]
    assert http.post("/v1/spool-sync/heal").json()["ok"] is True
    assert http.drift()["drift"] == 0
    assert http.ledger(around)[-1]["actor"] == "spool-sync"


# -- GET /v1/spool-sync/status -----------------------------------------------------------------------------


def test_status_needs_no_token_and_says_the_schedule_is_off(
    shop: Shop, http: Http
) -> None:
    sid = shop.receive()
    shop.ui_edit(sid, 412)
    r = http.get("/v1/spool-sync/status", token=None)
    assert r.status_code == 200
    assert r.json() == {
        "enabled": False,
        "interval_s": 0,
        "last_pass": None,
        "anomalies_open": 0,
    }
    # off means off: nobody heals it behind the user's back
    for _ in range(6):
        time.sleep(0.05)
    assert http.drift()["drift"] == 1
    assert http.get("/v1/spool-sync/status", token=None).json()["last_pass"] is None


# -- the schedule -------------------------------------------------------------------------------------------


def _wait_for_a_pass(client: TestClient, deadline_s: float = 8.0) -> dict[str, Any]:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        st: dict[str, Any] = client.get("/v1/spool-sync/status").json()
        if st["last_pass"] and st["last_pass"]["at"]:
            return st
        time.sleep(0.05)
    raise AssertionError("the scheduled pass never ran")


def test_the_schedule_heals_a_ui_edit_without_anyone_calling_heal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    printops_settings: Settings,
    printops_db: sqlite3.Connection,
    printops_tokens: dict[str, str],
    fake_spoolman: FakeSpoolman,
) -> None:
    shop = build_shop(tmp_path, monkeypatch, printops_db, fake_spoolman)
    sid = shop.receive()
    shop.ui_edit(sid, 412)  # BEFORE the API starts
    assert [r.kind for r in shop.ledger_rows(sid)] == ["anchor"]  # only the receive

    monkeypatch.setenv("PRINTOPS_SPOOL_SYNC_S", "10")
    app = create_app(printops_settings, spoolman=fake_spoolman)
    with TestClient(app) as client:
        wait_fields(app)
        st = _wait_for_a_pass(client)
        assert st["enabled"] is True and st["interval_s"] == 10
        assert st["last_pass"]["healed"] == 1 and st["last_pass"]["error"] is None
        rest = Http(client, printops_tokens)
        assert rest.drift()["ok"] is True
        anchor = rest.ledger(sid)[-1]
        assert (anchor["source"], anchor["actor"], anchor["anchor_g"]) == (
            "manual",
            "spool-sync",
            412.0,
        )
        assert rest.drift()["last_pass"]["healed"] == 1
    assert shop.fake.get("spool", sid)["remaining_weight"] == 412


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (None, 60.0),
        ("", 60.0),
        ("0", 0.0),
        ("3", 10.0),
        ("x", 60.0),
        ("-5", 60.0),
        ("nan", 60.0),
        ("inf", 60.0),
        ("120", 120.0),
        ("10", 10.0),
        ("12.5", 12.5),
    ],
)
def test_interval_from_env(raw: str | None, want: float) -> None:
    env = {} if raw is None else {"PRINTOPS_SPOOL_SYNC_S": raw}
    assert routes_spool_sync.interval_from_env(env) == want


def test_interval_from_env_reads_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRINTOPS_SPOOL_SYNC_S")
    assert routes_spool_sync.interval_from_env() == 60.0
    monkeypatch.setenv("PRINTOPS_SPOOL_SYNC_S", "30")
    assert routes_spool_sync.interval_from_env() == 30.0


# -- what a pass cannot fix: anomalies; Spoolman down ------------------------------------------------------------


class Plant:
    """An app with the loop OFF whose scheduled pass the test runs itself (`pass_once`, the exact
    function the loop calls), so a test needs no 10 s wait to see a second pass."""

    def __init__(self, app: FastAPI, http: Http) -> None:
        self.app = app
        self.http = http

    def run_pass(self) -> dict[str, Any]:
        routes_spool_sync.pass_once(self.app.state.printops, self.app.state.spool_sync)
        st: dict[str, Any] = self.http.get("/v1/spool-sync/status", token=None).json()
        return st

    def anomalies(self, status: str = "open,ack") -> list[dict[str, Any]]:
        r = self.http.get("/v1/anomalies", printer="spools", status=status)
        assert r.status_code == 200, r.text
        rows: list[dict[str, Any]] = r.json()["anomalies"]
        return rows


@pytest.fixture
def plant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    printops_settings: Settings,
    printops_db: sqlite3.Connection,
    printops_tokens: dict[str, str],
    fake_spoolman: FakeSpoolman,
) -> Any:
    shop = build_shop(tmp_path, monkeypatch, printops_db, fake_spoolman)
    app = create_app(printops_settings, spoolman=fake_spoolman)
    with TestClient(app) as client:
        wait_fields(app)
        yield shop, Plant(app, Http(client, printops_tokens))


def test_an_unhealable_drift_opens_an_anomaly_that_closes_when_the_condition_clears(
    plant: tuple[Shop, Plant],
) -> None:
    shop, p = plant
    sid = shop.receive()
    assert p.run_pass()["anomalies_open"] == 0 and p.anomalies() == []

    shop.fake.update("spool", sid, {"location": "WH:A/S2"})  # PrintOps does not know it
    st = p.run_pass()
    assert st["anomalies_open"] == 1
    assert st["last_pass"]["drift"] == 1 and st["last_pass"]["failed"] == 0
    [an] = p.anomalies()
    assert (an["kind"], an["scope"], an["subject"], an["status"]) == (
        "location_unknown",
        "spools",
        f"spool:{sid}",
        "open",
    )
    assert "WH:A/S2" in str(an["evidence"])
    # the same condition on the next pass is the same anomaly, not a new one
    assert p.run_pass()["anomalies_open"] == 1
    assert [a["id"] for a in p.anomalies()] == [an["id"]]

    r = p.http.post("/v1/locations", {"path": "WH:A/S2", "kind": "shelf"})
    assert r.status_code == 201, r.text  # the condition clears: the location exists now
    st = p.run_pass()
    assert st["anomalies_open"] == 0 and st["last_pass"]["drift"] == 0
    assert p.anomalies() == []
    [closed] = p.anomalies("resolved")
    assert closed["id"] == an["id"] and closed["resolved_by"] is not None


def test_a_spoolman_that_is_down_records_the_error_and_keeps_everything_else(
    plant: tuple[Shop, Plant],
) -> None:
    shop, p = plant
    sid = shop.receive()
    shop.fake.update("spool", sid, {"location": "WH:A/S2"})
    before = p.run_pass()
    assert before["anomalies_open"] == 1 and before["last_pass"]["drift"] == 1

    shop.fake.down = True
    try:
        down = p.run_pass()
    finally:
        shop.fake.down = False
    last = down["last_pass"]
    assert last["error"] and "unreachable" in last["error"]
    # the previous outcome stays visible and nothing was resolved on guesswork
    assert (last["at"], last["checked"], last["drift"]) == (
        before["last_pass"]["at"],
        before["last_pass"]["checked"],
        before["last_pass"]["drift"],
    )
    assert down["anomalies_open"] == 1
    assert [a["status"] for a in p.anomalies()] == ["open"]

    again = (
        p.run_pass()
    )  # Spoolman is back: the error clears, the anomaly is still real
    assert again["last_pass"]["error"] is None and again["last_pass"]["drift"] == 1
    assert again["anomalies_open"] == 1


def test_a_pass_with_no_spoolman_configured_says_so(
    printops_settings: Settings, printops_tokens: dict[str, str]
) -> None:
    app = create_app(printops_settings)  # no SPOOLMAN_URL, no injected Spoolman
    with TestClient(app) as client:
        rest = Http(client, printops_tokens)
        assert rest.get("/v1/spool-sync").status_code == 503
        assert rest.post("/v1/spool-sync/heal").status_code == 503
        routes_spool_sync.pass_once(app.state.printops, app.state.spool_sync)
        st = client.get("/v1/spool-sync/status").json()
        assert st["last_pass"]["error"] == "SPOOLMAN_URL is not set"


def test_a_spoolman_refusal_is_a_502_not_a_crash(shop: Shop, http: Http) -> None:
    shop.fake.down = True
    try:
        r = http.get("/v1/spool-sync")
    finally:
        shop.fake.down = False
    assert r.status_code == 502 and "unreachable" in r.json()["detail"]
    assert http.drift()["ok"] is True  # and it recovers


# -- `3d printops health`: the data path -----------------------------------------------------------------------


def _status(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "enabled": True,
        "interval_s": 60.0,
        "last_pass": {
            "at": "2026-11-01T10:00:00Z",
            "checked": 3,
            "drift": 0,
            "healed": 0,
            "failed": 0,
            "error": None,
        },
        "anomalies_open": 0,
    }
    base.update(over)
    return base


def test_health_judges_the_status_ok_only_when_nothing_drifts_and_nothing_is_stuck() -> (
    None
):
    assert spool_sync_ok(_status()) is True
    assert (
        spool_sync_ok(_status(last_pass=None)) is True
    )  # never ran: nothing to report
    assert spool_sync_ok(_status(enabled=False, last_pass=None)) is True
    assert spool_sync_ok(None) is True  # an old 3d-api
    drifting = _status()
    drifting["last_pass"]["drift"] = 2
    assert spool_sync_ok(drifting) is False
    failing = _status()
    failing["last_pass"]["error"] = "Spoolman is unreachable"
    assert spool_sync_ok(failing) is False
    assert spool_sync_ok(_status(anomalies_open=1)) is False


def test_health_renders_each_state_distinctly() -> None:
    ok = render_spool_sync(_status())
    assert "spool sync  ok" in ok and "0 drift" in ok and "every 60 s" in ok

    drifting = _status()
    drifting["last_pass"]["drift"] = 2
    text = render_spool_sync(drifting)
    assert "DRIFT" in text and "2 drift" in text and "3d printops spool drift" in text

    failing = _status()
    failing["last_pass"]["error"] = "Spoolman is unreachable"
    assert "LAST PASS FAILED: Spoolman is unreachable" in render_spool_sync(failing)

    stuck = render_spool_sync(_status(anomalies_open=2))
    assert "DRIFT" in stuck and "2 anomaly(ies)" in stuck

    off = render_spool_sync(_status(enabled=False, interval_s=0, last_pass=None))
    assert "OFF" in off and "PRINTOPS_SPOOL_SYNC_S=0" in off
    assert "starting" in render_spool_sync(_status(last_pass=None))
    old = render_spool_sync(None)
    assert "not available" in old and "DRIFT" not in old


def test_health_reads_the_live_status_and_treats_a_404_as_an_old_api(
    api_client: ApiClient, shop: Shop
) -> None:
    got = spool_sync_status(api_client)
    assert got == {
        "enabled": False,
        "interval_s": 0,
        "last_pass": None,
        "anomalies_open": 0,
    }
    assert spool_sync_ok(got) and "OFF" in render_spool_sync(got)

    old = FastAPI()  # a 3d-api from before the sync: no such route
    old_api = ApiClient("http://old", "tok", TestClientTransport(TestClient(old)))
    assert spool_sync_status(old_api) is None

    broken = FastAPI()

    @broken.get("/v1/spool-sync/status")
    def _boom() -> None:
        raise SpoolmanError("boom", status=500)

    broken_api = ApiClient(
        "http://broken",
        "tok",
        TestClientTransport(TestClient(broken, raise_server_exceptions=False)),
    )
    with pytest.raises(
        ApiError
    ) as e:  # only a 404 means "old": a 500 is a real failure
        spool_sync_status(broken_api)
    assert e.value.status == 500
