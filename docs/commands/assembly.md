# `3d assembly` - whole-assembly checks over declared mates

`3d fit-check` judges one insert in one socket. `3d assembly` judges a whole assembly (a case, its board, its snap
hooks): which parts overlap and by how much, whether each mate has the clearance of its fit type on this printer and
plastic, whether every part can be put in and taken out along a straight path, whether a hand or tool can reach the
hooks and screws, and where each printed part sits on the plates. Umbrella #570, slice S3 (#574); it also closes
#553 (declared insertion axis) and covers the assembly-motion part of #61.

Every subcommand takes `--json` (schema `3d.assembly/1`, `kind` = `check | sweep | sweeps | order | ergonomics | explode |
bom | position | show | pair`). Exit codes: `0` ok, `1` a verdict failed (blocking intersection, blocked path, no access, no
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
3d assembly pair A B [--scale-a S] [--scale-b S] [--pose-a POSE] [--pose-b POSE] [--axis AXIS] [--axis-point X,Y,Z]
                 [--a-side inner|outer --b-side inner|outer [--pitch MM] [--hand auto|right|left] [--a-z LO:HI] [--b-z LO:HI]
                  [--a-r LO:HI] [--b-r LO:HI] [--feature-name N] | --features FILE]
                 [--min-radial-gap MM | --fit TYPE [--shape S] [--printer P] [--material M] [--nozzle MM] [--layer MM]]
                 [--min-clearance MM] [--zone XMIN,YMIN,ZMIN,XMAX,YMAX,ZMAX] [--max-overlap-mm3 V] [--seat-tol MM]
                 [--screw-to-best] [--gcode-a F --object-a NAME --print-a POSE] [--gcode-b F --object-b NAME --print-b POSE]
                 [--skip-below MM] [--max-faces N] [--json]
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
3d assembly pair rim.stl plug.stl --a-side inner --b-side outer --pitch 3.007 --screw-to-best
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

## `pair` - do two parts mate? (no spec)

`3d assembly check` judges declared mates of a spec. `pair` takes two meshes (STL / 3MF / OBJ / PLY), each with an optional
scale and pose into the assembled frame, and answers the question for one pair, down to a thread or a rib. A 3MF is read with the
stdlib reader (no lxml); one that holds several objects needs `plate.3mf#ID` (the refusal lists the ids), and its build-item
transform is part of the mesh:

```bash
# rim (the ball's bore) and plug at the print scale, plug seated under the rim; thread pitch 3.007 mm; model only
3d assembly pair rim.stl plug.stl --scale-a 2.00493695 --scale-b 2.00493695 --pose-b '{translate: [0, 0, -37.782]}' \
    --a-side inner --b-side outer --pitch 3.007 --a-z -36.78:-28.28 --a-r 0:36.4 --b-z -36.78:-30.98 --b-r 0:36 --screw-to-best
# the same, as printed: the plug from its K1 G-code, the new rim from its slice; both placed on the bed automatically
3d assembly pair rim.stl plug.stl ... --gcode-a rim.gcode --object-a "rim.stl" --gcode-b plate.gcode --object-b "Plug x2.stl"
# several features from a file, the clearance the fit table asks for on this printer and plastic
3d assembly pair a.stl b.stl --features features.yaml --fit sliding --printer k1se --material PETG
```

* **interpenetration** - exact overlap regions (volume, depth, xyz as in `check`); a region thinner than `--seat-tol`
  (0.02 mm) is a seat contact, not a collision. FAILs above `--max-overlap-mm3` (default 0.01): a designed press fit is allowed
  only by saying how much.
* **clearance** - the exact minimum gap of the two solids (0 where parts seat on a face); `--zone BOX` measures only the surface
  samples inside a box (spacing reported), which leaves a seat face out; `--min-clearance` makes it a verdict.
* **features** - the part of the question a volume cannot answer. A feature is the surface of each part around a COMMON AXIS
  (`--axis`, `--axis-point`; z windows are the coordinate along it, r windows the radius from it): `--a-side inner` is the wall
  of a bore, `outer` a boss; one of each. The surface radius is read against the PHASE `u` (a thread: `z/pitch -+ theta/2pi`, one
  value per `u` for a perfect single-start thread; ribs and keys, `--pitch 0`: the angle), 360 bins, from the model's sections
  every 0.12 mm. The bore minus the boss is the radial gap, at the posed phase and at every other relative phase - which is
  screwing (a turn = an axial advance of one pitch) or clocking. Reported: the gap at the pose, the best phase and the tightest
  radial gap there, the free play (mm of axial travel of a thread, degrees for ribs), male crest -> female root and female crest ->
  male root clearances, handedness (detected, a mismatch FAILs: the threads cannot screw together), pitch coherence (a wrong pitch
  shows as a large value and a warning), coverage of the phase bins (unmeasured = FAIL under 50 %), and the concentricity of each
  feature's own axis with the declared one. FAILs when the best-phase gap is under `--min-radial-gap` (default 0.1) or the
  `--fit TYPE` row (`clearance - tol` per face of `3d fits resolve`, printer x material corrected). The posed phase may interfere
  while a free phase exists: that is a WARN with the move that fixes it, in turns, degrees and mm of advance.
  A bore with no `--a-r` is read from the axis out to the boss's largest radius + 3 mm (else its own outer wall would count).
* **`--screw-to-best`** turns B about the axis to the best phase of the first feature before anything else is measured (a screwed
  fit has no unique pose: the phase is free). The turn keeps B's axial position, so a part that seats on a face stays seated.
* **as printed** - `--gcode-a/--gcode-b` (+ `--object-a/b` when the plate holds several objects; the slicer's own object labels - a wrong or
  ambiguous name, a name that matches several copies, or none on a multi-object plate lists the file's labels; `"Cup.stl id:0
  copy 1"` picks one copy) read the OUTER WALL paths of the object (`lib/gcode_walls.py`: Orca / Snapmaker Orca / Bambu /
  PrusaSlicer, arcs, relative extrusion). The model is put on the bed (`--print-a POSE`: orientation like a pose; without a
  `translate` it is dropped on z = 0 and REGISTERED on the walls in XY by a translation-only fit - the slicer's own placement), the
  per-layer deviation of the printed wall from the model's section at the layer mid height is reported (the wall centre line is
  half a line width inside the surface; layers under `--skip-below` are listed apart: first-layer flare), overall and inside the
  feature's window, and the feature clearance is recomputed with the printed surface (`centre line +- width/2` radially). A part
  without a G-code is taken from its model, the report says which side is printed. The slicer's compensation
  (`xy_hole_compensation`, elephant-foot, line width) therefore shows in the numbers, which a model-only check cannot see.

`features.yaml` (several features, the single-feature flags are the same keys):

```yaml
features:
  - name: rim_thread
    pitch: 3.007            # 0 / absent = ribs or keys
    hand: auto              # auto | right | left
    min_gap_mm: 0.2         # per-feature requirement (else --min-radial-gap / --fit / 0.1)
    a: {side: inner, z: [-36.78, -28.28], r: [0, 36.4]}
    b: {side: outer, z: [-36.78, -30.98], r: [0, 36.0]}
```

A value that starts with `-` may be given after `=` (`--a-z=-36:-28`) or as the next argument (`--a-z -36:-28`).
Exit codes as the other subcommands: 0 ok, 1 a verdict failed, 2 bad input (an unknown object name, a window without surface, two
bores), 127 missing dependency. `--json` document: `kind: pair` with `parts`, `axis`, `interpenetration`, `clearance`, `features`
(each with `model` and, with a G-code, `as_printed`, `printed_vs_model`), `printed`, `fit`, `summary`.

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
