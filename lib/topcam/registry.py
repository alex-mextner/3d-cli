"""topcam.registry — camera-node records in 3d-api, as the CLI sees them.

WHAT: `lookup_url(name)` → the URL a node registered (`PUT /v1/camera-nodes/{name}`, sent by
  `topcam.node` every minute), or None when 3d-api is not configured or has no such node.
WHY: the Pi gets its address from DHCP; the record is how `--camera topcam` finds it.
INVARIANTS: stdlib at import; the API client is the shared `printops.client` (same config:
  --api/--token, PRINTOPS_API/PRINTOPS_TOKEN, ~/.config/3d-cli/printops.yaml).
"""

from __future__ import annotations

import sys
import urllib.parse

from topcam.client import CameraError


def lookup_url(
    name: str, *, api: str | None = None, token: str | None = None
) -> str | None:
    from printops.client import ApiClient, ApiConfigError, ApiError

    try:
        client = ApiClient.from_config(api=api, token=token)
    except ApiConfigError:
        return None
    try:
        record = client.get(f"/v1/camera-nodes/{urllib.parse.quote(name)}")
    except ApiError as exc:
        status = getattr(exc, "status", 0)
        if status == 404:
            return None
        if status == 0:
            print(f"camera {name}: 3d-api unreachable ({exc})", file=sys.stderr)
            return None
        raise CameraError(f"camera {name}: 3d-api: {exc}") from exc
    url = record.get("url") if isinstance(record, dict) else None
    return str(url) if url else None
