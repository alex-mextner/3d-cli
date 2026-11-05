"""macros.py — what `3d printer macro run` and the alias subcommands may send to a Klipper printer.

WHAT: the pure rules between a user's words and one G-code script: a macro name and its
  `KEY=VALUE` parameters are checked character by character (`build_line`), a command is put
  into a risk class (`classify`), and `assess` turns a script into the gates it must pass
  (`--yes`, an idle printer). `MacroCatalog` is what the printer says it defines
  (Moonraker `/printer/objects/list` + `/printer/gcode/help`).

WHY: Moonraker's `/printer/gcode/script` runs ANY text, newlines and `;` included, so one
  unchecked value could append `M112` or a whole second command. Nothing here is free text:
  a name is an identifier, a value is a short token from a fixed alphabet, and the emergency
  stop is never sent (the printer's own button is the way to stop, not a remote script).
  Issue https://github.com/alex-mextner/3d-cli/issues/718.

RISK CLASSES (`classify`): `inert` (reads, messages: any time), `job` (pause/resume/cancel
  style: allowed while a job runs), `heating`, `motion` (everything not known, because a
  macro body is not checked), `dangerous` (restarts, config writes, kinematics). heating,
  motion and dangerous need an idle printer; dangerous and any macro outside the printer's
  `trusted_macros` also need `--yes`.

INVARIANTS: stdlib only; nothing here touches the network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from errors import InvalidArgument
from printer_ops.base import PrinterRefused

INERT, JOB, HEATING, MOTION, DANGEROUS = (
    "inert",
    "job",
    "heating",
    "motion",
    "dangerous",
)
KINDS: Final = (INERT, JOB, HEATING, MOTION, DANGEROUS)
IDLE_KINDS: Final = frozenset({HEATING, MOTION, DANGEROUS})

# Never sent, whatever the flags: the emergency stop is the printer's own button.
DENIED: Final = frozenset({"M112", "EMERGENCY_STOP"})
_JOB: Final = frozenset(
    {
        "PAUSE",
        "RESUME",
        "CANCEL_PRINT",
        "CLEAR_PAUSE",
        "M600",
        "M220",
        "M221",
        "SET_PAUSE_NEXT_LAYER",
        "SET_PAUSE_AT_LAYER",
    }
)
_INERT: Final = frozenset(
    {
        "M105",
        "M114",
        "M115",
        "M117",
        "GET_POSITION",
        "STATUS",
        "HELP",
        "RESPOND",
        "SET_GCODE_VARIABLE_QUERY",
    }
)
_INERT_PREFIXES: Final = ("QUERY_", "GET_")
_HEATING: Final = frozenset(
    {
        "M104",
        "M109",
        "M140",
        "M141",
        "M190",
        "M191",
        "SET_HEATER_TEMPERATURE",
        "TURN_OFF_HEATERS",
        "TEMPERATURE_WAIT",
        "SET_TEMPERATURE_FAN_TARGET",
        "SET_PURIFIER_MODE",
    }
)
_DANGEROUS: Final = frozenset(
    {
        "FIRMWARE_RESTART",
        "RESTART",
        "SAVE_CONFIG",
        "SDCARD_RESET_FILE",
        "SET_KINEMATIC_POSITION",
        "FORCE_MOVE",
        "M999",
        "M84",
        "M18",
        "SHUTDOWN",
        "SHAPER_CALIBRATE",
        "PID_CALIBRATE",
        "PROBE_CALIBRATE",
        "Z_OFFSET_APPLY_PROBE",
        "Z_OFFSET_APPLY_ENDSTOP",
    }
)

_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_NUMERIC_CODE: Final = re.compile(
    r"[GMT][0-9]+"
)  # G28, M104, T0: standard codes, not macros
# A value is one short token: no space, quote, `;`, `{`, `%`, newline. `,` and `:` carry lists/pairs.
_VALUE: Final = re.compile(r"[A-Za-z0-9_.,:+/-]{1,64}")
_WORD: Final = re.compile(r"[A-Za-z0-9_]+")


def is_numeric_code(name: str) -> bool:
    """`G28`, `M104`, `T2`: a standard G-code, which only a printer-defined macro of that name may override."""
    return _NUMERIC_CODE.fullmatch(name.upper()) is not None


def check_name(name: str) -> str:
    """The upper-cased command name; InvalidArgument unless it is a plain identifier, PrinterRefused when denied."""
    if not _NAME.fullmatch(name):
        raise InvalidArgument(
            "macro",
            repr(name),
            ["an identifier: letters, digits, _ (starts with a letter or _)"],
            command="printer",
            extra="Newlines, spaces, `;` and quotes are never sent.",
        )
    upper = name.upper()
    refuse_denied(upper, "macro name")
    return upper


def refuse_denied(token: str, what: str) -> None:
    for word in _WORD.findall(token.upper()):
        if word in DENIED:
            raise PrinterRefused(
                f"{what} contains {word}: 3d never sends the emergency stop",
                command="printer",
                remediation=[
                    "Stop the printer with its own emergency-stop control (touchscreen, Fluidd, power)."
                ],
            )


def check_value(key: str, value: str) -> str:
    if not _VALUE.fullmatch(value):
        raise InvalidArgument(
            key,
            repr(value),
            ["1-64 characters from A-Z a-z 0-9 _ . , : + / -"],
            command="printer",
            extra="No spaces, quotes, `;`, braces or newlines: a value is one token.",
        )
    refuse_denied(value, f"value of {key}")
    return value


def build_line(name: str, params: list[str]) -> str:
    """`NAME KEY=VALUE ...` from a macro name and `KEY=VALUE` words; every character is validated here."""
    out = [check_name(name)]
    seen: set[str] = set()
    for word in params:
        key, sep, value = word.partition("=")
        if not sep or not _NAME.fullmatch(key):
            raise InvalidArgument(
                "parameter",
                repr(word),
                ["KEY=VALUE, e.g. EXTRUDER=0"],
                command="printer",
            )
        key = key.upper()
        if key in seen:
            raise InvalidArgument(
                "parameter", key, ["each key once"], command="printer"
            )
        seen.add(key)
        out.append(f"{key}={check_value(key, value)}")
    return " ".join(out)


def command_of(line: str) -> str:
    """First word of a G-code line, upper-cased (`G28 X` -> `G28`)."""
    return line.split(None, 1)[0].upper() if line.strip() else ""


def classify(name: str) -> str:
    n = name.upper()
    if n in _DANGEROUS:
        return DANGEROUS
    if n in _JOB:
        return JOB
    if n in _HEATING:
        return HEATING
    if n in _INERT or n.startswith(_INERT_PREFIXES):
        return INERT
    return MOTION


@dataclass(frozen=True)
class Assessment:
    kind: str  # the strictest class among the lines
    needs_idle: bool
    yes_reasons: tuple[str, ...]  # why `--yes` is needed; empty = not needed


_RANK: Final = {INERT: 0, JOB: 1, HEATING: 2, MOTION: 3, DANGEROUS: 4}


def assess(
    lines: list[str],
    trusted: frozenset[str],
    *,
    curated: bool,
    kind: str | None = None,
    entry_yes: bool = False,
) -> Assessment:
    """Gates for a script. `curated` lines come from the alias table (the printer's own table or the
    user's `aliases:`): they are vetted, so only a dangerous class or `yes: true` asks for --yes.
    A `macro run` line (not curated) outside `trusted` asks for --yes. `kind` is an alias's declared class;
    the strictest of it and each line's class wins."""
    worst = kind if kind in _RANK else INERT
    reasons: list[str] = []
    for line in lines:
        cmd = command_of(line)
        refuse_denied(cmd, "command")
        cls = classify(cmd)
        if _RANK[cls] > _RANK[worst]:
            worst = cls
        if cls == DANGEROUS:
            reasons.append(
                f"{cmd} is dangerous (restart, config write or kinematics change)"
            )
        elif not curated and cmd not in trusted:
            reasons.append(f"{cmd} is not in this printer's trusted_macros")
    if entry_yes:
        reasons.append("this alias is declared to need confirmation")
    return Assessment(worst, worst in IDLE_KINDS, tuple(dict.fromkeys(reasons)))


@dataclass(frozen=True)
class MacroCatalog:
    """What a printer defines: `macros` = [gcode_macro X] names, `commands` = every extended command
    with its help text (macros included)."""

    macros: frozenset[str]
    commands: dict[str, str] = field(default_factory=dict, hash=False)

    def knows(self, name: str) -> bool:
        n = name.upper()
        return n in self.macros or n in self.commands

    def describe(self, name: str) -> str:
        return self.commands.get(name.upper(), "")


def parse_catalog(objects: object, help_text: object) -> MacroCatalog:
    """Moonraker's `objects/list` result (`{"objects": [...]}`) and `gcode/help` result (`{CMD: text}`)."""
    names = objects.get("objects") if isinstance(objects, dict) else None
    if not isinstance(names, list) or not isinstance(help_text, dict):
        raise ValueError("objects/list or gcode/help has an unexpected shape")
    macros = frozenset(
        str(n).split(" ", 1)[1].strip().upper()
        for n in names
        if isinstance(n, str) and n.lower().startswith("gcode_macro ") and " " in n
    )
    return MacroCatalog(macros, {str(k).upper(): str(v) for k, v in help_text.items()})
