"""3d settings — the printer settings-change journal via 3d-api: import the offline buffers, read history.

WHAT: `import` replays the offline buffer `<data_dir>/printer-history/<printer>.jsonl` (or the
  given files) into 3d-api's append-only `settings_change` table, idempotent by uid; lines that
  record a calibration (flow, pressure advance, max volumetric speed, input shaper, …) also become
  calibration records, so `3d calibrate plan|check` know what was already calibrated.
  `history` lists the journal of a printer, newest change first.
WHY: changes made while 3d-api was not involved (an agent at the printer, a profile push) must
  still be traceable and must not make the calibration gate demand a recalibration. Issue #264,
  plan §7.4.
INVARIANTS: stdlib-only at import time (registry contract); the buffer files are only read; the
  domain logic runs in 3d-api (printops.settings_history), this module parses arguments and
  speaks HTTP.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from cli.registry import Command
from errors import GateFailure, InputNotFound, ThreeDError, UsageError

USAGE = """3d settings import [FILE.jsonl…] [--spool UID=ID[,…]] [--method UID=METHOD[,…]] [--dry-run]
3d settings history --printer P [--kind KIND] [--since DATE] [--limit N]
  common: [--json] [--api URL] [--token TOKEN]

  The journal of every printer setting change (plan §7.4): what changed, old → new, when, who and
  with which command, the source (photo, accelerometer, user's visual choice, …) and the binding
  (printer, head, nozzle, spool). Lines are kept in an offline buffer
  ~/.local/share/3d-cli/printer-history/<printer>.jsonl (schema printops.settings_change/1) and
  imported into 3d-api.

Subcommands:
  import   import buffer files (default: every <printer>.jsonl of the buffer dir) into 3d-api.
           Idempotent by uid: a second import adds 0. Per line: imported (a journal row, plus a
           calibration record when the line records one), skipped (already imported) or refused
           (nothing written; the reason says what to pass). A torn (non-JSON) line stops the
           import of its file with the line number. Exit 1 when a line was refused.
           A line becomes a calibration record when its kind is a catalog kind
           (fleet/calibrations.yaml) or a measured slicer-profile key a kind applies
           (filament_max_volumetric_speed), with status applied — or measured_not_applied when
           the measurement equals the value in force. A measurement that was not applied stays
           journal-only: the printer still runs the old value.
  history  the journal, newest change first

Options:
  --spool UID=ID     bind the calibration line UID to Spoolman spool ID when the buffer names no
                     spool and no spool_binding line of that head has the line's colour
  --method UID=M     the catalog method of the calibration line UID when the line has none the
                     catalog knows and the kind has no `manual` (e.g. input_shaper:
                     klipper_shaper_calibrate | k1_auto | u1_auto)
  --dry-run          run the import and roll it back: show what would happen
  --printer P        fleet printer name (history)
  --kind KIND        only this kind (pressure_advance, slicer_profile, spool_binding, …)
  --since DATE       only changes at or after DATE (ISO date or time)
  --limit N          history: at most N rows (default 50)
  --json             print the API's JSON
  --api URL          3d-api base URL (default: $PRINTOPS_API, else ~/.config/3d-cli/printops.yaml)
  --token TOKEN      bearer token (default: $PRINTOPS_TOKEN, else the same file)

Buffer location: `~/.local/share/3d-cli/printer-history/` ($XDG_DATA_HOME/3d-cli). A buffer in
the pre-#264 `~/.local/share/3d/printer-history/` is moved there once, on the first `import`
without files; when both exist the command stops and prints the import commands for both.

Examples:
  3d settings import --dry-run
  3d settings import
  3d settings import ~/.local/share/3d-cli/printer-history/k1se.jsonl --method 01a140fe-3d83-768e-b634-af490ca0bd5b=klipper_shaper_calibrate
  3d settings history --printer k1se --kind pressure_advance
  3d settings history --printer k1se --since 2026-10-16 --json | jq '.changes[] | {at, kind, key, new}'"""

PREFIX = "settings"
COMMON_VALUES = {"--api", "--token"}
COMMON_FLAGS = {"--json"}


def _pairs(raw: str, opt: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in (x.strip() for x in raw.split(",")):
        if not pair:
            continue
        uid, sep, value = pair.partition("=")
        if not sep or not uid.strip() or not value.strip():
            raise UsageError(
                f"{opt}: {pair!r} must be UID=VALUE", command=f"{PREFIX} import"
            )
        out[uid.strip()] = value.strip()
    return out


def _spools(raw: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for uid, sid in _pairs(raw, "--spool").items():
        if not sid.isdigit():
            raise UsageError(
                f"--spool: {uid}={sid} needs a spool id number",
                command=f"{PREFIX} import",
            )
        out[uid] = int(sid)
    return out


def _files(pos: list[str]) -> list[Path]:
    from printops import history_buffer as hb

    if pos:
        paths = [Path(p).expanduser() for p in pos]
        for path in paths:
            if not path.is_file():
                raise InputNotFound(str(path), command=f"{PREFIX} import")
        return paths
    try:
        where, found = hb.buffer_files()
    except hb.HistoryBufferError as e:
        raise ThreeDError(
            e.message, command=f"{PREFIX} import", remediation=e.remediation
        ) from e
    if not found:
        print(f"no buffer files in {where}", file=sys.stderr)
    return found


def _read_lines(path: Path) -> list[str]:
    """The buffer's lines split on `\\n` only: JSON written with ensure_ascii=False may carry
    U+2028/U+2029 inside a string, which str.splitlines() would treat as line breaks."""
    try:
        return path.read_bytes().decode("utf-8").split("\n")
    except UnicodeDecodeError as e:
        line = path.read_bytes()[: e.start].count(b"\n") + 1
        raise ThreeDError(
            f"{path}: line {line} is not UTF-8 (a torn write?)",
            command=f"{PREFIX} import",
            remediation=[
                "Fix or remove that line (the file is never changed by the import), then import again."
            ],
        ) from e


def _safe(text: object) -> str:
    """Buffer text for the terminal: control characters (escape sequences) shown, not run."""
    return "".join(
        c if c.isprintable() or c == " " else repr(c)[1:-1] for c in str(text)
    )


def _summary(rep: dict[str, Any]) -> str:
    c = rep["counts"]
    head = "DRY RUN (rolled back) " if rep["dry_run"] else ""
    return (
        f"{head}{rep['file']}: {rep['lines']} lines — {c['imported']} imported "
        f"({c['calibrations']} calibration records), {c['skipped']} skipped, {c['refused']} refused"
    )


def _print_reports(reports: list[dict[str, Any]], as_json: bool) -> None:
    if as_json:
        print(json.dumps({"files": reports}, indent=2, ensure_ascii=False))
        return
    for rep in reports:
        print(_safe(_summary(rep)))
        for r in rep["results"]:
            if r["outcome"] == "refused" or r["calibration_id"] is not None:
                print(
                    _safe(
                        f"  line {r['line']:<4} {r['outcome']:<9} {r['kind']:<18} {r['detail']}"
                    )
                )


def _import(argv: list[str]) -> int:
    from printops.cli._common import api_errors, client, parse

    p = parse(
        argv,
        "import",
        values={"--spool", "--method"} | COMMON_VALUES,
        flags={"--dry-run"} | COMMON_FLAGS,
        n_pos=(0, 1000),
        prefix=PREFIX,
    )
    for opt in ("--spool", "--method"):
        if (
            argv.count(opt) > 1
        ):  # the parser keeps only the last value: never drop one silently
            raise UsageError(
                f"{opt} given {argv.count(opt)} times; pass them once, comma-separated: {opt} UID=…,UID=…",
                command=f"{PREFIX} import",
            )
    spools = _spools(p.opts.get("--spool", ""))
    methods = _pairs(p.opts.get("--method", ""), "--method")
    files = _files(p.pos)
    reports: list[dict[str, Any]] = []
    with api_errors("import", prefix=PREFIX):
        api = client(p, "import", prefix=PREFIX)
        try:
            for path in files:
                body = {
                    "file": str(path),
                    "lines": _read_lines(path),
                    "spools": spools,
                    "methods": methods,
                    "dry_run": "--dry-run" in p.flags,
                }
                # one file at a time, so a later file's error still reports the earlier ones
                reports.append(api.post("/v1/settings/import", body))
        except BaseException:
            _print_reports(
                reports, "--json" in p.flags
            )  # the files already imported before the error
            raise
    _print_reports(reports, "--json" in p.flags)
    # an override uid no file contained (with no files at all, every override is unused)
    unused = sorted(
        u for u in {*spools, *methods} if all(u in r["unused_uids"] for r in reports)
    )
    if unused:
        print(
            f"warning: --spool/--method name uid(s) in no imported file: {_safe(', '.join(unused))}",
            file=sys.stderr,
        )
    refused = sum(r["counts"]["refused"] for r in reports)
    if refused:
        raise GateFailure(
            f"{refused} line(s) refused",
            command=f"{PREFIX} import",
            remediation=[
                "Nothing of a refused line was written; fix the cause (--spool / --method UID=…) and "
                "import again — imported lines are skipped by uid."
            ],
        )
    return 0


def _change_line(c: dict[str, Any]) -> str:
    where = "/".join(x for x in (c["printer"], c.get("head")) if x)
    new = json.dumps(c["new"], ensure_ascii=False) if c["new"] is not None else "-"
    if len(new) > 80:
        new = new[:77] + "…"
    cal = f" cal #{c['calibration_id']}" if c.get("calibration_id") else ""
    return _safe(
        f"#{c['id']:<5} {c['at'][:16].replace('T', ' ')}  {where:<9} {c['kind']:<21} "
        f"{c['status']:<21} {c['source']:<18} {c['key']}  → {new}{cal}"
    )


def _history(argv: list[str]) -> int:
    from printops.cli._common import api_errors, client, parse

    p = parse(
        argv,
        "history",
        values={"--printer", "--kind", "--since", "--limit"} | COMMON_VALUES,
        flags=COMMON_FLAGS,
        prefix=PREFIX,
    )
    if not p.opts.get("--printer"):
        raise UsageError("--printer is required", command=f"{PREFIX} history")
    raw_limit = p.opts.get("--limit", "50")
    if not raw_limit.isdigit() or int(raw_limit) < 1:
        raise UsageError(
            f"--limit must be a positive integer, got {raw_limit!r}",
            command=f"{PREFIX} history",
        )
    limit = int(raw_limit)
    with api_errors("history", prefix=PREFIX):
        data = client(p, "history", prefix=PREFIX).get(
            "/v1/settings/history",
            printer=p.opts["--printer"],
            kind=p.opts.get("--kind"),
            since=p.opts.get("--since"),
            limit=limit,
        )
    if "--json" in p.flags:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    if not data["changes"]:
        print("no changes")
    for c in data["changes"]:
        print(_change_line(c))
    return 0


SUBS = {"import": _import, "history": _history}


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if argv else 1
    if any(a in ("-h", "--help") for a in argv[1:]):
        print(USAGE)
        return 0
    fn = SUBS.get(argv[0])
    if fn is None:
        raise UsageError(
            f"unknown settings subcommand {argv[0]!r}; accepted: {', '.join(SUBS)}",
            command=PREFIX,
            remediation=["Run `3d settings --help`."],
        )
    return fn(argv[1:])


COMMAND = Command(
    name="settings",
    group="SLICING",
    summary="printer settings-change journal via 3d-api: import the offline buffers (calibrations included), history",
    usage=USAGE,
    run=run,
)
