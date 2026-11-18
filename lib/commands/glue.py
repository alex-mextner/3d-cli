"""3d glue — the glue-stick reminder: record a glue application, see when the next one is due.

WHAT: `status` shows per printer the last application, the prints and the time since it, the limits and whether
  a fresh layer is due; `applied` records an application (now or --at), `limits` sets the per-printer limits or
  turns the reminder off/on. The logic, the files and the default limits live in printer_ops/glue.py;
  `3d printer upload|start` run its check before sending anything.

WHY: issue https://github.com/alex-mextner/3d-cli/issues/762 (a K1 SE part came loose twice; nobody knew how
  old the glue on the plate was).

INVARIANTS: stdlib-only at import time (registry contract); the printer is only read (GET job history).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cli.registry import Command
from errors import InvalidArgument, UsageError

if TYPE_CHECKING:
    from printer_ops.glue import Verdict

USAGE = """3d glue <status|applied|limits> [--printer NAME] [options]
  The glue-stick reminder, on for every printer 3d knows. A fresh glue layer on a printer's bed is DUE when
  the time since the last application reaches the printer's day limit OR the prints since it reach its print
  limit, whichever comes first (defaults: 5 prints or 7 days, for PLA on PEI with a glue stick), and while
  no application is on record at all. Prints are counted from the printer's own job history (Moonraker, GET
  only), so prints started from Orca or the touchscreen count too: every job started after the glue that
  printed (completed, cancelled, errored, the job printing now), once per Moonraker job id. Without an answer
  from the printer, 3d's own start log gives an estimate ("~N": the starts 3d sent, not verified as printed).
  `3d printer upload` and `3d printer start` check it first: when due they say which limit was reached,
  ask "Glue applied? [y/N]" on a terminal (y records it now) and otherwise print the command to record it.
  The job watch's Telegram alert carries the same warning. One record per printer: the glue on the bed it
  has now (after swapping to a plate glued earlier, record that glue with `applied --at`).

  status [--printer NAME] [--json]     last glue, prints and time since it, limits, due or not (every printer,
                                       or NAME)
  applied [--printer NAME] [--at TIME]
                                       record a glue application (default: now): the time and the print count
                                       both start again from it
  limits [--printer NAME] [--prints N] [--days D] [--reset] [--off | --on] [--json]
                                       show or set the printer's limits (~/.config/3d-cli/glue.json);
                                       --reset goes back to the defaults (and on); --off: no glue on this
                                       printer, no reminders; --on undoes it

Options:
  --printer NAME   a printer of `3d printer list`; may be left out when only one is configured
  --at TIME        when it happened: ISO date or date-time, local time without an offset (2026-11-18T00:35,
                   2026-11-17T23:35Z); not in the future
  --prints N       print limit (whole number >= 1)
  --days D         day limit (number > 0 and <= 3650, e.g. 7 or 3.5)
  --off, --on      turn the reminder off / on for the printer
  --reset          the default limits again, reminder on
  --json           status/limits as JSON

Files: ~/.local/state/3d-cli/glue/NAME.json (the records), ~/.config/3d-cli/glue.json (limits, off),
~/.local/state/3d-cli/prints/NAME.jsonl (3d's own start log, the fallback count).

Examples:
  3d glue status
  3d glue status --printer k1se --json | jq '.[0] | {due, fired, prints, elapsed_days}'
  3d glue applied --printer k1se
  3d glue applied --printer k1se --at 2026-11-18T00:35
  3d glue limits --printer k1se --prints 4 --days 5
  3d glue limits --printer k1se --json
  3d glue limits --printer u1 --off
  3d glue limits --printer u1 --on
  3d glue limits --printer k1se --reset"""

_FLAGS = {"--json", "--reset", "--off", "--on"}
_ALLOWED = {
    "status": {"--printer", "--json"},
    "applied": {"--printer", "--at"},
    "limits": {"--printer", "--prints", "--days", "--reset", "--off", "--on", "--json"},
}


def _parse(sub: str, argv: list[str]) -> dict[str, str]:
    opts: dict[str, str] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg not in _ALLOWED[sub]:
            raise UsageError(
                f"glue {sub}: unexpected argument {arg!r}",
                command="glue",
                remediation=[
                    f"3d glue {sub} accepts: {', '.join(sorted(_ALLOWED[sub]))}"
                ],
            )
        if arg in _FLAGS:
            opts[arg] = ""
            i += 1
            continue
        if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
            raise UsageError(
                f"glue {sub}: {arg} needs a value", command="glue", remediation=[USAGE]
            )
        opts[arg] = argv[i + 1]
        i += 2
    return opts


def _printer(opts: dict[str, str]) -> str:
    """--printer, checked against the printers 3d knows; without it the only one there is."""
    from printer_ops.glue import known_printers

    known = known_printers()
    name = opts.get("--printer")
    if name is not None:
        if name not in known:
            raise InvalidArgument(
                "--printer",
                name,
                known or ["(no printers configured: see `3d printer --help`)"],
                command="glue",
            )
        return name
    if len(known) == 1:
        return known[0]
    raise UsageError(
        "say which printer: --printer NAME",
        command="glue",
        remediation=[f"Printers: {', '.join(known) or 'none configured'}"],
    )


def _cmd_status(opts: dict[str, str]) -> int:
    import json

    from printer_ops.glue import evaluate, known_printers

    names = [_printer(opts)] if "--printer" in opts else known_printers()
    verdicts: list[Verdict] = [v for v in (evaluate(n) for n in names) if v is not None]
    if "--json" in opts:
        print(json.dumps([v.to_json() for v in verdicts], indent=1, ensure_ascii=False))
    else:
        print(_status_text(names, verdicts))
    return 0


def _status_text(names: list[str], verdicts: list[Verdict]) -> str:
    from printer_ops.glue import status_lines

    shown = {v.printer for v in verdicts}
    blocks = ["\n".join(status_lines(v)) for v in verdicts]
    blocks += [
        f"{n}  glue reminder off (3d glue limits --printer {n} --on)"
        for n in names
        if n not in shown
    ]
    return (
        "\n\n".join(blocks)
        if blocks
        else "No printers configured: see `3d printer --help`."
    )


def _cmd_applied(opts: dict[str, str]) -> int:
    import time

    from printer_ops.glue import evaluate, parse_at, record_applied, status_lines

    name = _printer(opts)
    now = time.time()
    at = parse_at(opts["--at"], now=now) if "--at" in opts else now
    record_applied(name, at, now=now)
    print(f"glue applied recorded: {name}")
    verdict = evaluate(name, now=now)
    print(
        "\n".join(status_lines(verdict))
        if verdict is not None
        else f"(the glue reminder is off for {name}: 3d glue limits --printer {name} --on)"
    )
    return 0


def _positive_number(opts: dict[str, str], flag: str, *, whole: bool) -> float | None:
    from printer_ops.glue import MAX_LIMIT

    raw = opts.get(flag)
    if raw is None:
        return None
    try:
        value = int(raw) if whole else float(raw)
    except ValueError:
        value = 0
    if not 0 < value <= MAX_LIMIT:  # NaN and inf fail too
        raise InvalidArgument(
            flag,
            raw,
            [
                f"a whole number 1..{MAX_LIMIT}"
                if whole
                else f"a number in (0, {MAX_LIMIT}]"
            ],
            command="glue",
        )
    return value


def _switch(opts: dict[str, str]) -> bool | None:
    if "--off" in opts and "--on" in opts:
        raise UsageError("--off and --on contradict each other", command="glue")
    return False if "--off" in opts else (True if "--on" in opts else None)


def _cmd_limits(opts: dict[str, str]) -> int:
    import json

    from printer_ops.glue import configure, enabled, limits_for

    name = _printer(opts)
    prints = _positive_number(opts, "--prints", whole=True)
    days = _positive_number(opts, "--days", whole=False)
    on = _switch(opts)
    reset = "--reset" in opts
    if reset and (prints is not None or days is not None or on is not None):
        raise UsageError(
            "--reset goes back to the defaults; it takes no --prints/--days/--off/--on",
            command="glue",
        )
    if reset or prints is not None or days is not None or on is not None:
        configure(
            name,
            max_prints=None if prints is None else int(prints),
            max_days=days,
            on=on,
            reset=reset,
        )
    limits, active = limits_for(name), enabled(name)
    if "--json" in opts:
        print(
            json.dumps(
                {
                    "printer": name,
                    "enabled": active,
                    "max_prints": limits.max_prints,
                    "max_days": limits.max_days,
                    "source": limits.source,
                }
            )
        )
    elif not active:
        print(f"{name}: glue reminder OFF (3d glue limits --printer {name} --on)")
    else:
        print(
            f"{name}: glue due after {limits.max_prints} prints or {limits.max_days:g} days, whichever comes "
            f"first ({limits.source})"
        )
    return 0


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if argv else 1
    sub, rest = argv[0], argv[1:]
    if sub not in _ALLOWED:
        raise InvalidArgument("subcommand", sub, sorted(_ALLOWED), command="glue")
    if "-h" in rest or "--help" in rest:
        print(USAGE)
        return 0
    opts = _parse(sub, rest)
    if sub == "status":
        return _cmd_status(opts)
    if sub == "limits":
        return _cmd_limits(opts)
    return _cmd_applied(opts)


COMMAND = Command(
    name="glue",
    group="SLICING",
    summary="glue-stick reminder per printer: due after N prints or D days; record `applied`",
    usage="glue <status|applied|limits> [--printer NAME] [--at TIME]",
    run=run,
)
