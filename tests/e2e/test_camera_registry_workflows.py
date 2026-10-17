"""E2E: `3d camera ls|show|rm` through bin/3d against a real uvicorn 3d-api (#341)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from printops_fixtures import LiveApi

from printops import db, tokens
from printops.client import ApiClient

from .workflow_helper import run_cli

NODE = {
    "url": "http://192.168.0.50:8080",
    "hostname": "topcam.local",
    "stream_url": "http://192.168.0.50:8081/stream",
    "card": "HD Pro Webcam C920",
    "device": "/dev/v4l/by-id/usb-046d_HD_Pro_Webcam_C920-video-index0",
    "formats": {"YUYV": [[2304, 1536], [1920, 1080]]},
    "led": True,
    "version": "1.0.0",
}


def _camera(
    tmp_path: Path, live: LiveApi, *argv: str, token: str = "rw"
) -> subprocess.CompletedProcess[str]:
    env = {"PRINTOPS_API": live.url, "PRINTOPS_TOKEN": live.tokens[token]}
    return run_cli(tmp_path, "camera", *argv, env_extra=env)


def test_user_lists_shows_and_removes_registered_camera_nodes(
    tmp_path: Path, live_api: LiveApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two nodes register with the Pi's camera-scope token (one went quiet in 2020); the user
    sees which is online, inspects the live one, removes the stale one."""

    def node(
        name: str,
    ) -> ApiClient:  # each node's own camera-scope token, named like it
        token = tokens.create(live_api.settings.tokens_file, name, ("camera",))[0]
        return ApiClient(live_api.url, token)

    with monkeypatch.context() as m:
        m.setattr(db, "utc_now", lambda: "2020-01-01T00:00:00.000Z")
        node("old-topcam").request(
            "PUT",
            "/v1/camera-nodes/old-topcam",
            json_body={"url": "http://192.168.0.9:8080", "version": "0.9.0"},
        )
    node("topcam").request("PUT", "/v1/camera-nodes/topcam", json_body=NODE)

    ls = _camera(tmp_path, live_api, "ls", token="read")
    assert ls.returncode == 0, ls.stderr
    lines = ls.stdout.splitlines()
    assert lines[0].split() == ["NAME", "STATE", "URL", "CARD", "VERSION"]
    assert re.match(
        r"old-topcam +offline \d+d +http://192\.168\.0\.9:8080 +- +0\.9\.0$", lines[1]
    ), lines[1]
    assert re.match(
        r"topcam +online \d+s +http://192\.168\.0\.50:8080 +HD Pro Webcam C920 +1\.0\.0$",
        lines[2],
    ), lines[2]

    as_json = json.loads(
        _camera(tmp_path, live_api, "ls", "--json", token="read").stdout
    )
    assert {n["name"]: n["online"] for n in as_json["items"]} == {
        "old-topcam": False,
        "topcam": True,
    }

    show = _camera(tmp_path, live_api, "show", "topcam", token="read")
    assert show.returncode == 0, show.stderr
    assert "url      http://192.168.0.50:8080" in show.stdout
    assert "stream   http://192.168.0.50:8081/stream" in show.stdout
    assert "formats  YUYV 2304x1536 1920x1080" in show.stdout
    assert "token    topcam" in show.stdout and "led      yes" in show.stdout
    shown = json.loads(
        _camera(tmp_path, live_api, "show", "topcam", "--json", token="read").stdout
    )
    assert shown["card"] == "HD Pro Webcam C920" and shown["last_addr"] == "127.0.0.1"

    denied = _camera(tmp_path, live_api, "rm", "old-topcam", token="read")
    assert denied.returncode == 1 and "lacks scope 'write'" in denied.stderr
    rm = _camera(tmp_path, live_api, "rm", "old-topcam", token="write")
    assert rm.returncode == 0 and rm.stdout.strip() == "removed camera node old-topcam"
    again = _camera(tmp_path, live_api, "rm", "old-topcam", token="write")
    assert again.returncode == 1 and "no camera node 'old-topcam'" in again.stderr
    missing = _camera(tmp_path, live_api, "show", "old-topcam", token="read")
    assert missing.returncode == 1 and "no camera node 'old-topcam'" in missing.stderr
    left = _camera(tmp_path, live_api, "ls", token="read")
    assert [ln.split()[0] for ln in left.stdout.splitlines()[1:]] == ["topcam"]


def test_ls_without_nodes_and_without_api_config(
    tmp_path: Path, live_api: LiveApi
) -> None:
    """The user lists cameras before any node registered, without an API config, with a token
    lacking the read scope, and with a missing name: each gets a clear message."""
    empty = _camera(tmp_path, live_api, "ls", token="read")
    assert empty.returncode == 0
    assert empty.stdout.startswith("no camera nodes registered")
    bare = run_cli(tmp_path, "camera", "ls")
    assert bare.returncode == 2
    assert "no 3d-api URL" in bare.stderr
    flags = run_cli(
        tmp_path,
        "camera",
        "ls",
        "--api",
        live_api.url,
        "--token",
        live_api.tokens["camera"],
    )
    assert flags.returncode == 1 and "lacks scope 'read'" in flags.stderr
    extra = run_cli(tmp_path, "camera", "show")
    assert extra.returncode == 2 and "takes 1 positional argument" in extra.stderr
