# `3d prep` — model to print-ready plate in minutes

One cached, incremental command that takes an imported STL/3MF through everything that used to be a
pile of one-off scripts: mesh diagnose and repair, orientation search, no-support adaptation, an
overhang re-check, a table-driven fit check (e.g. phones), stability with external loads, colour
regions to filaments and mixes, an adaptive layer-height plan, the Orca project 3MF, a headless
slice, the G-code gates and a proof pack. Issue: [#431](https://github.com/alex-mextner/3d-cli/issues/431);
the evidence is [the session ledger](../research/prep-session-ledger-2026-10-20.md); the flow and the
measured times are in [the workflow](../workflows/prep.md).

Why: preparing one cat-phone-stand model took 2 h 22 min of agent time and ended with no G-code,
because every step was a script written, run and re-run from scratch (repair 5x, cavity fill 7x,
G-code verifier 14x), and a nested lock hung one run for 25 minutes.

## Usage

```
3d prep MODEL.stl|.3mf [--out DIR] [--printer u1] [--colors colors.yaml] [--fit items.json] [options]
3d prep steps
3d prep status OUT_DIR
```

Outputs in `OUT_DIR` (default `./MODEL-prep`): `NAME.print.stl` (the adapted mesh), `NAME.3mf` (project)
+ `NAME.manifest.json`, `NAME.gcode`, `proof-report.md` + `proof/*.png`, `prep.json` (every step result,
verdict and timing), `steps/*.json` (the cache), `meshes/*.npz` (intermediate meshes).

Exit codes: 0 when no step FAILs (PASS/WARN/SKIP); 1 when any step FAILs; 2 bad arguments; 127 missing tool.

## Steps

| Step | Library | What it does | FAIL means |
|---|---|---|---|
| `diagnose` | `prep.meshfix` | counts open/non-manifold edges, pinch vertices, debris shells | informational (the next step repairs) |
| `repair` | `prep.meshfix` | drops debris, cuts and re-fills defective faces; the public meshfix library plugs in as a backend | still not a volume (blocks the rest) |
| `orient` | `prep.orient` | ranks 100+ orientations by overhang area, bed contact, height and tip margin; rotates when `--orient auto` and a candidate clearly cuts overhang | never FAILs the flow: "even the best has overhang" feeds `adapt` |
| `adapt` | `prep.adapt` | fills an open cavity and adds 45 degree wedges under overhangs (only material under material is added); `--no-adapt` only reports | operator output not watertight (blocks the rest) |
| `overhang` | `prep.overhang` | voxel re-check of the final mesh: islands, regions, bridge spans | an island, or a span over the bridge limit |
| `fit` | `prep.fit` | item x case table (`phones.json` schema) against a lean-slab or pocket holder | an item that cannot be held |
| `stability` | `prep.stability` | mass, centre of mass, support polygon, tip angle, tap/pull with the items from `fit` as loads | static tipping; tap/pull below safety factor 1 is a WARN unless `stability: {sf_fail: 1.0}` |
| `colors` | `prep.regions` | landmark boxes/volumes to disjoint colour parts, filament or layer mix per part, mix rules | a rule violation or an unknown filament |
| `layers` | `prep.layers` | adaptive layer heights, 0.08 only on the steepest surfaces, mix zones capped | mix cap broken |
| `project` | `prep.project3mf` | one multi-part object 3MF with per-part extruders, mixed rows, layer profile, prime tower | missing Orca resources: SKIP with the install hint |
| `slice` | `slicing.orca_cli` | headless slice under the heavy lock, every quirk handled ([U1 Orca CLI](../printers/u1-orca-cli.md)) | crash, segfault, no G-code |
| `gates` | `prep.gcode_gates` | walls, infill, supports, layer heights, mix-layer cap, tool/head sequence per region, prime tower, bed bounds | any gate FAIL; a gate without input is SKIP naming it |
| `proof` | `prep.proof` | views, before/after, overhang heat-map, section, fit matrix + pose, stability plan, regions, layers, G-code renders, one markdown report | never FAILs by itself |

A FAIL in `repair` or `adapt` stops the flow (later steps would run on a broken mesh); every other FAIL is
reported and the flow continues, so one run shows all problems. A step whose tool or input is missing
(no Orca install, no colour spec, no items table) is `SKIP` with the reason and is never cached.

## Cache

Every step stores `steps/NAME.json`; its key hashes the step's own parameters and the keys of the steps it
consumes. Re-running the same command prints `cached` for every step and takes seconds. Changing
`--colors` recomputes `colors layers project slice gates proof` and nothing before them; changing
`--angle` recomputes from `orient`. `--from STEP` forgets the cache from that step; `--force` recomputes
everything; `--only a,b` runs just those steps and what they need; `--skip a,b` leaves steps out.

## Options

- `--spec FILE.yaml` — settings file: `printer`, `orient`, `angle`, `pitch`, `adapt` (bool, or `{zones: [...]}`),
  `overhang: {bridge_max_mm}`, `fit` (items file), `holder` (the `fit.HolderSpec` fields), `loads` (extra
  stability loads), `stability` (analysis options), `colors`, `process: {preset, filament_preset, walls,
  infill, layer_min, layer_max, brim, mix_cap}`, `gcode` (gate thresholds). Relative paths resolve against the
  spec file. A flag always wins over the spec. See `examples/prep/chekhov/prep.yaml`.
- `--printer u1` — target printer; `u1` today (its 270 mm bed and 4 heads are built in).
- `--orient auto|keep` — `auto` (default) rotates only when a candidate clearly cuts the overhang area;
  `keep` never rotates. Landmark boxes in a colour spec follow the final orientation.
- `--angle 45` — overhang limit in degrees from vertical.
- `--pitch 0.25` — voxel size in mm; one layer per cell is the unit of the overhang tolerance.
- `--bridge-max 10` — the longest unsupported bridge (mm) that is only a WARN in the overhang check; a longer
  one is a FAIL. The spec's `adapt: {zones: [{z_min, z_max, angle_deg, columns}]}` gives a height range a
  relaxed angle and no vertical pillars (e.g. a cat's face: 75 degrees, no columns, so nose and chin keep their shape).
- `--no-adapt` — report overhangs, do not change the model.
- `--fit items.json` — items the object must hold, e.g. [`examples/prep/chekhov/phones.json`](../../examples/prep/chekhov/phones.json).
- `--colors colors.yaml` — filaments, landmark boxes and regions; see
  [`examples/prep/chekhov/colors.yaml`](../../examples/prep/chekhov/colors.yaml).
- `--process NAME` / `--filament NAME` — Orca process and filament presets.
- `--walls 4 --infill 15` — wall loops and sparse infill percent.
- `--layer-min 0.08 --layer-max 0.32` — adaptive layer band in mm.
- `--brim MM` — brim width.
- `--only a,b`, `--skip a,b`, `--from STEP`, `--force` — cache and step selection.
- `--no-slice` — stop before slicing (no Orca needed).
- `--no-lock` — do not take the heavy lock (see [`3d heavy`](heavy.md)); by default the whole run holds it, so
  slicing inside it never waits for its own lock.
- `--json` — the outcome as JSON; `--quiet` hides progress lines.

Examples:

```
3d prep cat.stl --printer u1 --colors colors.yaml --fit phones.json
3d prep cat.stl --out out --spec chekhov.yaml --walls 4 --infill 15 --layer-min 0.08 --layer-max 0.32
3d prep cat.stl --out out --colors colors.yaml                    # re-run: unchanged steps are cache hits
3d prep cat.stl --out out --orient keep --angle 50 --no-adapt     # report only
3d prep cat.stl --out out --only repair,orient,overhang --no-slice
3d prep cat.stl --out out --from colors                           # redo colours, layers, 3MF, slice, gates
3d prep cat.stl --out out --no-slice --json | jq '.steps[] | {name, verdict, seconds}'
3d prep status out
3d remote run --push cat.stl --pull -- 3d prep cat.stl --out ../out   # on the heavy box
```

## Library interface of the steps

Each step module returns an object with `verdict`, `to_dict()` and `summary()` (`prep.contracts.StepResult`),
which is what the cache and the proof read. The mesh repair backend interface is `prep.meshfix.MeshFixBackend`
(`register_backend`); the public `meshfix` library (C ABI + Python) plugs in through the `mesh_repair` adapter
module and `repair(..., backend="auto")` prefers it when present.
