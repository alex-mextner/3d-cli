"""3d mesh — watertight / manifold / self-intersection / volume report.

WHAT: runs a geometric health check on a mesh or .scad file, reporting whether it is
  watertight (closed), manifold (no holes), self-intersection-free, and its volume.

WHY: every slicer and every downstream gate (printability, collision, pack) requires a
  clean manifold mesh. `mesh` is the surgical primitive that exposes the raw geometry
  facts before the umbrella gates (`3d check`) bundle them into a verdict.

Examples:
  3d mesh part.stl                    # full mesh report on an STL
  3d mesh model.scad -D 'width=80'    # export + check at a specific parameter
  3d check part.scad --mesh           # same gate, run through the umbrella command

ROADMAP §3: "mesh — watertight/manifold/self-intersect/volume report.
  Engine tiers: trimesh+open3d (full) -> trimesh+manifold3d -> openscad warning grep.
  Exit 0 = PASS, 1 = FAIL (geometric defect), 2 = load/usage error."
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from cli.pyrun import exec_tool, tool_argv
from cli.registry import Command

USAGE = """3d mesh <file.stl|.3mf|.scad> [-D k=v ...] [--repair [-o OUT]]
  Report watertight / manifold / self-intersection / volume.
  --repair  fix the mesh first with meshfix (holes, flipped faces, duplicate vertices/faces, non-manifold
            edges, T-junctions, islands), write <file>.fixed.stl (or -o OUT), then gate the repaired mesh.
  Engine tiers: trimesh+open3d (full) -> trimesh+manifold3d -> openscad warning grep.
  Exit 0 = PASS, 1 = FAIL (geometric defect), 2 = load/usage error.

3d mesh deform <mesh> <spec.yaml> [-o OUT] [options]      deterministic sculpt-like deformation, no GUI
3d mesh fit-silhouette <mesh> <spec.yaml> [-o OUT]        heuristic silhouette-IoU handle fit (see `3d mesh deform --help`)

Example:
  3d mesh part.stl
  3d mesh model.scad -D 'width=80'
  3d mesh purchased.stl --repair -o purchased.fixed.stl
  3d mesh deform cat.stl examples/mesh-fit/cat-fur.yaml -o cat_fur.stl --verify-determinism"""

DEFORM_USAGE = """3d mesh deform <mesh.stl|.3mf> <spec.yaml|.json> [-o OUT] [options]
3d mesh fit-silhouette <mesh.stl|.3mf> <spec.yaml|.json> [-o OUT] [options]
  Apply an ordered, seeded list of sculpt-like ops (named regions with smooth falloff, keep-out zones that
  never move, grab/ARAP, smooth, inflate, crease, pinch, fur relief, remesh, decimate, repair, symmetrize)
  to a mesh, with no GUI and no Blender. Same input + same spec + same seed = the same SHA-256.
  fit-silhouette is the same engine for a spec whose ops include `fit_silhouette` (a HEURISTIC greedy
  search that nudges region handles to raise the silhouette IoU against reference masks) and prints the
  per-iteration IoU table.

Options:
  -o, --out FILE            result .stl or .3mf (byte-reproducible); .obj/.ply via trimesh (default <mesh>.deformed.stl)
  --report FILE             JSON report (default <out>.report.json): per-op moved counts / max displacement,
                            keep-out gate, watertight + volume before/after, self-intersections, timings
  --collage FILE.png        before / after / displacement heat-map views (numpy rasteriser; needs Pillow)
  --verify-determinism      run the spec twice, fail (exit 1) unless both meshes are identical
  --seed N                  override the spec seed
  --labels FILE.npz         per-face labels (array `face_label`, plus any other array with one row per face) to carry
                            to the result; faces split by a remesh refine op give their row to every child (refused
                            next to decimate / repair / botsch / pymeshlab, which have no input-to-output face map)
  --out-labels FILE.npz     where to write them (default <out>.labels.npz); needs --labels
  --no-self-intersections   skip the self-intersection count (pymeshlab, else a built-in KD-tree test)
  --json                    print the full report instead of the summary

Spec (YAML/JSON): seed, regions, keepout, ops, [landmarks, keepout_tolerance_mm].
  regions:  name -> one of box [x0,x1,y0,y1,z0,z1] | sphere | ellipsoid | capsule | halfspace | zrange | points |
            ramp {quantity: nx|ny|nz|x|y|z|curvature, from, to} | normal_cone | gap {min, max} |
            union / intersect / product / difference [names]; modifiers falloff (mm), margin, invert, scale, blur
  landmarks: {file: landmarks.json, prefix: lm_}   every landmark box becomes the region <prefix><name>
  keepout:  regions whose vertices never move (weight 1 = bit-identical; the falloff band fades to 0).
            The gate re-checks the OUTPUT against the INPUT: max displacement must be 0.0 mm, else exit 1.
  ops:      smooth | inflate | draw | displace (direction, amount) | pinch | crease | scale_local | symmetrize | grab (solver none|harmonic|
            biharmonic|arap, handles, anchors, influence) | fur (alias relief; overhang_safe) | remesh (engine
            refine|botsch|pymeshlab; refine takes target_edge or max_edge and honours `region`) | decimate (engine manifold3d|quadric) | repair (engine builtin|meshfix) |
            fit_silhouette (views, handles)

Exit: 0 PASS, 1 gate FAIL (keep-out moved / not reproducible), 2 usage or input error.

Example:
  3d mesh deform part.stl spec.yaml -o part_fit.stl --collage part_fit.png --verify-determinism
  3d mesh deform part.stl spec.yaml --report fit.json --json
  3d mesh deform part.stl spec.yaml --labels part.labels.npz --out-labels part_fit.labels.npz
  3d mesh fit-silhouette part.stl fit.yaml -o part_fit.stl"""

DEFORM_SUBCOMMANDS = ("deform", "fit-silhouette")


def _spec_mentions(argv: list[str], word: str) -> bool:
    """True when the spec file (2nd positional after the subcommand) contains `word`; unreadable -> False."""
    if len(argv) < 3:
        return False
    try:
        with open(argv[2], encoding="utf-8") as fh:
            return word in fh.read()
    except OSError:
        return False


def _offer_target(argv: list[str]) -> Path | None:
    """The file a deform/fit-silhouette run writes when it sits in a project (or a downloaded model's
    directory) and the publish offer is not switched off; else None (the plain exec path)."""
    try:
        from publishing import offer, offer_run

        if offer.default_mode() == "off":
            return None
        from geometry.deform_args import output_option

        out = output_option(argv[0], argv[1:])
        target = Path(out) if out else Path(argv[1]).with_suffix(".deformed.stl")
        return target if offer_run.project_of(target) is not None else None
    except Exception:
        return (
            None  # an offer that cannot be decided must not change how the tool is run
        )


def _run_deform(argv: list[str]) -> int:
    if len(argv) == 1 or argv[1] in ("-h", "--help"):
        print(DEFORM_USAGE)
        return 0 if len(argv) > 1 else 1
    # Pillow only for reference masks (fit-silhouette) and the collage PNG.
    needs_pillow = (
        argv[0] == "fit-silhouette"
        or "--collage" in argv
        or _spec_mentions(argv, "fit_silhouette")
    )
    deps = "trimesh,manifold3d,numpy,scipy,rtree,pyyaml" + (
        ",pillow" if needs_pillow else ""
    )
    offered = _offer_target(argv)
    if offered is None:
        return exec_tool(deps, "geometry/mesh_deform.py", argv)
    # A modify step that lands in a project: run the tool as a child (not exec) so the publish offer can
    # follow it. The child's exit code is returned unchanged; the offer never changes it.
    done = subprocess.run(tool_argv(deps, "geometry/mesh_deform.py", argv), check=False)
    if done.returncode == 0:
        from publishing import offer_run

        offer_run.after_modify(offered)
    return done.returncode


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if argv[0] in DEFORM_SUBCOMMANDS:
        return _run_deform(argv)
    return exec_tool("trimesh,manifold3d,numpy,scipy,rtree", "mesh_check.py", argv)


COMMAND = Command(
    name="mesh",
    group="QA & GATES",
    summary="watertight/manifold/self-intersect/volume (trimesh/manifold3d/open3d)",
    usage=USAGE,
    run=run,
)
