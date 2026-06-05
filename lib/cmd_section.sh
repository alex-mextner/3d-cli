#!/usr/bin/env bash
# 3d section — TRUE cross-section of an OpenSCAD model (6-param VECTOR camera + --render).
#
# Why this exists: the 7-param --camera in old docs is gimbal mode (translate,rotate,DIST)
# — a trailing 0 means dist=0 => empty frame. Cross-sections must use the 6-param VECTOR
# camera (eye + center) AND --render (CGAL/manifold) so the cut face is a real solid face,
# not a throwntogether orange artifact. The temp section file is written next to the INPUT
# so `use`/`include` resolve relative paths correctly (OpenSCAD resolves them against the
# file's own directory, not the cwd).
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

usage() {
cat <<EOF
3d section <file.scad> -o out.png [options]
  Colored cross-section via a 6-param vector camera + --render.

Options:
  -o, --out PATH        output PNG (REQUIRED)
  --module 'name();'    geometry call to section (REQUIRED for part files; the module to cut)
  --plane YZ|XZ|XY      cut plane (default YZ: removes +X half)
  --keep neg|pos        which half to keep (default neg)
  --size WxH            image size (default 1200x900)
  -D k=v                pass-through define (repeatable)

Example:
  3d section part.scad -o sec.png --module 'my_part();' --plane YZ
EOF
}
[ $# -lt 1 ] && { usage; exit 1; }
case "$1" in -h|--help) usage; exit 0 ;; esac
require_openscad

INPUT="$1"; shift
OUT=""; MODULE=""; PLANE="YZ"; KEEP="neg"; SIZE="1200,900"; DEFS=()
while [ $# -gt 0 ]; do
    case "$1" in
        -o|--out) OUT="$2"; shift 2 ;;
        --module) MODULE="$2"; shift 2 ;;
        --plane) PLANE="$2"; shift 2 ;;
        --keep) KEEP="$2"; shift 2 ;;
        --size) SIZE="${2/x/,}"; shift 2 ;;
        -D) DEFS+=("-D" "$2"); shift 2 ;;
        *) echo "section: unknown option '$1'" >&2; usage; exit 1 ;;
    esac
done
[ -f "$INPUT" ] || { echo "section: file not found: $INPUT" >&2; exit 2; }
[ -n "$OUT" ] || { echo "section: -o out.png is required" >&2; exit 2; }
[ -n "$MODULE" ] || { echo "section: --module 'name();' is required (the geometry call to cut)" >&2; exit 2; }

INPUT_DIR="$(cd "$(dirname "$INPUT")" && pwd)"
INPUT_BASE="$(basename "$INPUT")"
mkdir -p "$(dirname "$OUT")"

case "$PLANE" in
    YZ)  if [ "$KEEP" = neg ]; then CUT="translate([0,-300,-300]) cube([600,600,600]);"; EYE="240,70,120";
         else CUT="translate([-600,-300,-300]) cube([600,600,600]);"; EYE="-240,70,120"; fi ;;
    XZ)  if [ "$KEEP" = neg ]; then CUT="translate([-300,0,-300]) cube([600,600,600]);"; EYE="70,240,120";
         else CUT="translate([-300,-600,-300]) cube([600,600,600]);"; EYE="70,-240,120"; fi ;;
    XY)  if [ "$KEEP" = neg ]; then CUT="translate([-300,-300,0]) cube([600,600,600]);"; EYE="120,70,260";
         else CUT="translate([-300,-300,-600]) cube([600,600,600]);"; EYE="120,70,-260"; fi ;;
    *) echo "section: unknown plane '$PLANE' (use YZ|XZ|XY)" >&2; exit 2 ;;
esac

TMP="$INPUT_DIR/.section_tmp_$$.scad"
cat > "$TMP" <<EOF
// AUTO-GENERATED cross-section (3d section) — safe to delete
use <$INPUT_BASE>
difference() {
    $MODULE
    $CUT
}
EOF
trap 'rm -f "$TMP"' EXIT

echo "section: $INPUT plane=$PLANE keep=$KEEP -> $OUT"
"$OPENSCAD" --render --camera="$EYE,0,0,0" --imgsize="$SIZE" \
    --colorscheme="Tomorrow Night" --autocenter --viewall \
    ${DEFS[@]+"${DEFS[@]}"} -o "$OUT" "$TMP"
echo "section: wrote $OUT"
