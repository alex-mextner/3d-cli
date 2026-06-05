#!/usr/bin/env bash
# 3d overlay — render-vs-reference diagnostic overlays (report §7.3 c/d + diff).
#
# Given a RENDER png and a REFERENCE image, produces three diagnostics that make the
# spatial error literal for a human / vision critic:
#   (a) overlay.png      — difference composite (where the two disagree)
#   (b) ghost.png        — 50% ghost blend (render over reference)
#   (c) edge_overlay.png — canny edge-on-edge: ref edges RED, render edges CYAN, matched GREY
# The reference is resized to the render's exact pixel box so composites line up.
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

usage() {
cat <<EOF
3d overlay <render.png> <reference.{png,jpg}> [-o outdir]
  Difference / 50% ghost / canny edge-overlay diagnostics into outdir.

Example:
  3d overlay render.png ref.jpg -o work/
EOF
}
case "${1:-}" in -h|--help|"") usage; [ -z "${1:-}" ] && exit 1 || exit 0 ;; esac
[ $# -lt 2 ] && { usage; exit 1; }
require_magick

RENDER="$1"; REF="$2"; shift 2
OUT_DIR="$(dirname "$RENDER")"
while [ $# -gt 0 ]; do
    case "$1" in
        -o|--out) OUT_DIR="$2"; shift 2 ;;
        *) echo "overlay: unknown option '$1'" >&2; usage; exit 1 ;;
    esac
done
for f in "$RENDER" "$REF"; do
    [ -f "$f" ] || { echo "overlay: input not found: $f" >&2; exit 2; }
done
mkdir -p "$OUT_DIR"

CANNY="0x1+10%+30%"          # radius x sigma + lower% + upper% hysteresis (report §7.3d)
GHOST_OPACITY="50"

OVERLAY="$OUT_DIR/overlay.png"
GHOST="$OUT_DIR/ghost.png"
EDGE="$OUT_DIR/edge_overlay.png"

GEOM="$($MAGICK identify -format '%wx%h' "$RENDER")"
echo "[overlay] render=$RENDER ref=$REF frame=$GEOM"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
REF_FIT="$TMP/ref_fit.png"; REN_RGB="$TMP/ren_rgb.png"
$MAGICK "$REF"    -alpha remove -alpha off -colorspace sRGB -resize "${GEOM}!" "$REF_FIT"
$MAGICK "$RENDER" -alpha remove -alpha off -colorspace sRGB -resize "${GEOM}!" "$REN_RGB"

# (a) difference composite
$MAGICK "$REF_FIT" "$REN_RGB" -compose Difference -composite -auto-level "$OVERLAY"
# (b) 50% ghost blend
$MAGICK "$REF_FIT" "$REN_RGB" -compose blend -define compose:args="$GHOST_OPACITY" -composite "$GHOST"
# (c) canny edge-on-edge
R_EDGE="$TMP/edges_ref.png"; C_EDGE="$TMP/edges_render.png"
R_RED="$TMP/r_red.png"; C_CYAN="$TMP/c_cyan.png"
$MAGICK "$REF_FIT" -colorspace Gray -canny "$CANNY" "$R_EDGE"
$MAGICK "$REN_RGB" -colorspace Gray -canny "$CANNY" "$C_EDGE"
$MAGICK "$R_EDGE" -colorspace sRGB -type TrueColor -fill red  -opaque white "$R_RED"
$MAGICK "$C_EDGE" -colorspace sRGB -type TrueColor -fill cyan -opaque white "$C_CYAN"
$MAGICK "$R_RED" "$C_CYAN" -colorspace sRGB -compose Screen -composite -type TrueColor "$EDGE"

AE="$($COMPARE -metric AE -fuzz 5% "$REF_FIT" "$REN_RGB" null: 2>&1 || true)"
echo "[overlay] wrote:"
echo "  (a) difference  : $OVERLAY"
echo "  (b) ghost 50%   : $GHOST"
echo "  (c) edge-on-edge: $EDGE"
echo "[overlay] AE(fuzz5%) mismatched-pixels = $AE  (diagnostic; 0 = identical)"
