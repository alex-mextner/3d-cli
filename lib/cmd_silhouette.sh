#!/usr/bin/env bash
# 3d silhouette — camera-locked render -> binary silhouette mask (white shape, black bg).
# The target/render silhouette for the IoU/AE match metric (report §7.1).
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

usage() {
cat <<EOF
3d silhouette <file.scad> [options]
  Render at a locked camera, then threshold to a binary silhouette mask.

Options:
  -o, --out PATH        output mask PNG (default: <file>_mask.png)
  --cam ex,ey,ez,cx,cy,cz   6-param VECTOR camera (default: auto ISO via viewall)
  --size WxH            image size (default 1200x900)
  --ortho              orthographic projection (recommended for reference overlay)
  -D k=v                pass-through define (repeatable)

Example:
  3d silhouette model.scad -o mask.png --ortho --cam 130,-600,52,130,0,52 --size 1600x700
EOF
}
[ $# -lt 1 ] && { usage; exit 1; }
case "$1" in -h|--help) usage; exit 0 ;; esac
require_openscad
require_magick

INPUT="$1"; shift
OUT=""; CAM=""; SIZE="1200,900"; ORTHO=(); DEFS=()
while [ $# -gt 0 ]; do
    case "$1" in
        -o|--out) OUT="$2"; shift 2 ;;
        --cam) CAM="$2"; shift 2 ;;
        --size) SIZE="${2/x/,}"; shift 2 ;;
        --ortho) ORTHO=("--projection=ortho"); shift ;;
        -D) DEFS+=("-D" "$2"); shift 2 ;;
        *) echo "silhouette: unknown option '$1'" >&2; usage; exit 1 ;;
    esac
done
[ -f "$INPUT" ] || { echo "silhouette: file not found: $INPUT" >&2; exit 2; }
[ -z "$OUT" ] && OUT="${INPUT%.scad}_mask.png"
mkdir -p "$(dirname "$OUT")"

CAM_ARGS=()
if [ -n "$CAM" ]; then
    n=$(awk -F, '{print NF}' <<< "$CAM")
    [ "$n" -eq 6 ] || { echo "silhouette: --cam needs 6 values (got $n)" >&2; exit 2; }
    CAM_ARGS=(--camera="$CAM")
else
    CAM_ARGS=(--camera="1,1,1,0,0,0" --autocenter --viewall)
fi

BG="#ffffe5"   # OpenSCAD default render background (srgb 255,255,229)
RENDER="$(mktemp "${TMPDIR:-/tmp}/3d_sil.XXXXXX.png")"
trap 'rm -f "$RENDER"' EXIT

"$OPENSCAD" --render ${ORTHO[@]+"${ORTHO[@]}"} ${CAM_ARGS[@]+"${CAM_ARGS[@]}"} --imgsize="$SIZE" \
    ${DEFS[@]+"${DEFS[@]}"} -o "$RENDER" "$INPUT" >/dev/null 2>&1 \
    || { echo "silhouette: render failed" >&2; exit 1; }

# key out the known background -> black, everything else -> white.
$MAGICK "$RENDER" -fuzz 10% -fill black -opaque "$BG" -fill white +opaque black "$OUT" \
    || { echo "silhouette: mask build failed" >&2; exit 1; }

echo "silhouette: $INPUT -> $OUT  ($(${MAGICK} identify -format '%wx%h' "$OUT"))"
