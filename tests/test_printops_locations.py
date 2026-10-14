"""PrintOps locations (#159): path grammar, tree CRUD, moves with Spoolman re-sync, archive rules,
fleet printer slots, scan resolution, and the /v1/locations routes."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from printops_fakes import FakeSpoolman

from printops import events
from printops import locations as locs
from printops.client import ApiClient, ApiError
from printops.fleet import load_fleet
from printops.spoolman import ensure_extra_fields

FLEET = """\
u1:
  model: Snapmaker U1
  owner: own
  access: offline
  heads:
    - {label: T1, index: 0}
    - {label: T2, index: 1, usable: false, reason: "head T2 does not work"}
boris:
  model: Bambu Lab A1 mini
  owner: boris
  access: offline
  heads: [{label: T1, index: 0}]
  ams: [{id: lite, kind: ams_lite, slots: 2}]
"""


@pytest.fixture
def conn(printops_db: sqlite3.Connection) -> sqlite3.Connection:
    return printops_db


@pytest.fixture
def sm() -> FakeSpoolman:
    fake = FakeSpoolman()
    ensure_extra_fields(fake)
    return fake


def _spool(sm: FakeSpoolman, location: str | None = None) -> int:
    vendors = sm.find("vendor")
    vid = vendors[0]["id"] if vendors else sm.create("vendor", {"name": "Bambu"})["id"]
    fid = sm.create("filament", {"name": "PLA Basic", "vendor_id": vid, "material": "PLA", "density": 1.24,
                                 "diameter": 1.75, "weight": 1000})["id"]
    return int(sm.create("spool", {"filament_id": fid, "location": location})["id"])


def _tree(conn: sqlite3.Connection, *paths: str) -> dict[str, locs.Location]:
    kinds = ["warehouse", "shelf", "box", "slot"]
    out = {}
    for p in paths:
        out[p] = locs.add(conn, p, kinds[min(p.count("/"), 3)], actor="test")
    return out


def _fleet(tmp_path: Path, text: str = FLEET) -> Any:
    f = tmp_path / "printers.yaml"
    f.write_text(text, encoding="utf-8")
    return load_fleet(f)


# ------------------------------------------------------------------------------- grammar
@pytest.mark.parametrize("path", ["WH:A", "WH:A/S2/B3/04", "dryer:1", "drybox:shelf-2.top", "WH:" + "A" * 32,
                                  "W:" + "/".join(["abcdefg"] * 7) + "/a"])
def test_valid_paths_pass_the_grammar(path: str) -> None:
    assert locs.check_path(f"  {path} ") == path and len(path) <= 64


@pytest.mark.parametrize(("path", "match"), [
    ("WH", "SCHEME:NAME"), ("WH:", "SCHEME:NAME"), (":A", "scheme"), ("1WH:A", "scheme"),
    ("cell:A", "reserved"), ("WH:A//B", "segment"), ("WH:A/-x", "segment"), ("WH:A B", "segment"),
    ("WH:A/contents", "reserved"), ("WH:" + "A" * 33, "segment"), ("WH:Ä", "segment"),
    ("W:" + "/".join(["abcdefgh"] * 7) + "/abc", "at most 64"),
])
def test_bad_paths_name_the_rule_they_break(path: str, match: str) -> None:
    with pytest.raises(locs.LocationError, match=match):
        locs.check_path(path)


def test_parent_path_and_label_payloads() -> None:
    assert locs.parent_path("WH:A/S2/B3") == "WH:A/S2"
    assert locs.parent_path("WH:A") is None
    assert locs.qr_payload("WH:A/S2") == "cell:WH:A/S2"
    assert locs.qr_payload("dryer:1") == "dryer:1" and locs.qr_payload("slot:u1/T3") == "slot:u1/T3"


# ---------------------------------------------------------------------------------- CRUD
def test_add_needs_an_active_parent_and_unique_path(conn: sqlite3.Connection) -> None:
    with pytest.raises(locs.LocationError, match="parent 'WH:A' of 'WH:A/S1' does not exist") as e:
        locs.add(conn, "WH:A/S1", "shelf", actor="t")
    assert e.value.status == 409
    a = locs.add(conn, "WH:A", "warehouse", actor="t", capacity=None)
    s1 = locs.add(conn, "WH:A/S1", "shelf", actor="t", capacity=3, note="top")
    assert (s1.parent_id, s1.capacity, s1.note, s1.source) == (a.id, 3, "top", "manual")
    with pytest.raises(locs.LocationError, match="already exists"):
        locs.add(conn, "wh:a/s1", "shelf", actor="t")
    assert [e.kind for e in events.last(conn, 5)] == ["location.created", "location.created"]


@pytest.mark.parametrize(("kwargs", "match"), [
    ({"path": "slot:u1/T9", "kind": "printer_slot"}, "fleet"),
    ({"path": "WH:B", "kind": "printer_slot"}, "only for fleet"),
    ({"path": "WH:B", "kind": "garage"}, "unknown location kind"),
    ({"path": "WH:B", "kind": "box", "capacity": 0}, "capacity"),
    ({"path": "WH:B", "kind": "box", "capacity": True}, "capacity"),
    ({"path": "WH:B", "kind": "box", "humidity_sensor_entity": "drybox 1"}, "entity id"),
    ({"path": "WH:B", "kind": "box", "tag_uid": "zz"}, "hex"),
])
def test_add_validates_kind_and_attributes(conn: sqlite3.Connection, kwargs: dict[str, Any], match: str) -> None:
    path, kind = kwargs.pop("path"), kwargs.pop("kind")
    with pytest.raises(locs.LocationError, match=match):
        locs.add(conn, path, kind, actor="t", **kwargs)


def test_get_find_by_path_or_id_and_missing_is_lookup_error(conn: sqlite3.Connection) -> None:
    a = locs.add(conn, "WH:A", "warehouse", actor="t")
    assert locs.get(conn, a.id) == locs.get(conn, str(a.id)) == locs.get(conn, "wh:A")
    with pytest.raises(LookupError, match="no location 'dryer:9'"):
        locs.get(conn, "dryer:9")


def test_update_attributes_and_revive(conn: sqlite3.Connection, sm: FakeSpoolman) -> None:
    t = _tree(conn, "WH:A", "WH:A/S1")
    s1 = locs.update(conn, t["WH:A/S1"], {"humidity_sensor_entity": "sensor.box_rh", "tag_uid": "04:a2:2b:1a:7f:61:80",
                                          "capacity": 2}, actor="t")
    assert (s1.humidity_sensor_entity, s1.tag_uid, s1.capacity) == ("sensor.box_rh", "04A22B1A7F6180", 2)
    with pytest.raises(locs.LocationError, match="path: use move"):
        locs.update(conn, s1, {"path": "WH:B"}, actor="t")
    with pytest.raises(locs.LocationError, match="archive with DELETE"):
        locs.update(conn, s1, {"archived": True}, actor="t")
    locs.archive(conn, sm, s1, actor="t")
    locs.archive(conn, sm, t["WH:A"], actor="t")
    with pytest.raises(locs.LocationError, match="parent 'WH:A' of 'WH:A/S1' is archived"):
        locs.update(conn, locs.get(conn, "WH:A/S1"), {"archived": False}, actor="t")
    assert not locs.update(conn, locs.get(conn, "WH:A"), {"archived": False}, actor="t").archived
    assert not locs.update(conn, locs.get(conn, "WH:A/S1"), {"archived": False}, actor="t").archived
    other = locs.add(conn, "WH:B", "warehouse", actor="t")
    with pytest.raises(locs.LocationError, match="tag 04A22B1A7F6180 is already on location 'WH:A/S1'"):
        locs.update(conn, other, {"tag_uid": "04A22B1A7F6180"}, actor="t")


def test_ancestors_and_inherited_humidity_sensor(conn: sqlite3.Connection) -> None:
    locs.add(conn, "drybox:1", "dry_box", actor="t", humidity_sensor_entity="sensor.drybox_1_rh")
    inner = locs.add(conn, "drybox:1/left", "slot", actor="t")
    assert [a.path for a in locs.ancestors(conn, inner)] == ["drybox:1"]
    assert locs.humidity_sensor(conn, inner) == "sensor.drybox_1_rh"
    assert locs.humidity_sensor(conn, locs.add(conn, "WH:A", "warehouse", actor="t")) is None


def test_resolve_accepts_every_scanned_form(conn: sqlite3.Connection) -> None:
    t = _tree(conn, "WH:A", "WH:A/S2")
    s2 = locs.update(conn, t["WH:A/S2"], {"tag_uid": "04A22B1A7F6180", "qr": "cell:WH:old/S2"}, actor="t")
    dryer = locs.add(conn, "dryer:1", "dryer", actor="t")
    for text in ("WH:A/S2", "cell:WH:A/S2", f"loc-{s2.id}", str(s2.id), "04:A2:2B:1A:7F:61:80", "cell:WH:old/S2"):
        assert locs.resolve(conn, text).id == s2.id, text
    assert locs.resolve(conn, "dryer:1") == dryer
    with pytest.raises(locs.LocationNotFound):
        locs.resolve(conn, "cell:WH:Z")


def test_tree_nests_by_parent(conn: sqlite3.Connection) -> None:
    _tree(conn, "WH:A", "WH:A/S1", "WH:A/S1/B1", "WH:A/S2", "dryer:1")
    roots = locs.tree(locs.all_locations(conn))
    assert [r["path"] for r in roots] == ["dryer:1", "WH:A"]
    assert [c["path"] for c in roots[1]["children"]] == ["WH:A/S1", "WH:A/S2"]
    assert roots[1]["children"][0]["children"][0]["qr_payload"] == "cell:WH:A/S1/B1"


# ------------------------------------------------------------------------- Spoolman side
def test_place_spool_writes_path_id_and_caller_extras_in_one_patch(conn: sqlite3.Connection, sm: FakeSpoolman) -> None:
    loc = _tree(conn, "WH:A")["WH:A"]
    sid = _spool(sm)
    before = len(sm.writes())
    spool = locs.place_spool(sm, sid, loc, extra={"printops_slot": "", "dry_state": "ok"})
    assert sm.writes()[before:] == [("PATCH", f"/spool/{sid}")]
    assert spool["location"] == "WH:A"
    assert spool["extra"]["printops_location_id"] == str(loc.id) and spool["extra"]["dry_state"] == '"ok"'
    cleared = locs.place_spool(sm, sid, None)
    assert cleared["location"] == "" and "printops_location_id" not in cleared["extra"]


def test_contents_by_id_or_by_hand_typed_location(conn: sqlite3.Connection, sm: FakeSpoolman) -> None:
    t = _tree(conn, "WH:A", "WH:A/S1")
    bound = _spool(sm)
    locs.place_spool(sm, bound, t["WH:A/S1"])
    by_hand = _spool(sm, "wh:a/s1")  # typed in the Spoolman UI, no id
    elsewhere = _spool(sm)
    locs.place_spool(sm, elsewhere, t["WH:A"])
    sm.update("spool", elsewhere, {"location": "WH:A/S1"})  # a stale string: the id wins
    assert {s["id"] for s in locs.contents(conn, sm, t["WH:A/S1"])} == {bound, by_hand}
    assert {s["id"] for s in locs.contents(conn, sm, t["WH:A"], recursive=True)} == {bound, by_hand, elsewhere}


def test_move_renames_subtree_and_resyncs_spoolman(conn: sqlite3.Connection, sm: FakeSpoolman) -> None:
    t = _tree(conn, "WH:A", "WH:A/S1", "WH:A/S1/B1", "WH:B")
    deep = _spool(sm)
    locs.place_spool(sm, deep, t["WH:A/S1/B1"])
    typed = _spool(sm, "WH:A/S1")
    res = locs.move(conn, sm, t["WH:A/S1"], "WH:B/S9", actor="t")
    assert res.renamed == [("WH:A/S1", "WH:B/S9"), ("WH:A/S1/B1", "WH:B/S9/B1")]
    assert res.location.parent_id == t["WH:B"].id and sorted(res.spoolman.updated) == sorted([deep, typed])
    assert sm.get("spool", deep)["location"] == "WH:B/S9/B1"
    assert sm.get("spool", typed)["extra"]["printops_location_id"] == str(t["WH:A/S1"].id)
    assert locs.get(conn, "WH:B/S9/B1").id == t["WH:A/S1/B1"].id
    assert events.last(conn, 1)[0].payload == {"from": "WH:A/S1", "to": "WH:B/S9", "subtree": 2}
    # case-only rename of a root
    assert locs.move(conn, sm, locs.get(conn, "WH:B"), "wh:b", actor="t").renamed[1] == ("WH:B/S9", "wh:b/S9")


@pytest.mark.parametrize(("src", "dst", "match"), [
    ("WH:A", "WH:A/S1/X", "own subtree"),
    ("WH:A/S1", "WH:B", "already exists"),
    ("WH:A/S1", "WH:Z/S1", "does not exist"),
    ("WH:A", "WH:" + "x" * 30 + "/" + "y" * 28, "longer than 64"),
    ("WH:A/S1", "slot:u1/T9", "fleet"),
])
def test_move_refusals(conn: sqlite3.Connection, sm: FakeSpoolman, src: str, dst: str, match: str) -> None:
    t = _tree(conn, "WH:A", "WH:A/S1", "WH:A/S1/B1", "WH:B")
    with pytest.raises(locs.LocationError, match=match):
        locs.move(conn, sm, t[src], dst, actor="t")
    assert locs.get(conn, src).path == src


def test_move_with_spoolman_down_commits_and_resync_repairs(conn: sqlite3.Connection, sm: FakeSpoolman) -> None:
    t = _tree(conn, "WH:A", "WH:A/S1")
    sid = _spool(sm)
    locs.place_spool(sm, sid, t["WH:A/S1"])
    sm.down = True
    res = locs.move(conn, sm, t["WH:A/S1"], "WH:A/S2", actor="t")
    assert res.spoolman.failed and res.spoolman.failed[0]["spool_id"] is None
    sm.down = False
    assert sm.get("spool", sid)["location"] == "WH:A/S1"  # drifted
    assert locs.resync(conn, sm).updated == [sid]
    assert sm.get("spool", sid)["location"] == "WH:A/S2"
    assert locs.resync(conn, sm).updated == []


def test_archive_refuses_children_spools_and_blind_checks(conn: sqlite3.Connection, sm: FakeSpoolman) -> None:
    t = _tree(conn, "WH:A", "WH:A/S1")
    with pytest.raises(locs.LocationError, match="1 active child"):
        locs.archive(conn, sm, t["WH:A"], actor="t")
    sid = _spool(sm)
    locs.place_spool(sm, sid, t["WH:A/S1"])
    with pytest.raises(locs.LocationError, match=f"still holds 1 spool.*#{sid}") as e:
        locs.archive(conn, sm, t["WH:A/S1"], actor="t")
    assert e.value.status == 409
    with pytest.raises(locs.LocationError, match="Spoolman is not configured"):
        locs.archive(conn, None, t["WH:A/S1"], actor="t")
    locs.place_spool(sm, sid, None)
    assert locs.archive(conn, sm, t["WH:A/S1"], actor="t").archived
    with pytest.raises(locs.LocationError, match="archived"):
        locs.place_spool(sm, sid, locs.get(conn, "WH:A/S1"))
    assert events.last(conn, 1)[0].kind == "location.archived"


# ------------------------------------------------------------------------- fleet slots
def test_fleet_slots_cover_heads_units_and_unusable_reasons(tmp_path: Path) -> None:
    slots = {s.path: s for s in locs.fleet_slots(_fleet(tmp_path))}
    assert list(slots) == ["slot:boris", "slot:boris/T1", "slot:boris/lite", "slot:boris/lite/1",
                           "slot:boris/lite/2", "slot:u1", "slot:u1/T1", "slot:u1/T2"]
    assert slots["slot:u1/T2"].note == "unusable: head T2 does not work" and slots["slot:u1/T1"].note is None
    assert (slots["slot:boris/lite"].kind, slots["slot:u1"].kind) == ("ams", "printer")


def test_sync_fleet_creates_is_idempotent_archives_gone_and_revives(conn: sqlite3.Connection, sm: FakeSpoolman,
                                                                    tmp_path: Path) -> None:
    first = locs.sync_fleet(conn, locs.fleet_slots(_fleet(tmp_path)), sm)
    assert len(first.created) == 8 and locs.get(conn, "slot:u1/T1").capacity == 1
    assert locs.get(conn, "slot:u1/T1").source == "fleet"
    assert not locs.sync_fleet(conn, locs.fleet_slots(_fleet(tmp_path)), sm).changed
    sid = _spool(sm)
    locs.place_spool(sm, sid, locs.get(conn, "slot:u1/T2"))
    gone = FLEET.split("boris:")[0].replace("    - {label: T2, index: 1, usable: false, reason: \"head T2 does not work\"}\n", "")
    second = locs.sync_fleet(conn, locs.fleet_slots(_fleet(tmp_path, gone)), sm)
    # deepest first (children before their parent); same depth in path order, case-insensitive
    assert second.archived == ["slot:boris/lite/1", "slot:boris/lite/2", "slot:boris/lite", "slot:boris/T1",
                               "slot:boris"]
    assert [k["path"] for k in second.kept] == ["slot:u1/T2"] and "still holds" in second.kept[0]["reason"]
    third = locs.sync_fleet(conn, locs.fleet_slots(_fleet(tmp_path)), sm)
    assert sorted(third.revived) == sorted(second.archived)
    blind = locs.sync_fleet(conn, locs.fleet_slots(_fleet(tmp_path, gone)), None)
    assert blind.archived == [] and {k["path"] for k in blind.kept} >= {"slot:boris", "slot:u1/T2"}


def test_fleet_locations_cannot_be_edited_by_hand(conn: sqlite3.Connection, sm: FakeSpoolman, tmp_path: Path) -> None:
    locs.sync_fleet(conn, locs.fleet_slots(_fleet(tmp_path)), sm)
    t1 = locs.get(conn, "slot:u1/T1")
    for call in (lambda: locs.move(conn, sm, t1, "slot:u1/T9", actor="t"),
                 lambda: locs.archive(conn, sm, t1, actor="t"),
                 lambda: locs.update(conn, t1, {"kind": "box"}, actor="t")):
        with pytest.raises(locs.LocationError, match="fleet"):
            call()
    assert locs.update(conn, t1, {"tag_uid": "04A22B1A7F6180"}, actor="t").tag_uid == "04A22B1A7F6180"


# --------------------------------------------------------------------------------- API
def test_routes_need_read_and_write_scopes(printops_client: Any, printops_tokens: dict[str, str]) -> None:
    def call(method: str, path: str, token: str | None, **kw: Any) -> int:
        headers = {"Authorization": f"Bearer {printops_tokens[token]}"} if token else {}
        return int(printops_client.request(method, path, headers=headers, **kw).status_code)

    assert call("GET", "/v1/locations", None) == 401
    assert call("GET", "/v1/locations", "write") == 403
    assert call("POST", "/v1/locations", "read", json={"path": "WH:A", "kind": "warehouse"}) == 403
    assert call("POST", "/v1/locations", "write", json={"path": "WH:A", "kind": "warehouse"}) == 201
    assert call("GET", "/v1/locations", "read") == 200


def test_api_tree_show_contents_move_archive(api_client: ApiClient, fake_spoolman: FakeSpoolman) -> None:
    api_client.post("/v1/locations", {"path": "WH:A", "kind": "warehouse"})
    s1 = api_client.post("/v1/locations", {"path": "WH:A/S1", "kind": "shelf", "capacity": 4})
    ensure_extra_fields(fake_spoolman)
    sid = _spool(fake_spoolman)
    locs.place_spool(fake_spoolman, sid, locs.Location(**{k: s1[k] for k in (
        "id", "path", "kind", "parent_id", "tag_uid", "qr", "capacity", "humidity_sensor_entity", "note", "source",
        "archived", "created_at", "updated_at")}))
    tree = api_client.get("/v1/locations")
    assert tree["count"] == 2 and tree["tree"][0]["children"][0]["path"] == "WH:A/S1"
    shown = api_client.get("/v1/locations/WH:A/S1")
    assert shown["ancestors"][0]["path"] == "WH:A" and shown["qr_payload"] == "cell:WH:A/S1"
    assert [s["id"] for s in api_client.get("/v1/locations/WH:A/S1/contents")["spools"]] == [sid]
    assert api_client.get(f"/v1/locations/resolve?q=loc-{s1['id']}")["path"] == "WH:A/S1"
    moved = api_client.patch("/v1/locations/WH:A/S1", {"path": "WH:A/S2", "note": "by the door"})
    assert moved["location"]["note"] == "by the door" and moved["spoolman"]["updated"] == [sid]
    assert fake_spoolman.get("spool", sid)["location"] == "WH:A/S2"
    with pytest.raises(ApiError) as e:
        api_client.delete("/v1/locations/WH:A/S2")
    assert e.value.status == 409 and "still holds 1 spool" in e.value.message
    with pytest.raises(ApiError) as e:
        api_client.get("/v1/locations/WH:nope/contents")
    assert e.value.status == 404
    with pytest.raises(ApiError) as e:
        api_client.post("/v1/locations", {"path": "WH:A/S3", "kind": "shelf", "colour": "red"})
    assert e.value.status == 422
    with pytest.raises(ApiError) as e:
        api_client.patch("/v1/locations/WH:A/S2", {"capacity": "four"})
    assert e.value.status == 400
    assert api_client.get("/v1/locations", flat="true")["locations"][1]["capacity"] == 4


def test_api_materialises_fleet_slots_on_first_request(api_client: ApiClient, tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "fleet.yaml"
    f.write_text(FLEET, encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(f))
    paths = [loc["path"] for loc in api_client.get("/v1/locations", flat="true")["locations"]]
    assert "slot:u1/T2" in paths and "slot:boris/lite/2" in paths
    assert api_client.post("/v1/locations/sync-fleet") == {"created": [], "revived": [], "updated": [],
                                                           "archived": [], "kept": []}
    monkeypatch.setenv("THREED_FLEET", "")
    with pytest.raises(ApiError) as e:
        api_client.post("/v1/locations/sync-fleet")
    assert e.value.status == 409
