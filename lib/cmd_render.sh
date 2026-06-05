#!/usr/bin/env bash
# 3d render — UNIFIED view/section render command (backed by lib/render.py, typed+async).
#
#   3d render <file.scad> [-o out] [--size WxH] [--ortho] [-D k=v]...    single view
#       --view <name>     front|back|left|right|top|bottom|iso|3-4|front-left|front-right|rear-left|rear-right
#       --cam ex,..,cz    manual 6-param vector camera (wins over --view)
#   3d render <file.scad> --multi [outdir] [--render] [--size WxH] [-D k=v]...
#   3d render <file.scad> --section [--plane YZ|XZ|XY] [--color] [--keep neg|pos] [--module 'm();'] [-o out]
#
# The named-view camera is computed from the model BOUNDING BOX (centroid + named
# direction + fit distance) when the trimesh mesh stack is available; otherwise it
# orbits along the view direction with --autocenter --viewall. Section cuts ARBITRARY
# geometry (STL-import + halfspace), or uses the per-part coloured `-D cut=true` contract
# with --color. NEVER a 7-param gimbal camera.
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

usage() {
cat <<EOF
3d render <file.scad> [mode + options]
  Default: single CGAL render, ISO view, locked 6-param vector camera.

Modes (mutually exclusive):
  (default)            single view render
  --multi [OUTDIR]     render front/back/left/right/top/iso into OUTDIR (default previews/), async
  --section            true cross-section (generic STL-cut, or --color assembly mode)

Single-view options:
  --view NAME          front|back|left|right|top|bottom|iso|3-4|front-left|front-right|rear-left|rear-right (default iso)
  --cam ex,ey,ez,cx,cy,cz   manual 6-param VECTOR camera (wins over --view)
  --ortho              orthographic projection
  --colorscheme NAME   OpenSCAD colorscheme (default 'Tomorrow Night')

Section options:
  --plane YZ|XZ|XY     cut plane (default YZ)
  --keep neg|pos       which half to keep (default neg)
  --color              coloured per-part ASSEMBLY mode (assembly must honour -D cut=true)
  --module 'name();'   module to cut (only needed in the no-mesh-stack fallback)

Common:
  -o, --out PATH       output PNG (single/section). Default: <file>.png
  --size WxH           image size (single/section 1200x900, multi 800x600)
  -D k=v               pass-through define (repeatable)

Examples:
  3d render model.scad --view left -o left.png
  3d render model.scad --view 3-4 --ortho
  3d render model.scad --multi previews/ --render
  3d render model.scad --section --plane YZ -o sec.png
  3d render assembly.scad --section --color --plane YZ -o sec.png
EOF
}

[ $# -lt 1 ] && { usage; exit 1; }
case "$1" in -h|--help) usage; exit 0 ;; esac
require_openscad

INPUT="$1"; shift
MODE="single"
# pass-through arg arrays per mode
declare -a ARGS=()
OUT=""; SIZE=""; MULTI_OUTDIR=""
while [ $# -gt 0 ]; do
    case "$1" in
        --multi)
            MODE="multi"
            # optional positional outdir (next token if it's not a flag)
            if [ $# -ge 2 ] && [ "${2#-}" = "$2" ]; then MULTI_OUTDIR="$2"; shift 2; else shift; fi ;;
        --section)   MODE="section"; shift ;;
        --view)      ARGS+=(--view "$2"); shift 2 ;;
        --cam)       ARGS+=(--cam "$2"); shift 2 ;;
        --ortho)     ARGS+=(--ortho); shift ;;
        --colorscheme) ARGS+=(--colorscheme "$2"); shift 2 ;;
        --plane)     ARGS+=(--plane "$2"); shift 2 ;;
        --keep)      ARGS+=(--keep "$2"); shift 2 ;;
        --color)     ARGS+=(--color); shift ;;
        --module)    ARGS+=(--module "$2"); shift 2 ;;
        --render)    ARGS+=(--render); shift ;;
        -o|--out)    OUT="$2"; shift 2 ;;
        --size)      SIZE="$2"; shift 2 ;;
        -D)          ARGS+=(-D "$2"); shift 2 ;;
        *) echo "render: unknown option '$1'" >&2; usage; exit 1 ;;
    esac
done
[ -f "$INPUT" ] || { echo "render: file not found: $INPUT" >&2; exit 2; }

# Per-mode python deps:
#  - single / multi: the mesh stack is a pure ENHANCEMENT (bbox-exact cameras). render.py
#    catches ImportError and degrades to --autocenter --viewall, so pass NO deps and never
#    block an offline `3d render` on a trimesh download.
#  - section (generic): the STL-import cut genuinely needs trimesh to read the bbox. Request
#    it; if the runtime/network can't provide it, render.py prints the --module fallback hint.
case "$MODE" in
    single)
        [ -n "$OUT" ]  && ARGS+=(-o "$OUT")
        [ -n "$SIZE" ] && ARGS+=(--size "$SIZE")
        exec bash "$REPO_ROOT/lib/pyrun" "" "$REPO_ROOT/lib/render.py" single "$INPUT" "${ARGS[@]+"${ARGS[@]}"}" ;;
    multi)
        set -- multi "$INPUT"
        [ -n "$MULTI_OUTDIR" ] && set -- "$@" "$MULTI_OUTDIR"
        [ -n "$SIZE" ] && ARGS+=(--size "$SIZE")
        exec bash "$REPO_ROOT/lib/pyrun" "" "$REPO_ROOT/lib/render.py" "$@" "${ARGS[@]+"${ARGS[@]}"}" ;;
    section)
        [ -n "$OUT" ]  && ARGS+=(-o "$OUT")
        [ -n "$SIZE" ] && ARGS+=(--size "$SIZE")
        exec bash "$REPO_ROOT/lib/pyrun" "trimesh,numpy" "$REPO_ROOT/lib/render.py" section "$INPUT" "${ARGS[@]+"${ARGS[@]}"}" ;;
esac
