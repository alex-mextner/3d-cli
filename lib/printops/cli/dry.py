"""`3d printops dry` — drying sessions and the due list, through 3d-api (#162)."""

from __future__ import annotations

import json
from typing import Any

from errors import UsageError
from printops.cli import Sub
from printops.cli._common import Parsed, api_errors, client, parse

USAGE = """3d printops dry start DRYER SPOOL_ID... [--temp C] [--hours H] [--rh PCT] [--weights ID=G,...] [--note TEXT]
3d printops dry finish SESSION_ID [--rh PCT] [--weights ID=G,...]
3d printops dry ls [--open] [--spool ID] [--limit N]
3d printops dry due [--all] [--check]
  common: [--json] [--api URL] [--token TOKEN]

  Drying sessions: which spools went into which dryer, at what temperature, for how long. Finishing
  a session sets the spools' Spoolman `last_dried_at` and `dry_state=fresh`. 3d-api re-checks every
  spool against fleet/policy.yaml (`drying:` hygroscopic classes) and emits `drying.due` when a
  spool needs drying. `ls` and `due` need the `read` scope, the rest `write`.

Subcommands:
  start   put spools into a dryer location (kind dryer or dry_box, e.g. dryer:1). Without --temp/--hours
          the spools' recipes decide: the lowest recommended temperature and the longest time.
  finish  take them out; optional weights record the water lost (ledger note — not filament usage).
  ls      sessions, newest first.
  due     spools due (or wet) for drying, with the reason; read-only unless --check.

Options:
  --temp C           drying temperature, °C (20..150)
  --hours H          target time, hours (the timer emits drying.finished when it elapses)
  --rh PCT           dryer humidity before (start) / after (finish), % — default: read from the
                     dryer location's Home Assistant humidity sensor when one is configured
  --weights ID=G,... gross scale weights in grams per spool, e.g. 12=1012.5,13=998
  --note TEXT        free text kept with the session
  --open             only sessions still running
  --spool ID         only sessions that dried this spool
  --limit N          at most N sessions (default 50)
  --all              every active spool's assessment, not only due/wet ones
  --check            run the periodic check now: update Spoolman dry_state, emit drying.due
  --json             print the API's JSON
  --api URL          3d-api base URL (default: $PRINTOPS_API, else ~/.config/3d-cli/printops.yaml)
  --token TOKEN      bearer token (default: $PRINTOPS_TOKEN, else the same file)

Examples:
  3d printops dry start dryer:1 12 13
  3d printops dry start dryer:1 21 --temp 80 --hours 12 --weights 21=1180
  3d printops dry finish 4 --weights 21=1171.5
  3d printops dry ls --open
  3d printops dry due
  3d printops dry due --all --json | jq '.spools[] | {spool_id, state, reasons}'"""

COMMON_VALUES = {"--api", "--token"}
COMMON_FLAGS = {"--json"}


def _float(p: Parsed, name: str) -> float | None:
    raw = p.opts.get(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError as e:
        raise UsageError(
            f"{name} needs a number, got {raw!r}", command="printops dry"
        ) from e


def _int(raw: str, what: str) -> int:
    try:
        return int(raw)
    except ValueError as e:
        raise UsageError(
            f"{what} must be an integer id, got {raw!r}", command="printops dry"
        ) from e


def _weights(p: Parsed) -> dict[str, float] | None:
    raw = p.opts.get("--weights")
    if raw is None:
        return None
    out: dict[str, float] = {}
    for part in raw.split(","):
        sid, sep, grams = part.partition("=")
        if not sep:
            raise UsageError(
                f"--weights wants ID=GRAMS pairs, got {part!r}",
                command="printops dry",
                remediation=["e.g. --weights 12=1012.5,13=998"],
            )
        try:
            out[str(_int(sid.strip(), "spool"))] = float(grams)
        except ValueError as e:
            raise UsageError(
                f"--weights: {grams!r} is not grams", command="printops dry"
            ) from e
    return out


def _session_line(s: dict[str, Any]) -> str:
    spools = ", ".join(
        str(x["spool_id"])
        + (
            f" {x['weight_before']:g}→{x['weight_after']:g} g"
            if x.get("weight_before") and x.get("weight_after")
            else ""
        )
        for x in s["spools"]
    )
    if s["open"]:
        status = f"running, ends {s['ends_at']}"
    else:
        status = f"finished {s['ended_at']}"
    rh = ""
    if s.get("rh_before") is not None or s.get("rh_after") is not None:

        def fmt(v: Any) -> str:
            return "?" if v is None else f"{v:g}%"

        rh = f"  RH {fmt(s.get('rh_before'))}→{fmt(s.get('rh_after'))}"
    return (
        f"#{s['id']}  {s['dryer']}  {s['temp_c']:g} °C × {s['target_h']:g} h  {status}  "
        f"spools: {spools}{rh}"
    )


def _due_line(a: dict[str, Any]) -> str:
    where = f"  @ {a['location']}" if a.get("location") else ""
    why = "; ".join(a.get("reasons") or []) or "-"
    line = f"{a['state']:<7} {a['label']}  [{a['hygroscopic_class']}]{where}  {why}"
    for note in a.get("notes") or []:
        line += f"\n        note: {note}"
    return line


def _print(as_json: bool, data: Any, human: list[str]) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        for line in human:
            print(line)


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    verb, rest = argv[0], argv[1:]
    sub = f"dry {verb}"
    if verb == "start":
        p = parse(
            rest,
            sub,
            values={*COMMON_VALUES, "--temp", "--hours", "--rh", "--weights", "--note"},
            flags=COMMON_FLAGS,
            n_pos=(2, 17),
        )
        body: dict[str, Any] = {
            "dryer": p.pos[0],
            "spools": [_int(x, "spool") for x in p.pos[1:]],
            "source": "cli",
        }
        for opt, key in (
            ("--temp", "temp_c"),
            ("--hours", "hours"),
            ("--rh", "rh_before"),
        ):
            if (value := _float(p, opt)) is not None:
                body[key] = value
        if (weights := _weights(p)) is not None:
            body["weights_before"] = weights
        if "--note" in p.opts:
            body["note"] = p.opts["--note"]
        with api_errors(sub):
            out = client(p, sub).post("/v1/drying/sessions", body)
        _print(
            "--json" in p.flags,
            out,
            [
                f"started {_session_line(out['session'])}",
                *(f"  warning: {w}" for w in out["warnings"]),
            ],
        )
        return 0
    if verb == "finish":
        p = parse(
            rest,
            sub,
            values={*COMMON_VALUES, "--rh", "--weights"},
            flags=COMMON_FLAGS,
            n_pos=1,
        )
        session_id = _int(p.pos[0], "session")
        body = {}
        if (rh := _float(p, "--rh")) is not None:
            body["rh_after"] = rh
        if (weights := _weights(p)) is not None:
            body["weights_after"] = weights
        with api_errors(sub):
            out = client(p, sub).post(f"/v1/drying/sessions/{session_id}/finish", body)
        _print(
            "--json" in p.flags,
            out,
            [
                f"finished {_session_line(out['session'])}",
                *(f"  warning: {w}" for w in out["warnings"]),
            ],
        )
        return 0
    if verb == "ls":
        p = parse(
            rest,
            sub,
            values={*COMMON_VALUES, "--spool", "--limit"},
            flags={*COMMON_FLAGS, "--open"},
        )
        params = {
            "open": "true" if "--open" in p.flags else None,
            "spool": p.int_opt("--spool", sub),
            "limit": p.int_opt("--limit", sub),
        }
        with api_errors(sub):
            out = client(p, sub).get("/v1/drying/sessions", **params)
        _print(
            "--json" in p.flags,
            out,
            [_session_line(s) for s in out["sessions"]] or ["no drying sessions"],
        )
        return 0
    if verb == "due":
        p = parse(
            rest, sub, values=COMMON_VALUES, flags={*COMMON_FLAGS, "--all", "--check"}
        )
        api = client(p, sub)
        with api_errors(sub):
            if "--check" in p.flags:
                report = api.post("/v1/drying/check")
                head = [
                    f"checked {report['checked']} spool(s); dry_state changed: {len(report['changed'])}; "
                    f"drying.due emitted: {len(report['alerts'])}",
                    *(f"  error: {e}" for e in report["errors"]),
                ]
            else:
                head = []
            out = api.get("/v1/drying/due", all="true" if "--all" in p.flags else None)
        rows = out["spools"]
        lines = [*head, *(_due_line(a) for a in rows)]
        if not rows:
            lines.append(f"nothing due for drying ({out['checked']} spool(s) checked)")
        _print("--json" in p.flags, out, lines)
        return 0
    raise UsageError(
        f"unknown `printops dry` subcommand {verb!r}",
        command="printops dry",
        remediation=["One of: start, finish, ls, due. See `3d printops dry --help`."],
    )


SUB = Sub(
    name="dry",
    summary="drying sessions (start/finish/ls) and spools due for drying",
    usage=USAGE,
    run=run,
)
