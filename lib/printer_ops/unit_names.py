"""unit_names.py — the names of the printer supervision units, with NO imports (#747).

WHAT: which systemd user unit files exist (`TEMPLATES`) and how an instance of one is named for a printer.
WHY a module of its own: the on-demand check (ondemand.py) runs every 30 s as a one-shot process and must start
  in a few milliseconds; it needs the worker unit names but not supervise_units.py (re, shlex, subprocess,
  dataclasses, pathlib). supervise_units.py re-exports these names, so there is one definition.

KINDS: `watch` and `vision` are the heavy workers (a service each); `ondemand` is the supervisor and is the
  TIMER `3d-printer-ondemand@NAME.timer`; `ondemand-check` is the one-shot SERVICE the timer runs
  (`3d-printer-ondemand@NAME.service`, nothing runs between two checks).
"""

from __future__ import annotations

KINDS = ("watch", "vision")  # the workers
SUPERVISOR = (
    "ondemand"  # the always-armed unit: a timer, so no process lives between checks
)
CHECK = "ondemand-check"  # the one-shot service the timer starts
TEMPLATES = {
    SUPERVISOR: "3d-printer-ondemand@.timer",
    CHECK: "3d-printer-ondemand@.service",
    "watch": "3d-printer-watch@.service",
    "vision": "3d-printer-vision@.service",
}
TEMPLATE_KINDS = tuple(TEMPLATES)
RUNNING = frozenset({"active", "activating", "reloading"})


def template_name(kind: str) -> str:
    return TEMPLATES[kind]


def unit_name(kind: str, printer: str) -> str:
    """The instance of `kind`'s template for `printer`: `3d-printer-watch@u1.service`, and for the supervisor the
    timer `3d-printer-ondemand@u1.timer`."""
    return TEMPLATES[kind].replace("@", f"@{printer}")
