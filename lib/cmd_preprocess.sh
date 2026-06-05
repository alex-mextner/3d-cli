#!/usr/bin/env bash
# 3d preprocess — produce a subject mask + proportional depth from a reference photo.
# Full path: SAM2 / Depth-Anything-V2 if installable; always-available OpenCV fallback.
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

usage() {
cat <<EOF
3d preprocess <reference.jpg> [-o outdir] [options]
  Produce mask.png (subject silhouette) + depth.png (proportional depth).
  Tiers (auto): SAM2/rembg -> grabCut (mask); Depth-Anything-V2 -> pseudo-depth.
  Always writes both outputs (degrades gracefully if heavy models unavailable).

Options:
  -o, --out DIR         output dir (default: alongside the image)
  --force-fallback     skip model tiers, use OpenCV/numpy floor only
  --sam2-checkpoint P   enable SAM2 mask tier (path to a .pt checkpoint)
  --depth-model ID      HF model id (default: depth-anything/Depth-Anything-V2-Small-hf)

Example:
  3d preprocess ref.jpg -o work/
  3d preprocess ref.jpg --force-fallback
EOF
}
[ $# -lt 1 ] && { usage; exit 1; }
case "$1" in -h|--help) usage; exit 0 ;; esac

IMG="$1"; shift
[ -f "$IMG" ] || { echo "preprocess: image not found: $IMG" >&2; exit 2; }

# translate -o/--out to the script's --out-dir
ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        -o|--out) ARGS+=(--out-dir "$2"); shift 2 ;;
        *) ARGS+=("$1"); shift ;;
    esac
done

# Floor deps = opencv+numpy+pillow (always). Heavy tiers (torch/transformers/rembg/
# sam2) are import-guarded inside the script and skipped if absent. Try them via uv
# only when the user opts in with a heavier dep set; default keeps it light & fast.
exec bash "$REPO_ROOT/lib/pyrun" "opencv-python-headless,numpy,pillow" \
    "$REPO_ROOT/lib/preprocess_reference.py" "$IMG" ${ARGS[@]+"${ARGS[@]}"}
