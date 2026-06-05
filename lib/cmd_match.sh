#!/usr/bin/env bash
# 3d match — forced-monotonic acceptance loop (render->score->critic->apply->accept/revert).
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

usage() {
cat <<EOF
3d match <assembly.scad> <reference> [options]
  Forced-monotonic silhouette-match loop (report §3.2/§7.4). The LLM critic proposes
  one numeric param delta; an IoU/AE metric + manifold gate dispose. Keep iff the score
  strictly improves AND manifold passes; else revert. Every step logged to a changelog.

Options:
  --rounds N            max rounds (default 8)
  --dry-run            skip the codex critic; synthesise deterministic edits (smoke test)
  --constants FILE      file holding the tunable constants (default: the assembly)
  --params a,b,c        restrict which constants the critic may tune
  --metric iou|ae       primary metric (default iou)
  --no-improve N        stop after N consecutive non-improving rounds (default 4)
  --margin F            strict-improvement margin (default 1e-4)
  --cam ex,..,cz        6-param vector camera for renders
  --size WxH            render size (default 1200x900)
  --ortho              orthographic renders
  --work DIR            work dir (default: <assembly_dir>/match_work)

Examples:
  3d match model.scad ref.jpg --rounds 2 --dry-run
  3d match model.scad ref.jpg --rounds 8 --ortho --cam 130,-600,52,130,0,52
EOF
}
case "${1:-}" in -h|--help|"") usage; [ -z "${1:-}" ] && exit 1 || exit 0 ;; esac
[ $# -lt 2 ] && { usage; exit 1; }

# pure-python (subprocess to `3d`); no third-party deps -> system python3.
exec python3 "$REPO_ROOT/lib/match_loop.py" "$@"
