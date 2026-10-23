"""printer_fw — the engine behind `3d printers fw` (firmware / root / Klipper-unlock tool).

Layout: profiles.py (YAML schema, loader, validator), probes.py (read-only probes), detect.py
(state detection), plan.py (shortest transition path), guards.py (machine-checked pitfalls),
backup.py (read-only backups + manifests), engine.py (dry-run / apply), runner.py (the ssh
command-runner abstraction), target.py (fleet printer -> endpoint), render.py (text/JSON output).
Data lives in lib/data/firmware/*.yaml; reference: docs/printers/firmware-profiles.md.

Import-light on purpose: stdlib only (pyyaml is imported lazily where profiles are read).
"""

from __future__ import annotations
