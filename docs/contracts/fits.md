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

`3d fit-niche --fit TYPE` and `3d fit-check --fit TYPE` resolve through the table, so the same inputs give the same
clearance; the old `snug|normal|loose` are the `aliases:` block (`snug` = `tight`, `normal` = `sliding`, `loose` =
`sliding` + 0.15 mm: 0.10 / 0.20 / 0.35 mm as before). There is no second table: no `FIT_CLEARANCES` in `lib/`.

Calibration record kind `fit_clearance` = `{fit_type, shape, gap_mm, result: free|press|fused}`;
`fits.validate_fit_clearance` checks one, `fits.corrections_from_fit_clearance` turns a coupon's readings into
`source: calibration` rows (smallest `free` gap minus the table clearance) that `resolve(extra_corrections=...)`
applies. The coupon and the recording command are S2.

## Provenance (S1 #572)

`Resolution.provenance` lists, in this order: `fit_type`, `alias` (if any), `note` (shape outside `applies_to`),
the applied `correction` (with `status`: `calibrated` | `manual override` | `default, not calibrated`, plus
`fallback`/`fallback_reason`), the `correction_superseded` rows, the `printer` and `material` registry lookups
(`lib/fits_registry.py`: fleet key -> model, `Elegoo Rapid PLA+` -> `PLA`, `known: false` for an unknown name) and
a `material_correction` entry. `Resolution.xy_scale` is the model scale of the material's optional
`shrink_xy_pct` (`lib/data/materials.yaml`, cited there); it never changes `clearance_mm`, and a material without
the field gets the entry "no material correction". A row of a model name (`Creality K1 SE`) answers a query by its
fleet key (`k1se`), a row of one fleet key never answers the model name.

## Correction sources: the hook for calibration

`fits.resolve` is pure (table + the rows it is handed). Every consumer that must honour manual and calibration rows
calls `fits_sources.resolve(...)` instead (`3d fits`, `3d fit-check --fit`, `3d fit-niche --fit`): it collects the
rows of every source in `lib/fits_sources/` and passes them as `extra_corrections`.

A source is one module `lib/fits_sources/<name>.py` defining

```python
SOURCE = Source("calibration", "coupon rows of 3d calibrate fit", load)   # load(query: Query) -> list[Correction]
```

`discover()` imports every non-underscore module there, so **slice S2 (#573) adds `calibration.py` and edits no
shared file**: `load` turns the stored `fit_clearance` records into rows with `fits.corrections_from_fit_clearance`
(`source="calibration"`, `measured_at`, the record id in `note`), reading the existing calibration store (3d-api
with the offline JSONL buffer). A calibration row outranks a manual row of the same specificity and shows as
`status: calibrated`. A source that cannot read its store raises a `ThreeDError`; it never returns a silently partial
list. `manual.py` is the built-in source: `<config dir>/fits-corrections.yaml` (`$THREED_FITS_CORRECTIONS`), managed by
`3d fits corrections add|remove|list`, one row per scope, atomic locked writes.

## Declared clearances: the `@fit` marker

`lib/fits_apply.py` finds `NAME = NUMBER;  // @fit TYPE [shape=SHAPE]` lines in OpenSCAD and rewrites the number to
the resolved clearance (absolute, idempotent): `3d fits apply FILE.scad --printer P --material M [--yes]`.
`3d fit-niche` writes its clearance in this form; `3d fits apply --fit TYPE` emits a ready snippet.
