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
  True cross-section via a 6-param vector camera + --render (the cut face is a real
  solid face, not a throwntogether artifact).

Two modes:
  (default)  PART mode: cut a single module. The cut face renders mono (tan).
             Requires --module 'name();'.
  --color    ASSEMBLY mode: TRUE per-part COLOURED section. The assembly must honour
             -D cut=true and wrap each part as  color(c) section_cut() part();  (color
             OUTSIDE difference). This script auto-passes -D cut=true. --module ignored.

Options:
  -o, --out PATH        output PNG (REQUIRED)
  --module 'name();'    geometry call to section (REQUIRED in part mode)
  --color              assembly coloured-section mode (per-part colour preserved)
  --plane YZ|XZ|XY      cut plane (default YZ: removes +X half)
  --keep neg|pos        which half to keep (default neg)
  --center x,y,z        camera look-at (colour mode; default 0,0,0)
  --dist N              camera distance (colour mode; default 250)
  --size WxH            image size (default 1200x900)
  -D k=v                pass-through define (repeatable)

Examples:
  3d section part.scad -o sec.png --module 'my_part();' --plane YZ
  3d section assembly.scad -o sec.png --color --plane YZ --dist 300
EOF
}
[ $# -lt 1 ] && { usage; exit 1; }
case "$1" in -h|--help) usage; exit 0 ;; esac
require_openscad

INPUT="$1"; shift
OUT=""; MODULE=""; PLANE="YZ"; KEEP="neg"; SIZE="1200,900"; COLOR=0
CENTER="0,0,0"; DIST="250"; DEFS=()
while [ $# -gt 0 ]; do
    case "$1" in
        -o|--out) OUT="$2"; shift 2 ;;
        --module) MODULE="$2"; shift 2 ;;
        --color) COLOR=1; shift ;;
        --plane) PLANE="$2"; shift 2 ;;
        --keep) KEEP="$2"; shift 2 ;;
        --center) CENTER="$2"; shift 2 ;;
        --dist) DIST="$2"; shift 2 ;;
        --size) SIZE="${2/x/,}"; shift 2 ;;
        -D) DEFS+=("-D" "$2"); shift 2 ;;
        *) echo "section: unknown option '$1'" >&2; usage; exit 1 ;;
    esac
done
[ -f "$INPUT" ] || { echo "section: file not found: $INPUT" >&2; exit 2; }
[ -n "$OUT" ] || { echo "section: -o out.png is required" >&2; exit 2; }
case "$PLANE" in YZ|XZ|XY) ;; *) echo "section: unknown plane '$PLANE' (use YZ|XZ|XY)" >&2; exit 2 ;; esac
case "$KEEP"  in neg|pos)  ;; *) echo "section: unknown keep '$KEEP' (use neg|pos)" >&2; exit 2 ;; esac
mkdir -p "$(dirname "$OUT")"

# -------- COLOURED ASSEMBLY section (port of section-color.sh) ----------------
# The assembly must honour -D cut=true and colour each part OUTSIDE its difference.
# Eye sits on the REMOVED side, angled for depth; 6-param vector camera (eye+center).
if [ "$COLOR" -eq 1 ]; then
    IFS=',' read -r CX CY CZ <<< "$CENTER"
    CX="${CX:-0}"; CY="${CY:-0}"; CZ="${CZ:-0}"
    sign=1; [ "$KEEP" = pos ] && sign=-1
    read -r EX EY EZ <<EOF
$(awk -v cx="$CX" -v cy="$CY" -v cz="$CZ" -v d="$DIST" -v s="$sign" -v plane="$PLANE" 'BEGIN{
    main=s*d; o1=0.29*d; o2=0.50*d;
    if(plane=="YZ"){ex=cx+main;ey=cy+o1;ez=cz+o2}
    else if(plane=="XZ"){ex=cx+o1;ey=cy+main;ez=cz+o2}
    else {ex=cx+o2;ey=cy+o1;ez=cz+main}
    printf "%.4f %.4f %.4f", ex, ey, ez }')
EOF
    CAM="$EX,$EY,$EZ,$CX,$CY,$CZ"
    echo "section: COLOURED assembly  plane=$PLANE keep=$KEEP center=$CENTER dist=$DIST"
    echo "  camera(eye+center): $CAM"
    "$OPENSCAD" --render --camera="$CAM" --imgsize="$SIZE" --colorscheme="Tomorrow Night" \
        -D cut=true -D "section_plane=\"$PLANE\"" -D "section_keep=\"$KEEP\"" \
        ${DEFS[@]+"${DEFS[@]}"} -o "$OUT" "$INPUT"
    if [ -f "$OUT" ]; then
        BYTES=$(wc -c < "$OUT" | tr -d ' ')
        echo "section: wrote $OUT ($BYTES bytes)"
        [ "$BYTES" -lt 20000 ] && echo "  WARNING: <20KB — frame may be empty (check --center/--dist, or the assembly's cut contract)."
    else
        echo "section: no output produced" >&2; exit 1
    fi
    exit 0
fi

# -------- PART section (mono cut face) ---------------------------------------
[ -n "$MODULE" ] || { echo "section: --module 'name();' is required in part mode (or use --color for assemblies)" >&2; exit 2; }

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
