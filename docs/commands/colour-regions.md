# `3d colour-regions` — surface labels to colour regions

Colour regions derived from the SURFACE of a mesh: per-face labels (what a sculpt or classification step
delivers) become colour groups through a rules file, and every group becomes a closed, conformal colour volume
(a Voronoi cell of the skin) that a slicer step cuts out of the model. Ported from the Chekhov cat phone stand
(issue [#551](https://github.com/alex-mextner/3d-cli/issues/551)); nothing in it is specific to that model.

Why: colour drawn from a picture (outlines, projections, noise on the edges) does not sit on the relief, so the
borders read as drawn on. Derived from the surface, a colour border IS a mesh edge between two label groups, the
same mesh can judge it (`verify`), and an approved border can be carried to a newer mesh unchanged
(`transfer-class`).

## Usage

```
3d colour-regions build MESH --track NAME=LABELS[@LABEL_MESH] [--track ...] --rules RULES.json --out DIR [options]
3d colour-regions shell-volume MESH --labels LABELS[@LABEL_MESH] --select REGEX [--select ...] --depth MM --pitch MM --out OUT.ply [options]
3d colour-regions classify SKIN_MESH --part NAME=PART_MESH [--part ...] --out LABELS [options]
3d colour-regions transfer MESH --labels LABELS@LABEL_MESH --out LABELS [options]
3d colour-regions transfer-class MESH --source LABELS@SOURCE_MESH --positive NAMES --as NAME --out LABELS [options]
3d colour-regions snap MESH --labels LABELS[@LABEL_MESH] --out LABELS --band MM [options]
3d colour-regions verify MESH --work DIR [options]
```

`MESH` is any file trimesh reads (STL, PLY, OBJ, 3MF); all meshes of one run share one coordinate frame. The work
runs in the mesh interpreter (`uv sync --extra prep`: numpy, scipy, trimesh, rtree, scikit-image, pyyaml) under the
machine heavy lock (`3d heavy`); `--no-lock` skips the lock, `--memory-max SIZE` sizes its memory cap (default 6G).
`pymeshlab` is optional: with it, cells of more than 150 000 faces are decimated and open cells repaired
(`pip install pymeshlab`); without it they stay at full resolution and the plain repair applies.

## The label file

A `.npz` (or `.json` with the same keys):

| Key | Meaning |
|---|---|
| `face_label` | int16 per face of the labelled mesh; `-1` = unlabelled = the default colour |
| `names` | JSON list, label id -> name |
| `mesh_digest` | optional: sha1 of that mesh's float32 face triangles `(n, 3, 3)` in face order (independent of vertex numbering) |
| `mesh` | optional: path of the labelled mesh, relative to the label file |

`LABELS@LABEL_MESH` names the mesh the labels were made on. When it is not the target MESH itself the labels are
transferred by nearest face (the label surface is sampled every 0.1 mm, plus every face centroid) and a label counts
only where that mesh lies within `--tol` of the target skin. Without a label mesh the face count (and a
`mesh_digest`, when present) must match MESH. A wrong label count, ids outside `-1..len(names)-1`, a digest of
another mesh or a label mesh with another face count is an error (exit 2): labels are never padded or truncated.

## The rules file (`build`)

```json
{
  "island_mm2": 3.0,
  "transfer_tol_mm": 0.25,
  "rules": [
    {"label": "eye_[LR]_iris", "group": "iris", "assign": {"mix": "GKGKWK"}, "depth_mm": 1.6, "pitch_mm": 0.08, "min_speck_mm3": 0.2},
    {"label": "white_.*", "group": "white", "assign": {"key": "W"}, "depth_mm": 3.0, "pitch_mm": 0.15, "owner": "white"},
    {"label": "eye_[LR]_pupil", "group": "default", "assign": null},
    {"label": "base", "group": "default", "assign": null, "passive": true}
  ]
}
```

`.yaml`/`.yml` works the same. The rules file is the ONLY place that maps a label name to a colour; the first rule
whose `label` regex fully matches a name decides, and a name without a rule stops the build. Order = priority.

| Field | Meaning |
|---|---|
| `label` | regex, full match on the label name |
| `group` | colour group (letters, digits, `_.-`): rules of one group form ONE volume per connected piece; one `assign`/`depth_mm`/`pitch_mm` per group |
| `assign` | `{"key": "W"}`, `{"slot": 2}`, `{"mix": "WR"}` (the `assign` of a `3d prep` colour spec) or `null` = the default colour: no volume, but its faces still END the neighbouring volumes exactly on the label border |
| `depth_mm`, `pitch_mm` | depth of the volume under/over the skin, voxel pitch (border error <= ~0.5 pitch); required with an `assign` |
| `min_speck_mm3` | optional, passed through to `regions.json` `groups` for the slicer step |
| `owner` | optional: the label is taken only from the track of this name while that track is an input (an approved zone track replaces the zones of a sculpt track) |
| `passive` | optional, only with `assign: null`: the label never overrides a lower-priority track ("nothing sculpted here") |
| `island_mm2` | top level: label islands and holes smaller than this merge into the surround, except a label that is itself tiny (a toe bean is the feature, not a speck) |
| `transfer_tol_mm` | top level: the transfer tolerance |

## `build` — labels to volumes

`--track NAME=LABELS[@MESH]` in INCREASING priority: on a face labelled by several tracks the later track wins. Colour
groups compete only against faces of OTHER groups and the default skin, so adjacent groups meet on the bisector of
their skin faces, i.e. on the label border. Outputs in `--out DIR`:

| File | Content |
|---|---|
| `patches/GROUP.ply` | the colour volumes (read back and checked: closed, wound, positive, no inverted piece) |
| `regions.json` | `regions`: `{name, assign, shape: {type: stl, path}}` with the path relative to the file: these are the `regions` of a `3d prep` colour spec as written; `groups`: depth, pitch, `min_speck_mm3`, faces, area, patch; `labels`; `mesh_digest` |
| `labels_final.npz` | per face: `face_label`, `names`, `face_group`, `group_names`, `group_assign`, `mesh_digest` |
| `transfer.json` | per track: identity or nearest-face, distances (p50, p95, max), dropped faces, faces per label |

A patch is rebuilt only when its faces, depth, pitch or the volume code changed (sidecar `.sha`); `--force` rebuilds.
A coloured label on a body that is not the main surface (a cavity, an inserted part) is an error.

## The other subcommands

- `shell-volume` — one volume from the faces whose label matches a `--select` regex: the points whose NEAREST skin
  face is selected, within `--depth`; pieces under `--min-area` mm2 are skipped.
- `classify` — a SKIN mesh labelled by the PART meshes it lies on (first listed part within `--tol` of the face
  centroid wins; the same NAME again joins meshes); `--shift X,Y,Z` undoes the parts' frame offset;
  `--split-edge MM` (optionally inside `--split-box x0,x1,y0,y1,z0,z1`) splits giant skin faces and judges the faces
  of that region by their distance to the part SURFACE (parts sampled every 0.8 `--tol`), so a part cut out of a giant
  face cannot mislabel the whole face by where its centroid is; the split mesh is written as `OUT.mesh.ply` and named
  by the label file's `mesh` key.
- `transfer` — every label of `LABELS@LABEL_MESH` onto the faces of MESH (labels beyond `--tol`: -1).
- `transfer-class` — ONE approved class onto a newer mesh: faces nearest to an approved face of the `--positive`
  names get the label `--as NAME` (id 0), the rest -1. `--exclude` names are classes of unknown colour and never a
  source; faces farther than `--max-dist` from every source face get no label (new relief: the new labels decide).
  Islands under `--island-mm2` are flipped to their surround only when the nearest approved faces agree with the
  surround, so the approved border itself is never altered; `--fill-box BOX:AREA` fills other-class islands under
  AREA mm2 inside a box (a region the approved build painted solid).
- `snap` — pull label borders onto the strongest crease within `--band` mm (watershed on the crease strength at
  `--scale`, creases under `--min-crease` degrees count as flat); where the skin has no relief the border stays;
  labels matching `--fixed` never change.
- `verify` — the surface gates on a `build` output (`--work`): GATE 1, the share of each group's border (and of every
  piece >= 3 mm2) on a geometric edge (`--dihedral` / `--crease`), gated at `--min-share` overall and `--min-group`
  each, with a teeth test (the same labels with the borders pushed `--shift-mm` inward must score lower and the real
  share must beat chance); GATE 2, the volume (`--regions`, default `WORK/regions.json`) holding the point 0.25 mm
  under the skin is the one of the face's group (`--agree`), with a teeth test (permuted groups must collapse it);
  `--skip-volumes` runs gate 1 only. `--waive G,G` reports groups without gating them (labels with no relief to
  follow); `--approved-groups G,G` gates owner-approved borders against chance only. `--report FILE` writes the result
  as JSON; `--json` prints it instead of the text report.
  Exit 1 when a gate fails.

Exit codes: 0 ok; 1 a `verify` gate FAILed or a colour cell is not a closed volume; 2 bad arguments or inputs
(including a wrong label count); 127 a missing dependency.

## Options

| Option | Subcommand | Example |
|---|---|---|
| `--track NAME=LABELS[@MESH]` | build | `--track features=features.npz --track eyes=eyes.npz@eyes_step.stl` |
| `--rules FILE` | build | `--rules rules.json` |
| `--out PATH` | all but verify | `--out work` (build), `--out pink.ply` (shell-volume), `--out new_labels.npz` (the label-file ones) |
| `--island-mm2 F` | build, transfer-class | `--island-mm2 4` |
| `--transfer-tol F` | build | `--transfer-tol 0.3` |
| `--tol MM` | shell-volume, transfer, snap, classify | `--tol 0.25` (classify: part distance, `--tol 0.06`) |
| `--force` | build | `--force` |
| `--labels LABELS` | shell-volume, transfer, snap | `--labels sculpt.npz` |
| `--select REGEX` | shell-volume | `--select 'pad_.*' --select nose_leather` |
| `--depth MM`, `--pitch MM` | shell-volume | `--depth 1.6 --pitch 0.08` |
| `--min-area MM2` | shell-volume | `--min-area 0.5` |
| `--part NAME=MESH` | classify | `--part pink=pink_part.stl --part white=white_a.stl` |
| `--shift X,Y,Z` | classify | `--shift 10,20,0` |
| `--split-edge MM`, `--split-box BOX` | classify | `--split-edge 2 --split-box -50,50,-20,20,0,40` |
| `--source LABELS@MESH` | transfer-class | `--source approved.npz@approved.mesh.ply` |
| `--positive NAMES`, `--exclude NAMES`, `--as NAME` | transfer-class | `--positive white --exclude pink --as white_approved` |
| `--max-dist MM` | transfer-class | `--max-dist 6` |
| `--fill-box BOX:AREA` | transfer-class | `--fill-box 10,30,-20,0,0,15:15` |
| `--band MM`, `--scale MM`, `--min-crease DEG` | snap | `--band 2.5 --scale 0.9 --min-crease 15` |
| `--fixed REGEX` | snap | `--fixed 'pad_.*'` |
| `--work DIR`, `--regions FILE` | verify | `--work work --regions work/regions.json` |
| `--skip-volumes` | verify | `--skip-volumes` |
| `--samples N` | verify | `--samples 5000` |
| `--waive G,G`, `--approved-groups G,G` | verify | `--waive iris,goatee --approved-groups white` |
| `--json` | all | `--json` prints the result as JSON instead of the text report |
| `--report FILE` | verify | `--report gates.json` |
| `--dihedral DEG`, `--crease DEG`, `--min-share F`, `--min-group F`, `--shift-mm MM`, `--agree F` | verify | `--dihedral 30 --crease 30 --min-share 0.5 --min-group 0.35 --shift-mm 1.5 --agree 0.97` |
| `--shift-margin F`, `--chance-margin F` | verify | `--shift-margin 0.1 --chance-margin 0.3` |
| `--quiet` | build | `--quiet` |
| `--memory-max SIZE` | all | `--memory-max 10G` |
| `--no-lock` | all | `--no-lock` |

## Examples

Examples:

```
3d colour-regions build cat.stl --track features=features.npz --track eyes=eyes.npz@eyes_step.stl --rules rules.json --out work
3d colour-regions build cat.stl --track sculpt=sculpt.npz --rules rules.yaml --out work --island-mm2 4 --transfer-tol 0.3 --force --quiet
3d colour-regions shell-volume cat.stl --labels sculpt.npz --select 'pad_.*' --select nose_leather --depth 1.6 --pitch 0.08 --out pink.ply --min-area 0.5
3d colour-regions classify approved_skin.stl --part pink=pink_part.stl --part white=white_a.stl --part white=white_b.stl --out approved.npz --shift 10,20,0 --tol 0.06 --split-edge 2 --split-box -50,50,-20,20,0,40
3d colour-regions transfer new.stl --labels old.npz@old.stl --out new_labels.npz --tol 0.25
3d colour-regions transfer-class new.stl --source approved.npz@approved.mesh.ply --positive white --exclude pink --as white_approved --out white.npz --max-dist 6 --island-mm2 6 --fill-box 10,30,-20,0,0,15:15
3d colour-regions snap cat.stl --labels zones.npz --out snapped.npz --band 2.5 --scale 0.9 --min-crease 15 --fixed 'pad_.*'
3d colour-regions verify cat.stl --work work --waive iris,goatee --approved-groups white --report gates.json
3d colour-regions verify cat.stl --work work --regions work/regions.json --dihedral 30 --crease 30 --min-share 0.5 --min-group 0.35 --shift-mm 1.5 --agree 0.97
3d colour-regions verify cat.stl --work work --skip-volumes
3d colour-regions verify cat.stl --work work --samples 5000
3d colour-regions verify cat.stl --work work --shift-margin 0.1 --chance-margin 0.3
3d colour-regions build cat.stl --track sculpt=sculpt.npz --rules rules.json --out work --json --memory-max 10G --no-lock
```

A re-sculpt keeping the approved borders, end to end:

```
3d colour-regions classify approved_skin.stl --part white=white.stl --out approved.npz --shift 10,20,0
3d colour-regions transfer-class new.stl --source approved.npz@approved_skin.stl --positive white --as white_approved --out white.npz
3d colour-regions build new.stl --track white=white.npz --track sculpt=sculpt.npz --rules rules.json --out work
3d colour-regions verify new.stl --work work --approved-groups white
```

## Limits (measured, not hidden)

- Gate 1 judges borders against the relief of the mesh itself: on smooth skin without relief any border fails it,
  which is the point; use `--waive` for labels that are known to have no relief to follow and say so in the report.
  Grown borders are not a teeth direction: a feature enclosed by rims ~1 mm outside its border scores as high or
  higher when grown, so the teeth test erodes.
- A `classify` match outside the `--split-box` is by face centroid: a skin face much larger than the cut pieces of a
  part mislabels as a whole unless `--split-edge` splits it and judges it by surface distance.
- The teeth test erodes a border into the DEFAULT skin only: a border between two coloured groups does not move in the
  eroded labels, so labels that cover the whole skin with coloured groups and no default faces cannot pass the teeth
  check (they exit 1 even when every border sits on a crease); and when every gated region is narrower than twice
  `--shift-mm` the eroded share is empty and only the comparison with chance is meaningful. Waive such groups.
- Cell borders are exact to the voxel (<= ~0.5 pitch); a volume grid over 140 M voxels is refused (raise the pitch).
