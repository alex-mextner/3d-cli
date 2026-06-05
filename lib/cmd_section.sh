#!/usr/bin/env bash
# 3d section — THIN ALIAS for `3d render --section` (back-compat).
#   3d section <file.scad> -o out.png [--plane YZ|XZ|XY] [--color] [--keep neg|pos] [--module 'm();'] [-D k=v]...
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

case "${1:-}" in
    -h|--help|"")
cat <<EOF
3d section <file.scad> -o out.png [options]
  Alias for: 3d render <file.scad> --section ...
  True cross-section. Generic STL-cut by default (any geometry); --color does the
  per-part coloured ASSEMBLY mode (assembly must honour -D cut=true).

Options:
  -o, --out PATH        output PNG (required)
  --plane YZ|XZ|XY      cut plane (default YZ)
  --keep neg|pos        which half to keep (default neg)
  --color               coloured per-part assembly mode
  --module 'name();'    module to cut (no-mesh-stack fallback only)
  --size WxH            image size (default 1200x900)
  -D k=v                pass-through define (repeatable)
EOF
        [ -z "${1:-}" ] && exit 1 || exit 0 ;;
esac

INPUT="$1"; shift
exec bash "$REPO_ROOT/lib/cmd_render.sh" "$INPUT" --section "$@"
