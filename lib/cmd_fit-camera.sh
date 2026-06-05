#!/usr/bin/env bash
# 3d fit-camera — silhouette-based camera POSE FITTING (generic, project-agnostic).
#
# Iteratively searches OpenSCAD camera params (azimuth, elevation, distance,
# pan-x, pan-z) so the rendered silhouette best overlaps a reference photo's
# silhouette (maximize IoU). Saves the fitted 6-param vector camera to JSON so
# later per-detail verification uses that exact viewpoint. Search bounds are
# derived from the model's bounding box, so it works at any scale with no
# hardcoded values.
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

usage() {
cat <<EOF
3d fit-camera <model.scad> <reference> [options]
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
  --rand N              random-search samples (default 80)
  --refine N            coordinate-descent refine steps (default 40)
  --draw-axes           overlay PCA principal axis + bbox contour of both silhouettes
  --seed N              RNG seed for reproducibility (default 7)

Output JSON keys: camera_arg (6-num string), camera, params (azim/elev/dist/panx/panz),
  center, iou, model_diag, opt_size, final_size, ref, fit_render, overlay.

Use the result:
  openscad --render --camera="\$(jq -r .camera_arg camera.json)" -o view.png model.scad

Examples:
  3d fit-camera model.scad ref.jpg
  3d fit-camera model.scad ref.jpg --out match/camera.json --draw-axes
  3d fit-camera examples/cube.scad ref.png --rand 8 --refine 3   # quick smoke
EOF
}
case "${1:-}" in -h|--help|"") usage; [ -z "${1:-}" ] && exit 1 || exit 0 ;; esac
[ $# -lt 2 ] && { usage; exit 1; }
require_openscad   # exports OPENSCAD; the python tool reads it from the env.

MODEL="$1"; REF="$2"; shift 2
[ -f "$MODEL" ] || { echo "fit-camera: model not found: $MODEL" >&2; exit 2; }
[ -f "$REF" ]   || { echo "fit-camera: reference not found: $REF" >&2; exit 2; }

# translate the friendly positional + flags into the python tool's --flags.
ARGS=(--model "$MODEL" --ref "$REF")
while [ $# -gt 0 ]; do
    case "$1" in
        --out)        ARGS+=(--out "$2"); shift 2 ;;
        --center)     ARGS+=(--center "$2"); shift 2 ;;
        --opt-size)   ARGS+=(--opt-size "$2"); shift 2 ;;
        --final-size) ARGS+=(--final-size "$2"); shift 2 ;;
        --thresh)     ARGS+=(--thresh "$2"); shift 2 ;;
        --rand)       ARGS+=(--rand "$2"); shift 2 ;;
        --refine)     ARGS+=(--refine "$2"); shift 2 ;;
        --seed)       ARGS+=(--seed "$2"); shift 2 ;;
        --draw-axes)  ARGS+=(--draw-axes); shift ;;
        *) echo "fit-camera: unknown option '$1'" >&2; usage; exit 1 ;;
    esac
done

exec bash "$REPO_ROOT/lib/pyrun" "numpy,pillow" \
    "$REPO_ROOT/lib/fit_camera.py" "${ARGS[@]}"
