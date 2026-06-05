#!/usr/bin/env bash
# 3d multi — 6-angle previews: iso/front/back/left/right/top.
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

usage() {
cat <<EOF
3d multi <file.scad> <outdir> [options]
  Render 6 angles (iso, front, back, left, right, top) into outdir.

Options:
  --render             CGAL render (manifold solid; no throwntogether artifacts)
  --size WxH           per-image size (default 800x600)
  -D k=v               pass-through define (repeatable)

Example:
  3d multi model.scad previews/
  3d multi model.scad previews/ --render
EOF
}
case "${1:-}" in -h|--help|"") usage; [ -z "${1:-}" ] && exit 1 || exit 0 ;; esac
[ $# -lt 2 ] && { usage; exit 1; }
require_openscad

INPUT="$1"; OUTDIR="$2"; shift 2
RENDER=(); SIZE="800,600"; DEFS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --render) RENDER=("--render"); shift ;;
        --size) SIZE="${2/x/,}"; shift 2 ;;
        -D) DEFS+=("-D" "$2"); shift 2 ;;
        *) echo "multi: unknown option '$1'" >&2; usage; exit 1 ;;
    esac
done
[ -f "$INPUT" ] || { echo "multi: file not found: $INPUT" >&2; exit 2; }
mkdir -p "$OUTDIR"
BASE="$(basename "$INPUT" .scad)"

# angle:gimbal-camera pairs (tx,ty,tz,rx,ry,rz,dist; dist=0 + viewall autofits)
ANGLES="iso:0,0,0,55,0,25,0
front:0,0,0,90,0,0,0
back:0,0,0,90,0,180,0
left:0,0,0,90,0,90,0
right:0,0,0,90,0,-90,0
top:0,0,0,0,0,0,0"

echo "multi: $INPUT -> $OUTDIR ${RENDER[*]:-}"
while IFS=: read -r angle cam; do
    out="$OUTDIR/${BASE}_${angle}.png"
    echo "  [$angle]"
    "$OPENSCAD" ${RENDER[@]+"${RENDER[@]}"} --camera="$cam" --imgsize="$SIZE" \
        --colorscheme="Tomorrow Night" --autocenter --viewall \
        ${DEFS[@]+"${DEFS[@]}"} -o "$out" "$INPUT" 2>/dev/null
done <<< "$ANGLES"
echo "multi: wrote 6 views to $OUTDIR"
ls "$OUTDIR/${BASE}_"*.png 2>/dev/null | sed 's/^/  /'
