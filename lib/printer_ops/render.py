"""render.py — the text of a printer's live status, shared by `3d printer status` and `3d printers show`.

WHAT: `status_lines` turns a PrinterStatus into the labelled lines (state, file + progress,
  temperatures, loaded heads, message); `render_status` prefixes the adapter header line.
WHY one place: the two commands print the same facts and must not drift apart.
INVARIANTS: stdlib only; pure formatting, no I/O.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from printer_ops.base import Adapter, PrinterStatus


def fmt_temp(v: float | None) -> str:
    return "-" if v is None else f"{v:.0f}" if float(v).is_integer() else f"{v:.1f}"


def status_lines(st: PrinterStatus, width: int = 10) -> list[str]:
    """The labelled status lines, indented two spaces, labels padded to `width`."""

    def line(label: str, text: str) -> str:
        return f"  {label:<{width}}{text}"

    lines = [line("state", f"{st.state}  [{st.raw_state}]")]
    if st.filename:
        prog = f"  {st.progress * 100:.1f}%" if st.progress is not None else ""
        lines.append(line("file", f"{st.filename}{prog}"))
    if st.temps:
        lines.append(line("temps", "  ".join(f"{k} {fmt_temp(a)}/{fmt_temp(t)}" for k, (a, t) in st.temps.items())))
    if st.slots:
        heads = [
            f"{s.label} {s.color or '?'} {s.material or ''}".rstrip() if s.present else f"{s.label} empty"
            for s in st.slots
        ]
        lines.append(line("heads", " | ".join(heads)))
    if st.message:
        lines.append(line("message", st.message))
    return lines


def render_status(name: str, adapter: Adapter, st: PrinterStatus) -> str:
    head = f"{name}  ({adapter.kind}  {adapter.host}:{adapter.port}{'  ' + st.model if st.model else ''})"
    return "\n".join([head, *status_lines(st)])
