#!/usr/bin/env bash
# 3d check — manifold gate (--render + grep ERROR/WARNING/Assertion) + a quick
# printability pass. PASS/FAIL. Cheap first-line gate used standalone and by `3d match`.
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

usage() {
cat <<EOF
3d check <file.scad> [-D k=v ...]
  Manifold render gate + quick printability. Prints '>>> CHECK: PASS/FAIL'.
  - MANIFOLD: openscad --render to a temp STL, grep WARNING:/ERROR:/Assertion.
  - PRINTABILITY (quick): mesh wall/overhang/watertight if the mesh stack is available;
    SKIP (advisory) if not. A failed HARD printability rule fails the check.
  Exit 0 = PASS, 1 = FAIL.
EOF
}
[ $# -lt 1 ] && { usage; exit 1; }
case "$1" in -h|--help) usage; exit 0 ;; esac
require_openscad

INPUT="$1"; shift
DEFS=()
while [ $# -gt 0 ]; do
    case "$1" in -D) DEFS+=("-D" "$2"); shift 2 ;; *) shift ;; esac
done
[ -f "$INPUT" ] || { echo "check: file not found: $INPUT" >&2; exit 2; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/3d_check.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
FAIL=0

# ---- MANIFOLD (HARD) -------------------------------------------------------
echo "[MANIFOLD] openscad --render, grep WARNING:/ERROR:/Assertion"
LOG="$WORK/man.log"
if "$OPENSCAD" --render --export-format binstl ${DEFS[@]+"${DEFS[@]}"} -o "$WORK/out.stl" "$INPUT" >"$LOG" 2>&1; then :; else
    echo "RENDER-ERROR rc=$? on $INPUT" >>"$LOG"
fi
# grep ':' so we never match the token "NoError".
# NOTE: OpenSCAD's modern (manifold) backend often stays SILENT on a non-manifold
# mesh (no WARNING/ERROR text, exit 0) — so the grep alone is not a real gate. Back
# it with the trimesh watertight/manifold check on the produced STL when available.
if grep -Eq 'WARNING:|ERROR:|Assertion' "$LOG"; then
    echo "  FAIL — geometry warnings/errors:"
    grep -E 'WARNING:|ERROR:|Assertion|RENDER-ERROR' "$LOG" | head -5 | sed 's/^/    /'
    FAIL=1
elif [ ! -s "$WORK/out.stl" ]; then
    echo "  FAIL — no mesh produced"
    FAIL=1
else
    mout="$(bash "$REPO_ROOT/lib/pyrun" "trimesh,manifold3d,numpy" "$REPO_ROOT/lib/mesh_check.py" "$WORK/out.stl" 2>&1)"
    if printf '%s\n' "$mout" | grep -q 'ModuleNotFoundError\|No module named\|no python runtime'; then
        echo "  PASS — no openscad warnings (mesh stack absent; grep-only — install trimesh for the real check)"
    elif printf '%s\n' "$mout" | grep -q '>>> MESH CHECK: FAIL'; then
        echo "  FAIL — mesh-verified non-manifold:$(printf '%s\n' "$mout" | grep 'MESH CHECK: FAIL' | sed 's/.*FAIL//')"
        FAIL=1
    else
        echo "  PASS — clean manifold (mesh-verified)"
    fi
fi

# ---- PRINTABILITY (quick; HARD if it can run) -----------------------------
echo "[PRINTABILITY] quick wall/overhang/watertight"
MESH_PY="$REPO_ROOT/lib/printability_mesh.py"
if [ -s "$WORK/out.stl" ]; then
    pout="$(bash "$REPO_ROOT/lib/pyrun" "trimesh,numpy,rtree,scipy" "$MESH_PY" "$WORK/out.stl" --name "$(basename "$INPUT" .scad)" 2>&1)"; prc=$?
    if printf '%s\n' "$pout" | grep -q 'ModuleNotFoundError\|No module named'; then
        echo "  SKIP — mesh stack unavailable (install trimesh for the quick pass)"
    else
        printf '%s\n' "$pout" | sed 's/^/  /'
        [ $prc -ne 0 ] && FAIL=1
    fi
else
    echo "  SKIP — no mesh to analyze"
fi

echo "------------------------------------------------"
if [ "$FAIL" -eq 0 ]; then
    echo ">>> CHECK: PASS"
    exit 0
else
    echo ">>> CHECK: FAIL"
    exit 1
fi
