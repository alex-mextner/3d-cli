"""render.py — the text of a printer's live status, shared by `3d printer status` and `3d printers show`.

WHAT: `status_lines` turns a PrinterStatus into the labelled lines (state, file + progress,
  temperatures, loaded heads, message); `render_status` prefixes the adapter header line.
WHY one place: the two commands print the same facts and must not drift apart.
  `clean` is the ONE sanitizer of every printer-supplied string a text renderer prints.
INVARIANTS: stdlib only; pure formatting, no I/O. A string the printer (or a web page) controls
  never reaches a terminal raw: control characters (ESC, BEL, C1 incl. 0x9b, newlines, Unicode line
  separators and bidi overrides) become `?` and a long value is cut with an ellipsis, so a hostile
  or broken printer can neither drive the user's terminal nor flood it. Normal text is unchanged.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

_CONTROLS = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
NAME_W, WORD_W, TEXT_W = 80, 24, 200  # file names / one-word states / free text and errors
MAX_ITEMS = 16  # heads or heaters listed on one line (a U1 has 4); the rest is `+N more`

if TYPE_CHECKING:
    from printer_ops.base import Adapter, PrinterStatus


def strip_controls(text: str) -> str:
    """`text` with every control character replaced by `?` (no truncation)."""
    return _CONTROLS.sub("?", text)


def clean(value: object, cap: int = TEXT_W) -> str:
    """A printer-supplied value, safe to print: controls replaced, cut to `cap` chars with an ellipsis."""
    text = strip_controls(str(value))
    return text if len(text) <= cap else text[: cap - 1] + "\u2026"


def _more(parts: list[str], sep: str) -> str:
    """At most MAX_ITEMS of `parts` joined by `sep`, then `+N more`, never longer than TEXT_W."""
    shown = sep.join(parts[:MAX_ITEMS])
    if len(parts) > MAX_ITEMS:
        shown += f"{sep}+{len(parts) - MAX_ITEMS} more"
    return clean(shown, TEXT_W)


def fmt_temp(v: float | None) -> str:
    return "-" if v is None else f"{v:.0f}" if float(v).is_integer() else f"{v:.1f}"


def status_lines(st: PrinterStatus, width: int = 10) -> list[str]:
    """The labelled status lines, indented two spaces, labels padded to `width`."""

    def line(label: str, text: str) -> str:
        return f"  {label:<{width}}{text}"

    lines = [line("state", f"{clean(st.state, WORD_W)}  [{clean(st.raw_state, NAME_W)}]")]
    if st.filename:
        prog = f"  {st.progress * 100:.1f}%" if st.progress is not None else ""
        lines.append(line("file", f"{clean(st.filename, NAME_W)}{prog}"))
    if st.temps:
        lines.append(line("temps", _more(
            [f"{clean(k, WORD_W)} {fmt_temp(a)}/{fmt_temp(t)}" for k, (a, t) in st.temps.items()], "  ")))
    if st.slots:
        heads = [
            f"{clean(s.label, WORD_W)} {clean(s.color or '?', WORD_W)} {clean(s.material or '', WORD_W)}".rstrip()
            if s.present else f"{clean(s.label, WORD_W)} empty"
            for s in st.slots
        ]
        lines.append(line("heads", _more(heads, " | ")))
    if st.message:
        lines.append(line("message", clean(st.message)))
    return lines


def render_status(name: str, adapter: Adapter, st: PrinterStatus) -> str:
    model = "  " + clean(st.model, NAME_W) if st.model else ""
    head = f"{clean(name, NAME_W)}  ({adapter.kind}  {clean(adapter.host, NAME_W)}:{adapter.port}{model})"
    return "\n".join([head, *status_lines(st)])
