"""env_stage.py — the BACKUP + HOME ASSISTANT stage of `3d init --env`, run after Spoolman/3d-api answered.

ACCESSED VIA: `printops.env_setup` (`3d init --env`) calls `parse_stage_options`, then `run_stage` once the stack is
healthy; `3d printops backup discover` uses `printops.backup_setup` directly.

FLOW (every step prints what it will do; every write is behind a yes the user typed, and `--no-input` writes
only what a flag names):
  1. backup target: read-only discovery on the Spoolman host -> FOUND / MANUAL / AGENT / skip (nothing pre-selected)
     -> confirm -> `configure_backup` (bind /backup, marker, selftest).
  2. Home Assistant: read-only detection (configured URL, mDNS, homeassistant.local:8123, the Spoolman host)
     -> if found: ask "wire it in?" -> declined = NOTHING is written anywhere.
     -> token (env / ~/.config/3d-cli/ha.yaml mode 600 / prompt) -> Spoolman integration via HA's config flow
     -> backup schedule package (only when step 1 passed) into the HA config dir -> check_config -> reloads.
  Exit code 0 unless a step the user asked for failed (1).

FLAGS (added to `3d init --env`): --backup auto|skip|agent|path:DIR, --backup-disk-gb N, --allow-same-disk,
  --ha auto|skip|URL, --ha-wire ask|yes|no, --ha-dir DIR, --ha-ssh USER@HOST, --keep SPEC, --backup-at HH:MM
"""

from __future__ import annotations

import getpass
import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from errors import InvalidArgument, ThreeDError

from printops import backup_setup, ha_detect, ha_locations, ha_wire
from printops.backup_discovery import Candidate, Discovery, Shell
from printops.backup_policy import parse_keep
from printops.backup_setup import SUBDIR, TARGET, BackupContext
from printops.ha_rest import HaRest, HaRestError

CLI_NAME = "init --env"
STAGE_VALUE_FLAGS = {
    "--backup",
    "--backup-disk-gb",
    "--ha",
    "--ha-wire",
    "--ha-dir",
    "--ha-ssh",
    "--keep",
    "--backup-at",
}
STAGE_BOOL_FLAGS = {"--allow-same-disk"}
STAGE_USAGE = """  --backup WHAT        backups of Spoolman + PrintOps: auto (menu / print what was found), skip, agent (print a
                       hand-off prompt for an AI agent or yourself), path:DIR (a directory on the Spoolman host)
  --backup-disk-gb N   size hint (default 512) used only to mark a found disk as LIKELY; nothing is chosen by it
  --allow-same-disk    accept a backup directory on the same disk as the Spoolman data (it is not a backup)
  --ha WHAT            Home Assistant: auto (detect, ask), skip, or URL (e.g. http://homeassistant.local:8123); the token
                       goes only to a URL you named (--ha URL, $HA_URL, ha.yaml url:) or approved, never to a discovered one
  --ha-wire ask|yes|no add the Spoolman integration + the 04:00 backup package to HA (ask = confirm; --no-input: yes needed)
  --ha-dir DIR         HA config dir (where configuration.yaml is) for the package;  --ha-ssh USER@HOST  if it is on another host
  --keep SPEC          retention for the HA package, default daily=7,weekly=4,monthly=6;  --backup-at HH:MM  default 04:00"""


@dataclass
class StageOptions:
    backup: str = "auto"
    hint_gb: int = 512
    allow_same_disk: bool = False
    ha: str = "auto"
    ha_wire: str = "ask"
    ha_dir: str | None = None
    ha_ssh: str | None = None
    keep: str = "daily=7,weekly=4,monthly=6"
    at: str = "04:00"


@dataclass
class StageEnv:
    """Everything the stage needs from `3d init --env`; every side effect is injectable (tests use fakes)."""

    ctx: BackupContext
    repo_root: Path
    env: Mapping[str, str]
    api_url_for_ha: str  # 3d-api as Home Assistant sees it
    spoolman_url_for_ha: str
    mint_ha_token: Callable[
        [], str
    ]  # a 3d-api token (scopes read,write) for Home Assistant
    host_names: Sequence[str] = ()  # the Spoolman host name/IP: HA often runs there too
    fetch: ha_detect.Fetch = ha_detect.http_get
    mdns: Callable[[], list[str]] = ha_detect.mdns_browse
    ha_shell: Callable[[str | None], Shell] = lambda host: (
        backup_setup.ssh_shell(host) if host else backup_setup.local_shell()
    )
    ha_factory: Callable[[str, str], HaRest] = lambda url, token: HaRest(url, token)
    ask_secret: Callable[[str], str] = (
        getpass.getpass
    )  # the HA token is never echoed to the terminal


def _is_http_url(value: str) -> bool:
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def parse_stage_options(values: Mapping[str, str], flags: set[str]) -> StageOptions:
    opts = StageOptions(
        backup=values.get("--backup", "auto"),
        ha=values.get("--ha", "auto"),
        ha_wire=values.get("--ha-wire", "ask"),
        ha_dir=values.get("--ha-dir"),
        ha_ssh=values.get("--ha-ssh"),
        keep=values.get("--keep", "daily=7,weekly=4,monthly=6"),
        at=values.get("--backup-at", "04:00"),
        allow_same_disk="--allow-same-disk" in flags,
    )
    if not (
        opts.backup in ("auto", "skip", "agent")
        or opts.backup.startswith("path:")
        and len(opts.backup) > 5
    ):
        raise InvalidArgument(
            "--backup",
            opts.backup,
            ["auto", "skip", "agent", "path:DIR"],
            command=CLI_NAME,
        )
    if opts.ha not in ("auto", "skip") and not _is_http_url(opts.ha):
        raise InvalidArgument(
            "--ha", opts.ha, ["auto", "skip", "http(s)://HOST[:PORT]"], command=CLI_NAME
        )
    if opts.ha_wire not in ("ask", "yes", "no"):
        raise InvalidArgument(
            "--ha-wire", opts.ha_wire, ["ask", "yes", "no"], command=CLI_NAME
        )
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", opts.at):
        raise InvalidArgument("--backup-at", opts.at, ["HH:MM, 24 h"], command=CLI_NAME)
    if "--backup-disk-gb" in values:
        if (
            not values["--backup-disk-gb"].isdigit()
            or int(values["--backup-disk-gb"]) < 1
        ):
            raise InvalidArgument(
                "--backup-disk-gb",
                values["--backup-disk-gb"],
                ["a positive integer"],
                command=CLI_NAME,
            )
        opts.hint_gb = int(values["--backup-disk-gb"])
    parse_keep(opts.keep)  # validates early
    return opts


# ---------------------------------------------------------------------------------- backup


def _yes(ctx: BackupContext, question: str) -> bool:
    return ctx.interactive and ctx.ask(f"{question} [y/N] ").strip().lower() in (
        "y",
        "yes",
    )


def _same_disk(ctx: BackupContext, path: str) -> bool:
    """`path` and the Spoolman data sit on the same filesystem device OR on partitions/volumes of one
    physical disk (lsblk -s walks a device up to its `disk` ancestors); a copy there dies with the data."""
    q = shlex.quote
    _, out = ctx.prober()(
        "dev() { timeout -k 1 10 df -P \"$1\" | tail -n 1 | cut -d' ' -f1; }\n"
        'disks() { lsblk -s -n -r -o NAME,TYPE "$1" 2>/dev/null | awk \'$2 == "disk" { print $1 }\'; }\n'
        f'a="$(dev {q(path)})"; b="$(dev {q(ctx.data_dir)})"\n'
        'if [ -n "$a" ] && [ "$a" = "$b" ]; then echo same; else\n'
        '  for x in $(disks "$a"); do for y in $(disks "$b"); do [ "$x" = "$y" ] && echo same; done; done\n'
        "fi"
    )
    return (
        "same" in out
    )  # the script's exit code is that of its last comparison: not a verdict


def _menu_choice(ctx: BackupContext, found: Discovery) -> str | None:
    """The user's answer: a host path, 'agent', or None (skip). Never a default."""
    pickable = backup_setup.show(ctx, found)
    while True:
        ans = (
            ctx.ask(
                "Where should the backups go?  number = a found location, m = type a directory, "
                "a = agent hand-off prompt, s = skip: "
            )
            .strip()
            .lower()
        )
        if ans == "s":
            return None
        if ans == "a":
            return "agent"
        if ans == "m":
            typed = ctx.ask(
                f"Directory on {ctx.where} (the mounted backup disk): "
            ).strip()
            if typed:
                return typed
        elif ans.isdigit() and 1 <= int(ans) <= len(pickable):
            return pickable[int(ans) - 1].path
        ctx.say("Not a choice; pick again (or s to skip).")


def _pick(ctx: BackupContext, opts: StageOptions, found: Discovery) -> str | None:
    if opts.backup == "skip":
        return None
    if opts.backup == "agent":
        return "agent"
    if opts.backup.startswith("path:"):
        return opts.backup[5:]
    if ctx.interactive:
        return _menu_choice(ctx, found)
    backup_setup.show(ctx, found)
    ctx.say(
        "--no-input chooses nothing. Re-run with --backup path:DIR (a mounted disk above), --backup agent "
        "(hand-off prompt) or --backup skip."
    )
    return None


def backup_stage(
    ctx: BackupContext, opts: StageOptions
) -> backup_setup.Configured | None:
    """Discovery -> choice -> confirmation -> configure. None = nothing configured (skipped / declined / agent)."""
    if opts.backup == "skip":
        ctx.say(
            "backups: skipped (3d printops backup target add ... when you have a disk)"
        )
        return None
    found = backup_setup.discover(ctx)
    choice = _pick(ctx, opts, found)
    if choice is None:
        return None
    if choice == "agent":
        ctx.say(
            "\n----- hand this to an AI agent (or follow it yourself) -----\n"
            + backup_setup.handoff_prompt(ctx, found)
            + "\n----- end of hand-off prompt -----"
        )
        return None
    if _same_disk(ctx, choice) and not opts.allow_same_disk:
        ctx.say(
            f"{choice} is on the same disk as the Spoolman data ({ctx.data_dir}): that is not a backup. "
            "Pick another disk (or --allow-same-disk)."
        )
        return backup_setup.Configured(False, choice, "same disk as the data")
    if ctx.interactive and not _yes(
        ctx,
        f"Use {choice} on {ctx.where} as the backup disk? This writes the marker file "
        f"{choice}/.3d-backup-disk and recreates the 3d-api container (not Spoolman) "
        "with it mounted.",
    ):
        ctx.say("backups: not configured (declined); nothing was written")
        return None
    return backup_setup.configure_backup(ctx, choice)


# ------------------------------------------------------------------------------ home assistant


def _ha_url(se: StageEnv, opts: StageOptions) -> str | None:
    ctx = se.ctx
    if opts.ha == "skip":
        return None
    if opts.ha not in ("auto",):
        return opts.ha.rstrip("/")
    ctx.say(
        "Looking for Home Assistant (read-only: mDNS, configured URL, homeassistant.local, the Spoolman host) ..."
    )
    found = ha_detect.detect_ha(
        env=se.env, hosts=list(se.host_names), fetch=se.fetch, mdns=se.mdns
    )
    if not found:
        ctx.say("Home Assistant: none found (pass --ha URL if it is somewhere else)")
        return None
    for i, f in enumerate(found, 1):
        ctx.say(f"  {i}) {f.url}  (found via {f.via})")
    if not ctx.interactive:
        return found[0].url if len(found) == 1 else None
    ans = ctx.ask("Which one is yours? number, or Enter for none: ").strip()
    return (
        found[int(ans) - 1].url
        if ans.isdigit() and 1 <= int(ans) <= len(found)
        else None
    )


def _trusted_origin(se: StageEnv, opts: StageOptions, url: str) -> bool:
    """The user NAMED this Home Assistant (`--ha URL`, $HA_URL / $PRINTOPS_HA_URL, `url:` in ha.yaml). A URL that
    was only discovered (mDNS, homeassistant.local, the Spoolman host) is whatever answered on the LAN: the
    long-lived token must not be sent to it unless the user approves that origin."""
    if opts.ha not in ("auto", "skip"):
        return True
    named = (
        se.env.get("HA_URL"),
        se.env.get("PRINTOPS_HA_URL"),
        ha_detect.load_ha_config().get("url"),
    )
    return url.rstrip("/") in {str(u).rstrip("/") for u in named if u}


def _origin_approved(se: StageEnv, opts: StageOptions, url: str) -> bool:
    if _trusted_origin(se, opts, url):
        return True
    ctx = se.ctx
    if not ctx.interactive:
        ctx.say(
            f"Home Assistant at {url} was only discovered on the network, not configured: the token is not sent "
            f"to it. Pass --ha {url} (or set $HA_URL / `url:` in ha.yaml) to trust it."
        )
        return False
    return _yes(
        ctx,
        f"{url} was discovered on the network, it is not a URL you configured. Send your Home Assistant token to it?",
    )


def _confirm_wire(
    ctx: BackupContext, opts: StageOptions, url: str, with_backup: bool
) -> bool:
    what = "the Spoolman integration" + (
        " and the daily backup package" if with_backup else ""
    )
    if opts.ha_wire == "no":
        return False
    if opts.ha_wire == "yes":
        return True
    return _yes(ctx, f"Home Assistant is at {url}. Add {what} to it?")


def _ha_token(se: StageEnv, url: str) -> str | None:
    ctx = se.ctx
    token, where = ha_detect.resolve_token(
        se.env, se.ask_secret if ctx.interactive else None
    )
    if not token:
        ctx.say(
            "Home Assistant: no token (set $HA_TOKEN or put token: in ~/.config/3d-cli/ha.yaml, mode 600)"
        )
        return None
    if where == "prompt" and _yes(
        ctx,
        f"Store it in {ha_detect.config_file()} (mode 600) so the next run does not ask?",
    ):
        ha_detect.save_ha_config({"url": url, "token": token})
        ctx.say(f"saved {ha_detect.config_file()} (mode 600)")
    return token


def _install_package(
    se: StageEnv, opts: StageOptions, ha: HaRest, configured: bool
) -> int:
    ctx = se.ctx
    if not configured:
        ctx.say(
            "backup package: not installed (no backup target was configured and verified)"
        )
        return 0
    cfg = ha_detect.load_ha_config()
    ha_dir = opts.ha_dir or cfg.get("config_dir")
    ssh = opts.ha_ssh or cfg.get("ssh")
    if not ha_dir and ctx.interactive:
        ha_dir = ctx.ask(
            "Home Assistant config directory on its host (where configuration.yaml is; Enter = skip): "
        ).strip()
    if not ha_dir:
        ctx.say(
            "backup package: needs --ha-dir (and --ha-ssh USER@HOST when HA is on another host); "
            f"the file is {se.repo_root / ha_wire.PACKAGE_REL}"
        )
        return 0
    values = {
        "api_url": se.api_url_for_ha,
        "target": TARGET,
        "subdir": SUBDIR,
        **_keep_values(opts),
    }
    at = opts.at + ":00"
    for line in ha_wire.plan_install(ha_dir, values, at):
        ctx.say(f"  - {line}")
    if ctx.interactive and not _yes(ctx, "Do that?"):
        ctx.say("backup package: declined; nothing was written to Home Assistant")
        return 0
    result = ha_wire.install_package(
        se.ha_shell(ssh), ha, ha_dir, se.repo_root, values, se.mint_ha_token, at
    )
    ctx.say(f"backup package: {result.status}: {result.detail}")
    return 1 if result.status == "failed" else 0


def _keep_values(opts: StageOptions) -> dict[str, object]:
    keep = parse_keep(opts.keep)
    return {
        "keep_daily": keep.daily,
        "keep_weekly": keep.weekly,
        "keep_monthly": keep.monthly,
    }


def ha_stage(se: StageEnv, opts: StageOptions, url: str | None, backup_ok: bool) -> int:
    ctx = se.ctx
    if url is None:
        return 0
    if not _confirm_wire(ctx, opts, url, backup_ok):
        ctx.say(
            "Home Assistant: left alone (nothing was written there). Re-run with --ha-wire yes to add it later."
        )
        return 0
    if not _origin_approved(se, opts, url):
        return 1 if opts.ha_wire == "yes" else 0
    token = _ha_token(se, url)
    if token is None:
        return 1 if opts.ha_wire == "yes" else 0
    ha = se.ha_factory(url, token)
    try:
        info = ha.get("/api/config")
        ctx.say(f"Home Assistant {info.get('version', '?')} at {url}: token accepted")
        spool = ha_wire.wire_spoolman(ha, se.spoolman_url_for_ha)
        ctx.say(f"spoolman integration: {spool.status}: {spool.detail}")
        for line in spool.steps:
            ctx.say(f"  {line}")
        failed = 1 if spool.status == "failed" else 0
        return max(failed, _install_package(se, opts, ha, backup_ok))
    except (HaRestError, ThreeDError, RuntimeError) as e:
        ctx.say(f"Home Assistant: {getattr(e, 'message', e)}")
        return 1


def _ha_backup_candidates(
    se: StageEnv, opts: StageOptions, url: str | None
) -> list[Candidate]:
    """HA's own backup locations as discovery hints — only with a token that needs no prompt, and only
    from an origin the user named (this authenticates, before any confirmation)."""
    if not url or not _trusted_origin(se, opts, url):
        return []
    token, _ = ha_detect.resolve_token(se.env, None)
    return ha_locations.ha_backup_locations(url, token) if token else []


def run_stage(se: StageEnv, opts: StageOptions) -> int:
    """Home Assistant detection (read-only), backups, then the HA writes. 0 = ok or skipped, 1 = a requested step failed."""
    url = _ha_url(se, opts)
    se.ctx.ha_candidates = _ha_backup_candidates(se, opts, url)
    try:
        configured = backup_stage(se.ctx, opts)
    except (
        OSError,
        RuntimeError,
        ThreeDError,
    ) as e:  # a failed compose step / ssh: say so, do not traceback
        se.ctx.say(f"backups: {e}")
        configured = backup_setup.Configured(False, "", str(e))
    code = 0
    if configured is not None:
        se.ctx.say(
            f"backups: {'configured' if configured.ok else 'NOT configured'}: {configured.detail}"
        )
        code = 0 if configured.ok else 1
    return max(code, ha_stage(se, opts, url, bool(configured and configured.ok)))
