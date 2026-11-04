"""bed_light.py — the printer's light for a camera frame of the bed (issue #723).

WHY: a camera frame of the bed is only comparable to the reference when the bed is lit the same way.
  A dark chamber reads as "objects" (or as nothing), so every frame the bed check, the preflight
  `bed` line, a reference capture and the job watch take is taken with the printer's own light on
  and the light is put back as it was found.

WHAT:
  * `detect_light`    the light among Moonraker's `printer.objects.list`: `led NAME` / `neopixel NAME` /
                      `dotstar NAME` (SET_LED) or `output_pin NAME` (SET_PIN) whose name says light,
                      lamp, led or chamber; `watch.bed.light.object` names it when the guess is wrong.
  * `LightConfig`     `watch.bed.light` (validated in printer_ops/bed.py): object, on_gcode / off_gcode
                      (a macro such as LIGHT_ON / LIGHT_OFF), settle_s, enabled.
  * `LightSession`    reads the light's current state (read-only `objects/query`), switches it on when it
                      is off (`on()`, once, lazily), puts it back in `restore()` / on leaving the `with`.
                      `lit()` is the same as a context manager that switches on at once.
  * `LightReport`     what happened, in one line (`.text`) and as a dict for the verdict (`.to_dict()`).

WRITES: at most two scripts per frame, both through the adapter's `run_gcode`: the light on and the same
  light off again. Built-in SET_LED / SET_PIN for an autodetected light; the configured on_gcode /
  off_gcode otherwise, each first word listed in the printer's `trusted_macros` (checked when the config
  is read, like `clear_ack`). Never while a job prints or is paused, except for the job watch
  (`printing_ok`: its frame is taken during the print) and then only when the light was off. A light
  that is already on is not touched. A printer that is not full-access is never written to. No light, an
  unreadable printer or a failed switch only say so in the report: the frame is taken anyway.

INVARIANTS: stdlib only; never raises for a light problem (only KeyboardInterrupt and friends pass
  through, after the `finally` restored the light); a light that could not be read is switched on but
  not restored (its previous state is unknown, the report says "left on").
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

SETTLE_S = 1.5  # after the light is on: the camera's auto exposure and its next frame
LED_KINDS = ("led", "neopixel", "dotstar")  # SET_LED LED=name
PIN_KIND = "output_pin"  # SET_PIN PIN=name
LIGHT_KINDS = (*LED_KINDS, PIN_KIND)
# Klipper built-ins that only drive LEDs / pins: allowed in on_gcode / off_gcode without a trusted_macros entry.
BUILTIN_LIGHT_COMMANDS = ("SET_LED", "SET_PIN")
ACTIVE_JOB_STATES = (
    "printing",
    "paused",
)  # print_stats.state: the bed and the machine belong to the job
_OBJECT = re.compile(r"^(" + "|".join(LIGHT_KINDS) + r")\s+(\S+)$")
_STRONG_HINTS = (
    "light",
    "lamp",
)  # a substring of the name is enough: caselight, chamber_lamp
_WORD_HINTS = ("chamber", "led", "leds")  # whole words only: not "enabled"


@dataclass(frozen=True)
class LightConfig:
    """`watch.bed.light` of a printer (validated by printer_ops.bed.bed_config)."""

    enabled: bool = True
    object: str | None = (
        None  # "output_pin chamber_light": which Moonraker object is the light
    )
    on_gcode: tuple[
        str, ...
    ] = ()  # a macro (LIGHT_ON); default: SET_LED / SET_PIN on `object`
    off_gcode: tuple[str, ...] = ()
    settle_s: float = SETTLE_S


@dataclass(frozen=True)
class LightState:
    object: str
    on: (
        bool | None
    )  # None: the printer reports the object but not a value that says on or off


@dataclass
class LightReport:
    """What the light did for one frame; `to_dict()` goes into the bed verdict as `light`."""

    note: str = ""  # what the frame is for ("the bed check")
    found: bool = False
    object: str | None = None
    was_on: bool | None = None
    switched: bool = False
    restored: bool | None = None  # None: nothing to restore
    error: str | None = None
    detail: str = ""  # why the light was not touched / not found, with the way to fix it
    skipped: str | None = None  # off by config, or the printer may not be written to

    @property
    def on_for_frame(self) -> bool | None:
        """Was the bed lit when the frame was taken? None: unknown (no light, or an unreadable state)."""
        return True if self.switched else self.was_on

    @property
    def text(self) -> str:
        if self.skipped:
            return f"light not used: {self.skipped}"
        if not self.found:
            why = (
                self.error
                or self.detail
                or "none of the printer's objects looks like a light"
            )
            return f"no light found ({why}): the frame is taken with the room as it is"
        if self.detail:
            return f"light {self.object}: {self.detail}"
        if self.was_on:
            return f"light {self.object} was already on"
        if not self.switched:
            return f"light {self.object} could not be switched on ({self.error}): the frame is taken without it"
        for_what = f" for {self.note}" if self.note else ""
        if self.restored is None:
            return f"light {self.object} switched on{for_what}; its previous state is unknown, left on"
        if self.restored:
            return f"light {self.object} switched on{for_what} and restored (off)"
        return f"light {self.object} switched on{for_what}, NOT restored: {self.error}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "object": self.object,
            "was_on": self.was_on,
            "switched": self.switched,
            "restored": self.restored,
            "on_for_frame": self.on_for_frame,
            "error": self.error,
            "skipped": self.skipped,
            "text": self.text,
        }


# ---- finding the light ------------------------------------------------------------------------


def _hint_rank(name: str) -> int | None:
    """0 for a name that says light/lamp, 1 for chamber/led as a word, None for anything else."""
    low = name.lower()
    if any(h in low for h in _STRONG_HINTS):
        return 0
    words = re.split(r"[^a-z0-9]+", low)
    return 1 if any(w in _WORD_HINTS for w in words) else None


def detect_light(objects: Sequence[str], configured: str | None = None) -> str | None:
    """The Moonraker object that is the light: `configured` when the printer has it, else the best guess
    among the led / neopixel / dotstar / output_pin objects by name (light, lamp first, then chamber, led;
    LEDs before pins, then alphabetical so the answer never depends on the order Klipper listed them)."""
    if configured is not None:
        return configured if configured in objects else None
    ranked = []
    for obj in objects:
        m = _OBJECT.match(obj)
        rank = _hint_rank(m.group(2)) if m else None
        if m and rank is not None:
            ranked.append((rank, 1 if m.group(1) == PIN_KIND else 0, obj))
    return min(ranked)[2] if ranked else None


def list_objects(adapter: Any) -> list[str]:
    """`printer.objects.list` of a Moonraker adapter; [] for an adapter that cannot list objects."""
    get = getattr(adapter, "_get", None)
    if get is None:
        return []
    listed = get("/printer/objects/list")
    names = listed.get("objects", []) if isinstance(listed, dict) else []
    return [n for n in names if isinstance(n, str)]


def _number(x: Any) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def parse_state(obj: str, status: dict[str, Any]) -> LightState:
    """The state of `obj` in an `objects/query` status: an output_pin has `value`, a LED chain `color_data`
    (one [r, g, b, w] per LED); on = any channel above 0."""
    entry = status.get(obj)
    if not isinstance(entry, dict):
        return LightState(obj, None)
    value = _number(entry.get("value"))
    if value is not None:
        return LightState(obj, value > 0)
    data = entry.get("color_data")
    if isinstance(data, list) and data and all(isinstance(px, list) for px in data):
        levels = [_number(c) for px in data for c in px]
        return LightState(obj, any(c is not None and c > 0 for c in levels))
    return LightState(obj, None)


def read_state(adapter: Any, obj: str) -> tuple[LightState, str | None]:
    """(the light's state, print_stats.state or None) in one read-only `objects/query`."""
    status = adapter.query("print_stats&" + quote(obj))
    raw = (status.get("print_stats") or {}).get("state")
    return parse_state(obj, status), raw if isinstance(raw, str) else None


def _builtin(obj: str, level: int) -> list[str]:
    kind, name = obj.split(None, 1)
    if kind == PIN_KIND:
        return [f"SET_PIN PIN={name} VALUE={level}"]
    return [
        f"SET_LED LED={name} RED={level} GREEN={level} BLUE={level} WHITE={level} TRANSMIT=1"
    ]


def on_script(obj: str, cfg: LightConfig) -> str:
    return "\n".join(cfg.on_gcode or _builtin(obj, 1))


def off_script(obj: str, cfg: LightConfig) -> str:
    return "\n".join(cfg.off_gcode or _builtin(obj, 0))


def write_block(device: Any) -> str | None:
    """Why the printer may not be sent anything (not a full-access device), or None."""
    access = getattr(device, "access", "full")
    return None if access == "full" else f"{device.name} is {access}: no writes"


# ---- the session ------------------------------------------------------------------------------


class LightSession:
    """One light for one or more frames: `on()` when a frame is about to be taken (once; later calls do
    nothing), `restore()` when the frames are done; leaving the `with` restores, also after an error.

    `printing_ok`: the caller is the job watch, whose frame is taken while the job prints; the light is
    then switched only when it was off. Without it nothing is touched while a job prints or is paused,
    or when the printer's state cannot be read. `blocked`: why the printer may not be written to."""

    def __init__(
        self,
        adapter: Any,
        cfg: LightConfig | None = None,
        *,
        printing_ok: bool = False,
        note: str = "",
        blocked: str | None = None,
        objects: Sequence[str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.adapter = adapter
        self.cfg = cfg or LightConfig()
        self.printing_ok = printing_ok
        self.blocked = blocked
        self.objects = objects
        self.sleep = sleep
        self.report = LightReport(note=note)
        self._started = False
        self._dirty = (
            False  # the light may be on because of us: restore() sends the off script
        )

    def __enter__(self) -> LightSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.restore()

    def on(self) -> LightReport:
        if not self._started:
            self._started = True
            try:
                self._switch_on()
            except Exception as exc:  # noqa: BLE001 — a light problem never stops the frame
                self.report.error = f"{type(exc).__name__}: {exc}"
        return self.report

    def _skip_reason(self) -> str | None:
        if not self.cfg.enabled:
            return "off by config (watch.bed.light.enabled: false)"
        return self.blocked

    def _switch_on(self) -> None:
        rep = self.report
        why = self._skip_reason()
        if why is not None:
            rep.skipped = why
            return
        names = self.objects if self.objects is not None else list_objects(self.adapter)
        obj = detect_light(names, self.cfg.object)
        if obj is None:
            rep.detail = (
                f"the printer has no object {self.cfg.object!r}"
                if self.cfg.object
                else "no led / output_pin named light, lamp, led or chamber; name it in watch.bed.light.object"
            )
            return
        state, job = read_state(self.adapter, obj)
        rep.found, rep.object, rep.was_on = True, obj, state.on
        if job in ACTIVE_JOB_STATES and not self.printing_ok:
            rep.detail = f"left alone: the printer is {job}"
        elif job is None and not self.printing_ok:
            rep.detail = "left alone: the printer's state could not be read"
        elif state.on is not True:
            self._send_on(obj, state)

    def _send_on(self, obj: str, state: LightState) -> None:
        self._dirty = state.on is False  # an unreadable previous state is not restored
        try:
            self.adapter.run_gcode(on_script(obj, self.cfg))
        except Exception as exc:  # noqa: BLE001 — a half-sent script may have lit it: restore() still sends off
            self.report.error = f"{type(exc).__name__}: {exc}"
            return
        self.report.switched = True
        self.sleep(self.cfg.settle_s)

    def restore(self) -> None:
        """Put the light back (off). Idempotent; a failure is in the report, never raised."""
        if not self._dirty or self.report.object is None:
            return
        self._dirty = False
        try:
            self.adapter.run_gcode(off_script(self.report.object, self.cfg))
        except Exception as exc:  # noqa: BLE001
            self.report.restored, self.report.error = (
                False,
                f"{type(exc).__name__}: {exc}",
            )
            return
        self.report.restored = True


@contextmanager
def lit(
    adapter: Any,
    cfg: LightConfig | None = None,
    *,
    printing_ok: bool = False,
    note: str = "",
    blocked: str | None = None,
    objects: Sequence[str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[LightReport]:
    """The light on for the body, put back after it (also when the body raises). Yields the report."""
    session = LightSession(
        adapter,
        cfg,
        printing_ok=printing_ok,
        note=note,
        blocked=blocked,
        objects=objects,
        sleep=sleep,
    )
    try:
        yield session.on()
    finally:
        session.restore()


__all__ = [
    "ACTIVE_JOB_STATES",
    "BUILTIN_LIGHT_COMMANDS",
    "LIGHT_KINDS",
    "SETTLE_S",
    "LightConfig",
    "LightReport",
    "LightSession",
    "LightState",
    "detect_light",
    "lit",
    "list_objects",
    "off_script",
    "on_script",
    "parse_state",
    "read_state",
    "write_block",
]
