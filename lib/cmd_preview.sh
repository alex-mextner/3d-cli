#!/usr/bin/env bash
# 3d preview — fast throwntogether preview (no CGAL). For human eyeballing.
#
# Uses the gimbal camera + --viewall (good for a quick look). Cut faces of
# difference() show orange in throwntogether mode — that's a preview artifact, not
# a geometry bug; use `3d render` or `3d section` (CGAL) to inspect cuts.
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

usage() {
cat <<EOF
3d preview <file.scad> [options]
  Fast throwntogether preview PNG (no CGAL render).

Options:
  -o, --out PATH        output PNG (default: <file>.png)
  --cam C               camera (7-param gimbal tx,ty,tz,rx,ry,rz,dist OR 6-param vector)
  --size WxH            image size (default 800x600)
  -D k=v                pass-through define (repeatable)

Examples:
  3d preview model.scad
  3d preview model.scad -o look.png --size 1024x768
EOF
}
[ $# -lt 1 ] && { usage; exit 1; }
case "$1" in -h|--help) usage; exit 0 ;; esac
require_openscad

INPUT="$1"; shift
OUT=""; CAM="0,0,0,55,0,25,0"; SIZE="800,600"; DEFS=()
while [ $# -gt 0 ]; do
    case "$1" in
        -o|--out) OUT="$2"; shift 2 ;;
        --cam) CAM="$2"; shift 2 ;;
        --size) SIZE="${2/x/,}"; shift 2 ;;
        -D) DEFS+=("-D" "$2"); shift 2 ;;
        *) echo "preview: unknown option '$1'" >&2; usage; exit 1 ;;
    esac
done
[ -f "$INPUT" ] || { echo "preview: file not found: $INPUT" >&2; exit 2; }
[ -z "$OUT" ] && OUT="${INPUT%.scad}.png"
mkdir -p "$(dirname "$OUT")"

echo "preview: $INPUT -> $OUT (throwntogether)"
"$OPENSCAD" --camera="$CAM" --imgsize="$SIZE" \
    --colorscheme="Tomorrow Night" --autocenter --viewall \
    ${DEFS[@]+"${DEFS[@]}"} -o "$OUT" "$INPUT"
echo "preview: wrote $OUT"
