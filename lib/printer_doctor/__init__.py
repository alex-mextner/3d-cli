"""printer_doctor — the engine behind `3d printers doctor` (issue #519).

Layout: model.py (Check / Verdict / Fix / results), context.py (what a check may read, cached and
injectable), engine.py (run checks, apply fixes by risk class, verify, summarize), registry.py
(drop-in discovery of `checks/*.py`), journal.py (settings-change record of an applied fix),
targets.py (the fleet registry joined with the effective devices), render.py (text + JSON).
Stdlib only at import time. docs/commands/printers-doctor.md is the reference.
"""
