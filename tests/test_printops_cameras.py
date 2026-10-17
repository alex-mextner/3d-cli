"""3d-api camera-node registry (#341): PUT/GET/DELETE /v1/camera-nodes, the `camera` scope, lookup_url."""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

import pytest
from printops_fixtures import LiveApi

from printops import db, events, tokens
from printops.client import ApiClient
from printops.settings import Settings
from topcam import registry
from topcam.client import CameraError

# The body topcam.node's Node.record() sends.
RECORD = {
    "url": "http://192.168.0.50:8080",
    "hostname": "topcam.local",
    "stream_url": "http://192.168.0.50:8081/stream",
    "card": "HD Pro Webcam C920",
    "device": "/dev/v4l/by-id/usb-046d_HD_Pro_Webcam_C920-video-index0",
    "formats": {"YUYV": [[2304, 1536], [1920, 1080]], "MJPG": [[1920, 1080]]},
    "led": True,
    "version": "1.0.0",
}


def _h(printops_tokens: dict[str, str], name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {printops_tokens[name]}"}


@pytest.fixture
def topcam(printops_settings: Settings) -> dict[str, str]:
    """Headers of the node's own token: scope camera, named like the node (`topcam`)."""
    return {"Authorization": f"Bearer {tokens.create(printops_settings.tokens_file, 'topcam', ('camera',))[0]}"}


def _clock(monkeypatch: pytest.MonkeyPatch, ts: str) -> None:
    monkeypatch.setattr(db, "utc_now", lambda: ts)


def test_camera_token_registers_and_reader_lists_it(printops_client: Any, printops_tokens: dict[str, str], topcam: dict[str, str],
                                                     printops_db: Any) -> None:
    r = printops_client.put("/v1/camera-nodes/topcam", json=RECORD, headers=topcam)
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["name"] == "topcam" and rec["url"] == RECORD["url"] and rec["formats"] == RECORD["formats"]
    assert rec["led"] is True and rec["online"] is True and rec["age_s"] == 0
    assert rec["updated_by"] == "topcam" and rec["last_addr"] == "testclient"
    assert rec["first_seen"] == rec["last_seen"]

    listed = printops_client.get("/v1/camera-nodes", headers=_h(printops_tokens, "read")).json()
    assert [n["name"] for n in listed["items"]] == ["topcam"]
    one = printops_client.get("/v1/camera-nodes/topcam", headers=_h(printops_tokens, "read")).json()
    assert one["card"] == "HD Pro Webcam C920" and one["stream_url"] == RECORD["stream_url"]
    registered = [e for e in events.last(printops_db, 50) if e.kind == "camera.registered"]
    assert [(e.subject, e.actor) for e in registered] == [("camera:topcam", "topcam")]


def test_upsert_is_idempotent_and_keeps_first_seen(printops_client: Any, printops_tokens: dict[str, str], topcam: dict[str, str],
                                                   printops_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    h = topcam
    _clock(monkeypatch, "2026-10-16T10:00:00.000Z")
    first = printops_client.put("/v1/camera-nodes/topcam", json=RECORD, headers=h).json()
    _clock(monkeypatch, "2026-10-16T10:01:00.000Z")
    moved = {**RECORD, "url": "http://192.168.0.77:8080", "stream_url": None, "led": False}
    second = printops_client.put("/v1/camera-nodes/topcam", json=moved, headers=h).json()
    assert second["first_seen"] == first["first_seen"] == "2026-10-16T10:00:00.000Z"
    assert second["last_seen"] == "2026-10-16T10:01:00.000Z"
    assert second["url"] == "http://192.168.0.77:8080" and second["stream_url"] is None and second["led"] is False
    again = printops_client.put("/v1/camera-nodes/topcam", json=moved, headers=h).json()
    assert again == second
    items = printops_client.get("/v1/camera-nodes", headers=_h(printops_tokens, "read")).json()["items"]
    assert len(items) == 1
    assert sum(e.kind == "camera.registered" for e in events.last(printops_db, 50)) == 1


def test_online_flag_follows_last_seen_with_frozen_clock(printops_client: Any, printops_tokens: dict[str, str], topcam: dict[str, str],
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    _clock(monkeypatch, "2026-10-16T10:00:00.000Z")
    printops_client.put("/v1/camera-nodes/topcam", json=RECORD, headers=topcam)
    read = _h(printops_tokens, "read")
    _clock(monkeypatch, "2026-10-16T10:03:00.000Z")  # exactly ONLINE_S later: still online
    rec = printops_client.get("/v1/camera-nodes/topcam", headers=read).json()
    assert rec["online"] is True and rec["age_s"] == 180
    _clock(monkeypatch, "2026-10-16T10:03:00.500Z")
    rec = printops_client.get("/v1/camera-nodes", headers=read).json()["items"][0]
    assert rec["online"] is False and rec["age_s"] == 180.5


def test_auth_401_and_scopes_403(printops_client: Any, printops_tokens: dict[str, str], topcam: dict[str, str]) -> None:
    for method, path in (("put", "/v1/camera-nodes/topcam"), ("get", "/v1/camera-nodes"),
                         ("get", "/v1/camera-nodes/topcam"), ("delete", "/v1/camera-nodes/topcam")):
        kw: dict[str, Any] = {"json": RECORD} if method == "put" else {}
        assert getattr(printops_client, method)(path, **kw).status_code == 401
    cam = topcam
    assert printops_client.put("/v1/camera-nodes/topcam", json=RECORD, headers=cam).status_code == 200
    # the camera scope opens only the PUT
    assert printops_client.get("/v1/camera-nodes", headers=cam).status_code == 403
    assert printops_client.get("/v1/camera-nodes/topcam", headers=cam).status_code == 403
    assert printops_client.delete("/v1/camera-nodes/topcam", headers=cam).status_code == 403
    spools = printops_client.get("/v1/spools", headers=cam)
    assert spools.status_code == 403 and "lacks scope 'read'" in spools.json()["detail"]
    move = printops_client.post("/v1/spools/1/move", json={"to": "WH:default"}, headers=cam)
    assert move.status_code == 403 and "lacks scope 'write'" in move.json()["detail"]
    # a read-only token cannot register, a write token can (camera OR write)
    denied = printops_client.put("/v1/camera-nodes/topcam", json=RECORD, headers=_h(printops_tokens, "read"))
    assert denied.status_code == 403 and "'camera' or 'write'" in denied.json()["detail"]
    w = printops_client.put("/v1/camera-nodes/pc-c920", json=RECORD, headers=_h(printops_tokens, "write"))
    assert w.status_code == 200 and w.json()["updated_by"] == "test-write"


def test_camera_token_cannot_read_the_event_stream(printops_client: Any, printops_settings: Settings) -> None:
    """The WS path authenticates outside require(): it must refuse a camera-only token too."""
    from starlette.websockets import WebSocketDisconnect

    plain = tokens.create(printops_settings.tokens_file, "topcam", ("camera",))[0]
    with printops_client.websocket_connect("/v1/ws") as ws:
        ws.send_json({"type": "auth", "token": plain})
        assert ws.receive_json()["type"] == "auth_invalid"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 4403
    sse = printops_client.get("/v1/events", headers={"Authorization": f"Bearer {plain}",
                                                     "Accept": "application/json"})
    assert sse.status_code == 403


def test_camera_token_cannot_register_or_hijack_another_node(printops_client: Any, printops_tokens: dict[str, str],
                                                            topcam: dict[str, str]) -> None:
    """A stolen `topcam` card must not repoint `pc-c920` (or create any other name)."""
    w = _h(printops_tokens, "write")
    assert printops_client.put("/v1/camera-nodes/pc-c920", json=RECORD, headers=w).status_code == 200
    evil = {**RECORD, "url": "http://10.6.6.6:8080"}
    for name in ("pc-c920", "brand-new", "Topcam"):
        r = printops_client.put(f"/v1/camera-nodes/{name}", json=evil, headers=topcam)
        assert r.status_code == 403 and "may only register the camera node 'topcam'" in r.json()["detail"], r.text
    rec = printops_client.get("/v1/camera-nodes/pc-c920", headers=_h(printops_tokens, "read")).json()
    assert rec["url"] == RECORD["url"] and rec["updated_by"] == "test-write"
    names = [n["name"] for n in printops_client.get("/v1/camera-nodes", headers=_h(printops_tokens, "read")).json()["items"]]
    assert names == ["pc-c920"]


@pytest.mark.parametrize(("name", "body", "needle"), [
    ("topcam", {**RECORD, "url": "file:///etc/passwd"}, "http:// or https://"),
    ("topcam", {**RECORD, "url": "http://"}, "http:// or https://"),
    ("topcam", {**RECORD, "url": "http://[::1"}, "not a valid URL"),
    ("topcam", {**RECORD, "url": "http://192.168.0.50:abc"}, "not a valid URL"),
    ("topcam", {**RECORD, "stream_url": "http://192.168.0.50:99999/"}, "not a valid URL"),
    ("topcam", {**RECORD, "stream_url": "javascript:alert(1)"}, "stream_url"),
    ("topcam", {**RECORD, "url": 5}, "url must be"),
    ("topcam", {k: v for k, v in RECORD.items() if k != "url"}, "url is required"),
    ("topcam", {**RECORD, "led": "yes"}, "led must be"),
    ("topcam", {**RECORD, "card": ["C920"]}, "card must be"),
    ("topcam", {**RECORD, "formats": {"YUYV": [[2304]]}}, "formats must be"),
    ("topcam", {**RECORD, "formats": {"YUYV": [[True, 1]]}}, "formats must be"),
    ("topcam", {**RECORD, "colour": "red"}, "unknown field(s) colour"),
    ("topcam", {**RECORD, "card": "\x1b[2JC920"}, "printable chars"),
    ("topcam", {**RECORD, "version": "1.0\n"}, "printable chars"),
    ("topcam", {**RECORD, "hostname": "a\x9b31m"}, "printable chars"),
    ("topcam", {**RECORD, "url": "http://x/\x1b]52;c;"}, "url must be"),
    ("topcam", {**RECORD, "formats": {"YUYV": [[70000, 1]]}}, "formats must be"),
    ("topcam", {**RECORD, "formats": {"YUYV": [[1, 1]] * 65}}, "formats must be"),
    ("topcam", {**RECORD, "formats": {f"F{i}": [[1, 1]] for i in range(17)}}, "formats must be"),
])
def test_put_validates_name_and_body(printops_client: Any, printops_tokens: dict[str, str], topcam: dict[str, str], name: str,
                                     body: dict[str, Any], needle: str) -> None:
    r = printops_client.put(f"/v1/camera-nodes/{name}", json=body, headers=topcam)
    assert r.status_code == 422 and needle in r.json()["detail"], r.text


@pytest.mark.parametrize("name", ["bad name", "-lead", "x" * 65, "café"])
def test_put_rejects_bad_node_names(printops_client: Any, printops_tokens: dict[str, str], name: str) -> None:
    r = printops_client.put(f"/v1/camera-nodes/{urllib.parse.quote(name, safe='')}", json=RECORD,
                            headers=_h(printops_tokens, "write"))
    assert r.status_code == 422 and "camera node name" in r.json()["detail"], r.text


def test_formats_at_the_bounds_are_accepted(printops_client: Any, topcam: dict[str, str]) -> None:
    formats = {f"F{i}": [[65535, 1]] * 64 for i in range(16)}
    r = printops_client.put("/v1/camera-nodes/topcam", json={**RECORD, "formats": formats}, headers=topcam)
    assert r.status_code == 200 and r.json()["formats"] == formats


def test_get_and_delete_unknown_404_then_delete_204(printops_client: Any, printops_tokens: dict[str, str], topcam: dict[str, str]) -> None:
    rw = _h(printops_tokens, "rw")
    assert printops_client.get("/v1/camera-nodes/nope", headers=rw).status_code == 404
    assert printops_client.delete("/v1/camera-nodes/nope", headers=rw).status_code == 404
    printops_client.put("/v1/camera-nodes/topcam", json=RECORD, headers=topcam)
    assert printops_client.delete("/v1/camera-nodes/topcam", headers=_h(printops_tokens, "read")).status_code == 403
    gone = printops_client.delete("/v1/camera-nodes/topcam", headers=_h(printops_tokens, "write"))
    assert gone.status_code == 204 and gone.content == b""
    assert printops_client.get("/v1/camera-nodes", headers=rw).json() == {"items": []}


def test_lookup_url_resolves_through_a_live_api(live_api: LiveApi, monkeypatch: pytest.MonkeyPatch,
                                                 tmp_path: Path) -> None:
    """lookup_url over real HTTP: found → the registered url, 404 → None, no config → None."""
    cam = ApiClient(live_api.url, tokens.create(live_api.settings.tokens_file, "topcam", ("camera",))[0])
    cam.request("PUT", "/v1/camera-nodes/topcam", json_body=RECORD)
    monkeypatch.setenv("PRINTOPS_API", live_api.url)
    monkeypatch.setenv("PRINTOPS_TOKEN", live_api.tokens["read"])
    assert registry.lookup_url("topcam") == RECORD["url"]
    assert registry.lookup_url("absent") is None
    with pytest.raises(CameraError, match="403"):  # a camera-scope token cannot read records
        registry.lookup_url("topcam", token=live_api.tokens["camera"])
    monkeypatch.delenv("PRINTOPS_API")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-config"))
    assert registry.lookup_url("topcam") is None


def test_node_name_resolves_through_the_api_else_mdns(live_api: LiveApi, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--camera NAME`: topcam.client.node_url uses the registry record, else http://NAME.local:8080."""
    from topcam.client import node_url

    node = ApiClient(live_api.url, tokens.create(live_api.settings.tokens_file, "topcam", ("camera",))[0])
    node.request("PUT", "/v1/camera-nodes/topcam", json_body=RECORD)
    monkeypatch.setenv("PRINTOPS_API", live_api.url)
    monkeypatch.setenv("PRINTOPS_TOKEN", live_api.tokens["read"])
    assert node_url("topcam") == RECORD["url"]
    assert node_url("garage") == "http://garage.local:8080"
