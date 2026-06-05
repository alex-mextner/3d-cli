#!/usr/bin/env bash
# 3d render — CGAL (--render) PNG with a LOCKED 6-param VECTOR camera (report §7.1).
#
# The match-loop render: deterministic, manifold-solid (no throwntogether cut-face
# artifacts), optionally orthographic (a side-on reference is ~ortho; ortho makes the
# overlay exact). The camera is the 6-param VECTOR form ex,ey,ez,cx,cy,cz (eye->center),
# NEVER the 7-param gimbal (a trailing dist=0 renders an empty frame).
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

usage() {
cat <<EOF
3d render <file.scad> [options]
  CGAL (--render) PNG with a locked 6-param vector camera. Default: ISO.

Options:
  -o, --out PATH        output PNG (default: <file>.png next to input)
  --cam ex,ey,ez,cx,cy,cz   6-param VECTOR camera (eye -> center). Default: auto ISO via --viewall.
  --size WxH            image size (default 1200x900)
  --ortho              orthographic projection (recommended for reference overlay)
  --colorscheme NAME    OpenSCAD colorscheme (default 'Tomorrow Night')
  -D k=v                pass-through define (repeatable)

Examples:
  3d render model.scad -o out.png
  3d render model.scad --ortho --cam 130,-600,52,130,0,52 --size 1600x700
  3d render model.scad -D 'width=80' -D 'height=60'
EOF
}

[ $# -lt 1 ] && { usage; exit 1; }
case "$1" in -h|--help) usage; exit 0 ;; esac
require_openscad

INPUT="$1"; shift
OUT=""; CAM=""; SIZE="1200,900"; ORTHO=(); SCHEME="Tomorrow Night"; DEFS=()
while [ $# -gt 0 ]; do
    case "$1" in
        -o|--out) OUT="$2"; shift 2 ;;
        --cam) CAM="$2"; shift 2 ;;
        --size) SIZE="${2/x/,}"; shift 2 ;;
        --ortho) ORTHO=("--projection=ortho"); shift ;;
        --colorscheme) SCHEME="$2"; shift 2 ;;
        -D) DEFS+=("-D" "$2"); shift 2 ;;
        *) echo "render: unknown option '$1'" >&2; usage; exit 1 ;;
    esac
done
[ -f "$INPUT" ] || { echo "render: file not found: $INPUT" >&2; exit 2; }
[ -z "$OUT" ] && OUT="${INPUT%.scad}.png"
mkdir -p "$(dirname "$OUT")"

# Camera handling: explicit 6-param vector cam => fixed frame (no autocenter/viewall,
# so the render is pixel-registered for overlay). No cam => sensible ISO via viewall.
CAM_ARGS=()
if [ -n "$CAM" ]; then
    n=$(awk -F, '{print NF}' <<< "$CAM")
    if [ "$n" -ne 6 ]; then
        echo "render: --cam needs 6 values ex,ey,ez,cx,cy,cz (got $n). 7 = gimbal => empty frame." >&2
        exit 2
    fi
    CAM_ARGS=(--camera="$CAM")
else
    # auto ISO: 6-param vector eye on +X+Y+Z looking at origin, autofit the frame.
    CAM_ARGS=(--camera="1,1,1,0,0,0" --autocenter --viewall)
fi

echo "render: $INPUT -> $OUT  ${ORTHO[*]:-(perspective)}  size=$SIZE"
"$OPENSCAD" --render ${ORTHO[@]+"${ORTHO[@]}"} ${CAM_ARGS[@]+"${CAM_ARGS[@]}"} \
    --imgsize="$SIZE" --colorscheme="$SCHEME" \
    ${DEFS[@]+"${DEFS[@]}"} -o "$OUT" "$INPUT"
echo "render: wrote $OUT"
