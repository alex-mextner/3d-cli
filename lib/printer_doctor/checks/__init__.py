"""Drop-in checks of `3d printers doctor`: one module per theme, each defining
`CHECKS: tuple[Check, ...]` (printer_doctor.registry discovers them; see model.Check).
Modules whose name starts with `_` are helpers, not check modules."""
