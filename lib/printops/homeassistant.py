"""homeassistant.py — READ-ONLY Home Assistant REST client for PrintOps (sensor states).

ACCESSED VIA: `printops.drying` (dry-box / dryer humidity of a location's
`humidity_sensor_entity`). Only `GET /api/states/<entity_id>` is ever sent: PrintOps never
writes to Home Assistant through this module.

CONFIG (env of 3d-api): `PRINTOPS_HA_URL` (e.g. http://192.168.0.39:8123) and
`PRINTOPS_HA_TOKEN` (a long-lived access token; `PRINTOPS_HA_TOKEN_FILE` reads it from a file
instead). Unset URL → `from_env` returns None and humidity is simply not known.

INVARIANTS: stdlib only; a failed read raises HaError (callers degrade to "RH unknown"),
an `unavailable`/`unknown` state is HaError too, never a fake number.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from printops.client import NoRedirect

# Never follow a redirect: urllib would replay the HA bearer token to the new host.
_OPENER = urllib.request.build_opener(NoRedirect)

ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


class HaError(RuntimeError):
    """Home Assistant could not give a usable numeric state."""


@dataclass(frozen=True)
class Reading:
    entity_id: str
    value: float
    unit: str | None
    last_updated: str | None


class HaStates:
    def __init__(self, base_url: str, token: str, *, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> HaStates | None:
        env = os.environ if env is None else env
        url = env.get("PRINTOPS_HA_URL")
        if not url:
            return None
        token = env.get("PRINTOPS_HA_TOKEN")
        token_file = env.get("PRINTOPS_HA_TOKEN_FILE")
        if not token and token_file:
            token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        if not token:
            raise HaError(
                "PRINTOPS_HA_URL is set but PRINTOPS_HA_TOKEN / PRINTOPS_HA_TOKEN_FILE is not"
            )
        return cls(url, token)

    def numeric(self, entity_id: str) -> Reading:
        """The entity's state as a number (GET /api/states/<entity_id>)."""
        if not ENTITY_RE.fullmatch(entity_id):
            raise HaError(
                f"{entity_id!r} is not a Home Assistant entity id (domain.object_id)"
            )
        url = f"{self.base_url}/api/states/{urllib.parse.quote(entity_id)}"
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
        )
        try:
            with _OPENER.open(req, timeout=self.timeout) as resp:  # noqa: S310 — operator-configured URL
                body = json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            what = "not found" if e.code == 404 else f"HTTP {e.code}"
            raise HaError(f"Home Assistant {entity_id}: {what}") from e
        except (urllib.error.URLError, OSError) as e:
            raise HaError(
                f"Home Assistant at {self.base_url} is unreachable: {getattr(e, 'reason', e)}"
            ) from e
        except ValueError as e:
            raise HaError(f"Home Assistant {entity_id}: reply is not JSON") from e
        if not isinstance(body, dict):
            raise HaError(
                f"Home Assistant {entity_id}: unexpected reply {str(body)[:80]!r}"
            )
        state: object = body.get("state")
        if not isinstance(state, (str, int, float)) or isinstance(state, bool):
            raise HaError(
                f"Home Assistant {entity_id}: state {state!r} is not a number"
            )
        try:
            value = float(state)
        except ValueError:
            raise HaError(
                f"Home Assistant {entity_id}: state {state!r} is not a number"
            ) from None
        raw_attrs: object = body.get("attributes")
        attrs: dict[str, object] = raw_attrs if isinstance(raw_attrs, dict) else {}
        unit = attrs.get("unit_of_measurement")
        return Reading(
            entity_id,
            value,
            str(unit) if unit is not None else None,
            str(body["last_updated"]) if body.get("last_updated") else None,
        )

    def humidity(self, entity_id: str) -> Reading:
        """A relative-humidity reading in % (refuses other units and out-of-range values)."""
        reading = self.numeric(entity_id)
        if reading.unit not in (None, "%"):
            raise HaError(
                f"{entity_id} reports {reading.unit!r}, expected relative humidity in %"
            )
        if not 0 <= reading.value <= 100:
            raise HaError(
                f"{entity_id} = {reading.value} is not a relative humidity (0..100 %)"
            )
        return reading
