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

  vision [--write] [--root DIR] [--fleet FILE]
      Generate the HA side of camera vision for EVERY fleet printer that has a camera
      (fleet/printers.yaml, watch.vision.camera): the vision sensors (verdict p/q, events,
      printing/paused/alive), the go2rtc stream, the camera proxy service, the dashboard camera
      cards and, for a printer with a bundled camera calibration, the ghost overlay switch.
      Without --write it only checks: prints what each printer gets (and why not the ghost) and
      exits 1 when a committed file differs from what the fleet generates. --write rewrites the
      generated files (packages/printer_vision.yaml and the marked regions of go2rtc.yaml, the
      compose file and the dashboard). Never touches Home Assistant or a printer.

Options:
  --dry-run     print the plan and stop; exit 0 when nothing blocks (also with changes)
  --host H      ssh target of the HA host (default: manifest ha_repo.host,
                ultra@192.168.0.39); `local` runs on this machine
  --ref REF     3d-cli commit to deploy (default origin/main, fetched first); the HA host
                fetches it from GitHub, so it must be pushed
  --ha-dir DIR  HA repo checkout on the host (default: manifest ha_repo.dir)
  --source DIR  3d-cli checkout that resolves REF (default: this checkout)

  --write       vision: rewrite the generated files instead of only checking them
  --root DIR    vision: the 3d-cli checkout whose integrations/homeassistant/ is read and written
                (default: this checkout)
  --fleet FILE  vision: the fleet registry to read (default: THREED_FLEET or fleet/printers.yaml)

Exit codes: 0 deployed / dry-run clean / vision files current, 1 blocked, a failed check (rolled
back) or generated files out of date, 2 usage.
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


def _vision(argv: list[str]) -> int:
    from ha_vision import drift, generate, missing, plan
    from printops.fleet import load_fleet

    write, root, fleet_file = False, Path(__file__).resolve().parents[2], None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            print(USAGE)
            return 0
        if arg == "--write":
            write = True
            i += 1
        elif arg in ("--root", "--fleet"):
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                raise UsageError(f"{arg} needs a value", command="ha")
            if arg == "--root":
                root = Path(argv[i + 1]).resolve()
            else:
                fleet_file = Path(argv[i + 1]).expanduser()
            i += 2
        else:
            raise UsageError(f"unknown option '{arg}'", command="ha", remediation=["See `3d ha --help`."])
    fleet = load_fleet(fleet_file)
    if fleet is None:
        raise UsageError("no fleet registry to generate from", command="ha",
                         remediation=["Run from a 3d-cli checkout (fleet/printers.yaml) or pass --fleet FILE."])
    files, printers, _settings = generate(root, fleet)
    for line in plan(fleet, printers):
        print(line)
    stale = drift(root, files)
    if write:
        for path in stale:
            (root / path).write_text(files[path], encoding="utf-8")
            print(f"wrote {path}")
        print(f"{len(stale)} file(s) written, {len(files) - len(stale)} already current")
    for path in [] if write else stale:
        print(f"out of date: {path}")
    gaps = missing(root, printers)
    for gap in gaps:
        print(f"missing: {gap}")
    if gaps:
        return 1
    if write:
        return 0
    if stale:
        print("run `3d ha vision --write` and commit the result")
        return 1
    print(f"{len(files)} generated file(s) current")
    return 0


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
    if sub == "vision":
        return _vision(rest)
    raise UsageError(
        f"unknown subcommand '{sub}'",
        command="ha",
        remediation=["Use deploy or vision — see `3d ha --help`."],
    )


COMMAND = Command(
    name="ha",
    group="ENVIRONMENT",
    summary="Home Assistant: deploy integrations/homeassistant/ (submodule + symlinks); generate the per-printer camera vision files",
    usage=USAGE,
    run=run,
)
