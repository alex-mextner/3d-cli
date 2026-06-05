#!/usr/bin/env bash
# 3d multi — THIN ALIAS for `3d render --multi` (back-compat).
#   3d multi <file.scad> [outdir] [--render] [--size WxH] [-D k=v]...
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

case "${1:-}" in
    -h|--help|"")
cat <<EOF
3d multi <file.scad> [outdir] [--render] [--size WxH] [-D k=v]...
  Alias for: 3d render <file.scad> --multi [outdir] ...
  Renders front/back/left/right/top/iso (default outdir: previews/).
EOF
        [ -z "${1:-}" ] && exit 1 || exit 0 ;;
esac

INPUT="$1"; shift
# optional positional outdir (next token if it's not a flag)
OUTDIR=""
if [ $# -ge 1 ] && [ "${1#-}" = "$1" ]; then OUTDIR="$1"; shift; fi
exec bash "$REPO_ROOT/lib/cmd_render.sh" "$INPUT" --multi ${OUTDIR:+"$OUTDIR"} "$@"
