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

`--fit` presets map to sensible defaults for a 0.4 mm nozzle:

| Fit | Clearance (mm) | Feel |
|---|---|---|
| `snug` | 0.10 | press / sand-to-fit |
| `normal` (default) | 0.20 | slides by hand |
| `loose` | 0.35 | drop-in with play |

`--clearance MM` overrides the preset with an explicit value.

## Options

| Option | Default | What |
|---|---|---|
| `--shape rect\|round` | `rect` | Cavity shape. |
| `--width MM` | required (rect) | Rect cavity opening in X. |
| `--depth MM` | required (rect) | Rect cavity opening in Y. |
| `--diameter MM` | required (round) | Round cavity bore. |
| `--height MM` | required | Cavity depth in Z (= insert height). |
| `--fit snug\|normal\|loose` | `normal` | Clearance preset. |
| `--clearance MM` | from `--fit` | Explicit clearance per mating face / radial; overrides `--fit`. |
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
3d fit-niche --width 20 --depth 16 --height 12 --clearance 0.15 --json
3d fit-niche --spec cavity.json -o plug.scad --check
```

A `--spec` JSON file uses the same field names as the flags:

```json
{"shape": "rect", "width": 20, "depth": 16, "height": 12, "fit": "normal", "lead_in": true}
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
