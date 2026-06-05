#!/usr/bin/env bash
# 3d mesh — watertight/manifold/self-intersection/volume report for an STL or .scad.
# Full path uses trimesh + open3d/manifold3d; degrades to openscad warning-grep.
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

usage() {
cat <<EOF
3d mesh <file.stl|.3mf|.scad> [-D k=v ...]
  Report watertight / manifold / self-intersection / volume.
  Engine tiers: trimesh+open3d (full) -> trimesh+manifold3d -> openscad warning grep.
  Exit 0 = PASS, 1 = FAIL (geometric defect), 2 = load/usage error.

Example:
  3d mesh part.stl
  3d mesh model.scad -D 'width=80'
EOF
}
[ $# -lt 1 ] && { usage; exit 1; }
case "$1" in -h|--help) usage; exit 0 ;; esac

# Try the full mesh stack; the script itself falls back to openscad grep if trimesh
# is unimportable. open3d has no wheel for some pythons — list it but the tool
# degrades to manifold3d if open3d is absent.
exec bash "$REPO_ROOT/lib/pyrun" "trimesh,manifold3d,numpy,scipy,rtree" \
    "$REPO_ROOT/lib/mesh_check.py" "$@"
