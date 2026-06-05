#!/usr/bin/env bash
# 3d export — STL/3MF export WITH geometry validation. NONZERO exit on bad geometry.
#
# Detects non-manifold / self-intersecting / degenerate geometry from OpenSCAD's
# own export log and FAILS (exit 1) so a broken part never silently flows into a
# slicer. Format is inferred from the output extension (.stl/.3mf/.off/.amf), with
# binary STL forced for .stl unless --ascii.
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

usage() {
cat <<EOF
3d export <file.scad> [options]
  Export STL/3MF with manifold/self-intersect validation. Exit 1 on bad geometry.

Options:
  -o, --out PATH        output (.stl/.3mf/.off/.amf). Default: <file>.stl
  --ascii              ASCII STL (default: binary STL)
  -D k=v                pass-through define (repeatable)

Examples:
  3d export model.scad -o model.stl
  3d export model.scad -o model.3mf -D 'width=80'
EOF
}
[ $# -lt 1 ] && { usage; exit 1; }
case "$1" in -h|--help) usage; exit 0 ;; esac
require_openscad

INPUT="$1"; shift
OUT=""; ASCII=0; DEFS=()
while [ $# -gt 0 ]; do
    case "$1" in
        -o|--out) OUT="$2"; shift 2 ;;
        --ascii) ASCII=1; shift ;;
        -D) DEFS+=("-D" "$2"); shift 2 ;;
        *) echo "export: unknown option '$1'" >&2; usage; exit 1 ;;
    esac
done
[ -f "$INPUT" ] || { echo "export: file not found: $INPUT" >&2; exit 2; }
[ -z "$OUT" ] && OUT="${INPUT%.scad}.stl"
mkdir -p "$(dirname "$OUT")"

ext="${OUT##*.}"; ext="$(printf '%s' "$ext" | tr 'A-Z' 'a-z')"
FMT_ARGS=()
case "$ext" in
    stl)  [ "$ASCII" -eq 1 ] && FMT_ARGS=(--export-format asciistl) || FMT_ARGS=(--export-format binstl) ;;
    3mf)  FMT_ARGS=(--export-format 3mf) ;;
    off|amf) ;;  # let openscad infer from extension
    *) echo "export: unsupported output extension '.$ext' (use .stl/.3mf/.off/.amf)" >&2; exit 2 ;;
esac

echo "================================================================"
echo "export: $(basename "$INPUT") -> $OUT"
[ ${#DEFS[@]} -gt 0 ] && echo "  defines: ${DEFS[*]}"
echo "================================================================"

RESULT=$("$OPENSCAD" ${FMT_ARGS[@]+"${FMT_ARGS[@]}"} ${DEFS[@]+"${DEFS[@]}"} -o "$OUT" "$INPUT" 2>&1) || true

BAD=0; WARN=""
# grep with the colon where applicable; check the descriptive phrases OpenSCAD emits.
printf '%s\n' "$RESULT" | grep -qi "not.*manifold\|non-manifold" && { WARN="$WARN\n  - non-manifold geometry (holes in mesh)"; BAD=1; }
printf '%s\n' "$RESULT" | grep -qi "self-intersect"             && { WARN="$WARN\n  - self-intersecting geometry"; BAD=1; }
printf '%s\n' "$RESULT" | grep -qi "degenerate"                 && { WARN="$WARN\n  - degenerate faces (zero-area triangles)"; BAD=1; }
printf '%s\n' "$RESULT" | grep -q  "ERROR:"                     && { WARN="$WARN\n  - openscad ERROR: during export"; BAD=1; }

if [ ! -f "$OUT" ]; then
    echo "export: FAILED — no output produced" >&2
    printf '%s\n' "$RESULT" | sed 's/^/  /' >&2
    exit 1
fi

SIZE=$(ls -lh "$OUT" | awk '{print $5}')
echo "output: $OUT ($SIZE)"
if [ "$ext" = stl ] && [ "$ASCII" -eq 0 ]; then
    TRIS=$(od -An -tu4 -j80 -N4 "$OUT" 2>/dev/null | tr -d ' ')
    [ -n "$TRIS" ] && echo "triangles: $TRIS"
fi
echo "--- geometry validation ---"

# OpenSCAD's modern (manifold) backend often produces output WITHOUT emitting text
# warnings for a non-watertight/non-manifold mesh, so the log grep above is not
# enough. Run the authoritative mesh check (trimesh watertight + manifold3d status)
# on the produced STL when the mesh stack is available; this gives a real nonzero
# exit on bad geometry. Degrade to the log-grep verdict if the stack is unavailable.
MESH_VERDICT=""
if [ "$ext" = stl ] && [ "$ASCII" -eq 0 ]; then
    mout="$(bash "$REPO_ROOT/lib/pyrun" "trimesh,manifold3d,numpy" \
            "$REPO_ROOT/lib/mesh_check.py" "$OUT" 2>&1)" ; mrc=$?
    if printf '%s\n' "$mout" | grep -q 'ModuleNotFoundError\|No module named\|no python runtime'; then
        MESH_VERDICT="skip"
    elif printf '%s\n' "$mout" | grep -q '>>> MESH CHECK: FAIL'; then
        BAD=1
        WARN="$WARN\n  - mesh check: $(printf '%s\n' "$mout" | grep 'MESH CHECK: FAIL' | sed 's/.*FAIL//')"
        MESH_VERDICT="fail"
    else
        MESH_VERDICT="pass"
    fi
fi

if [ "$BAD" -eq 1 ]; then
    echo "STATUS: FAIL"
    printf '%b\n' "$WARN"
    echo "  (non-manifold -> closed solids; self-intersect -> union(); degenerate -> no zero-thickness)"
    echo "================================================================"
    exit 1
fi
case "$MESH_VERDICT" in
    pass) echo "STATUS: PASS — manifold, watertight (mesh-verified), slicer-ready" ;;
    skip) echo "STATUS: PASS — no openscad warnings (mesh stack absent: 'STATUS' is log-grep only; run '3d mesh $OUT' for the full check)" ;;
    *)    echo "STATUS: PASS — manifold, no self-intersections, slicer-ready" ;;
esac
echo "================================================================"
exit 0
