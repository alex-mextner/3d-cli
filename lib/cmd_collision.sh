#!/usr/bin/env bash
# 3d collision — generic collision/penetration engine (static gate).
# Takes a project config JSON (parts, phases, intended contacts, thresholds, placement .scad).
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

usage() {
cat <<EOF
3d collision <config.json> [--frame] [--viz]
  Run the generic collision/penetration engine over a project config.

  config.json supplies: pair_scad (placement), parts, phases, intended contacts,
  eps_mm3 / touch_tol_mm / contact_max_mm3 thresholds. All paths in the config are
  resolved RELATIVE TO THE CONFIG FILE'S DIRECTORY.

Modes:
  (default)   static gate: every pair at every phase (collision_check.py)
  --frame     per-frame gate over the config's timeline (frame_check.py)
  --viz       render each phase with overlaps highlighted red (collision_viz.py)

Env: PHASES_SEL="0,1" (static) / FRAMES=N (frame) override the config.

Example:
  3d collision project/verify/collision.json
  3d collision project/verify/collision.json --frame
EOF
}
[ $# -lt 1 ] && { usage; exit 1; }
case "$1" in -h|--help) usage; exit 0 ;; esac

CFG="$1"; shift
[ -f "$CFG" ] || { echo "collision: config not found: $CFG" >&2; exit 2; }

MODE="static"
while [ $# -gt 0 ]; do
    case "$1" in
        --frame) MODE="frame"; shift ;;
        --viz)   MODE="viz"; shift ;;
        *) echo "collision: unknown option '$1'" >&2; usage; exit 1 ;;
    esac
done

case "$MODE" in
    static) SCRIPT=collision_check.py; DEPS="trimesh,manifold3d,rtree,numpy,scipy" ;;
    frame)  SCRIPT=frame_check.py;     DEPS="trimesh,manifold3d,rtree,numpy,scipy" ;;
    viz)    SCRIPT=collision_viz.py;   DEPS="trimesh,manifold3d,rtree,numpy,scipy,pyvista" ;;
esac

exec bash "$REPO_ROOT/lib/pyrun" "$DEPS" "$REPO_ROOT/lib/$SCRIPT" "$CFG"
