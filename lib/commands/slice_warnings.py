"""3d slice-warnings — record the slicer's warnings for a G-code, accept them one by one.

WHAT: `record` reads what the slicer printed for one slice (CLI log, result.json, the sliced
  3MF's slice_info) and writes NAME.slice-warnings.json next to NAME.gcode with the G-code's
  SHA-256; `accept` stores a reason with one warning; `check` is the `slicer warn` gate of
  `3d print-preflight`: FAIL when the sidecar is missing, stale or holds an unaccepted warning.

WHY: the slicer is the only one who sees floating regions, empty layers, path conflicts and
  bad temperatures, and it says so in a log that scrolls past. Binding its warnings to the
  exact G-code turns them into a gate before `3d printer start`.

The parsing lives in lib/slicing/slice_warnings.py (stdlib); this module stays import-light.
"""

from __future__ import annotations

from pathlib import Path

from cli.registry import Command
from errors import GateFailure, InputNotFound, InvalidArgument, UsageError

USAGE = """3d slice-warnings <record|accept|check> FILE.gcode ...
  Bind the slicer's warnings to the G-code they belong to (NAME.slice-warnings.json, with the
  G-code's SHA-256). `3d print-preflight` / `3d printer start` FAIL until every warning of this
  exact G-code is resolved (re-slice without it) or accepted with a reason.

  record FILE.gcode [--log LOG]... [--result result.json]... [--sliced-3mf FILE.3mf]...
                    [--accept-warning ID REASON]...
      Read the slicer's output for the slice that produced FILE.gcode, after the last edit of
      the G-code (run it after `3d gcode-thumbnails add` or any other post-process; `3d
      gcode-thumbnails add` moves a current sidecar along itself):
        --log LOG          the slicer CLI's stdout+stderr (`-` = stdin); Orca-family
                           `[warning]` / `[error]` entries, multi-line messages included
        --result FILE      the CLI's result.json in its --outputdir (return_code, warning_message)
        --sliced-3mf FILE  a sliced project (.gcode.3mf, or --export-3mf output):
                           Metadata/slice_info.config <warning> entries
        --accept-warning ID REASON   accept a warning reported by this slice (repeatable)
      Acceptances in the previous sidecar carry over to the same warning (same level and
      message). Headless-host lines (version banner, no display: OpenGL/GLFW/Wayland) are kept
      under "ignored", not as warnings. Exit 0; open warnings are listed.

  accept FILE.gcode ID REASON
      Store REASON with warning ID (ids are printed by record/check). The sidecar must match
      FILE.gcode's SHA-256.

  check FILE.gcode [--json]
      The print-preflight gate on its own. Exit 1 on FAIL.

Examples:
  "$ORCA" --debug 2 --slice 0 --outputdir out/ --export-3mf sliced.3mf plate.3mf > out/slice.log 2>&1
  3d slice-warnings record out/plate.gcode --log out/slice.log --result out/result.json --sliced-3mf out/sliced.3mf
  3d slice-warnings accept out/plate.gcode wc6181fe0 'bed 60 C is the K1 profile default for this PLA'
  3d slice-warnings record out/plate.gcode --log out/slice.log --accept-warning wc264eaa3 'colours set per head on the printer'
  3d slice-warnings check out/plate.gcode"""

_CMD = "slice-warnings"


def _need(argv: list[str], i: int, n: int, flag: str) -> list[str]:
    """The n values after argv[i]; `-` (stdin) counts as a value, other `--x` do not."""
    values = argv[i + 1 : i + 1 + n]
    if len(values) < n or any(v.startswith("--") for v in values):
        raise UsageError(f"{flag} needs {n} value(s)", command=_CMD, remediation=["See `3d slice-warnings --help`."])
    return values


def _record(argv: list[str]) -> int:
    import sys

    from slicing.slice_warnings import SidecarError, record

    pos: list[str] = []
    logs: list[tuple[str, str]] = []
    results: list[Path] = []
    sliced: list[Path] = []
    accept: dict[str, str] = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--log", "--result", "--sliced-3mf"):
            value = _need(argv, i, 1, a)[0]
            if a == "--log":
                if value == "-":
                    logs.append(("stdin", sys.stdin.read()))
                else:
                    p = Path(value)
                    if not p.is_file():
                        raise InputNotFound(value, command=_CMD)
                    logs.append((str(p), p.read_text(encoding="utf-8", errors="replace")))
            else:
                p = Path(value)
                if not p.is_file():
                    raise InputNotFound(value, command=_CMD)
                (results if a == "--result" else sliced).append(p)
            i += 2
        elif a == "--accept-warning":
            wid, reason = _need(argv, i, 2, a)
            accept[wid] = reason
            i += 3
        elif a.startswith("--"):
            raise UsageError(f"unknown option {a}", command=_CMD, remediation=["See `3d slice-warnings --help`."])
        else:
            pos.append(a)
            i += 1
    gcode = _one_gcode(pos)
    try:
        rec = record(gcode, logs=logs, results=results, sliced_3mfs=sliced, accept=accept)
    except SidecarError as exc:
        raise UsageError(str(exc), command=_CMD, remediation=["See `3d slice-warnings --help`."]) from exc
    print(f"recorded {len(rec.warnings)} slicer warning(s) for {gcode.name} -> {rec.path} ({len(rec.open)} open, "
          f"{len(rec.ignored)} host-noise line(s) ignored)")
    for w in rec.warnings:
        state = f"accepted: {w.accepted.get('reason')}" if w.accepted else "OPEN"
        print(f"  {w.id}  {w.level:<7}  {' '.join(w.message.split())[:200]}  [{state}]")
    for n in rec.notes:
        print(f"  note: {n}")
    if rec.open:
        print(f"Resolve (fix + re-slice) or accept each: 3d slice-warnings accept {gcode} ID 'reason'")
    return 0


def _one_gcode(pos: list[str]) -> Path:
    if len(pos) != 1:
        raise UsageError(f"expected one G-code file, got {len(pos)}", command=_CMD)
    gcode = Path(pos[0])
    if not gcode.is_file():
        raise InputNotFound(str(gcode), command=_CMD)
    return gcode


def _accept(argv: list[str]) -> int:
    from slicing.slice_warnings import SidecarError, accept_warning

    if len(argv) != 3:
        raise UsageError("accept takes FILE.gcode ID REASON", command=_CMD,
                         remediation=["3d slice-warnings accept plate.gcode wc6181fe0 'why it is fine'"])
    gcode = _one_gcode(argv[:1])
    try:
        w = accept_warning(gcode, argv[1], argv[2])
    except SidecarError as exc:
        raise InvalidArgument("ID", argv[1], ["an id listed by `3d slice-warnings check`"], command=_CMD,
                              extra=str(exc)) from exc
    print(f"accepted {w.id} ({w.level}: {' '.join(w.message.split())[:160]}): {w.accepted and w.accepted['reason']}")
    return 0


def _check(argv: list[str]) -> int:
    import json

    from slicing.slice_warnings import evaluate

    as_json = "--json" in argv
    gcode = _one_gcode([a for a in argv if a != "--json"])
    status, detail, lines = evaluate(gcode)
    if as_json:
        print(json.dumps({"file": str(gcode), "status": status, "detail": detail, "lines": lines}, indent=1))
    else:
        print(f"slicer warn {gcode}: {status} — {detail}")
        for line in lines:
            print(f"  {line}")
    if status == "FAIL":
        raise GateFailure("slicer warnings not resolved", command=_CMD, silent=True)
    return 0


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    sub, rest = argv[0], argv[1:]
    if sub == "record":
        return _record(rest)
    if sub == "accept":
        return _accept(rest)
    if sub == "check":
        return _check(rest)
    raise InvalidArgument("subcommand", sub, ["record", "accept", "check"], command=_CMD)


COMMAND = Command(
    name="slice-warnings",
    group="SLICING",
    summary="record the slicer's warnings for a G-code (sidecar with its SHA-256); accept each with a reason",
    usage=USAGE,
    run=run,
)
