#!/usr/bin/env bash
# 3d params — extract customizable parameters from a .scad file.
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

usage() {
cat <<EOF
3d params <file.scad> [--json]
  Extract Customizer-style parameters (name = value; // [min:max] desc).

Example:
  3d params model.scad
  3d params model.scad --json
EOF
}
[ $# -lt 1 ] && { usage; exit 1; }
case "$1" in -h|--help) usage; exit 0 ;; esac

INPUT="$1"; shift
[ -f "$INPUT" ] || { echo "params: file not found: $INPUT" >&2; exit 2; }
# pure-python, no third-party deps -> system python3 directly (no uv overhead).
python3 "$REPO_ROOT/lib/extract_params.py" "$INPUT" "$@"
