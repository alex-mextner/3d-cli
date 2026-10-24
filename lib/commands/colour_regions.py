"""3d colour-regions — per-face surface labels -> colour regions (conformal colour volumes) on a mesh.

WHAT: the surface-label colour pipeline of the Chekhov cat phone stand, generic. Labels per face of a mesh (what a
  sculpt or classification step delivers) become colour groups through a rules file, one closed volume per group and
  connected piece (`build`); the volumes plus `regions.json` feed the `3d prep` colour spec / a slicer 3MF step.
  Around it: `shell-volume` (one volume from chosen labels), `classify` (a skin's labels from the part meshes of an
  earlier build), `transfer` / `transfer-class` (carry labels or one approved class onto a newer mesh by nearest
  face), `snap` (pull label borders onto the strongest crease) and `verify` (borders on geometric edges, volumes
  follow the labels; both gates have a teeth test).
WHY: colour drawn from a picture (outlines, projections) does not sit on the relief: the borders read as drawn on.
  Derived from the surface, the colour border IS a mesh edge between two label groups, and the same mesh judges it.
INVARIANTS: stdlib-only at import time (registry contract); the work runs in the mesh interpreter (`cli.pyrun`) under
  the machine heavy lock (`cli.heavy`, re-entrant) unless --no-lock; exit 1 when `verify` FAILs, 2 bad arguments
  (including a label count that does not match its mesh), 127 missing dependency.
"""

from __future__ import annotations

import subprocess

from cli import heavy
from cli.pyrun import tool_argv
from cli.registry import Command
from errors import UsageError

DEPS = "numpy,scipy,trimesh,rtree,scikit-image,pyyaml"

USAGE = """3d colour-regions build MESH --track NAME=LABELS[@LABEL_MESH] [--track ...] --rules RULES.json --out DIR [options]
3d colour-regions shell-volume MESH --labels LABELS[@LABEL_MESH] --select REGEX [--select ...] --depth MM --pitch MM --out OUT.ply [options]
3d colour-regions classify SKIN_MESH --part NAME=PART_MESH [--part ...] --out LABELS [options]
3d colour-regions transfer MESH --labels LABELS@LABEL_MESH --out LABELS [options]
3d colour-regions transfer-class MESH --source LABELS@SOURCE_MESH --positive NAMES --as NAME --out LABELS [options]
3d colour-regions snap MESH --labels LABELS[@LABEL_MESH] --out LABELS --band MM [options]
3d colour-regions verify MESH --work DIR [options]
  Colour regions derived from the surface of a mesh: per-face labels -> colour groups -> conformal volumes.
  MESH is any file trimesh reads (STL, PLY, OBJ, 3MF). All meshes of one run share one coordinate frame.

  LABELS is a `.npz` (or `.json`) label file: `face_label` int16 per face of ITS mesh (-1 = unlabelled = the default
  colour), `names` a JSON list (id -> name), optional `mesh_digest` (sha1 of the float32 face triangles, in face
  order) and `mesh` (path of its mesh, relative to the file). `LABELS@LABEL_MESH` names the mesh the labels were made
  on; when it is not the MESH itself the labels are transferred by nearest face, and a label counts only where that
  mesh lies within --tol of the MESH skin. Without a label mesh the face count (and a mesh_digest, when present) must
  match MESH: a wrong label count is an error (exit 2), never padded or truncated.

  build      labels of one or more tracks -> colour groups (RULES.json) -> one closed volume per group and
             connected piece. --track NAME=LABELS in INCREASING priority: on a face labelled by several tracks the
             later track wins. Writes into --out DIR: patches/GROUP.ply (the volumes), regions.json (`regions` =
             name, assign, shape stl path relative to the file: usable as the `regions` of a `3d prep` colour spec;
             `groups`: depth, pitch, faces, area), labels_final.npz (face_label, face_group, names, group assigns),
             transfer.json (how each track reached the mesh). Unchanged patches are cache hits (sidecar .sha).
  shell-volume  the volume of every point whose NEAREST skin face is one of the faces whose label matches a --select
             regex, within --depth mm under/over the skin: a Voronoi cell, so its border starts exactly on the label
             border. Read back and checked closed, wound and positive; otherwise exit 1.
  classify   a SKIN mesh labelled by which PART meshes it lies on (a face takes the first listed part whose face
             centroid is within --tol of its own): the classification of an earlier build's skin from its coloured
             volumes. --shift X,Y,Z is the frame offset of the parts (subtracted). --split-edge MM splits skin faces
             with a longer edge (optionally only inside --split-box) and judges the faces in that region by their
             distance to the part SURFACE, not to its face centroids, so a giant face a part was cut out of is not
             labelled whole by where its centroid is; the split mesh is written next to the labels as OUT.mesh.ply
             and named by the label file's `mesh` key.
  transfer   every label of LABELS@LABEL_MESH onto the faces of MESH by nearest face (labels beyond --tol: -1).
  transfer-class  ONE approved class of the source onto a newer mesh: faces of MESH nearest to an approved face of the
             --positive label names get the label --as NAME (id 0), all others -1. --exclude names are classes of
             unknown colour and never a source. Faces farther than --max-dist from every source face get no label.
             Islands under --island-mm2 are flipped to their surround only when the nearest approved faces agree, so
             the approved border itself is never altered; --fill-box fills small other-class islands inside a box.
  snap       pull label borders onto the strongest crease of the mesh within +-BAND mm (a watershed on the crease
             strength; where there is no relief the border stays); labels matching --fixed never change.
  verify     the surface gates on the output of `build` (--work = its --out). GATE 1: the share of each group's
             border (and of every piece >= 3 mm2) on a geometric edge (dihedral or crease >= thresholds); teeth: the
             borders pushed --shift-mm inward must score lower and the real share must beat chance. GATE 2: the
             volume holding the point 0.25 mm under the skin is the one of the face's group; teeth: permuted groups
             must collapse the agreement. Exit 1 when a gate fails.

Options:
  --track NAME=LABELS[@MESH]  build: a label track, repeatable, increasing priority
  --rules FILE        build: rules file (.json/.yaml): {"island_mm2", "transfer_tol_mm", "rules": [{"label": regex,
                      "group", "assign": {"key"|"slot"|"mix"} or null, "depth_mm", "pitch_mm", "min_speck_mm3",
                      "owner", "passive"}]}; order = priority; the first rule matching a label name decides
  --out PATH          build: work directory; shell-volume: PLY; classify/transfer/transfer-class/snap: label file
  --island-mm2 F      build: label islands/holes under F mm2 merge into the surround (default: rules file, else 3);
                      transfer-class: island fill limit (default 6)
  --transfer-tol F    build: a transferred label counts only within F mm of the skin (default: rules file, else 0.25)
  --tol MM            shell-volume, transfer, snap: the same transfer tolerance (0.25); classify: part match
                      distance (0.06)
  --force             build: rebuild every patch even when cached
  --labels LABELS     shell-volume, transfer, snap: the label file
  --select REGEX      shell-volume: label names to take (full match), repeatable
  --depth MM          shell-volume: depth of the volume around the skin
  --pitch MM          shell-volume: voxel pitch (border error <= ~0.5 pitch; memory grows with 1/pitch^3)
  --min-area MM2      shell-volume: skip connected pieces under this area (default 0.3)
  --part NAME=MESH    classify: a part mesh, repeatable; the same NAME again joins its meshes; first listed wins
  --shift X,Y,Z       classify: frame offset of the parts, mm
  --split-edge MM     classify: split skin faces with an edge longer than MM and judge them by surface distance
  --split-box BOX     classify: limit --split-edge to faces inside BOX = x0,x1,y0,y1,z0,z1
  --source LABELS@MESH  transfer-class: the approved labels and their mesh
  --positive NAMES    transfer-class: comma-separated label names of the class to carry over
  --exclude NAMES     transfer-class: comma-separated label names that are not a source
  --as NAME           transfer-class: the label name of the result
  --max-dist MM       transfer-class: faces farther than MM from the source get no label (default 6)
  --fill-box BOX:AREA transfer-class: other-class islands under AREA mm2 centred inside BOX = x0,x1,y0,y1,z0,z1
                      with only positive neighbours become positive; repeatable
  --band MM           snap: how far a border may move
  --scale MM          snap: crease scale (default 0.9)
  --min-crease DEG    snap: creases weaker than this count as flat (default 15)
  --fixed REGEX       snap: labels that never change, repeatable
  --work DIR          verify: the --out directory of `build`
  --regions FILE      verify: regions.json holding the volumes of gate 2 (default WORK/regions.json)
  --skip-volumes      verify: gate 1 only
  --waive G,G         verify: groups that are reported but not gated (labels with no relief to follow)
  --approved-groups G,G  verify: groups with owner-approved borders: gated against chance only
  --json              print the result as JSON instead of the text report (every subcommand; verify keeps its exit code)
  --report FILE       verify: also write the result as JSON to FILE
  --dihedral DEG --crease DEG   verify: what counts as a geometric edge (30, 30)
  --min-share F --min-group F   verify: gate-1 thresholds, all groups / each group (0.5, 0.35)
  --shift-mm MM       verify: how far the teeth test pushes the borders (1.5)
  --agree F           verify: gate-2 agreement threshold (0.97)
  --shift-margin F    verify: how far below the real share the eroded share must be (0.06)
  --chance-margin F   verify: how far above chance the real share must be (0.25)
  --samples N         verify: skin faces sampled by gate 2 (40000; each is a point-in-mesh test, about 2 ms without embreex)
  --quiet             build: no progress lines
  --memory-max SIZE   memory cap of the heavy lock scope (default 6G)
  --no-lock           do not take the heavy lock (e.g. already on a dedicated box)

Examples:
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
  3d colour-regions build cat.stl --track sculpt=sculpt.npz --rules rules.json --out work --json --memory-max 10G --no-lock"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if "-h" in argv or "--help" in argv:
        print(USAGE)
        return 0
    lock = "--no-lock" not in argv
    args = [a for a in argv if a != "--no-lock"]
    memory = heavy.DEFAULT_MEMORY_MAX
    if "--memory-max" in args:
        i = args.index("--memory-max")
        if i + 1 >= len(args):
            raise UsageError("--memory-max needs a size, e.g. 10G", command="colour-regions")
        memory = args[i + 1]
        del args[i : i + 2]
    cmd = tool_argv(DEPS, "colour_regions_cli.py", args)
    if not lock:
        return subprocess.run(cmd, check=False).returncode  # noqa: S603 — argv from cli.pyrun
    return heavy.run_heavy(cmd, memory_max=memory)


COMMAND = Command(
    name="colour-regions",
    group="GEOMETRY & EXPORT",
    summary="per-face surface labels -> colour groups -> conformal colour volumes; transfer, snap, verify",
    usage=USAGE,
    run=run,
)
