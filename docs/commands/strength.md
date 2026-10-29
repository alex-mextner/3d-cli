# `3d strength`

`3d strength` checks an **assembled model** against a load case and returns a verdict
(`PASS` / `WEAK` / `FAIL`) with a safety factor, the stress and displacement it came from, the
hotspot locations and a list of the assumptions behind it. It has two methods and always says
which one produced the number:

| `method` | `label` | What it is | Use it for |
|---|---|---|---|
| `beam` | `ANALYTICAL_SCREEN` | Closed-form strength of materials: a variable-section Euler-Bernoulli beam on cross-sections sliced from the mesh (cantilever, three-point bend, compression with Euler buckling), or the snap-hook strain formulas (`--hook`). No solid model, no contact. | A fast screen of one part; a snap hook before it is modelled. |
| `fea` | `FEA_LINEAR` | Linear-elastic finite elements on tetrahedral meshes of every body, tied or frictionless contact between mates, anisotropic FDM material, anchored loads from `3d.yaml`. | Assemblies, `3d.yaml` load cases, stress hotspots. |

`--method auto` (the default) runs FEA when `scikit-fem` and `pytetwild` are installed
(`uv sync --extra fea`) and falls back to the beam screen for a single part with a `--fixture`
load. The FEA path never reports a beam number and the beam path never claims FEA: the label and
`assumptions[]` in the result say what was and was not modelled.

The command also has the alias `3d structural-check`.

**Not evaluated (and listed as `unverified` in every report):** creep and stress relaxation under
sustained load, fatigue, plasticity above yield, buckling of the FEA model, large deformation, and
print quality (voids, seams, brand scatter). A `PASS` is a static, linear, first-load statement.

## Usage

```bash
3d strength <part.scad|.stl|.3mf | 3d.yaml | project-dir> --material NAME [--load NEWTONS] [options]
3d strength --hook t=T,L=L,w=W,y=Y --material NAME [--orientation SPEC] [--arm-axis X|Y|Z]
```

### Load cases

Pick one:

- `--load NEWTONS` with `--fixture cantilever|simple|compression` on a single file (below).
- `loads:` in `3d.yaml`, with the project (or a directory holding `3d.yaml`) as the input, or
  `--loads 3d.yaml` to analyse a mesh file with a project's anchors and loads.

### Options

| Option | Meaning |
|---|---|
| `--material NAME` | Default material (`3d materials list`). A part's own `material:` in `3d.yaml` wins over it. |
| `--axis X\|Y\|Z\|-X\|-Y\|-Z` | Load direction for `--fixture`. Default `Z`. |
| `--fixture TYPE` | `cantilever` (default: one end fixed, force on the other), `simple` (three-point bend: knife-edge rollers under both ends, force on the centre), `compression` (base fixed, force on the top face along the axis). |
| `--span-axis X\|Y\|Z` | Beam axis for `cantilever`/`simple`. Default: the longest extent off the load axis. |
| `--method auto\|beam\|fea` | See the table above. `beam` rejects assemblies and `3d.yaml` loads with exit 2. |
| `--orientation SPEC` | Print orientation, see [FDM anisotropy](#fdm-anisotropy). Default `z-up`. |
| `--infill PCT` | Sparse core, `20` or `20%`; a shell of `--walls` perimeters (default 3) x `--nozzle` mm (default 0.4) stays solid. Default: solid. |
| `--safety-factor N` | Required safety factor. Default `2`. |
| `--repeated-use` | Snap-fit strain limit x0.6 (frequent opening; Bayer, *Snap-Fit Joints for Plastics*, Table 2). |
| `--hook SPEC` | Closed-form snap hook without a file: `t=1.0,L=7,w=6,y=0.2[,taper=0.5,lead=30,retention=45,mu=0.4]` (arm thickness, free length, width, tip deflection in mm; optional tip/root thickness ratio, barb angles in degrees, friction). |
| `--arm-axis X\|Y\|Z` | Direction of the hook arm's bending fibre (its length axis), relative to the build axis. Default `Z`. |
| `--order 1\|2` | FEA element order. Default `2` (quadratic tets: bending is accurate with 1.5 elements across a wall; linear tets need ~3). |
| `--mesh-size MM`, `--max-elements N` | Target tet edge length, or the total tet budget (default 6000; 40000 for `--order 1`). |
| `--contact tied\|frictionless\|none` | Contact for touching bodies that have no declared mate. Default `tied`. |
| `--crop x0,y0,z0,x1,y1,z1` | Analyse only this box of every body: a sub-model of a large part. The cut faces need a support (a `kind: fixed` load), they are not fixed for you. |
| `-D NAME=VALUE` | OpenSCAD define, repeatable (e.g. `-D 'view="bottom"'`). |
| `--viz [OUT.png]` | Write the stress picture (von Mises on the deformed surface, numbered hotspots, colour bar). Default name `<input>.strength.png`. |
| `--dry-run` | Validate inputs and print the model that would be solved (bodies, orientation, contacts, load cases); no meshing, no verdict. |
| `--json` | Emit the `3d.strength/2` report. |

Exit codes: `0` PASS, `1` WEAK or FAIL (safety factor below the required one), `2` invalid or
unsupported input (non-finite or non-positive numbers, an unreadable or empty mesh, unknown
material / part / anchor, a model that is under-constrained, a `beam` request for an assembly),
`127` a missing dependency.

Verdicts: `PASS` safety factor >= required, `WEAK` between 1 and the required value (the part
holds but the margin is short), `FAIL` below 1.

## Examples

Quick cantilever screen of a bracket, the report carries the method label:

```bash
3d strength bracket.scad --material PLA --load 25 --axis Z
```

Three-point bend of a printed-flat bar as JSON, with the stress picture:

```bash
3d strength part.stl --material PETG --load 12.5 --fixture simple --orientation flat --json
3d strength part.stl --material PETG --load 12.5 --fixture simple --viz bend.png
```

Save a report before handing the load case to another tool:

```bash
3d strength bracket.scad --material PLA --load 25 --json > strength-report.json
```

Read the governing number in a shell pipeline:

```bash
jq '{verdict, method, label, safety_factor, governing}' strength-report.json
```

An assembled project (parts as bodies, `mates:` as contacts, `loads:` as load cases):

```bash
3d strength 3d.yaml --material PETG --viz --safety-factor 3
```

A single mesh with a project's loads, printed standing on its side:

```bash
3d strength case_half.stl --loads 3d.yaml --material PLA --orientation x-up
```

Record a compression load case with a higher target factor:

```bash
3d strength spacer.3mf --material ABS --load 40 --fixture compression --safety-factor 3 --json
```

Snap hook before modelling it (PLA, arm standing along the build axis, repeated opening):

```bash
3d strength --hook t=1.0,L=7,w=6,y=0.2 --material PLA --arm-axis Z --repeated-use
```

Use the compatibility alias:

```bash
3d structural-check part.stl --material PETG --load 12.5 --fixture simple --json
```

## FDM anisotropy

A printed part is stiffer and stronger inside a layer than across layers, so the same part gives
different allowables depending on how it is printed. The material card
(`lib/data/materials.yaml`, overridable per user / project, `3d materials show NAME`) holds

- `e_modulus_mpa`, `yield_mpa`: in-plane values;
- `layer_adhesion`: the across-layer strength as a fraction of the in-plane strength (PLA 0.45,
  PETG 0.7, ABS/ASA 0.6, TPU 0.8; Ahn et al. 2002, PMC9230522);
- `z_modulus_factor`: across-layer modulus / in-plane modulus (PLA 0.85, PETG/ABS/ASA 0.9);
- `poisson`, `strain_limit_pct`: Poisson ratio and the permissible single-assembly snap-fit strain
  (PLA 2.0 %, PETG 4.5 %, ABS 3.5 %, ASA 3.0 %, TPU 15 %; FabFlow 10-cycle FDM figures and Bayer
  Table 2). A card that omits the optional ones gets generic defaults and the report lists the
  fields it defaulted.

The solver treats the part as transversely isotropic about the **build axis** (stiffness from the
card, rotated into the part frame) and checks two failure modes separately:

- in-plane: plane von Mises of the in-layer stress / `yield_mpa`;
- across layers: tension normal to the layer plane and shear on it, `sqrt((<sigma_n>/Z)^2 + (tau/S_z)^2)`
  with `Z = yield x layer_adhesion` (compression across layers does not open them).

The safety factor is `1 / max(index)`; `safety_factors` in each case gives the in-plane and
across-layer value separately. The build axis comes from `--orientation` (or the part's
`orientation:` in `3d.yaml`), and the result records both the axis and where it came from:

| Spec | Build axis |
|---|---|
| `z-up` (default), `x-up`, `y-up` | the model's +Z / +X / +Y points up on the bed |
| `flat` | the part lies on its largest face: the thinnest bounding-box axis is vertical |
| `rotate=RX,RY,RZ` | the part is printed after an OpenSCAD-style rotation by these degrees |
| `FILE.3mf` | the rotation of the first build item of a slicer project 3MF |
| `auto` | the part's `orientation:` in `3d.yaml`, else the 3MF build transform of the input, else `z-up` |

Example: a PLA bar loaded in bending along X has an allowable of 45 MPa printed flat (`z-up`:
layers stacked along Z, X in plane) and 20.2 MPa printed standing on its end (`x-up`: the bending
fibre crosses the layers).

## `3d.yaml` loads, anchors and mates

Loads reuse the existing `anchors:` / `loads:` sections. Positions are in the model frame in mm.

```yaml
project: {name: lid-test, material: PETG}
parts:
  base: {file: base.scad, module: base, orientation: z-up}
  lid:  {file: base.scad, module: lid, material: PLA, orientation: flat}
anchors:
  wall:  {pos: [0, 0, 5]}                 # where the part is held
  latch: {pos: [30, 0, 12], area: 12}     # `area` (mm^2) sets the patch radius, else radius_mm, else 1.5 mm
loads:
  clamp: {anchor: wall, kind: fixed}      # a support: every case gets it. `components: xz` fixes only those
  tug:   {anchor: latch, vector: [0, 0, -15]}                    # force, N, spread over the patch
  snap:  {anchor: latch, kind: displacement, vector: [-0.2, 0, 0]}   # prescribed mm (non-zero entries)
  water: {anchor: wall, kind: pressure, pressure_mpa: 0.05}      # acts normal to the surface, inwards
  g:     {kind: gravity, vector: [0, 0, -9.81]}                  # m/s^2, whole model, no anchor
mates:
  - {a: base, b: lid, fit_type: glue}     # tied;  snap | sliding -> frictionless;  `contact:` overrides
```

- `kind: force` (default, `vector` in N), `pressure`, `displacement`, `gravity`, `fixed`. Optional
  per load: `radius_mm`, `part:` (restrict to one body), `case:` (loads sharing a `case:` name are
  one combined case; otherwise each load is its own case).
- Instead of an anchor, a load can name a planar face: `plane: z, at: -3.0, tol_mm: 0.05` (a support on the cut face of a `--crop` sub-model).
- `safety_factors.away_from_patches` is the lowest factor farther than two element lengths from every load / support patch: read it next to the conservative global one.
- Supports (`kind: fixed`) apply to every case. A case with no support is rejected (exit 2).
- A case whose only loads are prescribed displacements is **displacement controlled** (a snap
  being pushed past its barb): it is judged on the permissible strain, not on the stress, because
  stress relaxes while the strain is imposed. Force cases are judged on stress.
- `mates:` (the schema of the assembly command) name bodies by part name; `fit_type` chooses the
  contact (`glue`, `press`, `tight` -> tied; `snap`, `sliding` -> frictionless), or give
  `contact: tied|frictionless` and `contact_tolerance_mm`. Touching bodies without a mate get the
  `--contact` default, and the report lists every contact with its origin.
- A project is analysed at the pose its parts' files / modules define.

## Contact model

Contact is node-to-surface: points of the finer-meshed ("slave") body's contact face are matched to
the other body's surface with quadratic shape functions, and enforced by a penalty.

- **tied**: all three components are bonded. A beam cut in two halves and tied reproduces the
  monolithic beam's tip deflection to 0.1 % in the test-suite.
- **frictionless**: the normal gap may not close (active-set iterations), tangential sliding is
  free (a 1e-6 tangential spring only keeps the matrix non-singular). It cannot carry tension: a lid
  held only by frictionless contact separates, and the solve says so.

A mate whose faces are farther apart than the tolerance produces a warning, not a silent
coupling: those bodies are analysed as separate, and a body with neither a support nor a contact
to a supported body is rejected as under-constrained.

## Output

Text output lists the bodies with their orientation source, the materials with in-plane and
across-layer allowables, the mesh, the contacts, every case (verdict, safety factor, governing
mode, max von Mises, max displacement, safety factor per direction, hotspots), the assumptions,
the unverified list, warnings, and the stress picture path.

`--json` emits schema `3d.strength/2`:

```text
schema                 "3d.strength/2"
status                 "SOLVED" | "PLAN" (--dry-run)
verdict                "PASS" | "WEAK" | "FAIL" (null for a plan)
method / label         "beam" / "ANALYTICAL_SCREEN" | "fea" / "FEA_LINEAR"
safety_factor          governing (lowest) safety factor over all cases
safety_factor_required --safety-factor
max_von_mises_mpa      largest von Mises of any case (beam: the bending / axial stress)
max_displacement_mm    largest displacement of any case
governing              "in_plane" | "across_layers" | "permissible strain" | "bending stress" | ...
input                  {file, loads_from, bodies[{name, material, orientation{build_axis, source}, volume_mm3, infill}]}
materials[]            {name, e_in_plane_mpa, e_across_layers_mpa, allowables{in_plane_mpa, across_layers_mpa, shear_*},
                        strain_limit_*_pct, defaulted_fields[]}
cases[]                {name, control, verdict, safety_factor, governing, safety_factors{...}, max_von_mises_mpa,
                        max_principal_strain, max_displacement_mm, max_displacement_at_mm, reaction_n, applied_n,
                        per_body[], contact{iterations, active_nodes}, hotspots[{body, xyz, von_mises_mpa,
                        safety_factor, mode, near_constraint}], detail[]}
contacts[]             {a, b, kind, origin, fit_type, slave, master, nodes}
mesh                   {order, total_tets, total_dofs, bodies[{name, tets, nodes, dofs, edge_length_mm, thinnest_wall_mm}]}
assumptions[]          every modelling choice behind the number
unverified[]           what was not evaluated
warnings[]             mesh coarse for a thin wall, uncoupled mates, ...
artifacts              {stress_png}
timing_s               {mesh, solve, total}
```

Peak stress at a point load or fixed edge is a singularity of the model, not a property of the part:
it grows as the mesh is refined. The load is therefore spread over a patch and every hotspot
carries `near_constraint` when it sits on one. The verdict still uses the peak, which is
conservative; move or enlarge the patch (`radius_mm`) if the support is not what you mean.

## Validation

`tests/test_structural_closed_form.py` and `tests/test_structural_fea.py` check the solver against
analytic solutions: constant and tapered cantilevers (closed form vs numerical quadrature of the
curvature integral, within 1 %), three-point bend (FEA vs Euler-Bernoulli within 5 % and vs
Timoshenko within 1.5 %), cantilever tip deflection (within 2 %), tapered cantilever (3 %), a
thick pipe under internal pressure (hoop stress vs Lame, 5 %), the snap-hook root strain
`1.5 t y / L^2` (10 %), a tied two-body beam (4 %), a frictionless stack (5 %), and the orientation
dependence of modulus and allowables. The library choice and measured timings are in
[docs/notes/2026-10-24-strength-solver-evaluation.md](../notes/2026-10-24-strength-solver-evaluation.md).
