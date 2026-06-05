#!/usr/bin/env bash
# 3d printability — FDM printability gate for a part (wall / min-feature / overhang
# / orientation), Bambu A1 + PLA/PETG rules from the fdm-printability skill.
#
# Exports the .scad to STL (grabbing OpenSCAD geometry warnings) then runs the mesh
# analyzer (wall thickness via inward ray-cast, overhang area, watertight). Per HARD
# rule PASS/FAIL; exit 0 only if every passed part clears its hard rules.
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

usage() {
cat <<EOF
3d printability <file.scad|.stl> [more parts...] [-D k=v ...]
  Wall thickness / min feature / overhang / orientation flags (FDM, PLA/PETG).
  Thresholds: wall>=1.2  floor>=0.8  feature>=1.0  overhang<=45deg.
  Exit 0 = all parts clear HARD rules, 1 = a HARD rule failed.

Example:
  3d printability part.scad
  3d printability a.scad b.scad
  3d printability part.stl
EOF
}
[ $# -lt 1 ] && { usage; exit 1; }
case "$1" in -h|--help) usage; exit 0 ;; esac
require_openscad

PARTS=(); DEFS=()
while [ $# -gt 0 ]; do
    case "$1" in
        -D) DEFS+=("-D" "$2"); shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) PARTS+=("$1"); shift ;;
    esac
done
[ ${#PARTS[@]} -eq 0 ] && { echo "printability: no parts given" >&2; exit 2; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/3d_print.XXXXXX")"
MESH_PY="$REPO_ROOT/lib/printability_mesh.py"

echo "================================================================"
echo " printability gate  (Bambu A1 + PLA/PETG, fdm-printability rules)"
echo " thresholds: wall>=1.2 floor>=0.8 feature>=1.0 overhang<=45deg"
echo "================================================================"

FAIL=0
for f in "${PARTS[@]}"; do
    [ -f "$f" ] || { echo "  ! not found: $f" >&2; FAIL=1; continue; }
    name="$(basename "$f")"; name="${name%.*}"
    ext="${f##*.}"; ext="$(printf '%s' "$ext" | tr 'A-Z' 'a-z')"
    echo ""; echo "---- $name ----"

    if [ "$ext" = scad ]; then
        stl="$WORK/$name.stl"
        exp=$("$OPENSCAD" --export-format binstl ${DEFS[@]+"${DEFS[@]}"} -o "$stl" "$f" 2>&1) || true
        if [ ! -s "$stl" ]; then
            echo "  [FAIL] (HARD) export   OpenSCAD produced no STL"
            printf '%s\n' "$exp" | head -8 | sed 's/^/        /'
            FAIL=1; continue
        fi
        if printf '%s\n' "$exp" | grep -qiE 'non-manifold|not.*manifold|self-intersect|degenerate'; then
            echo "  [WARN] (HARD) export   OpenSCAD geometry warning:"
            printf '%s\n' "$exp" | grep -iE 'non-manifold|not.*manifold|self-intersect|degenerate' | sed 's/^/        /'
        fi
    else
        stl="$f"
    fi

    bash "$REPO_ROOT/lib/pyrun" "trimesh,numpy,rtree,scipy" "$MESH_PY" "$stl" --name "$name" || FAIL=1
done

echo ""
echo "================================================================"
if [ "$FAIL" -eq 0 ]; then
    echo ">>> PRINTABILITY: PASS  (all parts cleared HARD rules)"
else
    echo ">>> PRINTABILITY: FAIL  (see per-part FAIL lines above)"
fi
echo "================================================================"
exit $FAIL
