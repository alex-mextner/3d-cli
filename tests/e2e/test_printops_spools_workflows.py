"""E2E stories for `3d printops spool|tare` and `3d lint --printops` data rules through `bin/3d`,
against a real uvicorn 3d-api talking HTTP to FakeSpoolman (issues #160, #163)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fleet_fixtures import REPO_FLEET, k1se_moonraker_entry, u1_entry, write_fleet, write_policy_for_fleet
from printer_fakes import FACTORY_K, FakeMoonraker
from printops_fixtures import LiveApi

from printops import db, locations

from .workflow_helper import run_cli

FLEET = """\
u1:
  model: Snapmaker U1
  owner: own
  access: full
  adapter: moonraker
  host: 127.0.0.1
  port: 9
  heads:
    - {label: T1, index: 0, usable: true}
    - {label: T2, index: 1, usable: false, reason: T2 is broken}
    - {label: T3, index: 2, usable: true}
    - {label: T4, index: 3, usable: true}
"""


@pytest.fixture
def shop(tmp_path: Path, live_api: LiveApi, monkeypatch: pytest.MonkeyPatch) -> LiveApi:
    """The live 3d-api with a fleet (U1, T2 broken) and the default warehouse + one shelf."""
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(FLEET, encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(fleet))  # the API runs in this process
    monkeypatch.setenv("PRINTOPS_POLICY", str(write_policy_for_fleet(tmp_path / "policy.yaml", fleet)))
    conn = db.open_db(live_api.settings.db_path)
    try:
        locations.add(conn, "WH:default", "warehouse", actor="e2e")
        locations.add(conn, "WH:A", "zone", actor="e2e")
        locations.add(conn, "WH:A/S1", "shelf", actor="e2e")
    finally:
        conn.close()
    return live_api


def _cli(
    tmp_path: Path, live: LiveApi, *args: str, token: str | None = "rw"
) -> subprocess.CompletedProcess[str]:
    env = {"PRINTOPS_API": live.url, "THREED_FLEET": ""}
    if token:
        env["PRINTOPS_TOKEN"] = live.tokens[token]
    return run_cli(tmp_path, *args, env_extra=env)


def test_spool_story_receive_load_print_unload_weigh_empty(
    tmp_path: Path, shop: LiveApi
) -> None:
    """A spool arrives, goes on a shelf, into U1 T3, a print books its estimate, it comes back
    to the shelf and is weighed (the interval collapses), and finally runs out."""
    got = _cli(
        tmp_path,
        shop,
        "printops",
        "spool",
        "receive",
        "--vendor",
        "Elegoo",
        "--material",
        "PLA+",
        "--name",
        "RAPID PLA+ Green",
        "--color",
        "#4FE44D",
        "--net",
        "1000",
        "--price",
        "1499",
        "--location",
        "WH:default",
        "--json",
    )
    assert got.returncode == 0, got.stderr
    sid = str(json.loads(got.stdout)["id"])

    moved = _cli(tmp_path, shop, "printops", "spool", "move", sid, "WH:A/S1")
    broken = _cli(tmp_path, shop, "printops", "spool", "load", sid, "u1/T2")
    loaded = _cli(tmp_path, shop, "printops", "spool", "load", sid, "u1/T3")
    used = _cli(
        tmp_path,
        shop,
        "printops",
        "spool",
        "use",
        sid,
        "100",
        "--source",
        "gcode_estimate",
        "--job",
        "plate-3",
    )
    listed = _cli(tmp_path, shop, "printops", "spool", "ls", "--loaded")
    unloaded = _cli(
        tmp_path,
        shop,
        "printops",
        "spool",
        "unload",
        "u1/T3",
        "--to",
        "WH:A/S1",
        "--gross",
        "974",
    )
    shown = _cli(tmp_path, shop, "printops", "spool", "show", sid)
    emptied = _cli(
        tmp_path, shop, "printops", "spool", "empty", sid, "--note", "ran out"
    )

    assert moved.returncode == 0 and "WH:A/S1" in moved.stdout, moved.stderr
    assert (
        broken.returncode == 1
        and "T2 is broken" in broken.stderr
        and "HTTP 409" in broken.stderr
    )
    assert loaded.returncode == 0 and "in u1/T3" in loaded.stdout, loaded.stderr
    assert "printer head: failed" in loaded.stdout and "the printer head was NOT updated" in loaded.stderr
    assert used.returncode == 0 and "900 ± 22 g (878–922)" in used.stdout, used.stderr
    assert (
        listed.stdout.startswith(f"#{sid}")
        and "Elegoo PLA+ RAPID PLA+ Green #4FE44D  in u1/T3" in listed.stdout
    )
    assert unloaded.returncode == 0, unloaded.stderr
    assert (
        "WH:A/S1  820 ± 10 g (810–830)" in unloaded.stdout
        and "ledger was off by -80 g" in unloaded.stdout
    )
    assert "tare       154 ± 10 g (SpoolmanDB" in shown.stdout
    assert "weigh            = 820.0 g (gross 974 − tare 154)" in shown.stdout
    assert (
        "gcode_estimate   -100.0 g ±10" in shown.stdout
        and "job_id=plate-3" in shown.stdout
    )
    assert emptied.returncode == 0 and "[archived]" in emptied.stdout
    events = _cli(tmp_path, shop, "printops", "events", "--last", "50", "--json")
    kinds = [json.loads(line)["kind"] for line in events.stdout.splitlines()]
    kinds = [k for k in kinds if k.startswith(("spool.", "slot."))]
    assert kinds == [
        "spool.received",
        "spool.moved",
        "slot.loaded",
        "slot.printer_write_failed",  # the fleet's u1 is 127.0.0.1:9, nothing listens (#400)
        "spool.used",
        "spool.measured",  # unload --gross weighs first, then unloads
        "slot.unloaded",
        "spool.empty",
    ]


def test_load_shows_the_spool_on_the_u1_head_and_no_printer_write_leaves_it(
    tmp_path: Path, shop: LiveApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner loads a green spool into U1 T3: the printer head now says Elegoo PLA+ #4FE44D
    (read back), the factory pressure advance stays with a WARNING (no calibration yet). A
    second spool goes into T4 with --no-printer-write: the printer is not touched."""
    moon = FakeMoonraker()
    with moon.serve():
        fleet = write_fleet(tmp_path / "fleet-moon.yaml", u1_entry(moon))
        monkeypatch.setenv("THREED_FLEET", str(fleet))
        monkeypatch.setenv("PRINTOPS_POLICY", str(write_policy_for_fleet(tmp_path / "policy-moon.yaml", fleet)))
        ids = []
        for name, color in (("RAPID PLA+ Green", "#4FE44D"), ("RAPID PLA+ White", "#FFFFFF")):
            got = _cli(tmp_path, shop, "printops", "spool", "receive", "--vendor", "Elegoo", "--material",
                       "PLA+", "--name", name, "--color", color, "--net", "1000", "--location", "WH:default",
                       "--json")
            assert got.returncode == 0, got.stderr
            ids.append(str(json.loads(got.stdout)["id"]))
        loaded = _cli(tmp_path, shop, "printops", "spool", "load", ids[0], "u1/T3")
        quiet = _cli(tmp_path, shop, "printops", "spool", "load", ids[1], "u1/T4", "--no-printer-write")

    assert loaded.returncode == 0, loaded.stderr
    assert (
        f"printer head: #4FE44D Elegoo PLA+ Basic (read back), pressure advance K={FACTORY_K:g} (factory)"
        in loaded.stdout
    )
    assert f"WARNING: u1/T3 keeps the firmware's factory pressure advance K={FACTORY_K}" in loaded.stderr
    cfg = moon.status["print_task_config"]
    assert (cfg["filament_color_rgba"][2], cfg["filament_vendor"][2], cfg["filament_type"][2]) == (
        "4FE44DFF", "Elegoo", "PLA+")
    assert quiet.returncode == 0 and "in u1/T4" in quiet.stdout, quiet.stderr
    assert "printer head: skipped" in quiet.stdout and "WARNING" not in quiet.stderr
    assert len(moon.scripts) == 1 and moon.scripts[0].startswith("SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=2 ")


def test_load_and_unload_move_the_k1_se_moonraker_active_spool(
    tmp_path: Path, shop: LiveApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner puts the black spool on the rooted K1 SE while Fluidd still has the white #14
    selected: `spool load` says Moonraker's active spool is now the black one (read back), so the
    printer stops booking to white. `unload --no-printer-write` leaves it, a plain `unload` clears it."""
    moon = FakeMoonraker(spoolman=True, active_spool=14)
    moon.status.pop("print_task_config")  # the K1 has no per-head filament
    with moon.serve():
        fleet = write_fleet(tmp_path / "fleet-k1.yaml", k1se_moonraker_entry(moon))
        monkeypatch.setenv("THREED_FLEET", str(fleet))
        monkeypatch.setenv("PRINTOPS_POLICY", str(write_policy_for_fleet(tmp_path / "policy-k1.yaml", fleet)))
        got = _cli(tmp_path, shop, "printops", "spool", "receive", "--vendor", "Elegoo", "--material", "PLA+",
                   "--name", "RAPID PLA+ Black", "--color", "#000000", "--net", "1000", "--location",
                   "WH:default", "--json")
        assert got.returncode == 0, got.stderr
        sid = str(json.loads(got.stdout)["id"])
        loaded = _cli(tmp_path, shop, "printops", "spool", "load", sid, "k1se/T1")
        after_load = moon.active_spool
        kept = _cli(tmp_path, shop, "printops", "spool", "unload", "k1se/T1", "--no-printer-write")
        after_kept = moon.active_spool
        reloaded = _cli(tmp_path, shop, "printops", "spool", "load", sid, "k1se/T1")
        cleared = _cli(tmp_path, shop, "printops", "spool", "unload", "k1se/T1")

    assert loaded.returncode == 0, loaded.stderr
    assert f"printer: Moonraker active spool #{sid} (read back)" in loaded.stdout
    assert "printer head" not in loaded.stdout and "WARNING" not in loaded.stderr
    assert after_load == int(sid) and moon.scripts == []
    assert kept.returncode == 0 and "Moonraker" not in kept.stdout and after_kept == int(sid), kept.stderr
    assert reloaded.returncode == 0, reloaded.stderr
    assert cleared.returncode == 0, cleared.stderr
    assert f"printer: Moonraker active spool cleared (was #{sid}) (read back)" in cleared.stdout
    assert moon.active_spool is None


def test_a_fault_opened_from_the_cli_stops_loads_into_that_head_until_closed(
    tmp_path: Path, shop: LiveApi
) -> None:
    """The owner hears T3 clicking, opens a fault on it; loading a spool there is refused with the
    fault named, other heads still load, and once the fault is closed T3 takes the spool (#233)."""
    got = _cli(tmp_path, shop, "printops", "spool", "receive", "--vendor", "Elegoo", "--material", "PLA+",
               "--name", "RAPID PLA+ Green", "--color", "#4FE44D", "--net", "1000", "--location",
               "WH:default", "--json")
    assert got.returncode == 0, got.stderr
    sid = str(json.loads(got.stdout)["id"])

    opened = _cli(tmp_path, shop, "printops", "maint", "fault", "open", "u1/t3", "--symptom", "extruder clicks",
                  "--json")
    assert opened.returncode == 0, opened.stderr
    fid = str(json.loads(opened.stdout)["id"])

    refused = _cli(tmp_path, shop, "printops", "spool", "load", sid, "u1/t3")
    assert refused.returncode != 0
    assert "u1/T3" in refused.stderr and f"#{fid} extruder clicks" in refused.stderr
    elsewhere = _cli(tmp_path, shop, "printops", "spool", "load", sid, "u1/T1")
    assert elsewhere.returncode == 0, elsewhere.stderr

    closed = _cli(tmp_path, shop, "printops", "maint", "fault", "close", fid, "--root-cause", "gear cleaned")
    assert closed.returncode == 0, closed.stderr
    moved = _cli(tmp_path, shop, "printops", "spool", "load", sid, "u1/t3", "--json")
    assert moved.returncode == 0, moved.stderr
    assert json.loads(moved.stdout)["slot"] == "u1/T3"


def test_import_owned_dry_run_apply_rerun_and_lint(
    tmp_path: Path, shop: LiveApi
) -> None:
    """The user imports the spools babushkin-dom lists as owned: the dry run shows the plan and
    writes nothing, --apply creates them, a re-run changes nothing; lint then reports the
    never-weighed spools and the one without a tare."""
    plates = tmp_path / "babushkin-dom" / "gen" / "plates.json"
    plates.parent.mkdir(parents=True)
    plates.write_text(
        json.dumps(
            {
                "owned": {"белый": "#FFFFFF", "зелёный Elegoo": "#4FE44D"},
                "bought": {
                    "зелёный Elegoo": "Elegoo RAPID PLA+ Green (Game Centar, 1499 дин)"
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    dry = _cli(
        tmp_path,
        shop,
        "printops",
        "spool",
        "import-owned",
        str(plates),
        "--location",
        "WH:default",
    )
    writes_after_dry = [
        w for w in shop.spoolman.writes() if not w[1].startswith("/field")
    ]
    applied = _cli(
        tmp_path,
        shop,
        "printops",
        "spool",
        "import-owned",
        str(plates),
        "--location",
        "WH:default",
        "--apply",
    )
    rerun = _cli(
        tmp_path,
        shop,
        "printops",
        "spool",
        "import-owned",
        str(plates),
        "--location",
        "WH:default",
        "--apply",
    )
    lint = _cli(
        tmp_path,
        shop,
        "lint",
        "--printops",
        "--fleet",
        str(tmp_path / "fleet.yaml"),
        "--json",
    )
    tare_ls = _cli(tmp_path, shop, "printops", "tare", "ls", "elegoo")

    assert dry.returncode == 0, dry.stderr
    assert (
        "+ babushkin-dom:owned:белый: no vendor PLA 'белый' #FFFFFF (NEW filament) → WH:default"
        in dry.stdout
    )
    assert (
        "Elegoo (NEW vendor) PLA+ 'RAPID PLA+ Green' #4FE44D (NEW filament) → WH:default, price 1499"
        in dry.stdout
    )
    assert (
        "2 change(s), 0 already imported (DRY RUN" in dry.stdout
        and writes_after_dry == []
    )
    assert (
        applied.returncode == 0
        and "2 change(s)" in applied.stdout
        and "(applied)" in applied.stdout
    )
    assert (
        rerun.returncode == 0
        and "0 change(s), 2 already imported (applied)" in rerun.stdout
    )
    assert len(shop.spoolman.find("spool")) == 2

    report = json.loads(lint.stdout)
    rules = sorted(
        (f["rule"], f["subject"])
        for f in report["findings"]
        if f["rule"].startswith("spools/")
    )
    assert rules == [
        ("spools/no-tare", "spool:1"),
        ("spools/not-weighed", "spool:1"),
        ("spools/not-weighed", "spool:2"),
    ]
    assert lint.returncode == 0 and "2 spool(s)" in report["summary"]["data"]
    assert "ELEGOO" in tare_ls.stdout and "154 ± 10 g" in tare_ls.stdout


def test_import_owned_splits_a_multi_word_vendor_by_the_tare_db(
    tmp_path: Path, shop: LiveApi
) -> None:
    """`Bambu Lab …` is vendor "Bambu Lab", not "Bambu": the CLI knows 3d-api's tare-DB vendors."""
    plates = tmp_path / "proj" / "gen" / "plates.json"
    plates.parent.mkdir(parents=True)
    plates.write_text(
        json.dumps(
            {
                "owned": {"чёрный": "#000000"},
                "bought": {
                    "чёрный": "Bambu Lab PLA Basic Black (Game Centar, 2990 дин)"
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    dry = _cli(tmp_path, shop, "printops", "spool", "import-owned", str(plates))
    assert dry.returncode == 0, dry.stderr
    assert "Bambu Lab (NEW vendor) PLA 'PLA Basic Black' #000000" in dry.stdout, (
        dry.stdout
    )


def test_lint_printops_without_api_skips_data_rules(tmp_path: Path) -> None:
    """A user lints the repo fleet on a laptop with no 3d-api configured: the fleet rules run,
    and the report says the spool data rules were skipped (and why) instead of failing."""
    out = run_cli(
        tmp_path,
        "lint",
        "--printops",
        env_extra={"THREED_FLEET": str(REPO_FLEET), "PRINTOPS_API": ""},
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "data rules: skipped (no 3d-api configured" in out.stdout


LEGACY = {
    ("vendor", "spool_tare_g"): {"name": "Tare", "field_type": "float", "unit": "g"},
    ("spool", "printops_slot"): {"name": "Slot", "field_type": "text"},
    ("spool", "printops_location_id"): {"name": "Loc id", "field_type": "integer"},
    ("spool", "opened_at"): {"name": "Opened", "field_type": "datetime"},
    ("spool", "tag_uids"): {"name": "Tags", "field_type": "text"},
}


def _legacy_world(live: LiveApi) -> dict[str, int]:
    """What an old PrintOps left in Spoolman: the legacy fields and values in them."""
    sm = live.spoolman
    for (entity, key), params in LEGACY.items():
        if not any(f["key"] == key for f in sm.list_fields(entity)):  # type: ignore[arg-type]
            sm.set_field(entity, key, params)  # type: ignore[arg-type]
    vendor = sm.create("vendor", {"name": "Sunlu", "extra": {"spool_tare_g": json.dumps(135.0)}})
    fil = sm.create("filament", {"name": "PLA", "material": "PLA", "density": 1.24, "diameter": 1.75, "weight": 1000})
    loaded = sm.create(
        "spool",
        {
            "filament_id": fil["id"],
            "extra": {
                "printops_slot": json.dumps("u1/T3"),
                "printops_location_id": "7",
                "opened_at": json.dumps("2026-10-01T10:00:00Z"),
                "tag_uids": json.dumps('["04:aa:bb"]'),
            },
        },
    )
    return {"vendor": int(vendor["id"]), "spool": int(loaded["id"]), "filament": int(fil["id"])}


def test_fields_migrate_native_dry_run_then_apply_then_noop(
    tmp_path: Path, shop: LiveApi
) -> None:
    """A user upgrades PrintOps: `fields migrate-native` shows the plan, `--apply` moves every
    legacy value to Spoolman's native field and removes the legacy fields, a second run is a no-op."""
    ids = _legacy_world(shop)
    writes = len(shop.spoolman.writes())

    dry = _cli(tmp_path, shop, "printops", "fields", "migrate-native")
    assert dry.returncode == 0, dry.stderr
    assert "copy     vendor.spool_tare_g 'Sunlu': empty_spool_weight := 135 g" in dry.stdout
    assert "location := 'slot:u1/T3'" in dry.stdout and "link tag(s) 04AABB" in dry.stdout
    assert "field spool.tag_uids: would delete" in dry.stdout
    assert "DRY RUN — nothing written; re-run with --apply" in dry.stdout
    assert len(shop.spoolman.writes()) == writes  # the dry run wrote nothing to Spoolman

    applied = _cli(tmp_path, shop, "printops", "fields", "migrate-native", "--apply")
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert applied.stdout.rstrip().endswith("applied")
    vendor = shop.spoolman.get("vendor", ids["vendor"])
    assert vendor["empty_spool_weight"] == 135.0 and vendor["extra"] == {}
    spool = shop.spoolman.get("spool", ids["spool"])
    assert spool["location"] == "slot:u1/T3" and spool["first_used"] == "2026-10-01T10:00:00Z"
    assert [t["uid"] for t in spool["tags"]] == ["04AABB"] and spool["extra"] == {}
    assert not {k for _, k in LEGACY} & {
        str(f["key"]) for e in ("vendor", "spool") for f in shop.spoolman.list_fields(e)  # type: ignore[arg-type]
    }

    again = _cli(tmp_path, shop, "printops", "fields", "migrate-native", "--apply", "--json")
    assert again.returncode == 0, again.stderr
    report = json.loads(again.stdout)
    assert report["changes"] == [] and report["blocked"] is False and report["pending"] is False

    slots = _cli(tmp_path, shop, "printops", "spool", "show", str(ids["spool"]))
    assert slots.returncode == 0 and "u1/T3" in slots.stdout


def test_fields_migrate_native_conflict_exits_1_and_keeps_the_field(
    tmp_path: Path, shop: LiveApi
) -> None:
    """A different native value is never overwritten: exit 1 so scripts can gate on it, the other
    items still migrate, the legacy field stays until the user resolves it."""
    ids = _legacy_world(shop)
    shop.spoolman.update("vendor", ids["vendor"], {"empty_spool_weight": 150.0})

    run = _cli(tmp_path, shop, "printops", "fields", "migrate-native", "--apply")
    assert run.returncode == 1, run.stdout
    assert "conflict vendor.spool_tare_g 'Sunlu': extra says 135 g, empty_spool_weight says 150 g" in run.stdout
    assert "field vendor.spool_tare_g: kept" in run.stdout
    assert shop.spoolman.get("vendor", ids["vendor"])["empty_spool_weight"] == 150.0
    assert shop.spoolman.get("spool", ids["spool"])["location"] == "slot:u1/T3"  # the rest still moved
    assert any(f["key"] == "spool_tare_g" for f in shop.spoolman.list_fields("vendor"))

    shop.spoolman.update("vendor", ids["vendor"], {"empty_spool_weight": 135.0})
    fixed = _cli(tmp_path, shop, "printops", "fields", "migrate-native", "--apply")
    assert fixed.returncode == 0, fixed.stdout
    assert not any(f["key"] == "spool_tare_g" for f in shop.spoolman.list_fields("vendor"))


def test_fields_migrate_native_needs_write_scope_for_apply(
    tmp_path: Path, shop: LiveApi
) -> None:
    ids = _legacy_world(shop)
    assert _cli(tmp_path, shop, "printops", "fields", "migrate-native", token="read").returncode == 0
    denied = _cli(tmp_path, shop, "printops", "fields", "migrate-native", "--apply", token="read")
    assert denied.returncode != 0
    assert shop.spoolman.get("vendor", ids["vendor"])["extra"] == {"spool_tare_g": "135.0"}


def test_fields_fill_defaults_dry_run_apply_and_noop(tmp_path: Path, shop: LiveApi) -> None:
    """`fields fill-defaults` shows the plan, `--apply` sets 300 / 600 (PLA+) only where empty, keeps a
    number typed in the Spoolman UI, and a second run writes nothing."""
    sm = shop.spoolman
    vendor = sm.create("vendor", {"name": "Elegoo"})

    def fil(material: str, name: str, **extra: str) -> int:
        body = {"name": name, "vendor_id": vendor["id"], "material": material, "density": 1.24,
                "diameter": 1.75, "weight": 1000, "extra": extra}
        return int(sm.create("filament", body)["id"])

    plus, petg = fil("PLA", "RAPID PLA+ Green"), fil("PETG", "Grey")
    typed = fil("PLA+", "Typed", max_speed_mm_s="450")
    writes = len(sm.writes())

    dry = _cli(tmp_path, shop, "printops", "fields", "fill-defaults")
    assert dry.returncode == 0, dry.stderr
    assert "RAPID PLA+ Green" in dry.stdout and "new 600" in dry.stdout and "new 300" in dry.stdout
    assert "keep" in dry.stdout and "DRY RUN — nothing written; re-run with --apply" in dry.stdout
    assert len(sm.writes()) == writes

    applied = _cli(tmp_path, shop, "printops", "fields", "fill-defaults", "--apply")
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert applied.stdout.rstrip().endswith("applied") and "2 set, 1 keep" in applied.stdout
    got = {f: json.loads(sm.get("filament", f)["extra"]["max_speed_mm_s"]) for f in (plus, petg, typed)}
    assert got == {plus: 600, petg: 300, typed: 450}

    writes = len(sm.writes())
    again = _cli(tmp_path, shop, "printops", "fields", "fill-defaults", "--apply", "--json")
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["summary"] == {"set": 0, "keep": 3, "failed": 0}
    assert len(sm.writes()) == writes

    denied = _cli(tmp_path, shop, "printops", "fields", "fill-defaults", "--apply", token="read")
    assert denied.returncode != 0



def test_fields_migrate_native_restores_the_placement_the_location_id_recorded(
    tmp_path: Path, shop: LiveApi
) -> None:
    """#712: a spool with a recorded PrintOps location id and no location text used to lose its place
    when the legacy field went; the dry run lists the path it will write and the apply writes it."""
    sm = shop.spoolman
    _legacy_world(shop)
    shelf = {loc.path: loc.id for loc in _locations(shop)}["WH:A/S1"]
    fil = sm.create("filament", {"name": "PETG", "material": "PETG", "density": 1.27, "diameter": 1.75, "weight": 1000})
    spool = sm.create("spool", {"filament_id": fil["id"], "extra": {"printops_location_id": str(shelf)}})

    dry = _cli(tmp_path, shop, "printops", "fields", "migrate-native")
    assert "location := 'WH:A/S1' (recorded as PrintOps location #" in dry.stdout
    assert not sm.get("spool", spool["id"]).get("location")

    applied = _cli(tmp_path, shop, "printops", "fields", "migrate-native", "--apply")
    assert applied.returncode == 0, applied.stdout + applied.stderr
    placed = sm.get("spool", spool["id"])
    assert placed["location"] == "WH:A/S1" and placed["extra"] == {}


def _locations(shop: LiveApi) -> list[locations.Location]:
    conn = db.open_db(shop.settings.db_path)
    try:
        return locations.all_locations(conn)
    finally:
        conn.close()


def _received_max_speed(tmp_path: Path, shop: LiveApi, *flags: str) -> int | None:
    got = _cli(tmp_path, shop, "printops", "spool", "receive", "--vendor", "Elegoo", "--material", "PETG",
               "--name", "Grey", "--net", "1000", "--json", *flags)
    assert got.returncode == 0, got.stderr
    filament_id = json.loads(got.stdout)["filament_id"]
    raw = shop.spoolman.get("filament", filament_id)["extra"].get("max_speed_mm_s")
    return None if raw is None else int(json.loads(raw))


def test_spool_receive_max_speed_flag_sets_the_new_filaments_max_speed(tmp_path: Path, shop: LiveApi) -> None:
    """`spool receive --max-speed N` overrides the default (300, PLA+ 600) for the filament it creates."""
    assert _received_max_speed(tmp_path, shop, "--max-speed", "450") == 450


def test_spool_receive_without_max_speed_flag_keeps_the_default(tmp_path: Path, shop: LiveApi) -> None:
    assert _received_max_speed(tmp_path, shop) == 300


def test_spool_receive_max_speed_must_be_a_positive_integer(tmp_path: Path, shop: LiveApi) -> None:
    for bad, why in (("fast", "needs an integer"), ("4.5", "needs an integer"), ("0", "must be > 0"), ("-5", "must be > 0")):
        got = _cli(tmp_path, shop, "printops", "spool", "receive", "--vendor", "Elegoo", "--material", "PETG",
                   "--name", "Grey", "--net", "1000", "--max-speed", bad)
        assert got.returncode != 0 and why in got.stderr, (bad, got.stderr)
    assert shop.spoolman.find("spool") == []  # nothing was created by a refused flag
