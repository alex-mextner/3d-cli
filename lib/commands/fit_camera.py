"""3d fit-camera — fit an OpenSCAD camera to a reference photo by maximizing IoU.

WHAT: searches for the camera pose (distance, pan, elevation) that makes a rendered
  silhouette overlap maximally with a reference photo, then saves that pose as
  camera.json for locked downstream renders.

WHY: a model and a reference photo almost never start aligned. A drifting pose makes
  the match-loop score meaningless ("never improves for no reason" — the top failure
  signature). `fit-camera` locks the viewpoint first, so every subsequent score change
  reflects a real geometry change, not a camera wobble.

Examples:
  3d fit-camera model.scad ref.jpg
  3d fit-camera model.scad ref.jpg --out match/camera.json --draw-axes
  3d fit-camera examples/cube.scad ref.png --rand 8 --refine 3   # quick smoke
  3d fit-camera model.scad mask.png --mask-polarity light --backplate ref.jpg --objective contour --spatial-report match/spatial --trace match/trace.jsonl

ROADMAP §7: "3d fit-camera — silhouette-IoU camera pose fitting (bbox-derived bounds),
  saves camera.json, writes fit render + overlay. Pose freeze: hold the pose fixed
  through the shape match so the monotonic-acceptance score is meaningful."
"""
from __future__ import annotations

import os

from cli.env import require_openscad
from cli.pyrun import exec_tool
from cli.registry import Command
from errors import InputNotFound, UsageError

USAGE = """3d fit-camera <model.scad> <reference> [options]
  Fit an OpenSCAD camera to a reference photo by maximizing silhouette IoU.
  Optimizer: random search -> coordinate-descent refine (deterministic seed).
  Search bounds (distance/pan/steps) are derived from the model's bbox diagonal;
  the look-at center auto-estimates from the bbox centroid unless --center given.
  Writes camera.json + <out>_fit.png (full-res fit) + <out>_overlay.png
  (render-cyan over reference-red ghost, so you can SEE the alignment).

Options:
  --out FILE            output JSON (default ./camera.json)
  --center "x,y,z"     initial look-at (default: model bbox centroid, else origin)
  --opt-size WxH        optimization render size (default ~300px wide @ ref aspect)
  --final-size WxH      final fit render size (default: reference native resolution)
  --thresh N            ref subject darkness threshold 0..255 (default 150)
  --mask-polarity P     subject polarity: dark raw photo (default) or light binary mask
  --backplate FILE      original/reference photo to show in spatial proof panels
  --rand N              random-search samples (default 80)
  --refine N            coordinate-descent refine steps (default 40)
  --seed N              RNG seed for reproducibility (default 7)
  --el-range lo,hi      elevation search range in degrees (default -45,85); -89,89 restores full sphere
  --draw-axes           overlay PCA principal axis + bbox contour of both silhouettes
  --spatial-report DIR  write contour metrics + proof_panel.png for spatial diagnostics
  --trace FILE          write best-candidate trace JSONL for demo/video tooling
  --objective NAME      area-iou (default) or contour edge F1/SDF/Chamfer/p95

Use the result:
  openscad --render --camera="$(jq -r .camera_arg camera.json)" -o view.png model.scad

Examples:
  3d fit-camera model.scad ref.jpg
  3d fit-camera model.scad ref.jpg --out match/camera.json --draw-axes
  3d fit-camera examples/cube.scad ref.png --rand 8 --refine 3   # quick smoke
  3d fit-camera model.scad ref.jpg --el-range -20,75 --seed 11
  3d fit-camera model.scad mask.png --mask-polarity light --backplate ref.jpg --objective contour --spatial-report match/spatial --trace match/trace.jsonl"""

_VALUE_FLAGS = {
    "--out",
    "--center",
    "--opt-size",
    "--final-size",
    "--thresh",
    "--mask-polarity",
    "--backplate",
    "--rand",
    "--refine",
    "--el-range",
    "--seed",
    "--spatial-report",
    "--trace",
    "--objective",
}
_BOOL_FLAGS = {"--draw-axes"}


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 1 if not argv else 0
    if len(argv) < 2:
        print(USAGE)
        return 1
    require_openscad("fit-camera")  # exports OPENSCAD; the tool reads it from the env.

    model, ref = argv[0], argv[1]
    if not os.path.isfile(model):
        raise InputNotFound(model, command="fit-camera")
    if not os.path.isfile(ref):
        raise InputNotFound(ref, command="fit-camera")

    args = ["--model", model, "--ref", ref]
    i = 2
    rest = argv
    n = len(rest)
    needs_spatial_metrics = False
    while i < n:
        a = rest[i]
        if a in _VALUE_FLAGS:
            if i + 1 >= n:
                raise UsageError(f"option {a} needs a value", command="fit-camera")
            value = rest[i + 1]
            if a == "--backplate" and not os.path.isfile(value):
                raise InputNotFound(value, command="fit-camera")
            if a == "--spatial-report" or (a == "--objective" and value == "contour"):
                needs_spatial_metrics = True
            args += [a, value]
            i += 2
        elif a in _BOOL_FLAGS:
            args.append(a)
            i += 1
        else:
            print(USAGE)
            raise UsageError(f"unknown option '{a}'", command="fit-camera")

    deps = "numpy,pillow,scipy" if needs_spatial_metrics else "numpy,pillow"
    return exec_tool(deps, "fit_camera.py", args)


COMMAND = Command(
    name="fit-camera",
    group="REFERENCE-MATCH PIPELINE",
    summary="fit an OpenSCAD camera to a reference (silhouette IoU); saves the viewpoint",
    usage=USAGE,
    run=run,
)
