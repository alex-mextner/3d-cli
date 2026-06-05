#!/usr/bin/env bash
# 3d acceptance — THIN ALIAS for `3d check` (the unified master gate; back-compat).
#   3d acceptance <assembly.scad> [--part F]... [--collision cfg] [--ref img] [--cam ..] [--size WxH]
# `check` already runs ALL applicable gates with no selectors, which IS the acceptance gate.
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

case "${1:-}" in
    -h|--help|"")
cat <<EOF
3d acceptance <assembly.scad> [options]
  Alias for: 3d check <assembly.scad> ...  (runs ALL applicable gates = the master gate).

Options (forwarded to check):
  --part FILE           additional printable part to gate (repeatable)
  --collision CFG.json  collision/penetration gate (HARD)
  --ref IMAGE           silhouette reference (advisory)
  --cam ex,..,cz        6-param vector camera for the silhouette render
  --size WxH            silhouette render size
  -D k=v                pass-through define

Exit 0 = PASS, 1 = FAIL.
EOF
        [ -z "${1:-}" ] && exit 1 || exit 0 ;;
esac

exec bash "$REPO_ROOT/lib/cmd_check.sh" "$@"
