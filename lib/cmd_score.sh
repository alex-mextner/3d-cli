#!/usr/bin/env bash
# 3d score — silhouette AE + IoU metric (report §7.3). Machine-parseable output.
#
# Compares a RENDER (or a .scad it renders first) against a REFERENCE image, and
# prints:
#   AE=<int>          mismatched-pixel count       (lower = closer, 0 = perfect)
#   AE_NORM=<0..1>    AE / pixel_area              (lower = closer)
#   IoU=<0..1>        intersection / union         (higher = closer; primary)
#   CLOSENESS=<0..1>  == IoU
#   FRAME=<WxH>
#   OVERLAY=<path>    ref(red)+render(cyan) ghost (diagnostic)
#
# First arg may be:
#   a .scad   -> rendered at a locked camera, then scored vs the reference
#   a .png    -> treated as a render PNG (background keyed out) and scored vs reference
#   a mask    -> with --masks, two ready binary masks are compared directly
#
# Exit 0 on a scored comparison; exit 2 on usage/empty-mask error (a loop should
# treat non-zero as score = inf / reward 0).
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

usage() {
cat <<EOF
3d score <render.png|file.scad|mask.png> <reference|mask.png> [-o outdir] [options]
  Silhouette AE + IoU. Prints machine-parseable KEY=VALUE lines.

Modes:
  (default)   first arg is a render PNG or .scad; second is the reference image.
              A .scad is rendered at a locked camera first.
  --masks     both args are ready binary masks (white shape, black bg); compared directly.

Options:
  -o DIR                output dir for masks/overlay (default: /tmp/3dscore)
  --cam ex,..,cz        6-param vector camera for the .scad render (default locked side)
  --size WxH            image size for the .scad render (default 1200x900)
  --ortho              orthographic projection for the .scad render
  -D k=v                define passed to the .scad render (repeatable)

Examples:
  3d score model.scad ref.jpg
  3d score render.png ref.jpg -o work/
  3d score mask_a.png mask_b.png --masks
EOF
}
case "${1:-}" in -h|--help|"") usage; [ -z "${1:-}" ] && exit 1 || exit 0 ;; esac
[ $# -lt 2 ] && { usage; exit 1; }
require_magick

A="$1"; B="$2"; shift 2
OUT="/tmp/3dscore"; MASKS=0; CAM=""; SIZE="1200,900"; ORTHO=(); DEFS=()
while [ $# -gt 0 ]; do
    case "$1" in
        -o|--out) OUT="$2"; shift 2 ;;
        --masks) MASKS=1; shift ;;
        --cam) CAM="$2"; shift 2 ;;
        --size) SIZE="${2/x/,}"; shift 2 ;;
        --ortho) ORTHO=("--projection=ortho"); shift ;;
        -D) DEFS+=("-D" "$2"); shift 2 ;;
        *) echo "score: unknown option '$1'" >&2; usage; exit 1 ;;
    esac
done
[ -f "$A" ] || { echo "score: not found: $A" >&2; exit 2; }
[ -f "$B" ] || { echo "score: not found: $B" >&2; exit 2; }
mkdir -p "$OUT"

# locked masking constants (identical across runs)
BG="#ffffe5"; BG_FUZZ="10%"; REF_THRESH="78%"

# --- resolve A into a render PNG if it's a .scad ----------------------------
ext="${A##*.}"; ext="$(printf '%s' "$ext" | tr 'A-Z' 'a-z')"
if [ "$ext" = scad ] && [ "$MASKS" -eq 0 ]; then
    require_openscad
    [ -z "$CAM" ] && CAM="125,-330,52,125,28,44"   # locked side camera default
    n=$(awk -F, '{print NF}' <<< "$CAM")
    [ "$n" -eq 6 ] || { echo "score: --cam needs 6 values (got $n)" >&2; exit 2; }
    RPNG="$OUT/render.png"
    "$OPENSCAD" --render ${ORTHO[@]+"${ORTHO[@]}"} --camera="$CAM" --imgsize="$SIZE" \
        ${DEFS[@]+"${DEFS[@]}"} -o "$RPNG" "$A" >/dev/null 2>&1 \
        || { echo "score: render failed for $A" >&2; exit 1; }
    A="$RPNG"
fi

# --- build the two masks ----------------------------------------------------
if [ "$MASKS" -eq 1 ]; then
    MR="$A"; MF="$B"
    WA=$($MAGICK identify -format "%w" "$MR"); HA=$($MAGICK identify -format "%h" "$MR")
    WB=$($MAGICK identify -format "%w" "$MF"); HB=$($MAGICK identify -format "%h" "$MF")
    if [ "$WA" != "$WB" ] || [ "$HA" != "$HB" ]; then
        $MAGICK "$MF" -resize "${WA}x${HA}!" "$OUT/mask_ref_rs.png"; MF="$OUT/mask_ref_rs.png"
    fi
    W="$WA"; H="$HA"
else
    # A = render PNG -> key out background -> white shape.
    $MAGICK "$A" -fuzz "$BG_FUZZ" -fill black -opaque "$BG" -fill white +opaque black "$OUT/mask_render.png" \
        || { echo "score: render-mask build failed" >&2; exit 2; }
    W=$($MAGICK identify -format "%w" "$OUT/mask_render.png")
    H=$($MAGICK identify -format "%h" "$OUT/mask_render.png")
    # B = reference -> grayscale threshold, negate (dark shape -> white), resized to render frame.
    $MAGICK "$B" -resize "${W}x${H}!" -colorspace Gray -threshold "$REF_THRESH" -negate "$OUT/mask_ref.png" \
        || { echo "score: ref-mask build failed" >&2; exit 2; }
    MR="$OUT/mask_render.png"; MF="$OUT/mask_ref.png"
fi

AREA=$(( W * H ))
[ "$AREA" -gt 0 ] || { echo "score: zero-area frame" >&2; exit 2; }

# (a) AE
AE_RAW=$($COMPARE -metric AE "$MR" "$MF" null: 2>&1)
AE=$(printf '%s\n' "$AE_RAW" | awk '{print $1; exit}')
case "$AE" in ''|*[!0-9.eE+-]*) echo "score: compare AE failed: $AE_RAW" >&2; exit 2 ;; esac
AE_INT=$(printf "%.0f" "$AE")

# (b) IoU — binarize both, then multiply=AND, lighten=OR; ratio of means = inter/union.
INTER=$($MAGICK "$MR" -threshold 50% "$MF" -threshold 50% -compose multiply -composite -format "%[fx:mean]" info:)
UNION=$($MAGICK "$MR" -threshold 50% "$MF" -threshold 50% -compose lighten  -composite -format "%[fx:mean]" info:)

read -r IOU AE_NORM CLOSE <<EOF
$(awk -v i="$INTER" -v u="$UNION" -v ae="$AE_INT" -v area="$AREA" 'BEGIN{
  iou=(u>0)?i/u:0; aen=(area>0)?ae/area:1; printf "%.4f %.6f %.4f", iou, aen, iou }')
EOF
# empty render mask (nothing rendered) -> worst score, never reward a blank frame.
if awk -v u="$UNION" 'BEGIN{exit !(u<=0)}'; then IOU="0.0000"; CLOSE="0.0000"; fi

# (c) diagnostic ghost overlay
$MAGICK "$MF" -threshold 50% -fill red  -opaque white "$OUT/_ref_red.png"  2>/dev/null
$MAGICK "$MR" -threshold 50% -fill cyan -opaque white "$OUT/_ren_cyan.png" 2>/dev/null
OVERLAY="$OUT/overlay.png"
$MAGICK "$OUT/_ref_red.png" "$OUT/_ren_cyan.png" -compose Screen -composite "$OVERLAY" 2>/dev/null \
    || OVERLAY="(overlay-failed)"
rm -f "$OUT/_ref_red.png" "$OUT/_ren_cyan.png"

echo "AE=$AE_INT"
echo "AE_NORM=$AE_NORM"
echo "IoU=$IOU"
echo "CLOSENESS=$CLOSE"
echo "FRAME=${W}x${H}"
echo "OVERLAY=$OVERLAY"
