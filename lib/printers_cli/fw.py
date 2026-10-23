"""3d printers fw — detect, plan, back up and change a printer's firmware (root / Klipper unlock).

WHAT: `status` detects a printer's firmware state with read-only probes, `plan` prints the ordered
  steps to a target state (never executes), `backup` pulls what the runbook says to save into a
  timestamped directory with a manifest, `apply` runs the plan behind every safety gate, `profiles`
  lists the vendor profiles. A profile (lib/data/firmware/*.yaml) is DATA: which models it covers,
  how each state is detected, the transitions and their steps, the backups, the guards. Nothing in
  this module or in lib/printer_fw knows a printer name, model or address.

WHY: issue https://github.com/alex-mextner/3d-cli/issues/519 — rooting a K1, installing the
  Snapmaker U1 extended firmware or enabling a Bambu printer's LAN mode were hand-run runbooks
  (docs/printers/*.md); the same pitfalls bit more than once. This command makes the runbooks
  machine-checked and fleet-driven.

ACCESSED VIA: `3d printers fw ...` (alias `3d printers firmware ...`), a self-registering sub of
  lib/printers_cli. Reference: docs/commands/printers-fw.md, docs/printers/firmware-profiles.md.

INVARIANTS: thin: argv parsing, rendering, exit codes (0 ok / dry run, 1 refused for safety,
  verification failed or manual step pending, 2 usage, 127 no ssh binary). Imports stay stdlib
  only; the engine (printer_fw) is imported inside the handlers. `apply` never executes without
  `--yes` + a fresh backup + the typed printer name + an idle printer.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from errors import GateFailure, InvalidArgument, ThreeDError, UsageError
from printers_cli import Sub

from printers_cli._common import emit_json, parse, wants_help

if TYPE_CHECKING:
    from printer_fw.engine import Context, EventLog
    from printer_fw.probes import Backends
    from printer_fw.profiles import Profile
    from printer_fw.target import Target

USAGE = """3d printers fw <status|plan|backup|apply|profiles> ...   (alias: 3d printers firmware ...)
  Firmware / root / Klipper-unlock tool, driven by vendor profiles (lib/data/firmware/*.yaml)
  matched on each fleet printer's `model`. Works for every printer of the fleet registry.

  status [PRINTER|--all] [--json] [--no-live] [--no-ssh] [--verbose]
      detect the state of a printer (stock / rooted / klipper-ready / extended ...) with
      READ-ONLY probes; --all does every fleet printer
        --json        machine-readable:  3d printers fw status myprinter --json
        --no-live     profile resolution only, no network:  3d printers fw status --all --no-live
        --no-ssh      skip ssh/banner probes (they can take ~30 s on slow SSH servers):
                      3d printers fw status myprinter --no-ssh
        --verbose     show every probe and its evidence:  3d printers fw status myprinter --verbose
  plan PRINTER --to STATE [--from STATE] [--also STEP_ID ...] [--no-ssh] [--json]
      print the ordered plan (steps, preconditions, backups, risks, rollback, sources). NEVER executes.
        --to STATE    target state (see `fw profiles`):  3d printers fw plan myprinter --to extended
        --from STATE  assume this state instead of detecting (the only way to plan for an offline
                      printer; contacts nothing):  3d printers fw plan myprinter --from stock --to developer-mode
        --also ID     add an optional step of a transition on the path (repeatable):
                      3d printers fw plan myprinter --to klipper-ready --also install-gcode-shell-command
        --no-ssh      skip ssh probes while detecting:  3d printers fw plan myprinter --to extended --no-ssh
        --json        machine-readable:  3d printers fw plan myprinter --to extended --json
  backup PRINTER [--dest DIR] [--no-ssh] [--json]
      read-only pull of the files the runbook lists into DIR/<printer>-<profile>-<UTC>/ with a
      manifest.json (sha256, sizes, time, profile id, printer name). Exit 1 when an item failed.
        --dest DIR    where to write (default: ~/.local/share/3d-cli/firmware-backups):
                      3d printers fw backup myprinter --dest ~/fw-backups
        --no-ssh      take only the HTTP items:  3d printers fw backup myprinter --no-ssh
        --json        machine-readable:  3d printers fw backup myprinter --json
  apply PRINTER --to STATE [--yes] [--confirm NAME] [--backup-dir DIR] [--backup-max-age AGE]
        [--also STEP_ID ...] [--file [STEP_ID=]PATH ...] [--no-ssh] [--json]
      DRY RUN by default: prints exactly what would run. With --yes only steps flagged
      `automatable` execute (HTTP upload of the firmware file you supply, documented ssh scripts);
      manual steps are printed, then verified by their probe (interactive: after Enter; otherwise
      the run stops with `NEXT: do the step, then re-run the same command` and resumes on re-run).
      A step that reboots the printer is only verified after the printer was SEEN going down.
      REFUSES (exit 1) without a fresh backup, while the printer prints/pauses/is busy/unreadable,
      for an offline fleet printer, for an unreachable target or a guard violation; a wrong or
      missing --confirm is exit 2.
        --yes         execute:  3d printers fw apply myprinter --to extended --yes --confirm myprinter
        --confirm N   the printer's fleet name, typed explicitly (interactive prompt otherwise)
        --backup-dir  where `fw backup` wrote (default as for backup):
                      3d printers fw apply myprinter --to klipper-ready --backup-dir ~/fw-backups
        --backup-max-age AGE   newest allowed backup age: 24h (default), 90m, 2d; bare number = hours:
                      3d printers fw apply myprinter --to klipper-ready --backup-max-age 6h
        --also ID     optional step (repeatable); a guard-forbidden id is refused with its reason:
                      3d printers fw apply myprinter --to klipper-ready --also install-usb-camera
        --file [ID=]PATH   the firmware file an upload step sends (bare PATH when the plan has one
                      upload step):  3d printers fw apply myprinter --to extended --file ~/fw/update.bin
        --no-ssh      never use ssh: probes that need it are skipped and a plan with an ssh step is
                      refused (dry run: "not ready"):  3d printers fw apply myprinter --to extended --no-ssh
        --json        machine-readable event list, errors included as {"result": "error", ...}:
                      3d printers fw apply myprinter --to extended --json
  profiles [--json]
      list the vendor profiles: vendor, models matched, states, transitions, sources.
        --json        machine-readable:  3d printers fw profiles --json

  Exit codes: 0 ok / dry run / nothing to do; 1 refused for safety, verification failed or a manual
  step is pending; 2 usage / invalid argument; 127 ssh binary missing.
  ssh uses key authentication only (BatchMode); 3d stores no passwords. Ports of the profile's
  services can be redirected with THREED_FW_SERVICE_PORTS=name=PORT[,name=PORT] (port-forwards;
  a name that is no service of any profile is refused).
"""

_AGE = re.compile(r"(\d+(?:\.\d+)?)([smhd]?)")
_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, "": 3600.0}
_STEP_ID = re.compile(r"[a-z0-9][a-z0-9._-]*")  # same shape as profiles._ID


def _age_seconds(text: str) -> float:
    m = _AGE.fullmatch(text.strip())
    if m is None or float(m.group(1)) <= 0:
        raise InvalidArgument(
            "--backup-max-age",
            text,
            ["24h", "90m", "2d", "6 (hours)"],
            command="printers fw apply",
        )
    return float(m.group(1)) * _UNITS[m.group(2)]


def _take_multi(argv: list[str], flag: str) -> tuple[list[str], list[str]]:
    """Pull every `FLAG VALUE` out of argv (the shared parser keeps only the last of a repeated
    option); returns (values, remaining argv)."""
    values: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == flag:
            if i + 1 >= len(argv):
                raise UsageError(f"{flag} needs a value", command="printers fw")
            values.append(argv[i + 1])
            i += 2
            continue
        rest.append(argv[i])
        i += 1
    return values, rest


def _default_backup_dir() -> Path:
    from cli.paths import data_dir

    return data_dir() / "firmware-backups"


def _backends(use_ssh: bool, interactive: bool) -> Backends:
    from printer_fw.probes import Backends
    from printer_fw.runner import SshRunner

    def confirm(prompt: str) -> bool:
        return input(f"  {prompt} [y/N] ").strip().lower() in ("y", "yes")

    return Backends(
        runner=SshRunner(), use_ssh=use_ssh, confirm=confirm if interactive else None
    )


def _interactive(json_mode: bool) -> bool:
    return not json_mode and sys.stdin.isatty() and sys.stdout.isatty()


def _profile_for(target: Target, profiles: Mapping[str, Profile]) -> Profile:
    from printer_fw.engine import no_profile_note
    from printer_fw.profiles import match_profile

    profile = match_profile(profiles, target.model)
    if profile is None:
        raise GateFailure(
            no_profile_note(target),
            command="printers fw",
            remediation=[
                "`3d printers fw profiles` lists the models that have a firmware profile.",
                "To add one, write lib/data/firmware/<vendor>.yaml (docs/printers/firmware-profiles.md).",
            ],
        )
    return profile


def _status(argv: list[str]) -> int:
    from printer_fw import engine, render
    from printer_fw.profiles import load_profiles, match_profile
    from printer_fw.target import (
        all_targets,
        endpoint,
        resolve_target,
        validate_port_overrides,
    )

    p = parse(
        argv,
        "fw status",
        flags={"--all", "--json", "--no-live", "--no-ssh", "--verbose"},
        n_pos=(0, 1),
    )
    if p.pos and "--all" in p.flags:
        raise UsageError(
            "give a PRINTER or --all, not both", command="printers fw status"
        )
    if not p.pos and "--all" not in p.flags:
        raise UsageError(
            "`fw status` needs a PRINTER or --all",
            command="printers fw status",
            remediation=[
                "3d printers fw status myprinter",
                "3d printers fw status --all",
            ],
        )
    profiles = load_profiles()
    validate_port_overrides(profiles.values())
    targets = (
        all_targets() if "--all" in p.flags else [resolve_target(p.pos[0], "fw status")]
    )
    live = "--no-live" not in p.flags
    be = _backends("--no-ssh" not in p.flags, False) if live else None
    entries = []
    for t in targets:
        profile = match_profile(profiles, t.model)
        ep = endpoint(profile, t) if profile is not None else None
        entries.append(engine.status_for(t, profile, ep, be, live=live))
    if "--json" in p.flags:
        emit_json({"printers": [e.to_json() for e in entries]})
        return 0
    for e in entries:
        head = f"{e.printer} ({e.model or 'no model'}, access {e.access})"
        print(f"{head}: " + (f"profile {e.profile_id}" if e.profile_id else e.note))
        if e.profile_id and e.note:
            print(f"  {e.note}")
        if e.detection is not None:
            print(
                "\n".join(render.detection_lines(e.detection, "--verbose" in p.flags))
            )
    return 0


def _context(
    printer: str, command: str, *, use_ssh: bool, interactive: bool, log: EventLog
) -> Context:
    from printer_fw.engine import Context
    from printer_fw.profiles import load_profiles
    from printer_fw.target import endpoint, resolve_target, validate_port_overrides

    profiles = load_profiles()
    validate_port_overrides(profiles.values())
    target = resolve_target(printer, command)
    profile = _profile_for(target, profiles)
    ask = input if interactive else None
    return Context(
        profile,
        target,
        endpoint(profile, target),
        _backends(use_ssh, interactive),
        log,
        ask=ask,
    )


def _plan(argv: list[str]) -> int:
    from printer_fw import render
    from printer_fw.engine import EventLog, plan_for

    also, argv = _take_multi(argv, "--also")
    p = parse(
        argv,
        "fw plan",
        values={"--to", "--from"},
        flags={"--json", "--no-ssh"},
        n_pos=1,
    )
    if "--to" not in p.opts:
        raise UsageError(
            "`fw plan` needs --to STATE",
            command="printers fw plan",
            remediation=["3d printers fw plan myprinter --to extended"],
        )
    ctx = _context(
        p.pos[0],
        "fw plan",
        use_ssh="--no-ssh" not in p.flags,
        interactive=False,
        log=EventLog(),
    )
    report = plan_for(ctx, p.opts["--to"], tuple(also), p.opts.get("--from"))
    if "--json" in p.flags:
        out = render.plan_json(report.plan)
        out["detected"] = report.detection.to_json() if report.detection else None
        out["violations"] = [v.to_json() for v in report.violations]
        emit_json(out)
    else:
        if report.detection is not None:
            print("\n".join(render.detection_lines(report.detection)))
        if report.violations:
            for v in report.violations:
                print("REFUSED: " + v.render())
        else:
            print(
                "\n".join(
                    render.plan_lines(report.plan, ctx.profile, None, ctx.target.host)
                )
            )
            print("\nplan only: nothing was executed.")
    return 1 if report.violations else 0


def _backup(argv: list[str]) -> int:
    from printer_fw import render
    from printer_fw.engine import EventLog, backup_for

    p = parse(
        argv, "fw backup", values={"--dest"}, flags={"--json", "--no-ssh"}, n_pos=1
    )
    ctx = _context(
        p.pos[0],
        "fw backup",
        use_ssh="--no-ssh" not in p.flags,
        interactive=False,
        log=EventLog(),
    )
    dest = (
        Path(p.opts["--dest"]).expanduser()
        if "--dest" in p.opts
        else _default_backup_dir()
    )
    manifest = backup_for(ctx, dest)
    if "--json" in p.flags:
        emit_json(manifest.to_json())
    else:
        print("\n".join(render.manifest_lines(manifest)))
        if not manifest.ok:
            print(
                "BACKUP INCOMPLETE: fix the failed items and run it again; `fw apply` will not accept it."
            )
    return 0 if manifest.ok else 1


def _apply(argv: list[str]) -> int:
    from printer_fw.engine import EventLog

    json_mode = "--json" in argv
    log = EventLog(None if json_mode else print)
    try:
        return _apply_run(argv, json_mode, log)
    except ThreeDError as exc:
        if not json_mode:
            raise
        # --json promises one JSON document on stdout for EVERY outcome, errors included.
        emit_json(
            {
                "result": "error",
                "exit_code": exc.exit_code,
                "error": exc.message,
                "events": [e.to_json() for e in log.events],
            }
        )
        return exc.exit_code


def _apply_run(argv: list[str], json_mode: bool, log: EventLog) -> int:
    from printer_fw.backup import DEFAULT_MAX_AGE_S
    from printer_fw.engine import ApplyOptions, run_apply

    also, argv = _take_multi(argv, "--also")
    file_args, argv = _take_multi(argv, "--file")
    p = parse(
        argv,
        "fw apply",
        values={"--to", "--confirm", "--backup-dir", "--backup-max-age"},
        flags={"--yes", "--json", "--no-ssh"},
        n_pos=1,
    )
    if "--to" not in p.opts:
        raise UsageError(
            "`fw apply` needs --to STATE",
            command="printers fw apply",
            remediation=["3d printers fw apply myprinter --to extended   (dry run)"],
        )
    files: dict[str, Path] = {}
    default_file: Path | None = None
    for raw in file_args:
        step, sep, path = raw.partition("=")
        if sep and _STEP_ID.fullmatch(step):
            files[step] = Path(path).expanduser()
        else:
            default_file = Path(raw).expanduser()
    interactive = _interactive(json_mode)
    ctx = _context(
        p.pos[0],
        "fw apply",
        use_ssh="--no-ssh" not in p.flags,
        interactive=interactive,
        log=log,
    )
    opts = ApplyOptions(
        to=p.opts["--to"],
        yes="--yes" in p.flags,
        confirm=p.opts.get("--confirm"),
        backup_dir=Path(p.opts["--backup-dir"]).expanduser()
        if "--backup-dir" in p.opts
        else _default_backup_dir(),
        max_age_s=_age_seconds(p.opts["--backup-max-age"])
        if "--backup-max-age" in p.opts
        else DEFAULT_MAX_AGE_S,
        also=tuple(also),
        files=files,
        default_file=default_file,
    )
    result = run_apply(ctx, opts)
    if json_mode:
        emit_json(
            {"printer": ctx.target.name, "profile": ctx.profile.id, **result.to_json()}
        )
    return result.exit_code


def _profiles(argv: list[str]) -> int:
    from printer_fw import render
    from printer_fw.profiles import load_profiles

    p = parse(argv, "fw profiles", flags={"--json"}, n_pos=0)
    profiles = list(load_profiles().values())
    if "--json" in p.flags:
        emit_json({"profiles": [render.profile_json(x) for x in profiles]})
    else:
        print("\n".join(render.profiles_lines(profiles)))
    return 0


_ACTIONS = {
    "status": _status,
    "plan": _plan,
    "backup": _backup,
    "apply": _apply,
    "profiles": _profiles,
}


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help") or (wants_help(argv) and argv[0] not in _ACTIONS):
        print(USAGE)
        return 0
    action = _ACTIONS.get(argv[0])
    if action is None:
        raise InvalidArgument(
            "fw subcommand", argv[0], list(_ACTIONS), command="printers fw"
        )
    if wants_help(argv[1:]):
        print(USAGE)
        return 0
    return action(argv[1:])


SUB = Sub(
    name="fw",
    summary="firmware / root / Klipper-unlock tool: status, plan, backup, apply, profiles (vendor profiles, guarded)",
    usage=USAGE,
    run=run,
    aliases=("firmware",),
)
