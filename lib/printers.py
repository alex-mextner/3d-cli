"""Compatibility wrapper for registries.printers."""
from __future__ import annotations

from registries.printers import (
    ACCEPTED_FIRMWARE,
    DEFAULT_NOZZLE_MM,
    Printer,
    PrinterError,
    get_printer,
    load_printers,
)

__all__ = [
    "ACCEPTED_FIRMWARE",
    "DEFAULT_NOZZLE_MM",
    "Printer",
    "PrinterError",
    "get_printer",
    "load_printers",
]
