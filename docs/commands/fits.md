# `3d fits` - fit tolerances per fit type, corrected for printer and plastic

Tolerances are a table, not a number typed by hand: a **snap** fit needs more air than a **sliding** one, a
**press** fit needs negative air, and every printer/plastic pair prints a little differently. `3d fits` shows the
table (`lib/data/fits.yaml`, schema `3d.fits/1`, every value cited in the YAML), answers "which clearance for this
fit on this printer?" with the chain of rows behind the number, writes it into a model, and manages the
correction rows. `3d fit-check --fit` and `3d fit-niche --fit` call the same resolver, so the same inputs give
the same clearance everywhere ([contract](../contracts/fits.md)).

## Usage

```bash
3d fits list [--fit TYPE] [--shape SHAPE] [--json]
3d fits show TYPE [--printer P --material M --nozzle MM --layer MM] [--json]
3d fits resolve --fit TYPE [--shape SHAPE] [--printer P] [--material M] [--nozzle MM] [--layer MM] [--json]
3d fits apply --fit TYPE [--shape SHAPE] [--printer P] [--material M] [--var NAME] [--json]
3d fits apply FILE.scad... [--printer P] [--material M] [--nozzle MM] [--layer MM] [--yes] [--json]
3d fits corrections list [--origin table|manual|calibration] [--json]
3d fits corrections add --printer P --material M --delta MM [--nozzle MM] [--layer MM] [--shape S] [--fit TYPE] [--note TEXT] [--measured-at ISO] [--yes] [--json]
3d fits corrections remove --printer P --material M [--nozzle MM] [--layer MM] [--shape S] [--fit TYPE] [--yes] [--json]
```

## The fit types

Clearance is **per mating face** (radial for a round bore); the total slop on an axis is twice that. `press`
carries an **interference** instead: the resolved clearance is signed, so `-0.10` means "overlap by 0.10 mm".

| Fit | Clearance | Interference | tol (±) | Typical use |
|---|---|---|---|---|
| `snap` | 0.30 | 0 | 0.10 | snap hooks, lid lips |
| `sliding` (`free_running`) | 0.20 | 0 | 0.10 | drawers, inserts, hinge pins |
| `tight` (`tight_running`) | 0.10 | 0 | 0.05 | locating pins, close inserts |
| `press` | 0 | 0.10 | 0.05 | press pins, magnet pockets |
| `glue` | 0.15 | 0 | 0.10 | glued halves and seams |

Aliases: `snug` = `tight`, `normal` = `sliding`, `loose` = `sliding` + 0.15 mm (0.10 / 0.20 / 0.35 mm, the old
`3d fit-niche --fit` presets). Shapes: `planar`, `round`, `slot`, `pin`, `hinge`. A shape outside a fit's
`applies_to` still resolves, with a note in the provenance.

```bash
3d fits list                       # the table
3d fits list --shape round         # only the fits meant for round features
3d fits list --fit snug --json     # one alias, machine-readable
3d fits show snap --printer k1se --material PETG   # notes, features and the clearance for every shape
```

## `resolve`: the number and where it comes from

```bash
3d fits resolve --fit snap --shape round --printer k1se --material petg --nozzle 0.4 --layer 0.2
3d fits resolve --fit snap --shape round --printer k1se --material petg --nozzle 0.4 --layer 0.2 --json | jq '.clearance_mm, .provenance[].kind'
3d fits resolve --fit press --shape pin            # no printer: the table as written
```

The JSON (`schema: 3d.fits/1`, `command: resolve`) always carries `provenance[]`:

| kind | what it records |
|---|---|
| `fit_type` | the table row: clearance, interference, tolerance |
| `alias` | the legacy label and its `extra_mm` (`loose` = sliding + 0.15) |
| `note` | the shape is outside the fit's `applies_to` |
| `correction` | the ONE correction row applied: `delta_mm`, `source` (`calibration` \| `manual` \| `default`), `measured_at`, `status` (`calibrated` \| `manual override` \| `default, not calibrated`), `fallback` and `fallback_reason` |
| `correction_superseded` | matching rows that lost (less specific, or a weaker source) |
| `printer` / `material` | the registry lookup: fleet key -> model for `k1se`, `Elegoo Rapid PLA+` -> `PLA`; `known: false` for a name no registry has |
| `material_correction` | the material's shrink (`xy_scale` for the slicer's shrinkage compensation) or the entry "no material correction" |

Exactly one correction row is applied: the most specific match (printer, then material, nozzle, layer, shape,
fit type), then `calibration` > `manual` > `default`, then the newest `measured_at`. A row **replaces** a less
specific one, it does not add to it: a `k1se`/`PETG` row of `+0.07` gives `base + 0.07`, not
`base + 0.05 + 0.07` (the generic PETG row it superseded is listed). An unknown printer or material is not an
error: the table's `*` row applies, `fallback: true` says so, and the status stays `default, not calibrated`.

Example (K1 SE with Elegoo Rapid PLA+, no calibration recorded yet - the table value, honestly labelled):

```text
$ 3d fits resolve --fit sliding --shape round --printer k1se --material "Elegoo Rapid PLA+" --nozzle 0.4
fit sliding / round  ·  printer k1se · material Elegoo Rapid PLA+ · nozzle 0.4
  clearance     +0.200 mm per face (radial for round); negative = interference
  tolerance     ±0.1 mm
  interference  0 mm  (already included in the clearance)
  status        default, not calibrated  (fallback)
  xy scale      1.00301  (slicer shrinkage compensation; not part of the clearance)
provenance:
  fit type     sliding: clearance 0.2, interference 0, tol ±0.1 mm  [fits.yaml:fit_types.sliding]
  correction   +0 mm [default, not calibrated] any printer/material; fallback: no row for printer, material  (baseline: calibrated 0.4 mm desktop FDM printer, table values as written)
  printer      'k1se': fleet key k1se, model Creality K1 SE  [fleet/printers.yaml]
  material     'Elegoo Rapid PLA+': PLA (via word 'PLA+')  [materials.yaml]
  material     shrink 0.3% -> scale XY by 1.00301  [materials.yaml:PLA]
```

Material shrink is the optional `shrink_xy_pct` / `shrink_z_pct` of `lib/data/materials.yaml` (typical values,
sourced in the file; override them in `~/.config/3d-cli/materials.yaml`). It is reported as `xy_scale` and never
changes the clearance; a material without the field gets "no material correction" in the provenance, never a
silent zero. No per-material Z figure is shipped.

## `apply`: put the number into a model

`apply --fit TYPE` prints the clearance of one mate as an OpenSCAD snippet (or JSON with `--json`, schema
`3d.fits/1`, `command: apply`, `mode: snippet`). It is read-only; `--var` names the variable.

```bash
3d fits apply --fit sliding --shape round --printer k1se --material PETG --var pin_clearance >> mate.scad
```

```openscad
// 3d fits apply: sliding/round for k1se PETG (default, not calibrated)
pin_clearance = 0.25;  // @fit sliding shape=round mm per mating face (radial for round); negative = interference
pin_clearance_tol = 0.1;  // +/- mm scatter of the printed gap
// slicer: scale the model XY by 1.00402 (material shrink compensation)
```

`apply FILE.scad...` corrects the **declared** clearances of existing models. A declaration is a line

```openscad
NAME = NUMBER;   // @fit TYPE [shape=SHAPE] free comment
```

(`3d fit-niche --fit` writes its `clearance` line this way). Each marked number becomes the table value for the
chosen printer/material/nozzle/layer. The value is absolute, not a delta: re-applying the same printer changes
nothing, and a hand-edited number under a marker is replaced. Only the number changes; a bare `clearance = 0.2;`
without a marker is never touched. It is a dry run (a list of `before -> after`) until `--yes`; a file without any
marker exits 2 instead of passing silently.

```bash
3d fits apply insert.scad --printer k1se --material PETG            # what would change
3d fits apply insert.scad --printer k1se --material PETG --yes      # rewrite the file
3d fits apply a.scad b.scad --printer u1 --json | jq '.files[].changes[] | {line, before_mm, after_mm}'
```

## `corrections`: the rows behind the numbers

`list` shows every row the resolver can use: the table's own (`origin: table`), the manual ones, and the rows of
any other registered source such as `calibration` ([hook](../contracts/fits.md#correction-sources-the-hook-for-calibration)).

```bash
3d fits corrections list
3d fits corrections list --origin manual --json
```

`add` stores a manual row: `--delta` is the signed mm added to the per-face clearance (positive = bigger gap);
`*` means any printer/material; `--fit` takes a canonical type (rows match canonical names, not aliases). One row
per scope (printer, material, nozzle, layer, shape, fit type): the same scope replaces the old row. Without
`--yes` it is a dry run showing the clearance before -> after for the fit types it covers. Calibration rows are
not written here: `3d calibrate fit record --yes` stores them (`source: calibration`, `status: calibrated`; list
them with `corrections list --origin calibration`).

```bash
3d fits corrections add --printer k1se --material PETG --nozzle 0.4 --delta 0.05          # dry run
3d fits corrections add --printer k1se --material PETG --nozzle 0.4 --delta 0.05 --yes
3d fits corrections add --printer k1se --material PLA --fit snap --shape round --delta -0.05 --note "coupon 2026-10-24" --yes
3d fits corrections remove --printer k1se --material PETG --nozzle 0.4 --yes
```

Manual rows live in `<config dir>/fits-corrections.yaml` (`~/.config/3d-cli/`; `$THREED_FITS_CORRECTIONS`
overrides it): per machine on purpose, a correction measured on one printer does not travel with a repository.
Writes are atomic and locked. A broken file stops every resolution with exit 2 naming the file.

## Options

| Option | What |
|---|---|
| `--fit TYPE` | `snap`, `sliding`, `tight`, `press`, `glue` or an alias. Example: `3d fits resolve --fit snug` |
| `--shape SHAPE` | `planar` (default for `resolve`/`apply`), `round`, `slot`, `pin`, `hinge`. Example: `3d fits list --shape slot` |
| `--printer NAME` | fleet key (`k1se`) or printer model (`"Creality K1 SE"`). Example: `3d fits resolve --fit snap --printer k1se` |
| `--material NAME` | material (`PETG`) or a product name with one in it (`"Elegoo Rapid PLA+"`). Example: `3d fits resolve --fit snap --material PETG` |
| `--nozzle MM` | nozzle diameter. Example: `3d fits resolve --fit snap --printer k1se --nozzle 0.4` |
| `--layer MM` | layer height. Example: `3d fits resolve --fit snap --printer k1se --layer 0.2` |
| `--var NAME` | variable name of the `apply` snippet. Example: `3d fits apply --fit snap --var latch_gap` |
| `--delta MM` | signed correction of a manual row. Example: `3d fits corrections add --printer k1se --material PLA --delta 0.05` |
| `--note TEXT` | free text stored with a manual row. Example: `... --note "caliper, 3 coupons"` |
| `--measured-at ISO` | timestamp of a manual row (default: now, UTC). Example: `... --measured-at 2026-10-24T10:00:00Z` |
| `--origin ORIGIN` | filter of `corrections list`: `table`, `manual`, `calibration`. Example: `3d fits corrections list --origin manual` |
| `--yes` | perform the write of `apply FILE` and `corrections add|remove`; a dry run without it. Example: `3d fits apply insert.scad --yes` |
| `--json` | machine-readable output, `schema: 3d.fits/1`. Example: `3d fits list --json` |

## Exit codes

`0` ok; `2` unknown fit type / shape / subcommand / origin, missing input, a file without `@fit` markers, a
broken corrections file (the accepted values are listed in the message); `127` a missing dependency (pyyaml).

## Used by

- `3d fit-check --fit snap|sliding|tight|glue ...` and `fit:` in a `--spec` file ([fit-check](fit-check.md)).
- `3d fit-niche --fit TYPE --printer P --material M` ([fit-niche](fit-niche.md)).
- `3d calibrate fit` (slice S2, [#573](https://github.com/alex-mextner/3d-cli/issues/573)) stores the coupon
  readings as calibration records; `lib/fits_sources/calibration.py` turns them into calibration rows, so every
  command above shows `calibrated` with the date and the record id after a coupon was read
  ([calibrate](calibrate.md#fit--quick-fit-calibration-573)).
