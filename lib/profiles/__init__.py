"""profiles — the user's slicer presets kept in the repo `profiles/` dir and synced with the
slicers (PrintOps decision D10, issue https://github.com/alex-mextner/3d-cli/issues/180).

Layout: slicers.py (OrcaSlicer / Snapmaker Orca / Bambu Studio data dirs, running check),
preset.py (the slicers' preset JSON / `.info` formats, slicer-local keys), sync.py (three-way
repo <-> slicer sync against a base snapshot, backups), values.py (`apply_values`, the write
path calibrations use). Stdlib only; backs `3d profiles`.
"""

from __future__ import annotations
