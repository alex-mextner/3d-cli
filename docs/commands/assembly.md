# `3d assembly` - whole-assembly checks over declared mates

`3d fit-check` judges one insert in one socket. `3d assembly` judges a whole assembly (a case, its board, its snap
hooks): which parts overlap and by how much, whether each mate has the clearance of its fit type on this printer and
plastic, whether every part can be put in and taken out along a straight path, whether a hand or tool can reach the
hooks and screws, and where each printed part sits on the plates. Umbrella #570, slice S3 (#574); it also closes
#553 (declared insertion axis) and covers the assembly-motion part of #61.

Every subcommand takes `--json` (schema `3d.assembly/1`, `kind` = `check | sweep | sweeps | order | ergonomics | explode |
bom | position | show`). Exit codes: `0` ok, `1` a verdict failed (blocking intersection, blocked path, no access, no
straight order), `2` bad spec / usage, `127` missing dependency.

## Usage

```bash
3d assembly check TARGET [--mate NAME]... [--printer P] [--material M] [--viz DIR] [--view V] [--mem-limit-mb N] [--json]
3d assembly sweep TARGET --mate NAME [--direction insert|remove|both] [--step MM] [--distance MM] [--ignore PART]... [--json]
3d assembly order TARGET [--base PART] [--step MM] [--max-travel MM] [--json]
3d assembly ergonomics TARGET [--mate NAME]... [--finger MM] [--nail MM] [--tool MM] [--reach MM] [--json]
3d assembly explode TARGET [--viz OUT.png] [--usdz OUT.usdz] [--view V] [--factor F] [--json]
3d assembly bom TARGET [--json]
3d assembly position TARGET --3mf OUT.3mf [--bed MM] [--gap MM] [--margin MM] [--base-project BASE.3mf] [--printer P]
                     [--process NAME] [--filament NAME] [--preset-dir DIR]... [--set K=V]...
                     [--include-non-printable] [--reason TEXT] [--yes] [--json]
3d assembly show TARGET [--json]
```

`TARGET` is an assembly spec (`assembly.yaml` / `.json`), a project directory holding `assembly.yaml` next to its
`3d.yaml`, or - for everything but `position` - a 3MF written by `position`.

```bash
3d assembly check assembly.yaml --printer k1se --material "Rapid PLA+" --viz out/check
3d assembly sweep assembly.yaml --mate lid --direction remove --step 0.05
3d assembly order assembly.yaml --base case_back
3d assembly ergonomics assembly.yaml --finger 14 --nail 5
3d assembly explode assembly.yaml --viz out/exploded.png --usdz out/exploded.usdz --factor 1.5
3d assembly bom assembly.yaml --json
3d assembly position assembly.yaml --3mf out/plates.3mf --base-project print/mole.3mf --yes
3d assembly position assembly.yaml --3mf out/plates.3mf --include-non-printable --reason "fit test" --yes
3d assembly show assembly.yaml
```

## The spec (`3d.assembly/1`)

```yaml
schema: 3d.assembly/1
name: mole
root: .                         # base of relative mesh paths (default: the spec's dir); $VAR / ~ expand, unset VAR = error
context: {printer: k1se, material: "Rapid PLA+", nozzle: 0.4, layer: 0.2}    # for the fit lookup
settings: {eps_mm3: 0.01, seat_tol_mm: 0.02, step_mm: 0.1, finger_pad_mm: 12, finger_nail_mm: 6, tool_mm: 3,
           reach_mm: 40, pry_blade_mm: 0.8, max_faces: 400000}
base: mole_back                 # optional: the part `order` keeps fixed
groups: {electronics: [pcb, housing, wire]}   # optional rigid sub-assemblies `order` may move as one
parts:
  mole_back:  {role: printed, mesh: print/mole_back.stl, pose: {translate: [0, 0, -10]},
               print: {face: -Z, settings: {layer_height: 0.2, wall_loops: 3}}}
  pcb:        {role: stand_in, mesh: vitamins/pcb.stl}          # vitamin / reference need reference_source {url, license}
mates:
  - name: pcb_ribs
    a: mole_back
    b: pcb
    kind: press                 # snap | sliding | tight | press | glue | bolt
    fit: press                  # a fit type of `3d fits` (default from the kind); or explicit clearance_mm / tol_mm / interference_mm
    shape: planar
    zone: {min: [-10.6, 2, -2.1], max: [10.6, 13, -0.1]}     # where this mate's overlap / clearance is judged
    allow: {max_depth_mm: 0.15, max_volume_mm3: 4}           # extra caps for designed interference
    seat: false                 # true: the parts touch by design (a seam, a floor)
    insertion: {part: pcb, axis: -Z, distance_mm: 12, carry: [housing], ignore: [mole_front]}
    access: [{name: grip, target: [0, 16, 16.8], from: +Z, probe: finger, purpose: pull}]
    snap:
      part: mole_back
      flex_limit_mm: 0.326      # or strain_limit + arm geometry + modulus_mpa (S4 closed forms, when present)
      hooks:
        - {name: h1, point: [11.6, 16.5, 6], deflect: -X, release: pull, barb_mm: 0.4,
           region: {min: [10.9, 13.5, 0], max: [12.7, 19.5, 7.2]}, arm: {thickness_mm: 1, length_mm: 7, width_mm: 6}}
loads:                          # carried in the report for the S4 solver
  - {name: cable_tug, part: mole_front, point: [0, 40, 3], force_n: 10, direction: +Y}
```

* **Pose**: a mapping `{translate, rotate (deg, OpenSCAD order), scale, matrix}` or a list applied in order; inside one mapping
  rotate comes before translate. The pose places the mesh in the ASSEMBLED frame.
* **Insertion**: the straight path along which `part` moves to ASSEMBLE, any direction (`+X`..`-Z` or a vector); `hinge:
  {part, axis, origin, range_deg}` is a revolute path. `carry` parts travel with it; `ignore` parts are not obstacles on that path.
* **Zones** (`{min,max}`, `{center,size}`, `{center,radius}`) describe the CLOSED pose; they are ignored while a part moves.
* **Roles**: `printed | vitamin | reference | stand_in` (`lib/parts.py`); a vitamin or reference model must name its source
  URL and licence, a stand-in is labelled as invented in the BOM and drawn ghosted.

## What each check does

* **check** - per pair of parts, exact overlap regions (manifold3d, coarse to fine: boxes, then a crop to the overlap
  box, then the boolean; one pair at a time, peak memory reported). Each region is `contact` (thinner than
  `seat_tol_mm`: touching, #140), `allowed` (inside a mate's zone and within the interference its fit allows: press =
  table interference + tol, snap/tight = the fit tolerance, or the mate's own `allow` caps; PLA/PETG flexibility
  note attached) or `blocking` (pair, depth, volume, xyz, the offending faces, a suggested clearance change, a section
  PNG with `--viz`). Depth is the smallest width of the overlap region: an upper bound of the separating move, not a general penetration solver. Per mate the exact minimum gap in its
  zone (surface samples inside the window; a mate with an insertion axis is measured laterally, its seat face excluded) is
  compared with `clearance - tol` of the resolved fit (`fits_sources.resolve`: table x printer x material, provenance
  in the report). Explicit numbers in the spec replace the table and say so.
* **sweep** - samples the moving part every `--step` and reports the first contact and first blocking position
  (bisected to 0.005 mm; the report states the sampling resolution, it is not a continuous-collision proof). For snaps:
  the peak interference, the tip deflection each hook needs (the arm geometry is shifted along `deflect` until it is clear
  of the partner) against the flex limit, and the force estimate.
* **order** - depth-first search over "which part or group leaves along a straight line" (the reverse of every declared
  insertion axis, then the six axes), memoised; prints an assembly order or the stuck state with what blocks each part.
* **ergonomics** - finger pad / nail / tool capsule probes must reach each hook's release point (or the grip and
  prying points of a hook released by pulling the parts apart); a prying point also needs a seam gap that admits `pry_blade_mm`;
  the hook arm must be able to flex clear of its neighbours (including walls of its own part); the removal path is swept; the
  force row comes from the S4 closed-form hook formulas (`structural.hooks`) when that module is part of the checkout,
  otherwise it says so and gives no number.
* **position** - orients each printed part (`print.face`, `print.rotate`, or `auto`: least overhang on the mating surfaces),
  packs the plates with the `3d arrange` packer and writes an Orca/Bambu project 3MF: role tags on every object, a distinct colour per
  part, per-part `print.settings` as object metadata, `Metadata/3d_assembly.json` (spec + the assembled-to-object matrix of
  every part). Non-printable parts never enter a plate; `--include-non-printable` (logged) is the only way. With
  `--base-project` the slicer settings of an Orca-family project travel along and `3d retarget` / `--printer` can move the result;
  without it the file is geometry only. `3d assembly check PROJECT.3mf` then verifies the geometry that is in the file (also after
  arrange / retarget moved the items); an edited object is reported as drift and fails.

Every PNG, USDZ and 3MF colours parts with `parts.assign_styles` (deterministic per name + role, stand-ins ghosted) and carries the
legend (`legend` in the JSON, a strip in each PNG, a `.legend.json` beside a USDZ).

## The print guardrail

`3d arrange`, `3d retarget`, `3d prep` read the `3d:role` / `3d:printable` tags (`lib/parts_3mf.py`): `arrange` leaves
non-printable objects off every plate and names them, `retarget` and `prep` refuse (`NonPrintableParts`, exit 1); each takes
`--include-non-printable [--reason TEXT]`, which is logged. Tags survive arrange and retarget. Printer upload/start and publish wiring belongs to S8.

## Limits

Sweeps are sampled. Hook flex is a rigid shift of the arm geometry (conservative for a cantilever). Finger and tool are straight
capsules. `order` moves one part or declared group at a time along straight lines (no rotations, no closed-loop solving).
Forces need the S4 hook formulas; without them the reports say so.
