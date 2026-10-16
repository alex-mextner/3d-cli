"""`3d ha` — Home Assistant integration: deploy integrations/homeassistant/ to the live HA config."""

from __future__ import annotations

from pathlib import Path

from cli.registry import Command
from errors import UsageError

USAGE = """3d ha <subcommand> [options]
  Home Assistant integration. integrations/homeassistant/ is the source of truth for every 3D
  artifact of the HA config; the HA repo carries 3d-cli as a git submodule and points at it
  (manifest: integrations/homeassistant/manifest.yaml).

  deploy [--dry-run] [--host H] [--ref REF] [--ha-dir DIR] [--source DIR]
      Pin the HA repo's 3d-cli submodule to REF and converge the manifest files (relative
      symlinks into the submodule; www/ files as copies). Plan first, from a read-only probe
      of the host; anything blocking stops it before a change: orphans either way, live
      edits not in 3d-cli, missing configuration.yaml snippets, a compose file that doesn't
      include the 3D services, vendor.lock drift, a change that needs an HA restart. Then:
      backup tar -> submodule checkout -> converge -> check_config (rollback on failure) ->
      targeted reloads via the HA API (never a restart) -> recreate changed 3D services ->
      post-checks (printer entities, dashboard, go2rtc; rollback on failure) -> marker file ->
      branch + PR in the HA repo with the new submodule pointer.

Options:
  --dry-run     print the plan and stop; exit 0 when nothing blocks (also with changes)
  --host H      ssh target of the HA host (default: manifest ha_repo.host,
                ultra@192.168.0.39); `local` runs on this machine
  --ref REF     3d-cli commit to deploy (default origin/main, fetched first); the HA host
                fetches it from GitHub, so it must be pushed
  --ha-dir DIR  HA repo checkout on the host (default: manifest ha_repo.dir)
  --source DIR  3d-cli checkout that resolves REF (default: this checkout)

Exit codes: 0 deployed / dry-run clean, 1 blocked or a failed check (rolled back), 2 usage.
"""


def _deploy(argv: list[str]) -> int:
    from ha_deploy import Options, deploy

    opts = Options()
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            print(USAGE)
            return 0
        if arg == "--dry-run":
            opts.dry_run = True
            i += 1
            continue
        if arg in ("--host", "--ref", "--ha-dir", "--source"):
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                raise UsageError(f"{arg} needs a value", command="ha")
            value = argv[i + 1]
            if arg == "--host":
                opts.host = value
            elif arg == "--ref":
                opts.ref = value
            elif arg == "--ha-dir":
                opts.ha_dir = value
            else:
                opts.source = Path(value).resolve()
            i += 2
            continue
        raise UsageError(
            f"unknown option '{arg}'", command="ha", remediation=["See `3d ha --help`."]
        )
    return deploy(opts, Path(__file__).resolve().parents[2])


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    sub, rest = argv[0], argv[1:]
    if sub in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if sub == "deploy":
        return _deploy(rest)
    raise UsageError(
        f"unknown subcommand '{sub}'",
        command="ha",
        remediation=["Use deploy — see `3d ha --help`."],
    )


COMMAND = Command(
    name="ha",
    group="ENVIRONMENT",
    summary="Home Assistant: deploy integrations/homeassistant/ to the live HA config (submodule + symlinks)",
    usage=USAGE,
    run=run,
)
