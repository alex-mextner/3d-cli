# `3d fit-niche` - parametric insert that mates into a described cavity

Generates a printable, parametric OpenSCAD insert (plug) that seats into a described
cavity with correct FDM mating clearances. This is pure parametric geometry — it does not
call any AI backend. Describe the cavity (a rectangular pocket or a round bore, plus
optional retention/entry features); the command resolves the clearances and writes a
tunable `.scad` you can render, check, and export.

## Usage

```bash
3d fit-niche [cavity options] [feature flags] [output/proof]
```

## Clearance convention

Clearance is the air gap between the insert and the cavity wall. The insert is made
smaller than the cavity so it actually fits after FDM print tolerances:

- **rect cavity** — clearance is applied **per mating face**. Each of the two X walls and
  two Y walls gets `clearance` mm, so the insert footprint shrinks by `2*clearance` in
  both width and depth.
- **round cavity** — clearance is **radial** (a uniform gap all around), so the insert
  diameter shrinks by `2*clearance`.
- **height (Z)** is a full seat: no Z clearance by default, so the insert bottoms out on
  the cavity floor.

`--fit` takes any fit type of the shared fit table (`lib/data/fits.yaml`, `3d fits list`,
[contract](../contracts/fits.md)), the aliases below, and the same `--printer/--material/--nozzle/--layer`
corrections as `3d fit-check --fit` and `3d fits resolve` - the same inputs give the same clearance in all three.
The rect cavity resolves with shape `planar`, the round one with `round`.

| Fit | Clearance (mm) | Feel |
|---|---|---|
| `snap` | 0.30 | pops together and holds (hooks, lid lips) |
| `sliding` | 0.20 | slides by hand |
| `tight` | 0.10 | no visible play, light push |
| `press` | -0.10 (interference) | the insert is larger than the cavity; start from a coupon |
| `glue` | 0.15 | gap an adhesive fills |
| `snug` (alias of `tight`) | 0.10 | press / sand-to-fit |
| `normal` (alias of `sliding`, default) | 0.20 | slides by hand |
| `loose` (alias of `sliding` + 0.15) | 0.35 | drop-in with play |

The old `snug|normal|loose` presets are those aliases (clean cutover: the table in `lib/niche_fit.py` is gone,
the values live only in `fits.yaml`). With `--printer`/`--material` the printer x material correction rows apply
(`--fit sliding --printer k1se --material PETG` = 0.25 mm: the generic PETG row, `default, not calibrated`).
`--json` carries the whole `fit_resolution` (schema `3d.fits/1`, with `provenance[]`). The emitted `.scad` marks
its clearance line `// @fit TYPE shape=SHAPE`, so `3d fits apply insert.scad --printer u1 --material PLA --yes`
re-corrects an existing insert for another printer without regenerating it.

`--clearance MM` overrides the preset with an explicit value.

## Options

| Option | Default | What |
|---|---|---|
| `--shape rect\|round` | `rect` | Cavity shape. |
| `--width MM` | required (rect) | Rect cavity opening in X. |
| `--depth MM` | required (rect) | Rect cavity opening in Y. |
| `--diameter MM` | required (round) | Round cavity bore. |
| `--height MM` | required | Cavity depth in Z (= insert height). |
| `--fit TYPE` | `normal` | Fit type of the fit table: `snap`, `sliding`, `tight`, `press`, `glue`, or `snug`/`normal`/`loose`. |
| `--printer NAME` | none | Printer whose correction rows adjust `--fit`, e.g. `k1se` (unknown name = table default). |
| `--material NAME` | none | Material whose correction rows adjust `--fit`, e.g. `PETG`. |
| `--nozzle MM` | none | Nozzle diameter for the correction rows, e.g. `0.4`. |
| `--layer MM` | none | Layer height for the correction rows, e.g. `0.2`. |
| `--clearance MM` | from `--fit` | Explicit clearance per mating face / radial; overrides `--fit` (and cannot be combined with `--printer`/`--material`/`--nozzle`/`--layer`). |
| `--lead-in` | off | 45-degree entry chamfer at the top rim (self-guides into the cavity). |
| `--groove` | off | Retention channel around the insert. |
| `--tab`, `--snap` | off | Retention ridge on the +X mating face. |
| `--spec FILE` | none | Read the cavity spec from a JSON file; flags override matching fields. |
| `-o, --out PATH` | `insert.scad` | Output `.scad` path. |
| `--render` | off | Also render a preview PNG next to the `.scad` (needs OpenSCAD). |
| `--check` | off | Also run `3d check` on the emitted insert (needs OpenSCAD). |
| `--json` | off | Print the resolved spec (dims, clearance, insert size) as JSON. |

## Examples

```bash
3d fit-niche --width 20 --depth 16 --height 12 -o insert.scad
3d fit-niche --shape round --diameter 20 --height 14 --lead-in --render
3d fit-niche --width 24 --depth 18 --height 12 --fit loose --groove --snap
3d fit-niche --shape round --diameter 8 --height 10 --fit press --printer k1se --material PETG --json
3d fit-niche --width 20 --depth 16 --height 12 --fit sliding --printer k1se --material PETG --nozzle 0.4 --layer 0.2
3d fit-niche --width 20 --depth 16 --height 12 --clearance 0.15 --json
3d fit-niche --spec cavity.json -o plug.scad --check
```

A `--spec` JSON file uses the same field names as the flags:

```json
{"shape": "rect", "width": 20, "depth": 16, "height": 12, "fit": "sliding", "printer": "k1se", "material": "PETG", "lead_in": true}
```

## Proof: seeing the mating gap

The emitted `.scad` carries a `show_cavity` demo toggle. It is `false` by default, so a
plain `3d render` / `3d export` / `3d check` sees only the printable `insert()`. Turn it on
to render the surrounding cavity block (a solid with the nominal pocket cut out) and take a
cross-section that visibly shows the insert seated inside with the clearance gap between the
mating walls:

```bash
3d fit-niche --width 24 --depth 20 --height 16 -o insert.scad
3d render insert.scad --section --plane XY --keep neg -D show_cavity=true -o proof.png
```

The `--plane XY --keep neg` slice, viewed from the top, shows the outer cavity wall, the
clearance ring (a recessed gap), and the smaller insert inside it. A tiny production
clearance (0.2 mm) is a thin ring; pass a larger `--clearance` when you want the gap to be
obvious in a demo render.

## Feature printability

`--lead-in` keeps the part printable. `--groove` (0.8 mm channel) and `--tab`/`--snap`
(0.4 mm retention ridge) are deliberately near the FDM minimum-feature limit, so a fully
featured insert can be flagged by the `printability` gate as a thin feature. That is
expected: those features are functional retention geometry, not clean structural walls. Run
`3d check <insert>.scad --skip printability` when you intentionally want the retention
feature, or drop the feature for a gate-clean plug.
