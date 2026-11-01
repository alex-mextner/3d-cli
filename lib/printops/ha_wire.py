"""ha_wire.py — the two WRITES `3d init --env` makes to Home Assistant, each only after the user said yes:
install the backup package, and add the Spoolman integration. Nothing here prompts; the caller asks.

ACCESSED VIA: `printops.env_stage`.

1. BACKUP PACKAGE (`install_package`): renders integrations/homeassistant/packages/printops_backup.yaml with
   the user's values (only the lines of its two EDIT HERE spots change), writes it to
   `<HA config>/packages/printops_backup.yaml` through `run` (local bash or ssh to the HA host), adds the
   `printops_api_auth` secret when the HA secrets.yaml lacks it, asks HA's own `check_config`
   (POST /api/config/core/check_config) and, only if that says valid, reloads script / automation /
   rest_command / input_datetime. Invalid -> the old file (or none) is put back and HA is not reloaded.
   HA is never restarted; a domain that is not loaded yet is reported as "needs a restart".
   A HA config managed by `3d ha deploy` (a 3d-cli submodule next to configuration.yaml) is REFUSED:
   deploy it there (`3d ha deploy`), a second copy would fight the symlink.

2. SPOOLMAN INTEGRATION (`wire_spoolman`): Home Assistant CORE has no Spoolman integration (checked in
   homeassistant/generated/config_flows.py of core dev, 2026-11). The one that exists is the community
   custom integration `spoolman` (github.com/Disane87/spoolman-homeassistant, installed with HACS, config-flow
   only, no YAML). If it is installed, the flow `POST /api/config/config_entries/flow` is driven with the
   Spoolman URL; if not, the exact install steps are returned and nothing is created.
"""

from __future__ import annotations

import base64
import contextlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from printops.backup_discovery import RunHost
from printops.ha_rest import HaRest, HaRestError

PACKAGE_REL = "integrations/homeassistant/packages/printops_backup.yaml"
PACKAGE_NAME = "printops_backup.yaml"
AUTH_SECRET_NAME = "printops_api_auth"
VALUE_KEYS = (
    "api_url",
    "target",
    "subdir",
    "keep_daily",
    "keep_weekly",
    "keep_monthly",
    "min_free_gb",
)
RELOADS = (
    "script.reload",
    "automation.reload",
    "rest_command.reload",
    "input_datetime.reload",
)
HACS_REPO = "Disane87/spoolman-homeassistant"
SPOOLMAN_DOMAIN = "spoolman"
SPOOLMAN_FLOW_DEFAULTS: dict[str, Any] = {
    "notification_threshold_info": 50,
    "notification_threshold_warning": 75,
    "notification_threshold_critical": 95,
    "show_archived": False,
    "update-interval": 15,
}


@dataclass
class WireOutcome:
    status: str  # done | unchanged | refused | needs-restart | needs-install | failed
    steps: list[str] = field(default_factory=list)
    detail: str = ""


# ------------------------------------------------------------------------------ rendering


def _fmt(value: object) -> str:
    return f'"{value}"' if isinstance(value, str) else str(value)


def _marker_index(lines: list[str], needle: str) -> int:
    return next(i for i, ln in enumerate(lines) if needle in ln)


def _set_value(lines: list[str], start: int, end: int, key: str, value: object) -> None:
    hit = [i for i in range(start + 1, end) if re.fullmatch(rf"\s+{key}: .*", lines[i])]
    if len(hit) != 1:
        raise ValueError(f"package has no single `{key}:` line in its EDIT HERE block")
    lines[hit[0]] = f"      {key}: {_fmt(value)}"


def render_package(
    text: str, values: Mapping[str, object], at: str = "04:00:00"
) -> str:
    """The package with `values` and the run time put into its EDIT HERE spots; nothing else changes."""
    lines = text.splitlines()
    start = _marker_index(lines, "EDIT HERE (1/2)")
    end = _marker_index(lines, "end of the editable values")
    for key in (k for k in VALUE_KEYS if k in values):
        _set_value(lines, start, end, key, values[key])
    marker = _marker_index(lines, "EDIT HERE (2/2)")
    at_idx = next(
        i for i in range(marker, len(lines)) if re.fullmatch(r'\s+at: ".*"', lines[i])
    )
    lines[at_idx] = f'        at: "{at}"'
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------- install


def _write_script(path: str, text: str) -> str:
    b64 = base64.b64encode(text.encode()).decode()
    return (
        f"set -e; mkdir -p \"$(dirname '{path}')\"; [ -f '{path}' ] && cp -p '{path}' '{path}.3d-bak'; "
        f"printf %s '{b64}' | base64 -d > '{path}.new' && mv '{path}.new' '{path}'"
    )


def _ensure_secret(
    run: RunHost, secrets: str, bearer: Callable[[], str]
) -> tuple[int, str]:
    """Add `printops_api_auth` to secrets.yaml unless it is there; the token is minted only when it is needed."""
    rc, _ = run(f"touch '{secrets}' && grep -q '^{AUTH_SECRET_NAME}:' '{secrets}'")
    if rc == 0:
        return 0, ""
    line = f'{AUTH_SECRET_NAME}: "Bearer {bearer()}"'
    return run(f"printf '\\n%s\\n' '{line}' >> '{secrets}'")


def plan_install(ha_dir: str, values: Mapping[str, object], at: str) -> list[str]:
    """What install_package will do — shown to the user BEFORE they confirm."""
    return [
        f"write {ha_dir}/packages/{PACKAGE_NAME} (target={values.get('target')}, subdir={values.get('subdir')}, "
        f"keep {values.get('keep_daily')}/{values.get('keep_weekly')}/{values.get('keep_monthly')}, at {at}, "
        f"3d-api {values.get('api_url')}); a previous file is kept as .3d-bak",
        f"add `{AUTH_SECRET_NAME}` to {ha_dir}/secrets.yaml if it is missing (a 3d-api token)",
        "ask Home Assistant to check_config; put everything back if it is not valid",
        f"reload {', '.join(r.split('.')[0] for r in RELOADS)} through the HA API (HA is not restarted)",
    ]


def _refuse_reason(run: RunHost, ha_dir: str) -> str:
    rc, out = run(
        f"test -f '{ha_dir}/configuration.yaml' && echo cfg; "
        f"test -f '{ha_dir}/3d-cli/integrations/homeassistant/manifest.yaml' && echo managed; "
        f"grep -Eq '^[[:space:]]*packages:' '{ha_dir}/configuration.yaml' && echo packages; true"
    )
    flags = set(out.split())
    if rc != 0 or "cfg" not in flags:
        return f"{ha_dir}/configuration.yaml not found on the HA host"
    if "managed" in flags:
        return f"{ha_dir} is managed by `3d ha deploy` (3d-cli submodule): deploy the package there with `3d ha deploy`"
    if "packages" not in flags:
        return (
            f"{ha_dir}/configuration.yaml has no `packages:` entry; add under `homeassistant:` "
            "`packages: !include_dir_named packages` first (3d does not edit configuration.yaml)"
        )
    return ""


def _reload(ha: HaRest) -> list[str]:
    missing = []
    for service in RELOADS:
        domain, name = service.split(".")
        try:
            ha.call_service(domain, name, {})
        except HaRestError as e:
            if e.status not in (400, 404):
                raise
            missing.append(domain)
    return missing


def _restore(run: RunHost, path: str) -> None:
    run(
        f"if [ -f '{path}.3d-bak' ]; then mv '{path}.3d-bak' '{path}'; else rm -f '{path}'; fi"
    )


def install_package(
    run: RunHost,
    ha: HaRest,
    ha_dir: str,
    repo_root: Path,
    values: Mapping[str, object],
    bearer: Callable[[], str],
    at: str = "04:00:00",
) -> WireOutcome:
    steps = plan_install(ha_dir, values, at)
    reason = _refuse_reason(run, ha_dir)
    if reason:
        return WireOutcome("refused", steps, reason)
    text = render_package(
        (repo_root / PACKAGE_REL).read_text(encoding="utf-8"), values, at
    )
    path = f"{ha_dir}/packages/{PACKAGE_NAME}"
    rc, current = run(f"cat '{path}' 2>/dev/null")
    if rc == 0 and current == text:
        return WireOutcome("unchanged", steps, "the same package is already installed")
    rc, out = run(_write_script(path, text))
    if rc != 0:
        return WireOutcome("failed", steps, f"could not write {path}: {out.strip()}")
    rc, out = _ensure_secret(run, f"{ha_dir}/secrets.yaml", bearer)
    if rc != 0:
        _restore(run, path)
        return WireOutcome(
            "failed", steps, f"could not update secrets.yaml: {out.strip()}"
        )
    try:
        verdict = ha.post("/api/config/core/check_config")
        if not isinstance(verdict, dict) or verdict.get("result") != "valid":
            _restore(run, path)
            return WireOutcome(
                "failed",
                steps,
                f"HA check_config rejected the package, put back: {verdict}",
            )
        missing = _reload(ha)
    except HaRestError as e:
        return WireOutcome(
            "failed", steps, f"package written, HA API failed: {e.message}"
        )
    if missing:
        return WireOutcome(
            "needs-restart",
            steps,
            "written and valid; restart Home Assistant once to load: "
            + ", ".join(missing),
        )
    return WireOutcome(
        "done",
        steps,
        f"installed; HA reloaded {', '.join(r.split('.')[0] for r in RELOADS)}",
    )


# ----------------------------------------------------------------------- Spoolman integration


def install_help() -> list[str]:
    return [
        "Home Assistant core ships no Spoolman integration; the community one is "
        + HACS_REPO
        + " (HACS, no YAML):",
        f"  1. HACS -> Integrations -> Custom repositories -> add https://github.com/{HACS_REPO} (Integration)",
        "  2. install 'Spoolman' and restart Home Assistant once (3d never restarts it)",
        "  3. run `3d init --env --no-input --ha-wire yes` again, or add it in Settings -> Devices & services",
    ]


def spoolman_entries(ha: HaRest) -> list[dict[str, Any]]:
    entries = ha.get(f"/api/config/config_entries/entry?domain={SPOOLMAN_DOMAIN}")
    if not isinstance(entries, list):
        return []
    return [
        e for e in entries if isinstance(e, dict) and e.get("domain") == SPOOLMAN_DOMAIN
    ]


def wire_spoolman(ha: HaRest, spoolman_url: str) -> WireOutcome:
    """Create the Spoolman config entry through HA's config-flow API. Idempotent: an entry that exists is left."""
    if spoolman_entries(ha):
        return WireOutcome(
            "unchanged",
            detail="a Spoolman integration is already configured in Home Assistant",
        )
    try:
        flow = ha.post(
            "/api/config/config_entries/flow",
            {"handler": SPOOLMAN_DOMAIN, "show_advanced_options": False},
        )
    except HaRestError as e:
        if e.status in (400, 404):
            return WireOutcome(
                "needs-install",
                install_help(),
                "the Spoolman integration is not installed in HA",
            )
        return WireOutcome("failed", detail=e.message)
    flow_path = f"/api/config/config_entries/flow/{flow['flow_id']}"
    try:
        done = ha.post(
            flow_path, {"spoolman_url": spoolman_url, **SPOOLMAN_FLOW_DEFAULTS}
        )
    except HaRestError as e:
        return WireOutcome("failed", detail=e.message)
    if done.get("type") == "create_entry":
        return WireOutcome(
            "done", detail=f"Spoolman integration created for {spoolman_url}"
        )
    with contextlib.suppress(HaRestError):
        ha.request("DELETE", flow_path)
    return WireOutcome(
        "failed",
        detail=f"Home Assistant could not use {spoolman_url}: {done.get('errors') or done}",
    )
