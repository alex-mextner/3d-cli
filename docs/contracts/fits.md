# Fit table contract (umbrella #570, S0 #571)

`lib/data/fits.yaml` (schema `3d.fits/1`) is the ONE fit/tolerance table; `lib/fits.py` loads, validates and
resolves it. Every value cites its source in the YAML comments.

| Fit | Clearance per face | Interference | tol (+/-) | Typical use |
|---|---|---|---|---|
| `snap` | 0.30 | 0 | 0.10 | snap hooks, lids |
| `sliding` (`free_running`) | 0.20 | 0 | 0.10 | drawers, inserts, hinge pins |
| `tight` (`tight_running`) | 0.10 | 0 | 0.05 | locating pins, close inserts |
| `press` | 0 | 0.10 | 0.05 | press pins, magnet pockets |
| `glue` | 0.15 | 0 | 0.10 | glued halves and seams |

`fits.resolve(fit_type, shape, printer, material, nozzle, layer)` returns a `Resolution`: signed `clearance_mm`
(base clearance - interference + correction; negative = overlap), `tol_mm`, `fallback` and a `provenance` list
of the fit-type row, the alias (if any) and the single correction row applied, plus the matching rows it
superseded. An unknown printer/material is not an error: the `*` default row applies and `fallback: true` says
so. Corrections are chosen by specificity (printer > material > nozzle > layer > shape > fit type), then source
(calibration > manual > default), then newest `measured_at`.

`3d fit-niche --fit snug|normal|loose` resolves through the table (`snug` = `tight`, `normal` = `sliding`,
`loose` = `sliding` + 0.15 mm, the `aliases:` block): 0.10 / 0.20 / 0.35 mm as before.

Calibration record kind `fit_clearance` = `{fit_type, shape, gap_mm, result: free|press|fused}`;
`fits.validate_fit_clearance` checks one, `fits.corrections_from_fit_clearance` turns a coupon's readings into
`source: calibration` rows (smallest `free` gap minus the table clearance) that `resolve(extra_corrections=...)`
applies. The coupon and the recording command are S2.
