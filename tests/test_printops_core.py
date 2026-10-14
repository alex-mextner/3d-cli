"""PrintOps core (#158): DB + migrations, events, tokens, Spoolman boundary, 3d-api, client, CLI."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import stat
from pathlib import Path
from typing import Any

import pytest
from printops_fakes import FakeSpoolman
from printops_fixtures import LiveApi

from printops import db, events, tokens
from printops.backup import BackupError, snapshot_db
from printops.client import ApiClient, ApiError, load_config, parse_sse
from printops.settings import Settings
from printops.spoolman import (
    EXTRA_FIELDS,
    ExtraField,
    ExtraFieldConflict,
    HttpSpoolman,
    SpoolmanError,
    decode_extra,
    encode_extra,
    ensure_extra_fields,
    missing_extra_fields,
)

# ---------------------------------------------------------------------------------------- db


def _write_migrations(d: Path, files: dict[str, str]) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    for name, sql in files.items():
        (d / name).write_text(sql, encoding="utf-8")
    return d


def test_open_db_sets_wal_foreign_keys_and_rows(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "sub" / "x.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS
    assert isinstance(conn.execute("SELECT 1 AS one").fetchone(), sqlite3.Row)


def test_migrate_applies_core_once_and_is_idempotent(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "p.db")
    applied = db.migrate(
        conn
    )  # every slice adds its own migration: core first, then in order
    assert applied[0] == 1 and applied == sorted(applied) == [
        m.version for m in db.discover_migrations()
    ]
    assert db.migrate(conn) == []
    row = conn.execute(
        "SELECT version, name FROM schema_migrations ORDER BY version"
    ).fetchone()
    assert (row["version"], row["name"]) == (1, "core")


def test_migrations_apply_in_version_order_with_triggers_intact(tmp_path: Path) -> None:
    mdir = _write_migrations(
        tmp_path / "m",
        {
            "0020_b.sql": "CREATE TABLE b (id INTEGER PRIMARY KEY, a_id INTEGER REFERENCES a(id));",
            "0010_a.sql": "-- a; with a semicolon in a comment\nCREATE TABLE a (id INTEGER PRIMARY KEY, n INT);\n"
            "CREATE TRIGGER a_guard BEFORE UPDATE ON a BEGIN SELECT RAISE(ABORT, 'no; updates'); END;",
        },
    )
    conn = db.open_db(tmp_path / "p.db")
    assert db.migrate(conn, mdir) == [10, 20]
    conn.execute("INSERT INTO a (n) VALUES (1)")
    with pytest.raises(sqlite3.IntegrityError, match="no; updates"):
        conn.execute("UPDATE a SET n = 2")


def test_migration_failure_rolls_back_and_records_nothing(tmp_path: Path) -> None:
    mdir = _write_migrations(
        tmp_path / "m", {"0001_x.sql": "CREATE TABLE ok1 (id INT);\nCREATE TABLE ;"}
    )
    conn = db.open_db(tmp_path / "p.db")
    with pytest.raises(db.MigrationError, match="0001_x.sql"):
        db.migrate(conn, mdir)
    assert (
        conn.execute("SELECT name FROM sqlite_master WHERE name = 'ok1'").fetchone()
        is None
    )
    assert db.applied_versions(conn) == {}


def test_edited_applied_migration_is_refused(tmp_path: Path) -> None:
    mdir = _write_migrations(tmp_path / "m", {"0001_x.sql": "CREATE TABLE t (id INT);"})
    conn = db.open_db(tmp_path / "p.db")
    db.migrate(conn, mdir)
    (mdir / "0001_x.sql").write_text(
        "CREATE TABLE t (id INT, extra TEXT);", encoding="utf-8"
    )
    with pytest.raises(db.MigrationError, match="changed after it was applied"):
        db.migrate(conn, mdir)


@pytest.mark.parametrize(
    "files, match",
    [
        ({"1_bad.sql": "SELECT 1;"}, "does not match"),
        (
            {"0001_a.sql": "SELECT 1;", "0001_b.sql": "SELECT 1;"},
            "duplicate migration version",
        ),
    ],
)
def test_bad_migration_files_are_rejected(
    tmp_path: Path, files: dict[str, str], match: str
) -> None:
    with pytest.raises(db.MigrationError, match=match):
        db.discover_migrations(_write_migrations(tmp_path / "m", files))


def test_transaction_rolls_back_on_error_and_is_not_reentrant(
    printops_db: sqlite3.Connection,
) -> None:
    with pytest.raises(RuntimeError, match="boom"), db.transaction(printops_db):
        events.emit(printops_db, "test.happened", "t:1")
        raise RuntimeError("boom")
    assert events.latest_id(printops_db) == 0
    with (
        db.transaction(printops_db),
        pytest.raises(RuntimeError, match="re-entrant"),
        db.transaction(printops_db),
    ):
        pass


def test_shipped_migrations_use_reserved_names() -> None:
    for mig in db.discover_migrations():
        assert mig.path.name == f"{mig.version:04d}_{mig.name}.sql"


# ------------------------------------------------------------------------------------ events


def test_emit_appends_and_reads_back_in_order(printops_db: sqlite3.Connection) -> None:
    a = events.emit(
        printops_db, "spool.measured", "spool:12", {"gross_g": 812.5}, "cli"
    )
    b = events.emit(printops_db, "slot.mismatch", "printer:u1/T3", None)
    assert (a.id, b.id) == (1, 2) and b.actor == "system" and b.payload == {}
    assert a.at.endswith("Z") and "T" in a.at
    assert [e.kind for e in events.since(printops_db, 0)] == [
        "spool.measured",
        "slot.mismatch",
    ]
    assert [e.id for e in events.since(printops_db, 1)] == [2]
    assert [e.id for e in events.last(printops_db, 1)] == [2]
    assert events.since(printops_db, 0)[0].payload == {"gross_g": 812.5}


def test_events_are_append_only(printops_db: sqlite3.Connection) -> None:
    events.emit(printops_db, "drying.due", "spool:3")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        printops_db.execute("UPDATE events SET kind = 'x.y'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        printops_db.execute("DELETE FROM events")


@pytest.mark.parametrize(
    "kind",
    [
        "measured",
        "Spool.measured",
        "spool.",
        "spool measured",
        ".x",
        "spool.measured\n",
    ],
)
def test_emit_rejects_malformed_kinds(
    printops_db: sqlite3.Connection, kind: str
) -> None:
    with pytest.raises(ValueError, match="dotted"):
        events.emit(printops_db, kind, "spool:1")


def test_payload_must_be_a_json_object_at_the_db_level(
    printops_db: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        printops_db.execute(
            "INSERT INTO events (at, kind, subject, payload, actor) VALUES ('t','a.b','s','[1]','x')"
        )


def test_bus_wakes_listener_on_emit_and_at_commit_only(
    printops_db: sqlite3.Connection,
) -> None:
    async def scenario() -> list[bool]:
        listener = events.BUS.listener()
        try:
            woke = [await listener.wait(0.01)]
            events.emit(printops_db, "spool.moved", "spool:1")
            woke.append(await listener.wait(1.0))
            printops_db.execute("BEGIN IMMEDIATE")
            events.emit(printops_db, "spool.moved", "spool:2")
            woke.append(await listener.wait(0.05))  # not yet committed: no wake-up
            printops_db.execute("COMMIT")
            with db.transaction(printops_db):
                events.emit(printops_db, "spool.moved", "spool:3")
            woke.append(await listener.wait(1.0))
            return woke
        finally:
            listener.close()

    assert asyncio.run(scenario()) == [False, True, False, True]
    assert events.BUS.listener_count() == 0


# ------------------------------------------------------------------------------------ tokens


def test_token_create_stores_only_sha256_with_0600(tmp_path: Path) -> None:
    path = tmp_path / "t" / "tokens.json"
    plain, rec = tokens.create(path, "bot", ("read", "write"))
    assert plain.startswith("po_") and len(plain) > 40
    raw = path.read_text(encoding="utf-8")
    assert plain not in raw and rec.sha256 in raw
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(raw)["tokens"][0]["scopes"] == ["read", "write"]


def test_token_store_verifies_and_sees_revocation_without_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tokens.json"
    store = tokens.TokenStore(path)
    assert store.verify("po_nothing") is None and store.count() == 0
    plain, _ = tokens.create(path, "ha", ("read",))
    rec = store.verify(plain)
    assert rec is not None and rec.name == "ha" and rec.scopes == ("read",)
    assert tokens.revoke(path, "ha") is True
    assert store.verify(plain) is None
    assert tokens.revoke(path, "ha") is False


@pytest.mark.parametrize(
    "name, scopes, match",
    [
        ("Bad Name", ("read",), "must match"),
        ("ok", ("admin",), "unknown scope"),
    ],
)
def test_token_create_validates(
    tmp_path: Path, name: str, scopes: tuple[str, ...], match: str
) -> None:
    with pytest.raises(tokens.TokenError, match=match):
        tokens.create(tmp_path / "t.json", name, scopes)


def test_token_names_are_unique_and_scopes_parse(tmp_path: Path) -> None:
    tokens.create(tmp_path / "t.json", "bot", ("read",))
    with pytest.raises(tokens.TokenError, match="already exists"):
        tokens.create(tmp_path / "t.json", "bot", ("read",))
    assert tokens.parse_scopes("read, write,read") == ("read", "write")
    with pytest.raises(tokens.TokenError, match="accepted"):
        tokens.parse_scopes("read,root")


def test_corrupt_tokens_file_is_a_clear_error(tmp_path: Path) -> None:
    (tmp_path / "t.json").write_text("[]", encoding="utf-8")
    with pytest.raises(tokens.TokenError, match="not valid"):
        tokens.load(tmp_path / "t.json")


# ---------------------------------------------------------------------------------- spoolman


def test_ensure_extra_fields_registers_every_contract_field_idempotently() -> None:
    fake = FakeSpoolman()
    first = ensure_extra_fields(fake)
    assert len(first.created) == len(EXTRA_FIELDS) and not first.updated
    assert missing_extra_fields(fake) == []
    second = ensure_extra_fields(fake)
    assert (
        not second.created
        and not second.updated
        and len(second.unchanged) == len(EXTRA_FIELDS)
    )
    spool_keys = {f["key"] for f in fake.list_fields("spool")}
    # the contract's spool fields; slices may append more (contract: append-only, via PR)
    assert spool_keys >= {
        "printops_slot",
        "opened_at",
        "last_dried_at",
        "dry_state",
        "remaining_confidence_g",
        "tag_uids",
        "printops_location_id",
    }
    filament = {f["key"]: f for f in fake.list_fields("filament")}
    assert filament["hygroscopic_class"]["choices"] == [
        "low",
        "medium",
        "high",
        "extreme",
    ]
    assert filament["abrasive"]["field_type"] == "boolean"
    vendor = {f["key"]: f for f in fake.list_fields("vendor")}
    assert (
        vendor["spool_tare_g"]["unit"] == "g"
        and vendor["spool_kind"]["field_type"] == "choice"
    )


def test_ensure_extra_fields_keeps_user_choices_and_repairs_drift() -> None:
    fake = FakeSpoolman()
    fake.set_field(
        "spool",
        "dry_state",
        {
            "name": "Dry",
            "field_type": "choice",
            "choices": ["ok", "soaked"],
            "multi_choice": False,
        },
    )
    report = ensure_extra_fields(fake)
    assert "spool.dry_state" in report.updated
    dry = next(f for f in fake.list_fields("spool") if f["key"] == "dry_state")
    assert (
        dry["choices"] == ["ok", "soaked", "fresh", "due", "wet"]
        and dry["name"] == "Dry state"
    )


def test_ensure_extra_fields_refuses_a_type_conflict() -> None:
    fake = FakeSpoolman()
    fake.set_field("vendor", "spool_tare_g", {"name": "tare", "field_type": "text"})
    with pytest.raises(
        ExtraFieldConflict, match=r"vendor\.spool_tare_g exists as 'text'"
    ):
        ensure_extra_fields(fake)
    assert "vendor.spool_tare_g" in missing_extra_fields(fake)


def test_multi_choice_drift_is_a_conflict_not_healthy() -> None:
    fake = FakeSpoolman()
    fake.set_field(
        "spool",
        "dry_state",
        {
            "name": "Dry state",
            "field_type": "choice",
            "choices": ["fresh", "ok", "due", "wet"],
            "multi_choice": True,
        },
    )
    with pytest.raises(ExtraFieldConflict, match="multi_choice"):
        ensure_extra_fields(fake)
    assert "spool.dry_state" in missing_extra_fields(fake)


def test_field_missing_a_required_choice_is_reported_until_repaired() -> None:
    fake = FakeSpoolman()
    ensure_extra_fields(fake)
    fake.fields["spool"] = [f for f in fake.fields["spool"] if f["key"] != "dry_state"]
    fake.set_field(
        "spool",
        "dry_state",
        {
            "name": "Dry state",
            "field_type": "choice",
            "choices": ["ok"],
            "multi_choice": False,
        },
    )
    assert missing_extra_fields(fake) == ["spool.dry_state"]
    assert ensure_extra_fields(fake).updated == ["spool.dry_state"]
    assert missing_extra_fields(fake) == []


def test_extra_values_round_trip_and_are_validated_like_spoolman() -> None:
    fake = FakeSpoolman()
    ensure_extra_fields(fake)
    vendor = fake.create(
        "vendor",
        {
            "name": "Elegoo",
            "empty_spool_weight": 160,
            "extra": {"spool_kind": encode_extra("plastic")},
        },
    )
    fil = fake.create(
        "filament",
        {
            "vendor_id": vendor["id"],
            "material": "PLA",
            "density": 1.24,
            "diameter": 1.75,
            "weight": 1000,
        },
    )
    spool = fake.create(
        "spool",
        {
            "filament_id": fil["id"],
            "location": "WH:A/S2/B3/04",
            "extra": {
                "dry_state": encode_extra("ok"),
                "tag_uids": encode_extra('["04AB"]'),
            },
        },
    )
    assert decode_extra(spool["extra"]["dry_state"]) == "ok"
    assert spool["spool_weight"] == 160 and spool["remaining_weight"] == 1000
    with pytest.raises(SpoolmanError, match="valid choice") as e:
        fake.update(
            "spool", spool["id"], {"extra": {"dry_state": encode_extra("soggy")}}
        )
    assert e.value.status == 400
    with pytest.raises(SpoolmanError, match="Unknown extra field"):
        fake.update("spool", spool["id"], {"extra": {"nope": "1"}})
    cleared = fake.update("spool", spool["id"], {"extra": {"dry_state": None}})
    assert "dry_state" not in cleared["extra"] and "tag_uids" in cleared["extra"]


def _pla_spool(fake: FakeSpoolman, **spool: Any) -> dict[str, Any]:
    vendor = fake.create("vendor", {"name": "Elegoo", "empty_spool_weight": 160})
    fil = fake.create(
        "filament",
        {
            "vendor_id": vendor["id"],
            "material": "PLA",
            "density": 1.24,
            "diameter": 1.75,
            "weight": 1000,
        },
    )
    return fake.create("spool", {"filament_id": fil["id"], **spool})


def test_fake_use_and_measure_follow_spoolman_semantics() -> None:
    fake = FakeSpoolman()
    s = _pla_spool(fake)
    used = fake.use(s["id"], use_weight=100)
    assert (
        used["used_weight"] == 100
        and used["remaining_weight"] == 900
        and "first_used" in used
    )
    by_len = fake.use(s["id"], use_length=1000)  # 1 m of 1.75 mm PLA ≈ 2.98 g
    assert by_len["used_weight"] == pytest.approx(100 + 2.982, abs=0.01)
    with pytest.raises(SpoolmanError, match="Only specify either"):
        fake.use(s["id"], use_weight=1, use_length=1)
    measured = fake.measure(s["id"], 160 + 500)  # gross = tare + 500 g net
    assert measured["remaining_weight"] == pytest.approx(500)
    below_tare = fake.measure(s["id"], 100)
    assert below_tare["remaining_weight"] == pytest.approx(0)
    heavier = fake.measure(s["id"], 160 + 1200)
    assert heavier["initial_weight"] == 1200 and heavier["used_weight"] == 0
    assert fake.use(s["id"], use_weight=-5000)["used_weight"] == 0


def test_fake_lists_hide_archived_and_filter_location() -> None:
    fake = FakeSpoolman()
    a = _pla_spool(fake, location="shelf")
    fake.create("spool", {"filament_id": a["filament"]["id"], "archived": True})
    assert [s["id"] for s in fake.find("spool")] == [a["id"]]
    assert len(fake.find("spool", {"allow_archived": "true"})) == 2
    assert fake.find("spool", {"location": "nowhere"}) == []
    with pytest.raises(SpoolmanError) as e:
        fake.create("spool", {"filament_id": a["filament"]["id"], "location": "x" * 65})
    assert e.value.status == 422


def test_http_spoolman_speaks_the_wire_format_of_the_fake() -> None:
    fake = FakeSpoolman()
    with fake.serve() as url:
        sp = HttpSpoolman(url)
        assert sp.info()["version"] == "0.27.0"
        report = ensure_extra_fields(sp)
        assert len(report.created) == len(EXTRA_FIELDS)
        assert missing_extra_fields(sp) == []
        vendor = sp.create(
            "vendor", {"name": "Sunlu", "extra": {"spool_tare_g": encode_extra(135.0)}}
        )
        fil = sp.create(
            "filament",
            {
                "vendor_id": vendor["id"],
                "material": "PETG",
                "density": 1.27,
                "diameter": 1.75,
                "weight": 1000,
            },
        )
        spool = sp.create("spool", {"filament_id": fil["id"], "remaining_weight": 700})
        assert sp.get("spool", spool["id"])["remaining_weight"] == 700
        assert sp.find("spool", {"filament.material": "petg"})[0]["id"] == spool["id"]
        assert sp.use(spool["id"], use_weight=50)["remaining_weight"] == 650
        assert sp.measure(spool["id"], 400)["remaining_weight"] == pytest.approx(400)
        assert (
            sp.update("spool", spool["id"], {"location": "dryer"})["location"]
            == "dryer"
        )
        with pytest.raises(SpoolmanError, match="No spool with ID 999") as e:
            sp.get("spool", 999)
        assert e.value.status == 404
        sp.delete("spool", spool["id"])
        assert sp.find("spool") == []
    with pytest.raises(SpoolmanError, match="unreachable") as down:
        HttpSpoolman(url, timeout=1).info()
    assert down.value.status == 0


def test_extra_field_definitions_follow_the_contract_rules() -> None:
    keys = [(f.entity, f.key) for f in EXTRA_FIELDS]
    assert len(keys) == len(set(keys)) >= 19
    for f in EXTRA_FIELDS:
        assert (f.field_type == "choice") == (f.choices is not None)
    params = ExtraField("spool", "k", "K", "choice", choices=("a",)).params(7)
    assert params == {
        "name": "K",
        "order": 7,
        "field_type": "choice",
        "choices": ["a"],
        "multi_choice": False,
    }


def test_concurrent_token_create_and_revoke_never_resurrect(tmp_path: Path) -> None:
    import threading

    path = tmp_path / "tokens.json"
    tokens.create(path, "old", ("read",))
    barrier = threading.Barrier(9)

    def mint(i: int) -> None:
        barrier.wait()
        tokens.create(path, f"t{i}", ("read",))

    def drop() -> None:
        barrier.wait()
        tokens.revoke(path, "old")

    threads = [threading.Thread(target=mint, args=(i,)) for i in range(8)] + [
        threading.Thread(target=drop)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    names = {r.name for r in tokens.load(path)}
    assert names == {f"t{i}" for i in range(8)}


# ----------------------------------------------------------------------------------- 3d-api


def test_health_is_public_and_reports_db_spoolman_and_fields(
    printops_client: Any,
) -> None:
    r = printops_client.get("/v1/health")
    body = r.json()
    assert r.status_code == 200 and body["status"] == "ok" and body["version"] == "test"
    assert body["db"]["ok"] and body["db"]["migrations"] == [
        m.version for m in db.discover_migrations()
    ]
    assert body["spoolman"]["version"] == "0.27.0"
    assert body["spoolman"]["extra_fields"]["status"] == "ok"
    assert body["spoolman"]["extra_fields"]["missing"] == []


def test_liveness_never_calls_spoolman(
    printops_client: Any, fake_spoolman: FakeSpoolman
) -> None:
    fake_spoolman.down = True
    calls = len(fake_spoolman.calls)
    r = printops_client.get("/v1/live")
    assert (
        r.status_code == 200
        and r.json() == {"ok": True}
        and len(fake_spoolman.calls) == calls
    )


@pytest.mark.parametrize("header", ["9" * 5000, "\u00b2"])
def test_malformed_last_event_id_is_ignored_not_a_500(
    live_api: LiveApi, header: str
) -> None:
    import http.client

    conn = http.client.HTTPConnection(live_api.url.removeprefix("http://"), timeout=10)
    conn.putrequest("GET", "/v1/events")
    conn.putheader("Authorization", f"Bearer {live_api.tokens['read']}")
    conn.putheader("Last-Event-ID", header.encode("latin-1"))
    conn.endheaders()
    resp = conn.getresponse()
    try:
        assert resp.status == 200 and resp.readline().decode().strip() == "retry: 3000"
    finally:
        conn.close()


def test_failed_stream_setup_does_not_leak_listeners(
    printops_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from printops.api import routes_core
    from printops.api.state import ApiState
    from printops.tokens import TokenStore

    def boom(_path: Any) -> sqlite3.Connection:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(routes_core, "open_db", boom)
    state = ApiState(
        settings=printops_settings,
        spoolman=None,
        tokens=TokenStore(printops_settings.tokens_file),
    )

    async def attempt() -> None:
        with pytest.raises(sqlite3.OperationalError):
            await routes_core._follow(state, 0, lambda: True).__anext__()

    for _ in range(3):
        asyncio.run(attempt())
    assert events.BUS.listener_count() == 0


def test_health_is_degraded_when_spoolman_is_down(
    printops_client: Any, fake_spoolman: FakeSpoolman
) -> None:
    fake_spoolman.down = True
    body = printops_client.get("/v1/health").json()
    assert body["status"] == "degraded" and "unreachable" in body["spoolman"]["error"]


def test_health_is_degraded_without_spoolman_url(printops_settings: Settings) -> None:
    from fastapi.testclient import TestClient

    from printops.api.app import create_app

    with TestClient(create_app(printops_settings)) as client:
        body = client.get("/v1/health").json()
    assert (
        body["status"] == "degraded"
        and body["spoolman"]["error"] == "SPOOLMAN_URL is not set"
    )


def test_extra_field_conflict_surfaces_in_health(printops_settings: Settings) -> None:
    from fastapi.testclient import TestClient
    from printops_fixtures import wait_fields

    from printops.api.app import create_app

    fake = FakeSpoolman()
    fake.set_field("spool", "opened_at", {"name": "opened", "field_type": "text"})
    app = create_app(printops_settings, spoolman=fake)
    with TestClient(app) as client:
        wait_fields(app)
        body = client.get("/v1/health").json()
    fields = body["spoolman"]["extra_fields"]
    assert body["status"] == "degraded" and fields["status"] == "error"
    assert (
        "spool.opened_at" in fields["missing"]
        and "cannot change a field type" in fields["error"]
    )


def test_extra_fields_get_registered_once_spoolman_comes_up(
    printops_settings: Settings,
) -> None:
    """3d-api may start before Spoolman answers: the startup task keeps retrying."""
    from fastapi.testclient import TestClient
    from printops_fixtures import wait_fields

    from printops.api.app import create_app

    fake = FakeSpoolman()
    fake.down = True
    app = create_app(printops_settings, spoolman=fake)
    with TestClient(app) as client:
        pending = client.get("/v1/health").json()
        fake.down = False
        wait_fields(app, timeout=10)
        ok = client.get("/v1/health").json()
    assert (
        pending["status"] == "degraded"
        and pending["spoolman"]["extra_fields"]["status"] == "pending"
    )
    assert ok["status"] == "ok" and missing_extra_fields(fake) == []


def test_routes_need_a_token_and_the_right_scope(
    printops_client: Any, printops_tokens: dict[str, str]
) -> None:
    accept = {"Accept": "application/json"}
    missing = printops_client.get("/v1/events", headers=accept)
    assert (
        missing.status_code == 401 and missing.headers["www-authenticate"] == "Bearer"
    )
    bogus = printops_client.get(
        "/v1/events", headers={**accept, "Authorization": "Bearer po_forged"}
    )
    assert bogus.status_code == 401
    write_only = printops_client.get(
        "/v1/events",
        headers={**accept, "Authorization": f"Bearer {printops_tokens['write']}"},
    )
    assert (
        write_only.status_code == 403
        and "lacks scope 'read'" in write_only.json()["detail"]
    )
    ok = printops_client.get(
        "/v1/events",
        headers={**accept, "Authorization": f"Bearer {printops_tokens['read']}"},
    )
    assert ok.status_code == 200 and ok.json() == {"events": [], "cursor": 0}


def test_events_json_pages_with_since_and_last(
    api_client: ApiClient, printops_db: sqlite3.Connection
) -> None:
    for i in range(5):
        events.emit(printops_db, "spool.moved", f"spool:{i}", {"i": i}, "test")
    assert [e["id"] for e in api_client.events(since=0, limit=2)["events"]] == [1, 2]
    page = api_client.events(since=3)
    assert [e["id"] for e in page["events"]] == [4, 5] and page["cursor"] == 5
    assert api_client.events(since=5) == {"events": [], "cursor": 5}
    last = api_client.events(last=2)
    assert [e["subject"] for e in last["events"]] == ["spool:3", "spool:4"] and last[
        "cursor"
    ] == 5
    with pytest.raises(ApiError) as e:
        api_client.events(since=1, last=1)
    assert e.value.status == 422


def test_routes_modules_are_auto_included() -> None:
    from printops.api.app import route_modules

    assert "printops.api.routes_core" in route_modules()


def test_websocket_requires_auth_message_then_streams(
    printops_client: Any,
    printops_tokens: dict[str, str],
    printops_db: sqlite3.Connection,
) -> None:
    from starlette.websockets import WebSocketDisconnect

    with printops_client.websocket_connect("/v1/ws") as ws:
        ws.send_json({"type": "auth", "token": "po_forged"})
        assert ws.receive_json()["type"] == "auth_invalid"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 4401
    events.emit(printops_db, "slot.loaded", "printer:u1/T1", {"spool": 1}, "test")
    with printops_client.websocket_connect("/v1/ws") as ws:
        ws.send_json({"type": "auth", "token": printops_tokens["read"], "since": 0})
        assert ws.receive_json() == {
            "type": "auth_ok",
            "name": "test-read",
            "cursor": 0,
        }
        first = ws.receive_json()
        assert first["type"] == "event" and first["event"]["kind"] == "slot.loaded"
        events.emit(printops_db, "slot.unloaded", "printer:u1/T1", None, "test")
        assert ws.receive_json()["event"]["kind"] == "slot.unloaded"


def test_sse_streams_backlog_then_live_events(live_api: LiveApi) -> None:
    conn = db.open_db(live_api.settings.db_path)
    events.emit(conn, "spool.measured", "spool:1", {"gross_g": 900}, "test")
    client = ApiClient(live_api.url, live_api.tokens["read"])
    stream = client.follow_events(since=0)
    first = next(stream)
    events.emit(conn, "drying.due", "spool:1", None, "test")
    second = next(stream)
    stream.close()
    conn.close()
    assert (first["kind"], first["payload"]) == ("spool.measured", {"gross_g": 900})
    assert (second["id"], second["kind"]) == (2, "drying.due")


def test_sse_stream_ends_when_its_token_is_revoked(live_api: LiveApi) -> None:
    conn = db.open_db(live_api.settings.db_path)
    plain, _ = tokens.create(live_api.settings.tokens_file, "short-lived", ("read",))
    stream = ApiClient(live_api.url, plain).follow_events(since=0)
    events.emit(conn, "spool.moved", "spool:1", None, "test")
    assert next(stream)["subject"] == "spool:1"
    tokens.revoke(live_api.settings.tokens_file, "short-lived")
    events.emit(conn, "spool.moved", "spool:2", None, "test")
    with pytest.raises(ApiError, match="revoked") as e:
        next(stream)
    conn.close()
    assert e.value.status == 401


def test_websocket_closes_when_its_token_is_revoked(
    printops_client: Any, printops_settings: Settings, printops_db: sqlite3.Connection
) -> None:
    from starlette.websockets import WebSocketDisconnect

    plain, _ = tokens.create(printops_settings.tokens_file, "ws-short", ("read",))
    with printops_client.websocket_connect("/v1/ws") as ws:
        ws.send_json({"type": "auth", "token": plain})
        assert ws.receive_json()["type"] == "auth_ok"
        tokens.revoke(printops_settings.tokens_file, "ws-short")
        events.emit(printops_db, "spool.moved", "spool:9", None, "test")
        assert ws.receive_json() == {"type": "auth_revoked"}
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 4401


def test_sse_without_token_is_401_over_http(live_api: LiveApi) -> None:
    with pytest.raises(ApiError) as e:
        next(ApiClient(live_api.url).follow_events())
    assert e.value.status == 401


def test_client_refuses_redirects_so_the_token_never_follows_them() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802 — http.server API
            seen.append(self.headers.get("Authorization"))
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.2:9/elsewhere")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = ApiClient(f"http://127.0.0.1:{server.server_address[1]}", "po_SECRET")
        with pytest.raises(ApiError) as e:
            client.events(last=1)
        with pytest.raises(ApiError):
            next(client.follow_events())
    finally:
        server.shutdown()
        server.server_close()
    assert e.value.status == 302 and seen == ["Bearer po_SECRET", "Bearer po_SECRET"]


def test_client_maps_html_health_errors_and_stream_eof_to_api_error() -> None:
    from collections.abc import Iterator, Mapping
    from contextlib import contextmanager

    from printops.client import Response

    class Fake:
        def request(
            self,
            method: str,
            url: str,
            headers: Mapping[str, str],
            body: bytes | None,
            timeout: float,
        ) -> Response:
            return Response(502, b"<html>Bad Gateway</html>")

        @contextmanager
        def stream_lines(
            self, url: str, headers: Mapping[str, str], timeout: float
        ) -> Iterator[Iterator[str]]:
            yield iter(["id: 1", 'data: {"id": 1}', ""])

    client = ApiClient("http://x", "po_t", Fake())
    with pytest.raises(ApiError) as e:
        client.health()
    assert e.value.status == 502
    stream = client.follow_events()
    assert next(stream) == {"id": 1}
    with pytest.raises(ApiError, match="ended unexpectedly"):
        next(stream)


def test_parse_sse_frames_skip_comments_and_join_data() -> None:
    lines = iter(
        [
            "retry: 3000",
            "",
            ": ping",
            "",
            "id: 7",
            "event: a.b",
            'data: {"x":',
            "data: 1}",
            "",
        ]
    )
    frames = list(parse_sse(lines))
    assert frames[-1] == {"id": "7", "event": "a.b", "data": '{"x":\n1}'}


def test_client_config_from_env_then_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "3d-cli").mkdir()
    (tmp_path / "3d-cli" / "printops.yaml").write_text(
        "api: http://yaml:1\ntoken: po_yaml\n", encoding="utf-8"
    )
    assert load_config({}) == ("http://yaml:1", "po_yaml")
    assert load_config({"PRINTOPS_API": "http://env:2"}) == ("http://env:2", "po_yaml")


# ----------------------------------------------------------------------------------- backup


def test_backup_snapshot_is_consistent_and_self_contained(
    printops_db: sqlite3.Connection, tmp_path: Path, printops_settings: Settings
) -> None:
    events.emit(printops_db, "spool.moved", "spool:1")
    snap = snapshot_db(printops_settings.db_path, tmp_path / "bk" / "printops.db")
    copy = sqlite3.connect(snap.dest)
    assert copy.execute("SELECT kind FROM events").fetchall() == [("spool.moved",)]
    assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    copy.close()
    assert not (tmp_path / "bk" / "printops.db-wal").exists()
    with pytest.raises(BackupError, match="does not exist"):
        snapshot_db(tmp_path / "missing.db", tmp_path / "bk" / "x.db")
    with pytest.raises(BackupError, match="is the source itself"):
        snapshot_db(
            printops_settings.db_path,
            printops_settings.db_path.parent / "." / "printops.db",
        )
    events.emit(
        printops_db, "spool.moved", "spool:2"
    )  # the live DB is untouched and writable
    assert events.latest_id(printops_db) == 2


def test_event_cursors_beyond_sqlite_range_are_rejected(
    printops_client: Any, printops_tokens: dict[str, str]
) -> None:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {printops_tokens['read']}",
    }
    r = printops_client.get("/v1/events", params={"since": 2**63}, headers=headers)
    assert r.status_code == 422


# ----------------------------------------------------------------------------------- deploy


def test_deploy_plan_targets_a_separate_compose_project(tmp_path: Path) -> None:
    from printops import deploy

    (tmp_path / "lib").mkdir()
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "3d").write_text("", encoding="utf-8")
    cfg = deploy.DeployConfig(repo_root=tmp_path, version="abc123")
    steps = deploy.plan(cfg)
    titles = [s.title for s in steps]
    assert (
        titles[0].startswith("create data")
        and titles[-1] == "install daily backup timer"
    )
    mkdirs, rsync, compose, timer = (s.argv for s in steps)
    assert (
        mkdirs[:3] == ["ssh", "-o", "BatchMode=yes"]
        and "/home/ultra/printops/data/spoolman" in mkdirs[-1]
    )
    assert (
        "printf '*\\n' > /home/ultra/homeassistant/printops-backup/.gitignore"
        in mkdirs[-1]
    )
    assert rsync[-1] == "ultra@192.168.0.39:/home/ultra/printops/src/"
    assert (
        "bin/3d" in rsync and "lib" in rsync and "fleet" not in rsync
    )  # only existing paths
    assert (
        "docker compose -p printops -f services/3d-api/compose.yaml up -d --build"
        in compose[-1]
    )
    assert (
        "PRINTOPS_VERSION=abc123" in compose[-1]
        and "homeassistant/ha" not in compose[-1]
    )
    assert "systemctl --user enable --now printops-backup.timer" in timer[-1]
    dry = deploy.plan(cfg, dry_run=True)[1].argv
    assert "--dry-run" in dry and "--itemize-changes" in dry
    assert cfg.health_url == "http://192.168.0.39:8765"


def test_deploy_wait_healthy_returns_ok_and_times_out(live_api: LiveApi) -> None:
    from printops import deploy

    assert deploy.wait_healthy(live_api.url, log=lambda _m: None)["status"] == "ok"
    live_api.spoolman.down = True
    ticks = iter(range(100))
    logs: list[str] = []
    with pytest.raises(ApiError, match="not healthy after 2s"):
        deploy.wait_healthy(
            live_api.url,
            timeout_s=2,
            log=logs.append,
            sleep=lambda _s: None,
            clock=lambda: float(next(ticks)),
        )
    assert logs and "degraded" in logs[0]
