"""ha_wire.py — the two WRITES `3d init --env` makes to Home Assistant, each only after the user said yes:
install the backup package, and add the Spoolman integration. Nothing here prompts; the caller asks.

ACCESSED VIA: `printops.env_stage`.

1. BACKUP PACKAGE (`install_package`): renders integrations/homeassistant/packages/printops_backup.yaml with
   the user's values (only the lines of its EDIT HERE spots change; the 3d-api URL goes into the
   rest_command's own `url:`, never into service data, so a caller cannot redirect the bearer token), makes
   sure the `printops_api_auth` secret exists (minted BEFORE anything is written, so a failed mint leaves
   Home Assistant untouched), writes it to `<HA config>/packages/printops_backup.yaml` through `run` (local
   bash or ssh to the HA host), asks HA's own `check_config` (POST /api/config/core/check_config) and, only
   if that says valid, reloads script / automation / rest_command / input_datetime and proves the package
   loaded (`script.printops_backup_run` exists). Invalid -> the old file (or none) is put back and HA is not
   reloaded. HA is never restarted; a domain that is not loaded yet is reported as "needs a restart".
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
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from printops.backup_discovery import RunHost, Shell
from printops.ha_rest import HaRest, HaRestError

PACKAGE_REL = "integrations/homeassistant/packages/printops_backup.yaml"
PACKAGE_NAME = "printops_backup.yaml"
AUTH_SECRET_NAME = "printops_api_auth"
URL_KEY = "api_url"  # rendered into the rest_command's `url:`, not into the script's variables
VALUE_KEYS = (
    "target",
    "subdir",
    "keep_daily",
    "keep_weekly",
    "keep_monthly",
    "min_free_gb",
)
LOADED_ENTITY = "script.printops_backup_run"
CHECK_CONFIG_TIMEOUT_S = (
    180.0  # check_config on a Pi or an Odroid takes far longer than a normal API call
)
PACKAGES_INCLUDE = re.compile(
    r"""^\s*packages:\s*!include_dir_named\s+["']?(?P<dir>[^"'\s#]+)["']?\s*(?:#.*)?$"""
)
URL_SAFE = re.compile(r"https?://(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?/?")
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


def _set_url(lines: list[str], api_url: str) -> None:
    if not URL_SAFE.fullmatch(api_url):
        raise ValueError(f"3d-api URL {api_url!r}: http(s)://HOST[:PORT] only")
    marker = _marker_index(lines, "EDIT HERE (url)")
    hit = next(
        (
            i
            for i in range(marker + 1, len(lines))
            if re.fullmatch(r'\s+url: ".*"', lines[i])
        ),
        None,
    )
    if hit is None:
        raise ValueError("package has no `url:` line after its EDIT HERE (url) marker")
    lines[hit] = f'    url: "{api_url.rstrip("/")}/v1/backup/run"'


def render_package(
    text: str, values: Mapping[str, object], at: str = "04:00:00"
) -> str:
    """The package with `values` and the run time put into its EDIT HERE spots; nothing else changes."""
    lines = text.splitlines()
    start = _marker_index(lines, "EDIT HERE (1/2)")
    end = _marker_index(lines, "end of the editable values")
    for key in (k for k in VALUE_KEYS if k in values):
        _set_value(lines, start, end, key, values[key])
    if URL_KEY in values:
        _set_url(lines, str(values[URL_KEY]))
    marker = _marker_index(lines, "EDIT HERE (2/2)")
    at_idx = next(
        i for i in range(marker, len(lines)) if re.fullmatch(r'\s+at: ".*"', lines[i])
    )
    lines[at_idx] = f'        at: "{at}"'
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------- install


def _write_script(path: str, text: str) -> str:
    b64 = base64.b64encode(text.encode()).decode()
    q, new, bak = (
        shlex.quote(path),
        shlex.quote(path + ".new"),
        shlex.quote(path + ".3d-bak"),
    )
    return (
        f'set -e; mkdir -p "$(dirname {q})"; '
        f"if [ -f {q} ]; then cp -p {q} {bak}; else rm -f {bak}; fi; "
        f"printf %s '{b64}' | base64 -d > {new} && mv {new} {q}"
    )


def _ensure_secret(
    run: Shell, secrets: str, bearer: Callable[[], str]
) -> tuple[int, str]:
    """Add `printops_api_auth` to secrets.yaml unless it is there; the token is minted only when it is needed.

    The bearer token travels on the script's STDIN (not in argv, where `ps` and ssh logs would show it) and
    is appended under `umask 077`: a secrets.yaml that did not exist is created 0600, never briefly 0644 (an
    existing one keeps the mode its owner gave it)."""
    q = shlex.quote(secrets)
    rc, _ = run(f"[ -f {q} ] && grep -q '^{AUTH_SECRET_NAME}:' {q}")
    if rc == 0:
        return 0, ""
    line = f'{AUTH_SECRET_NAME}: "Bearer {bearer()}"'
    return run(f"umask 077; cat >> {q}", f"\n{line}\n")


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
    cfg = shlex.quote(f"{ha_dir}/configuration.yaml")
    managed = shlex.quote(f"{ha_dir}/3d-cli/integrations/homeassistant/manifest.yaml")
    rc, out = run(
        f"test -f {cfg} && echo cfg; test -f {managed} && echo managed; "
        f"grep -E '^[[:space:]]*packages:' {cfg} | sed 's/^/packages-line: /'; true"
    )
    lines = out.splitlines()
    flags = {ln.strip() for ln in lines if not ln.startswith("packages-line: ")}
    includes = [
        ln.removeprefix("packages-line: ")
        for ln in lines
        if ln.startswith("packages-line: ")
    ]
    if rc != 0 or "cfg" not in flags:
        return f"{ha_dir}/configuration.yaml not found on the HA host"
    if "managed" in flags:
        return f"{ha_dir} is managed by `3d ha deploy` (3d-cli submodule): deploy the package there with `3d ha deploy`"
    if not includes:
        return (
            f"{ha_dir}/configuration.yaml has no `packages:` entry; add under `homeassistant:` "
            "`packages: !include_dir_named packages` first (3d does not edit configuration.yaml)"
        )
    if not any(_loads_packages_dir(line, ha_dir) for line in includes):
        return (
            f"{ha_dir}/configuration.yaml has `{includes[0].strip()}`, which does not load {ha_dir}/packages/ "
            "(where 3d writes the file); make it `packages: !include_dir_named packages` or put "
            f"{PACKAGE_REL} where your `packages:` points (3d does not edit configuration.yaml)"
        )
    return ""


def _loads_packages_dir(line: str, ha_dir: str) -> bool:
    """True when this `packages:` line is `!include_dir_named` of `<ha_dir>/packages` (relative or absolute)."""
    m = PACKAGES_INCLUDE.match(line)
    if m is None:
        return False
    target = m["dir"].removeprefix("./").rstrip("/")
    return target in ("packages", f"{ha_dir.rstrip('/')}/packages")


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
    q, bak = shlex.quote(path), shlex.quote(path + ".3d-bak")
    run(f"if [ -f {bak} ]; then mv {bak} {q}; else rm -f {q}; fi")


def _package_loaded(ha: HaRest) -> bool:
    try:
        ha.get(f"/api/states/{LOADED_ENTITY}")
    except HaRestError as e:
        if e.status == 404:
            return False
        raise
    return True


def _activate(ha: HaRest) -> tuple[str, str]:
    """check_config, the targeted reloads, then the proof the package loaded. (status, detail): `valid`,
    `invalid`, `not-loaded` or `api-failed`."""
    try:
        verdict = ha.post(
            "/api/config/core/check_config", timeout=CHECK_CONFIG_TIMEOUT_S
        )
        if not isinstance(verdict, dict) or verdict.get("result") != "valid":
            return "invalid", f"HA check_config rejected the package: {verdict}"
        missing = _reload(ha)
        if not missing and not _package_loaded(ha):
            return "not-loaded", LOADED_ENTITY
    except HaRestError as e:
        return "api-failed", f"HA API failed: {e.message}"
    return "valid", ", ".join(missing)


def _put_package(run: Shell, path: str, text: str) -> tuple[bool, str]:
    """Write the package when it differs. (wrote, problem)."""
    rc, current = run(f"cat {shlex.quote(path)} 2>/dev/null")
    if rc == 0 and current == text:
        return False, ""
    rc, out = run(_write_script(path, text))
    if rc != 0:
        return False, f"could not write {path}: {out.strip()}"
    return True, ""


def _outcome(
    run: Shell, steps: list[str], path: str, wrote: bool, state: str, detail: str
) -> WireOutcome:
    if state == "invalid":
        if wrote:
            _restore(run, path)
        left = f"; the package already on disk is left in place (fix or remove {path})"
        return WireOutcome("failed", steps, detail + (", put back" if wrote else left))
    if state == "not-loaded":
        return WireOutcome(
            "failed",
            steps,
            f"HA validated and reloaded, but {detail} does not exist: Home Assistant does not load {path}. "
            "Make configuration.yaml load it (`homeassistant: packages: !include_dir_named packages`), "
            "then run again",
        )
    if state == "api-failed":
        return WireOutcome(
            "failed",
            steps,
            f"package on disk but not activated ({detail}); run again to retry",
        )
    if detail:
        return WireOutcome(
            "needs-restart",
            steps,
            "written and valid; restart Home Assistant once to load: " + detail,
        )
    reloaded = ", ".join(r.split(".")[0] for r in RELOADS)
    if wrote:
        return WireOutcome("done", steps, f"installed; HA reloaded {reloaded}")
    return WireOutcome(
        "unchanged",
        steps,
        f"the same package was already installed; HA validated and reloaded {reloaded}",
    )


def install_package(
    run: Shell,
    ha: HaRest,
    ha_dir: str,
    repo_root: Path,
    values: Mapping[str, object],
    bearer: Callable[[], str],
    at: str = "04:00:00",
) -> WireOutcome:
    """Write (when it differs), then ALWAYS activate: a file already on disk proves nothing about whether HA
    loaded it (a previous run may have died between the write and check_config/reload), so an identical
    package is validated and reloaded again — `unchanged` means "was there and is active now"."""
    steps = plan_install(ha_dir, values, at)
    reason = _refuse_reason(run, ha_dir)
    if reason:
        return WireOutcome("refused", steps, reason)
    text = render_package(
        (repo_root / PACKAGE_REL).read_text(encoding="utf-8"), values, at
    )
    path = f"{ha_dir}/packages/{PACKAGE_NAME}"
    rc, out = _ensure_secret(
        run, f"{ha_dir}/secrets.yaml", bearer
    )  # before any write: a failed mint changes nothing
    if rc != 0:
        return WireOutcome(
            "failed", steps, f"could not update secrets.yaml: {out.strip()}"
        )
    wrote, problem = _put_package(run, path, text)
    if problem:
        return WireOutcome("failed", steps, problem)
    try:
        state, detail = _activate(ha)
        return _outcome(run, steps, path, wrote, state, detail)
    except BaseException:
        if wrote:
            _restore(run, path)  # never leave a package behind that nobody validated
        raise


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
