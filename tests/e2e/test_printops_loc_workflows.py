"""E2E: `3d printops loc` against a real uvicorn 3d-api talking HTTP to FakeSpoolman (#159)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from printops_fixtures import LiveApi

from printops import locations
from printops.db import open_db
from printops.spoolman import ensure_extra_fields

from .workflow_helper import run_cli

FLEET = """\
u1:
  model: Snapmaker U1
  owner: own
  access: offline
  heads:
    - {label: T1, index: 0}
    - {label: T2, index: 1, usable: false, reason: "head T2 does not work"}
"""


def _env(live: LiveApi, token: str = "rw") -> dict[str, str]:
    return {"PRINTOPS_API": live.url, "PRINTOPS_TOKEN": live.tokens[token]}


def _loc(tmp_path: Path, live: LiveApi, *argv: str, token: str = "rw") -> tuple[int, str, str]:
    r = run_cli(tmp_path, "printops", "loc", *argv, env_extra=_env(live, token))
    return r.returncode, r.stdout, r.stderr


def _spool_in(live: LiveApi, path: str) -> int:
    sm = live.spoolman
    ensure_extra_fields(sm)  # idempotent; the API's background task may not have run yet
    vid = sm.create("vendor", {"name": "Bambu"})["id"]
    fid = sm.create("filament", {"name": "PLA Basic", "vendor_id": vid, "material": "PLA", "weight": 1000,
                                 "density": 1.24, "diameter": 1.75,
                                 "color_hex": "1A2B3C"})["id"]
    sid = int(sm.create("spool", {"filament_id": fid})["id"])
    conn = open_db(live.settings.db_path)
    try:
        locations.place_spool(conn, sm, sid, locations.get(conn, path))
    finally:
        conn.close()
    return sid


def test_user_builds_a_storage_tree_and_moves_a_shelf_with_its_spool(tmp_path: Path, live_api: LiveApi) -> None:
    """The user lays out a warehouse, stores a spool on a shelf, sees it in `show`, renames the
    shelf, and Spoolman's location follows; archiving refuses while the spool is there."""
    for argv in (("add", "WH:default", "--kind", "warehouse"),
                 ("add", "WH:default/S1", "--kind", "shelf", "--capacity", "6", "--note", "by the window"),
                 ("add", "drybox:1", "--kind", "dry_box", "--humidity-sensor", "sensor.drybox_1_humidity")):
        code, out, err = _loc(tmp_path, live_api, *argv)
        assert code == 0, err
        assert out.startswith("added ")
    sid = _spool_in(live_api, "WH:default/S1")

    code, tree, _ = _loc(tmp_path, live_api, "tree")
    assert code == 0
    assert re.search(r"^WH:default  \(warehouse\)  #1$", tree, re.M)
    assert "  S1  (shelf, cap 6, by the window)  #2" in tree
    assert "drybox:1  (dry_box, RH sensor.drybox_1_humidity)" in tree

    code, shown, _ = _loc(tmp_path, live_api, "show", "WH:default/S1")
    assert code == 0
    assert "label    cell:WH:default/S1  (not printed yet)" in shown
    assert "spools   1 / 6" in shown and f"#{sid}  Bambu PLA Basic (PLA, #1A2B3C)  1000 g" in shown

    code, out, err = _loc(tmp_path, live_api, "mv", "WH:default/S1", "WH:default/S2")
    assert code == 0, err
    assert "WH:default/S1 -> WH:default/S2" in out and "Spoolman: 1 spool(s) re-synced" in out
    assert live_api.spoolman.get("spool", sid)["location"] == "WH:default/S2"

    code, _, err = _loc(tmp_path, live_api, "archive", "WH:default/S2")
    assert code == 1 and "still holds 1 spool" in err and f"#{sid}" in err
    code, _, err = _loc(tmp_path, live_api, "add", "WH:nowhere/S1", "--kind", "shelf")
    assert code == 1 and "parent 'WH:nowhere' of 'WH:nowhere/S1' does not exist" in err
    code, _, err = _loc(tmp_path, live_api, "add", "WH:default/S3", "--kind", "shelf", token="read")
    assert code == 1 and "HTTP 403" in err


def test_user_prints_a_label_sheet_and_scans_a_label_back(tmp_path: Path, live_api: LiveApi) -> None:
    """The user prints QR labels for a shelf and its boxes, then a scanned QR resolves to the
    cell; after a move `show` flags the stuck-on label as stale."""
    for path, kind in (("WH:A", "warehouse"), ("WH:A/S1", "shelf"), ("WH:A/S1/B1", "box"), ("WH:A/S1/B2", "box")):
        assert _loc(tmp_path, live_api, "add", path, "--kind", kind)[0] == 0
    pdf = tmp_path / "out" / "shelf.pdf"
    code, out, err = _loc(tmp_path, live_api, "label", "WH:A/S1", "--subtree", "-o", str(pdf), "--start", "3", "--ntag")
    assert code == 0, err
    assert f"wrote 3 label(s) on 1 page(s): {pdf}" in out
    assert re.search(r"^ntag213 WH:A/S1/B2  03[0-9a-f]+fe$", out, re.M)
    body = pdf.read_bytes()
    assert body.startswith(b"%PDF-1.4") and b"(WH:A/S1/B1) Tj" in body

    svg = tmp_path / "labels.svg"
    code, out, _ = _loc(tmp_path, live_api, "label", "WH:A/S1/B1", "WH:A/S1/B2", "-o", str(svg), "--grid", "1x1")
    assert code == 0 and (tmp_path / "labels-2.svg").is_file() and "<svg" in svg.read_text(encoding="utf-8")

    code, out, _ = _loc(tmp_path, live_api, "resolve", "cell:WH:A/S1/B2")
    assert code == 0 and out.startswith("WH:A/S1/B2  #4  (box)")
    assert _loc(tmp_path, live_api, "mv", "WH:A/S1/B2", "WH:A/S1/B9")[0] == 0
    code, out, _ = _loc(tmp_path, live_api, "resolve", "cell:WH:A/S1/B2")  # the old sticker still works
    assert code == 0 and out.startswith("WH:A/S1/B9  #4  (box, label STALE)")
    code, shown, _ = _loc(tmp_path, live_api, "show", "4")
    assert "printed: cell:WH:A/S1/B2 — STALE" in shown


def test_printer_slots_come_from_the_fleet_file(tmp_path: Path, live_api: LiveApi,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """With fleet/printers.yaml in place `sync-fleet` materialises the printer slots, unusable heads
    carry their reason, and they cannot be moved by hand."""
    fleet = tmp_path / "printers.yaml"
    fleet.write_text(FLEET, encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(fleet))  # read by the in-process 3d-api
    code, out, _ = _loc(tmp_path, live_api, "sync-fleet")  # a deploy's first step; reads never sync
    assert code == 0 and "slot:u1/T2" in out
    code, tree, _ = _loc(tmp_path, live_api, "tree", "slot:u1", "--json")
    assert code == 0
    (u1,) = json.loads(tree)
    assert [c["path"] for c in u1["children"]] == ["slot:u1/T1", "slot:u1/T2"]
    assert u1["children"][1]["note"] == "unusable: head T2 does not work"
    code, _, err = _loc(tmp_path, live_api, "mv", "slot:u1/T1", "slot:u1/T9")
    assert code == 1 and "fleet/printers.yaml" in err
    code, out, _ = _loc(tmp_path, live_api, "sync-fleet")
    assert code == 0 and "already match" in out


def test_loc_help_and_bad_invocations(tmp_path: Path) -> None:
    """A user discovers `loc` from its help, and a typo or a missing --kind is a usage error
    (exit 2) that names the fix instead of reaching 3d-api."""
    r = run_cli(tmp_path, "printops", "loc", "--help")
    assert r.returncode == 0
    assert "3d printops loc label REF..." in r.stdout and "slot:<printer>/<head>" in r.stdout
    bad = run_cli(tmp_path, "printops", "loc", "teleport", env_extra={"PRINTOPS_API": "http://127.0.0.1:9"})
    assert bad.returncode == 2 and "unknown loc action 'teleport'" in bad.stderr
    nokind = run_cli(tmp_path, "printops", "loc", "add", "WH:A", env_extra={"PRINTOPS_API": "http://127.0.0.1:9"})
    assert nokind.returncode == 2 and "add needs --kind" in nokind.stderr
