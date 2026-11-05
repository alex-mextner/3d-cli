"""ha_locations.py — where Home Assistant ITSELF keeps its backups (a hint for choosing a backup target).

ACCESSED VIA: `printops.env_stage` -> `printops.backup_discovery.discover_backup_targets(extra=...)`.

WHAT: one read-only session on HA's WebSocket API (`ws://HOST:8123/api/websocket`, the long-lived token):
  backup/agents/info   the backup agents (`backup.local` = <config>/backups, NAS/cloud agents)
  backup/config/info   the schedule/retention HA already runs
  supervisor/api       GET /mounts (HA OS / Supervised only): the network shares HA uses as backup storage
Each answer becomes an informational `ha` candidate — never selectable: the Spoolman host needs its own
mount of such a share, which 3d does not create. A command HA does not know (older HA, no Supervisor) is skipped.

INVARIANTS: only `get`-style commands are sent; `ws://` only (the stdlib client has no TLS); any failure
returns what was read so far, never an exception.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from printer_ops.ws import WebSocket, WebSocketError

from printops.backup_discovery import Candidate


def _command(ws: WebSocket, msg_id: int, payload: dict[str, Any]) -> Any:
    ws.send_text(json.dumps({"id": msg_id, **payload}))
    for _ in range(20):
        raw = ws.recv(5.0)
        if raw is None:
            return None
        msg = json.loads(raw)
        if msg.get("id") == msg_id:
            return msg.get("result") if msg.get("success") else None
    return None


def _session(url: str, token: str) -> WebSocket | None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        return None  # https: the stdlib client cannot do TLS; reported by the caller
    ws = WebSocket.connect(
        parts.hostname, parts.port or 80, "/api/websocket", timeout=5.0
    )
    try:
        json.loads(ws.recv(5.0) or "{}")  # auth_required
        ws.send_text(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(ws.recv(5.0) or "{}").get("type") != "auth_ok":
            ws.close()
            return None
    except (WebSocketError, ValueError):
        ws.close()
        return None
    return ws


def ha_backup_locations(url: str, token: str) -> list[Candidate]:
    try:
        ws = _session(url, token)
    except (WebSocketError, OSError):
        return []
    if ws is None:
        return []
    out: list[Candidate] = []
    try:
        agents = _command(ws, 1, {"type": "backup/agents/info"}) or {}
        config = _command(ws, 2, {"type": "backup/config/info"}) or {}
        mounts = (
            _command(
                ws,
                3,
                {"type": "supervisor/api", "endpoint": "/mounts", "method": "get"},
            )
            or {}
        )
    except (WebSocketError, OSError, ValueError):
        return out
    finally:
        ws.close()
    schedule = ((config.get("config") or {}).get("schedule") or {}).get(
        "recurrence", "no schedule"
    )
    for agent in agents.get("agents", []):
        name = str(agent.get("agent_id", "?"))
        where = (
            "<HA config>/backups on the HA host"
            if name == "backup.local"
            else "an HA backup agent"
        )
        out.append(
            Candidate(
                "ha",
                "",
                label=name,
                notes=[
                    f"Home Assistant stores its own backups here ({where}); "
                    f"its schedule: {schedule}"
                ],
            )
        )
    out.extend(
        Candidate(
            "ha",
            "",
            fstype=str(m.get("type", "")),
            label=str(m.get("name", "")),
            notes=[
                f"network share HA uses for backups: {m.get('server')}:{m.get('share', m.get('path', ''))} "
                f"({m.get('state', '?')}); mount the same share on the Spoolman host to reuse it"
            ],
        )
        for m in mounts.get("mounts") or []
        if m.get("usage") == "backup"
    )
    return out
