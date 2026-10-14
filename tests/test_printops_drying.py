"""Drying sessions, the drying policy, the due check and the read-only HA humidity link (#162)."""

from __future__ import annotations

import contextlib
import datetime
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from printops_fakes import FakeSpoolman

from printops import drying, events, locations, policy
from printops.api import routes_drying
from printops.client import ApiClient, ApiError
from printops.drying_policy import DryingPolicy
from printops.drying_policy import parse as parse_drying
from printops.homeassistant import HaError, HaStates
from printops.spoolman import decode_extra, encode_extra, ensure_extra_fields

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


# -- fixtures / helpers ---------------------------------------------------------------------
class FakeHA:
    """Home Assistant `GET /api/states/<id>` over real HTTP; records every request."""

    def __init__(self, token: str = "ha-secret") -> None:
        self.token = token
        self.states: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str]] = []

    def set(self, entity: str, state: str, unit: str | None = "%") -> None:
        attrs = {"unit_of_measurement": unit} if unit else {}
        self.states[entity] = {
            "entity_id": entity,
            "state": state,
            "attributes": attrs,
            "last_updated": "2026-10-14T11:59:00+00:00",
        }

    @contextlib.contextmanager
    def serve(self) -> Iterator[str]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                return

            def _any(self) -> None:
                fake.requests.append((self.command, self.path))
                if self.command != "GET":
                    self._reply(405, {"message": "Method not allowed"})
                elif self.headers.get("Authorization") != f"Bearer {fake.token}":
                    self._reply(401, {"message": "Unauthorized"})
                elif (
                    self.path.startswith("/api/states/")
                    and self.path[12:] in fake.states
                ):
                    self._reply(200, fake.states[self.path[12:]])
                else:
                    self._reply(404, {"message": "Entity not found."})

            do_GET = do_POST = do_PUT = do_DELETE = _any  # noqa: N815 — http.server's naming

            def _reply(self, status: int, body: Any) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            server.server_close()


@pytest.fixture
def fake_ha(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeHA]:
    ha = FakeHA()
    with ha.serve() as url:
        monkeypatch.setenv("PRINTOPS_HA_URL", url)
        monkeypatch.setenv("PRINTOPS_HA_TOKEN", ha.token)
        yield ha


@pytest.fixture(autouse=True)
def _no_ambient_ha(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "PRINTOPS_HA_URL",
        "PRINTOPS_HA_TOKEN",
        "PRINTOPS_HA_TOKEN_FILE",
        "PRINTOPS_POLICY",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def dpolicy() -> DryingPolicy:
    loaded: DryingPolicy = policy.section("drying")
    return loaded


def iso(days_ago: float, now: datetime.datetime = NOW) -> str:
    return drying.format_ts(now - datetime.timedelta(days=days_ago))


def add_spool(
    sp: FakeSpoolman,
    material: str,
    *,
    opened: str | None = None,
    dried: str | None = None,
    dry_state: str | None = None,
    location_id: int | None = None,
    filament_extra: dict[str, Any] | None = None,
) -> int:
    vendors = sp.find("vendor", {"name": "Elegoo"})
    vendor = vendors[0] if vendors else sp.create("vendor", {"name": "Elegoo"})
    fil = sp.create(
        "filament",
        {
            "name": f"{material} test",
            "vendor_id": vendor["id"],
            "material": material,
            "density": 1.24,
            "diameter": 1.75,
            "weight": 1000,
            "extra": {k: encode_extra(v) for k, v in (filament_extra or {}).items()},
        },
    )
    extra = {
        k: encode_extra(v)
        for k, v in (
            ("opened_at", opened),
            ("last_dried_at", dried),
            ("dry_state", dry_state),
            ("printops_location_id", location_id),
        )
        if v is not None
    }
    return int(sp.create("spool", {"filament_id": fil["id"], "extra": extra})["id"])


def spool_extra(sp: FakeSpoolman, spool_id: int) -> dict[str, Any]:
    return {k: decode_extra(v) for k, v in sp.get("spool", spool_id)["extra"].items()}


def kinds(conn: sqlite3.Connection) -> list[str]:
    return [e.kind for e in events.last(conn, 200)]


@pytest.fixture
def registered(fake_spoolman: FakeSpoolman) -> FakeSpoolman:
    ensure_extra_fields(fake_spoolman)
    return fake_spoolman


# -- policy ---------------------------------------------------------------------------------
def test_committed_policy_file_passes_the_schema_check(dpolicy: DryingPolicy) -> None:
    assert policy.check() == []
    assert set(dpolicy.classes) == {"low", "medium", "high", "extreme"}
    # every material default cites a source that is in the file
    assert all(
        all(s in dpolicy.sources for s in m.source.split("+"))
        for m in dpolicy.materials.values()
    )
    assert dpolicy.material("PLA").hygroscopic_class == "low"  # type: ignore[union-attr]
    assert dpolicy.material("PA6").hygroscopic_class == "extreme"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("material", "expected"),
    [
        ("PLA", "PLA"),
        ("pla+", "PLA"),
        ("PLA Silk", "PLA"),
        ("PETG-CF", "PETG"),
        ("PA12-CF", "PA12"),
        ("PA-CF", "PA"),
        ("PET-CF", "PET-CF"),
        ("PCTG", None),
        ("PET", None),
        ("", None),
        (None, None),
    ],
)
def test_material_lookup_matches_families_not_lookalikes(
    dpolicy: DryingPolicy, material: str | None, expected: str | None
) -> None:
    row = dpolicy.material(material)
    assert (row.name if row else None) == expected


def test_filament_extra_fields_override_the_material_recipe(
    dpolicy: DryingPolicy,
) -> None:
    plain = dpolicy.recipe({"material": "PETG", "extra": {}})
    hf = dpolicy.recipe(
        {
            "material": "PETG",
            "extra": {
                "hygroscopic_class": "high",
                "drying_temp_c": 65,
                "drying_hours": 10.5,
            },
        }
    )
    unknown = dpolicy.recipe({"material": "HIPS", "extra": {}})
    assert (plain.hygroscopic_class, plain.temp_c, plain.hours) == ("medium", 55, 8.0)
    assert (hf.hygroscopic_class, hf.temp_c, hf.hours) == ("high", 65, 10.5)
    assert "filament extra: class, temp, hours" in hf.origin
    assert (unknown.hygroscopic_class, unknown.temp_c, unknown.origin) == (
        "medium",
        None,
        "default_class",
    )


def _policy_text() -> str:
    return policy.policy_path().read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda t: t + "\ndryng: {}\n", "unknown section(s) dryng"),
        (
            lambda t: t.replace(
                "    extreme: {max_days_opened: 1,  max_days_since_dried: 3,  max_rh: 20}\n",
                "",
            ),
            "classes: missing key(s) extreme",
        ),
        (
            lambda t: t.replace("max_rh: 50", "max_rh: 150"),
            "classes.low.max_rh must be ≥ 0 and ≤ 100",
        ),
        (
            lambda t: t.replace("source: prusa}", "source: wiki}", 1),
            "cites unknown source(s) wiki",
        ),
        (
            lambda t: t.replace("https://help.prusa3d.com", "http://help.prusa3d.com"),
            "sources.prusa must be an https URL",
        ),
        (
            lambda t: t.replace("wet_factor: 2.0", "wet_factor: 1"),
            "wet_factor must be > 1",
        ),
        (
            lambda t: t.replace("    PLA:    {class: low,", "    pla:    {class: low,"),
            "must be upper-case",
        ),
        (
            lambda t: t.replace("fresh_days: 3", "fresh_days: 3\n  colour: red"),
            "unknown key(s) colour",
        ),
    ],
)
def test_policy_schema_check_reports_the_broken_rule(
    tmp_path: Path, mutate: Any, message: str
) -> None:
    bad = tmp_path / "policy.yaml"
    bad.write_text(mutate(_policy_text()), encoding="utf-8")
    problems = policy.check(bad)
    assert problems and message in problems[0], problems
    with pytest.raises(policy.PolicyError, match="drying|section"):
        policy.section("drying", bad)


def test_policy_path_follows_the_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alt = tmp_path / "p.yaml"
    alt.write_text(
        "drying:\n" + _policy_text().split("drying:\n", 1)[1], encoding="utf-8"
    )
    monkeypatch.setenv("PRINTOPS_POLICY", str(alt))
    assert policy.policy_path() == alt and policy.check() == []
    monkeypatch.setenv("PRINTOPS_POLICY", str(tmp_path / "missing.yaml"))
    assert "not found" in policy.check()[0]


def test_parse_rejects_a_non_integer_check_interval(dpolicy: DryingPolicy) -> None:
    import yaml

    body = yaml.safe_load(_policy_text())["drying"]
    body["check_every_min"] = 0.5
    with pytest.raises(policy.PolicyError, match="check_every_min"):
        parse_drying(body)


# -- the pure assessment --------------------------------------------------------------------
def _spool(material: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": 7,
        "filament": {"material": material, "name": "x", "extra": {}},
        "extra": {k: json.dumps(v) for k, v in extra.items()},
    }


@pytest.mark.parametrize(
    ("spool", "state", "reason"),
    [
        (_spool("PLA"), "unknown", "never opened or dried"),
        (_spool("PETG", opened_at=iso(10)), "ok", ""),
        (_spool("PETG", opened_at=iso(31)), "due", "opened 31 d ago ≥ 30 d (medium)"),
        (_spool("PETG", opened_at=iso(61)), "wet", "≥ 2 × 30 d"),
        (_spool("PA", last_dried_at=iso(1)), "fresh", ""),
        (
            _spool("PA", last_dried_at=iso(3.5)),
            "due",
            "dried 3.5 d ago ≥ 3 d (extreme)",
        ),
        (
            _spool("PA", last_dried_at=iso(4), opened_at=iso(400)),
            "due",
            "dried 4 d",
        ),  # last_dried_at wins
        (_spool("PLA", opened_at=iso(10), dry_state="due"), "due", ""),  # sticky
        (_spool("PLA", opened_at="yesterday"), "unknown", ""),
    ],
)
def test_assess_states(
    dpolicy: DryingPolicy, spool: dict[str, Any], state: str, reason: str
) -> None:
    a = drying.assess(dpolicy, spool, now=NOW)
    assert a.state == state, a
    assert reason in " ".join(a.reasons)


def test_assess_storage_humidity_makes_a_spool_due(dpolicy: DryingPolicy) -> None:
    damp = drying.assess(
        dpolicy,
        _spool("TPU", last_dried_at=iso(5)),
        now=NOW,
        humidity=drying.Humidity("sensor.box_rh", 41.0),
    )
    dry = drying.assess(
        dpolicy,
        _spool("TPU", last_dried_at=iso(5)),
        now=NOW,
        humidity=drying.Humidity("sensor.box_rh", 22.0),
    )
    sealed_in_damp = drying.assess(
        dpolicy, _spool("PA"), now=NOW, humidity=drying.Humidity("sensor.box_rh", 35.0)
    )
    broken = drying.assess(
        dpolicy,
        _spool("TPU", last_dried_at=iso(5)),
        now=NOW,
        humidity=drying.Humidity(
            "sensor.box_rh", None, "state 'unavailable' is not a number"
        ),
    )
    assert (damp.state, damp.reasons) == (
        "due",
        ["storage RH 41% > 30% (sensor.box_rh)"],
    )
    assert dry.state == "ok" and dry.rh == 22.0
    assert sealed_in_damp.state == "due"
    assert broken.state == "ok" and broken.notes == [
        "RH unknown: state 'unavailable' is not a number"
    ]


def test_assess_marks_spools_in_a_session_as_drying(dpolicy: DryingPolicy) -> None:
    a = drying.assess(dpolicy, _spool("PETG", opened_at=iso(90)), now=NOW, drying=True)
    assert a.state == "drying" and not a.alert


# -- Home Assistant (read-only) -------------------------------------------------------------
def test_ha_reader_reads_numeric_humidity_and_only_ever_gets() -> None:
    ha = FakeHA()
    ha.set("sensor.dry_box_rh", "18.4")
    ha.set("sensor.dead_rh", "unavailable")
    ha.set("sensor.box_temp", "24.0", unit="°C")
    ha.set("sensor.weird_rh", "140")
    with ha.serve() as url:
        client = HaStates(url, ha.token)
        reading = client.humidity("sensor.dry_box_rh")
        errors = {}
        for entity in (
            "sensor.dead_rh",
            "sensor.box_temp",
            "sensor.weird_rh",
            "sensor.nope",
            "not an entity",
        ):
            with pytest.raises(HaError) as e:
                client.humidity(entity)
            errors[entity] = str(e.value)
        with pytest.raises(HaError, match="HTTP 401"):
            HaStates(url, "wrong").humidity("sensor.dry_box_rh")
    assert (reading.value, reading.unit) == (18.4, "%")
    assert "'unavailable' is not a number" in errors["sensor.dead_rh"]
    assert "expected relative humidity in %" in errors["sensor.box_temp"]
    assert "not a relative humidity" in errors["sensor.weird_rh"]
    assert "not found" in errors["sensor.nope"]
    assert "not a Home Assistant entity id" in errors["not an entity"]
    assert ha.requests and {m for m, _ in ha.requests} == {"GET"}


def test_ha_reader_config_from_env(tmp_path: Path) -> None:
    token_file = tmp_path / "ha_token"
    token_file.write_text("from-file\n", encoding="utf-8")
    assert HaStates.from_env({}) is None
    from_file = HaStates.from_env(
        {
            "PRINTOPS_HA_URL": "http://ha:8123/",
            "PRINTOPS_HA_TOKEN_FILE": str(token_file),
        }
    )
    assert from_file is not None and (from_file.base_url, from_file.token) == (
        "http://ha:8123",
        "from-file",
    )
    with pytest.raises(HaError, match="PRINTOPS_HA_TOKEN"):
        HaStates.from_env({"PRINTOPS_HA_URL": "http://ha:8123"})
    with pytest.raises(HaError):
        HaStates("http://127.0.0.1:9", "t", timeout=1).humidity("sensor.x")


# -- sessions through the API ---------------------------------------------------------------
@pytest.fixture
def dryer(printops_client: Any, printops_db: sqlite3.Connection) -> locations.Location:
    return locations.add(
        printops_db,
        "dryer:1",
        "dryer",
        actor="test",
        humidity_sensor_entity="sensor.dryer_rh",
    )


def test_start_takes_recipe_defaults_warns_and_finish_updates_spoolman(
    api_client: ApiClient,
    fake_spoolman: FakeSpoolman,
    printops_db: sqlite3.Connection,
    dryer: locations.Location,
) -> None:
    pla = add_spool(
        fake_spoolman,
        "PLA",
        opened=iso(70, datetime.datetime.now(UTC)),
        dry_state="due",
    )
    pa = add_spool(fake_spoolman, "PA12-CF")

    started = api_client.post(
        "/v1/drying/sessions",
        {
            "dryer": "dryer:1",
            "spools": [pla, pa],
            "weights_before": {str(pa): 1180.0},
            "source": "cli",
        },
    )
    session = started["session"]
    assert (session["temp_c"], session["target_h"], session["open"]) == (45, 12, True)
    assert (
        session["dryer"] == "dryer:1" and session["rh_before"] is None
    )  # no HA configured
    assert any(
        "rated 80 °C: at 45 °C it may not dry fully" in w for w in started["warnings"]
    )
    with pytest.raises(ApiError) as busy:
        api_client.post(
            "/v1/drying/sessions",
            {"dryer": "dryer:1", "spools": [pa], "temp_c": 80, "hours": 4},
        )
    assert busy.value.status == 409

    listed = api_client.get("/v1/drying/sessions", open="true")["sessions"]
    assert [s["id"] for s in listed] == [session["id"]]
    due = api_client.get("/v1/drying/due", all="true")["spools"]
    assert {a["spool_id"]: a["state"] for a in due} == {pla: "drying", pa: "drying"}

    finished = api_client.post(
        f"/v1/drying/sessions/{session['id']}/finish",
        {"weights_after": {str(pa): 1171.5}, "rh_after": 12},
    )["session"]
    assert (
        finished["open"] is False
        and finished["rh_after"] == 12
        and finished["finished_by"] == "test-rw"
    )
    for sid in (pla, pa):
        extra = spool_extra(fake_spoolman, sid)
        assert (
            extra["dry_state"] == "fresh"
            and extra["last_dried_at"] == finished["ended_at"]
        )
    assert drying.parse_ts(finished["ended_at"]) <= datetime.datetime.now(UTC)
    notes = printops_db.execute(
        "SELECT spool_id, delta_g, note FROM ledger ORDER BY id"
    ).fetchall()
    assert [(r["spool_id"], r["delta_g"]) for r in notes] == [(pa, 0.0)]
    assert "1180 → 1171.5 g (+8.5 g water lost, not usage)" in notes[0]["note"]
    assert not [
        c
        for c in fake_spoolman.calls
        if c[1].endswith("/use") or c[1].endswith("/measure")
    ]
    finished_events = [
        e for e in events.last(printops_db, 50) if e.kind == "drying.finished"
    ]
    assert finished_events[-1].payload["trigger"] == "finish"
    assert finished_events[-1].payload["water_g"] == {str(pa): 8.5}
    assert "drying.started" in kinds(printops_db)

    with pytest.raises(ApiError) as again:
        api_client.post(f"/v1/drying/sessions/{session['id']}/finish", {})
    assert again.value.status == 409


@pytest.mark.parametrize(
    ("body", "status", "message"),
    [
        ({"dryer": "dryer:9", "spools": [1]}, 404, "dryer:9"),
        ({"dryer": "WH:A", "spools": [1]}, 400, "not a dryer"),
        (
            {"dryer": "dryer:1", "spools": [999]},
            400,
            "spool 999 does not exist in Spoolman",
        ),
        ({"dryer": "dryer:1", "spools": [1, 1]}, 400, "spool listed twice"),
        (
            {"dryer": "dryer:1", "spools": [1], "source": "telepathy"},
            400,
            "source must be one of",
        ),
        ({"dryer": "dryer:1", "spools": [1], "temp_c": 200}, 422, ""),
        (
            {"dryer": "dryer:1", "spools": [1], "weights_before": {"2": 900}},
            400,
            "spool 2 is not in this session",
        ),
        ({"dryer": "dryer:1", "spools": [2]}, 400, "no drying temperature known"),
    ],
)
def test_start_refuses_bad_requests(
    api_client: ApiClient,
    fake_spoolman: FakeSpoolman,
    printops_db: sqlite3.Connection,
    dryer: locations.Location,
    body: dict[str, Any],
    status: int,
    message: str,
) -> None:
    locations.add(printops_db, "WH:A", "warehouse", actor="test")
    add_spool(fake_spoolman, "PETG")
    add_spool(fake_spoolman, "HIPS")
    with pytest.raises(ApiError) as e:
        api_client.post("/v1/drying/sessions", body)
    assert e.value.status == status and message in e.value.message
    assert drying.list_sessions(printops_db) == []


def test_rh_is_read_from_the_dryer_sensor_when_ha_is_configured(
    api_client: ApiClient,
    fake_spoolman: FakeSpoolman,
    dryer: locations.Location,
    fake_ha: FakeHA,
) -> None:
    fake_ha.set("sensor.dryer_rh", "47.5")
    spool = add_spool(fake_spoolman, "PETG")
    started = api_client.post(
        "/v1/drying/sessions", {"dryer": "dryer:1", "spools": [spool]}
    )
    fake_ha.set("sensor.dryer_rh", "unavailable")
    finished = api_client.post(
        f"/v1/drying/sessions/{started['session']['id']}/finish", {}
    )
    assert started["session"]["rh_before"] == 47.5 and started["warnings"] == []
    assert finished["session"]["rh_after"] is None
    assert finished["warnings"] == [
        "RH not recorded: Home Assistant sensor.dryer_rh: state 'unavailable' is not a number"
    ]
    assert {m for m, _ in fake_ha.requests} == {"GET"}


def test_finish_with_spoolman_down_records_nothing_and_can_be_retried(
    api_client: ApiClient,
    fake_spoolman: FakeSpoolman,
    printops_db: sqlite3.Connection,
    dryer: locations.Location,
) -> None:
    spool = add_spool(fake_spoolman, "PETG")
    session_id = api_client.post(
        "/v1/drying/sessions", {"dryer": "dryer:1", "spools": [spool]}
    )["session"]["id"]
    fake_spoolman.down = True
    with pytest.raises(ApiError) as e:
        api_client.post(f"/v1/drying/sessions/{session_id}/finish", {})
    assert e.value.status == 502
    assert drying.get_session(printops_db, session_id).ended_at is None
    fake_spoolman.down = False
    assert (
        api_client.post(f"/v1/drying/sessions/{session_id}/finish", {})["session"][
            "open"
        ]
        is False
    )


def test_timer_emits_drying_finished_once(
    registered: FakeSpoolman, printops_db: sqlite3.Connection, dpolicy: DryingPolicy
) -> None:
    locations.add(printops_db, "dryer:1", "dryer", actor="test")
    spool = add_spool(registered, "PLA")
    session, _ = drying.start(
        printops_db,
        registered,
        dpolicy,
        dryer="dryer:1",
        spool_ids=[spool],
        actor="t",
        hours=6,
    )
    later = drying.parse_ts(session.started_at) + datetime.timedelta(hours=6, minutes=1)
    assert (
        drying.fire_timers(printops_db, now=later - datetime.timedelta(minutes=2)) == []
    )
    fired = drying.fire_timers(printops_db, now=later)
    assert [e.payload["trigger"] for e in fired] == ["timer"]
    assert drying.fire_timers(printops_db, now=later) == []
    assert drying.get_session(printops_db, session.id).timer_event_id == fired[0].id
    assert drying.get_session(printops_db, session.id).ended_at is None


# -- the periodic check ---------------------------------------------------------------------
def test_check_updates_dry_state_and_emits_due_only_on_entering(
    registered: FakeSpoolman, printops_db: sqlite3.Connection, dpolicy: DryingPolicy
) -> None:
    petg = add_spool(registered, "PETG", opened=iso(40))
    pla = add_spool(registered, "PLA", opened=iso(10))
    sealed = add_spool(registered, "PA")

    first = drying.check(printops_db, registered, dpolicy, now=NOW)
    second = drying.check(printops_db, registered, dpolicy, now=NOW)
    wetter = drying.check(
        printops_db, registered, dpolicy, now=NOW + datetime.timedelta(days=30)
    )

    assert first.changed == [
        {"spool_id": petg, "from": None, "to": "due"},
        {"spool_id": pla, "from": None, "to": "ok"},
    ]
    assert len(first.alerts) == 1 and second.changed == [] and second.alerts == []
    assert {c["spool_id"]: c["to"] for c in wetter.changed} == {petg: "wet"} and len(
        wetter.alerts
    ) == 1
    assert spool_extra(registered, petg)["dry_state"] == "wet"
    assert "dry_state" not in spool_extra(registered, sealed)
    due_events = [e for e in events.last(printops_db, 50) if e.kind == "drying.due"]
    assert [
        (e.subject, e.payload["state"], e.payload["previous"]) for e in due_events
    ] == [(f"spool:{petg}", "due", None), (f"spool:{petg}", "wet", "due")]
    assert (
        due_events[0].payload["class"] == "medium"
        and due_events[0].payload["limit_days"] == 30
    )


def test_check_reads_the_storage_sensor_through_the_location_tree(
    registered: FakeSpoolman, printops_db: sqlite3.Connection, dpolicy: DryingPolicy
) -> None:
    ha = FakeHA()
    ha.set("sensor.box1_rh", "35")
    locations.add(printops_db, "WH:A", "warehouse", actor="t")
    box = locations.add(
        printops_db,
        "WH:A/BOX1",
        "dry_box",
        actor="t",
        humidity_sensor_entity="sensor.box1_rh",
    )
    cell = locations.add(printops_db, "WH:A/BOX1/01", "slot", actor="t")
    tpu = add_spool(registered, "TPU", dried=iso(2), location_id=cell.id)
    pla = add_spool(registered, "PLA", dried=iso(2), location_id=box.id)
    with ha.serve() as url:
        report = drying.check(
            printops_db, registered, dpolicy, now=NOW, humidity=HaStates(url, ha.token)
        )
    states = {a.spool_id: a for a in report.assessments}
    assert (
        states[tpu].state == "due"
        and states[tpu].rh_entity == "sensor.box1_rh"
        and states[tpu].rh == 35
    )
    assert states[tpu].location == "WH:A/BOX1/01"
    assert states[pla].state == "fresh"  # PLA tolerates 35 % (max 50)
    assert ha.requests == [
        ("GET", "/api/states/sensor.box1_rh")
    ]  # one read per sensor per check


def test_due_endpoint_is_read_only_and_check_needs_write(
    printops_client: Any,
    printops_tokens: dict[str, str],
    fake_spoolman: FakeSpoolman,
    api_client: ApiClient,
) -> None:
    petg = add_spool(fake_spoolman, "PETG", opened=iso(40, datetime.datetime.now(UTC)))
    writes_before = len(fake_spoolman.writes())
    due = api_client.get("/v1/drying/due")
    assert [(a["spool_id"], a["state"]) for a in due["spools"]] == [(petg, "due")]
    assert len(fake_spoolman.writes()) == writes_before
    reader = printops_client.post(
        "/v1/drying/check",
        headers={"Authorization": f"Bearer {printops_tokens['read']}"},
    )
    assert reader.status_code == 403
    report = api_client.post("/v1/drying/check")
    assert (
        report["changed"] == [{"spool_id": petg, "from": None, "to": "due"}]
        and len(report["alerts"]) == 1
    )
    assert report["due"][0]["spool_id"] == petg


def test_broken_policy_is_a_503_not_a_crash(
    api_client: ApiClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / "policy.yaml"
    bad.write_text("drying: {}\n", encoding="utf-8")
    monkeypatch.setenv("PRINTOPS_POLICY", str(bad))
    with pytest.raises(ApiError) as e:
        api_client.get("/v1/drying/due")
    assert e.value.status == 503 and "drying policy unusable" in e.value.message


def test_background_loop_runs_the_check_and_fires_timers(
    printops_settings: Any, fake_spoolman: FakeSpoolman, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from printops.api.app import create_app
    from printops.db import open_db

    monkeypatch.setattr(routes_drying, "FIRST_CHECK_DELAY_S", 0.0)
    monkeypatch.setattr(routes_drying, "TIMER_TICK_S", 0.05)
    ensure_extra_fields(fake_spoolman)
    now = datetime.datetime.now(UTC)
    petg = add_spool(fake_spoolman, "PETG", opened=iso(40, now))
    app = create_app(printops_settings, spoolman=fake_spoolman)  # migrates the DB
    conn = open_db(printops_settings.db_path)
    try:
        locations.add(conn, "dryer:1", "dryer", actor="t")
        pla = add_spool(fake_spoolman, "PLA")
        session, _ = drying.start(
            conn,
            fake_spoolman,
            policy.section("drying"),
            dryer="dryer:1",
            spool_ids=[pla],
            actor="t",
            hours=0.0001,
        )  # elapses at once
        with TestClient(app):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and (
                spool_extra(fake_spoolman, petg).get("dry_state") != "due"
                or drying.get_session(conn, session.id).timer_event_id is None
            ):
                time.sleep(0.05)
        assert spool_extra(fake_spoolman, petg).get("dry_state") == "due"
        assert drying.get_session(conn, session.id).timer_event_id is not None
        assert "drying.due" in kinds(conn)
    finally:
        conn.close()


# -- 3d lint --printops rules ---------------------------------------------------------------
def test_lint_rules_flag_bad_dates_forgotten_sessions_and_stale_state(
        registered: FakeSpoolman, printops_db: sqlite3.Connection, dpolicy: DryingPolicy,
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from printops import datalint, lint_drying

    locations.add(printops_db, "dryer:1", "dryer", actor="t")
    bad = add_spool(registered, "PLA", opened="last tuesday")
    stale = add_spool(registered, "PETG", opened=iso(40), dry_state="ok")
    fine = add_spool(registered, "PETG", opened=iso(40), dry_state="due")
    session, _ = drying.start(printops_db, registered, dpolicy, dryer="dryer:1", spool_ids=[fine], actor="t", hours=2)

    def run(now: datetime.datetime) -> dict[str, list[str]]:
        ctx = datalint.LintContext(conn=printops_db, spoolman=registered, spools=registered.find("spool"), now=now)
        found: dict[str, list[str]] = {}
        for f in lint_drying.lint(ctx):
            found.setdefault(f.rule, []).append(f.subject)
        return found

    later = drying.parse_ts(session.ends_at) + datetime.timedelta(hours=25)
    found = run(later)
    assert found["drying/bad-date"] == [f"spool:{bad}"]
    assert found["drying/forgotten"] == [f"drying:{session.id}"]
    assert found["drying/state-stale"] == [f"spool:{stale}"]  # `fine` is drying, `bad` unknown
    assert "drying/policy" not in found

    broken = tmp_path / "policy.yaml"
    broken.write_text("drying: {}\n", encoding="utf-8")
    monkeypatch.setenv("PRINTOPS_POLICY", str(broken))
    found = run(later)
    assert found["drying/policy"] == [str(broken)] and "drying/state-stale" not in found


def test_check_lowering_wet_to_due_does_not_alert(
        registered: FakeSpoolman, printops_db: sqlite3.Connection, dpolicy: DryingPolicy) -> None:
    """E.g. after a policy relaxation: the state follows, but nothing new happened to the spool."""
    petg = add_spool(registered, "PETG", opened=iso(40), dry_state="wet")
    report = drying.check(printops_db, registered, dpolicy, now=NOW)
    assert report.changed == [{"spool_id": petg, "from": "wet", "to": "due"}] and report.alerts == []
    assert "drying.due" not in kinds(printops_db)
