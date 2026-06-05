#!/usr/bin/env bash
# 3d check — UNIFIED verification command = the acceptance MASTER gate.
#
# With NO selection flags it runs ALL applicable gates (the acceptance gate):
#     MANIFOLD  CONSISTENCY  PRINTABILITY  (+ COLLISION if --collision, + SILHOUETTE if --ref)
# Selection flags run only the named subset; --skip excludes a gate. Prints a per-gate
# breakdown + a single overall verdict and exits 0 (PASS) / 1 (FAIL).
#
#   3d check <file.scad> [parts...] [selectors] [--skip GATE]... [--collision cfg] [--ref img]
#
# Gates / selectors:
#   --manifold | --mesh   manifold/watertight (openscad --render + mesh watertight check)
#   --consistency         assert() checks (grep ERROR:/Assertion)
#   --printability        FDM wall/overhang/watertight (Bambu A1 + PETG)
#   --collision CFG.json  generic collision/penetration engine (HARD; needs a config)
#   --silhouette --ref I  image-space IoU/AE vs a reference (ADVISORY)
#   --skip GATE           exclude a gate (manifold|consistency|printability|collision|silhouette)
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

usage() {
cat <<EOF
3d check <file.scad> [more parts...] [options]
  Unified verification = acceptance master gate. No selectors => run ALL applicable gates.

Core-gate selectors (any combination runs ONLY those core gates):
  --manifold | --mesh     manifold / watertight gate
  --consistency           assert() consistency gate
  --printability          FDM printability gate (walls/overhangs)
  --skip GATE             exclude a gate (repeatable) — the way to get a subset

Data-driven gates (run whenever their data is supplied, never narrow the core set, so a
config can NEVER silently skip a HARD gate — to get a subset, use --skip):
  --collision CFG.json    collision/penetration gate (HARD; runs when CFG given)
  --silhouette / --ref I  silhouette IoU/AE vs reference (ADVISORY; runs when --ref given)

  To run ONLY collision:  3d check asm.scad --collision cfg --skip manifold --skip consistency --skip printability

Other:
  --part FILE             additional part to gate (or just pass extra positional files)
  --collision CFG.json    collision config (also selects the collision gate)
  --ref IMAGE             reference image for the silhouette gate
  --cam ex,..,cz          6-param vector camera for the silhouette render
  --size WxH              silhouette render size (default 1100x480)
  -D k=v                  pass-through define (repeatable)

Exit 0 = PASS (all HARD gates pass), 1 = FAIL.

Examples:
  3d check examples/cube.scad                 # all applicable gates
  3d check examples/cube.scad --mesh          # only the manifold gate
  3d check asm.scad --skip printability
  3d check asm.scad --collision verify/collision.json --ref ref.jpg
EOF
}
[ $# -lt 1 ] && { usage; exit 1; }
case "$1" in -h|--help) usage; exit 0 ;; esac
require_openscad

# ---- parse ------------------------------------------------------------------
FILES=(); DEFS=(); SKIP=(); SEL=()
COLL=""; REF=""; CAM="125,-330,52,125,28,44"; SILSIZE="1100,480"
while [ $# -gt 0 ]; do
    case "$1" in
        --manifold|--mesh)  SEL+=("manifold"); shift ;;
        --consistency)      SEL+=("consistency"); shift ;;
        --printability)     SEL+=("printability"); shift ;;
        # --collision/--ref supply DATA; the collision/silhouette gates then run whenever
        # that data is present (see want()) — they never narrow the core gate set, so a
        # supplied config/ref can't silently skip a HARD gate. --silhouette accepted as a
        # no-op for symmetry (the gate is driven by --ref).
        --collision)        COLL="$2"; shift 2 ;;
        --silhouette)       shift ;;
        --skip)             SKIP+=("$2"); shift 2 ;;
        --part)             FILES+=("$2"); shift 2 ;;
        --ref)              REF="$2"; shift 2 ;;
        --cam)              CAM="$2"; shift 2 ;;
        --size)             SILSIZE="${2/x/,}"; shift 2 ;;
        -D)                 DEFS+=("-D" "$2"); shift 2 ;;
        -h|--help)          usage; exit 0 ;;
        -*)                 echo "check: unknown option '$1'" >&2; usage; exit 1 ;;
        *)                  FILES+=("$1"); shift ;;
    esac
done
[ ${#FILES[@]} -eq 0 ] && { echo "check: no input file given" >&2; exit 2; }
for f in "${FILES[@]}"; do [ -f "$f" ] || { echo "check: file not found: $f" >&2; exit 2; }; done
ASSEMBLY="${FILES[0]}"

# gate selection:
#   - core gates (manifold/consistency/printability): empty SEL => all run; else only listed.
#   - collision/silhouette are DATA-driven: they run whenever their data is supplied
#     (--collision cfg / --ref img) AND not skipped — INDEPENDENT of SEL. This way a
#     supplied config can never narrow the gate set (no false-PASS by skipping HARD gates),
#     yet `--mesh --collision cfg` still runs BOTH manifold and the configured collision.
want() { # want GATE -> 0 if this gate should run
    local g="$1" s
    for s in ${SKIP[@]+"${SKIP[@]}"}; do [ "$s" = "$g" ] && return 1; done
    case "$g" in
        collision)  [ -n "$COLL" ] && return 0; return 1 ;;
        silhouette) [ -n "$REF" ]  && return 0; return 1 ;;
    esac
    [ ${#SEL[@]} -eq 0 ] && return 0
    for s in "${SEL[@]}"; do [ "$s" = "$g" ] && return 0; done
    return 1
}

if [ -t 1 ]; then GR=$'\e[32m'; RD=$'\e[31m'; YL=$'\e[33m'; BD=$'\e[1m'; ZZ=$'\e[0m'
else GR=""; RD=""; YL=""; BD=""; ZZ=""; fi
pass(){ printf "  ${GR}%-12s PASS${ZZ}  %s\n" "$1" "${2:-}"; }
fail(){ printf "  ${RD}%-12s FAIL${ZZ}  %s\n" "$1" "${2:-}"; }
skip(){ printf "  ${YL}%-12s SKIP${ZZ}  %s\n" "$1" "${2:-}"; }
info(){ printf "  ${BD}%-12s ----${ZZ}  %s\n" "$1" "${2:-}"; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/3d_check.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
HARD_FAIL=0

echo "${BD}=== check (acceptance gate) ===${ZZ}"
echo "  files: ${FILES[*]}   logs: $WORK"
[ ${#SEL[@]} -gt 0 ] && echo "  selected gates: ${SEL[*]}"
[ ${#SKIP[@]} -gt 0 ] && echo "  skipped gates:  ${SKIP[*]}"
echo

# ---- MANIFOLD (HARD) -------------------------------------------------------
if want manifold; then
    echo "${BD}[MANIFOLD]${ZZ} render --render + grep WARNING:/ERROR: + mesh watertight"
    man_bad=0; man_n=0; man_list=""; man_degraded=0
    for f in "${FILES[@]}"; do
        man_n=$((man_n+1))
        lf="$WORK/man_$(echo "$f" | tr '/.' '__').log"
        stl="$WORK/man_$(echo "$f" | tr '/.' '__').stl"
        "$OPENSCAD" --render --export-format binstl ${DEFS[@]+"${DEFS[@]}"} -o "$stl" "$f" >"$lf" 2>&1 || echo "RENDER-ERROR" >>"$lf"
        bad_f=0
        if grep -Eq 'WARNING:|ERROR:|Assertion|RENDER-ERROR' "$lf"; then
            bad_f=1
            printf "    ${RD}x${ZZ} %s (openscad warning)\n" "$f"
            grep -E 'WARNING:|ERROR:|Assertion|RENDER-ERROR' "$lf" | head -3 | sed 's/^/        /'
        elif [ -s "$stl" ]; then
            mout="$(bash "$REPO_ROOT/lib/cmd_mesh.sh" "$stl" 2>&1)"
            if printf '%s\n' "$mout" | grep -q 'ModuleNotFoundError\|No module named\|no python runtime'; then
                man_degraded=1
            elif printf '%s\n' "$mout" | grep -q '>>> MESH CHECK: FAIL'; then
                bad_f=1
                printf "    ${RD}x${ZZ} %s (mesh-verified non-manifold)\n" "$f"
                printf '%s\n' "$mout" | grep 'MESH CHECK: FAIL' | sed 's/^/        /'
            fi
        else
            bad_f=1
            printf "    ${RD}x${ZZ} %s (no mesh produced)\n" "$f"
        fi
        [ $bad_f -eq 1 ] && { man_bad=$((man_bad+1)); man_list="$man_list $f"; }
    done
    if [ $man_bad -gt 0 ]; then fail MANIFOLD "$man_bad/$man_n bad:$man_list"; HARD_FAIL=1
    elif [ $man_degraded -eq 1 ]; then pass MANIFOLD "$man_n file(s) clean (grep-only — mesh stack absent)"
    else pass MANIFOLD "$man_n file(s) clean (mesh-verified)"; fi
    echo
fi

# ---- CONSISTENCY (HARD) ----------------------------------------------------
if want consistency; then
    echo "${BD}[CONSISTENCY]${ZZ} assert() checks (grep ERROR:/Assertion)"
    con_files=0; con_bad=0; assert_files=""
    for f in "${FILES[@]}"; do
        grep -Eq '\bassert\s*\(' "$f" && assert_files="$assert_files $f"
    done
    if [ -z "$assert_files" ]; then
        skip CONSISTENCY "no assert() in inputs (nothing to check)"
    else
        for f in $assert_files; do
            con_files=$((con_files+1))
            lf="$WORK/con_$(echo "$f" | tr '/.' '__').log"
            "$OPENSCAD" ${DEFS[@]+"${DEFS[@]}"} -o "$WORK/con.csg" "$f" >"$lf" 2>&1 || true
            if grep -Eq 'ERROR:|Assertion failed' "$lf"; then
                con_bad=$((con_bad+1))
                printf "    ${RD}x${ZZ} %s\n" "$f"
                grep -E 'ERROR:|Assertion failed' "$lf" | head -3 | sed 's/^/        /'
            fi
        done
        if [ $con_bad -eq 0 ]; then pass CONSISTENCY "$con_files file(s) with asserts hold"
        else fail CONSISTENCY "$con_bad/$con_files with failed asserts"; HARD_FAIL=1; fi
    fi
    echo
fi

# ---- PRINTABILITY (HARD, degrade->SKIP) ------------------------------------
if want printability; then
    echo "${BD}[PRINTABILITY]${ZZ} walls/overhangs/watertight"
    lf="$WORK/print.log"
    bash "$REPO_ROOT/lib/cmd_printability.sh" "${FILES[@]}" ${DEFS[@]+"${DEFS[@]}"} >"$lf" 2>&1; prc=$?
    if grep -q 'ModuleNotFoundError\|No module named\|no python runtime' "$lf"; then
        skip PRINTABILITY "mesh stack unavailable (install trimesh) — not failing check"
    elif [ $prc -eq 0 ]; then
        pass PRINTABILITY "$(grep -E '>>> PRINTABILITY:' "$lf" | tail -1 | sed 's/>>> //')"
    else
        fail PRINTABILITY "$(grep -E '>>> PRINTABILITY:|FAIL' "$lf" | tail -1)"
        HARD_FAIL=1
    fi
    echo "    (log: $lf)"
    echo
fi

# ---- COLLISION (HARD, only if configured) ----------------------------------
if want collision; then
    echo "${BD}[COLLISION]${ZZ} overlap/penetration (needs --collision cfg.json)"
    if [ -z "$COLL" ]; then
        skip COLLISION "not configured (pass --collision cfg.json)"
    elif [ ! -f "$COLL" ]; then
        fail COLLISION "config not found: $COLL"; HARD_FAIL=1
    else
        lf="$WORK/coll.log"
        bash "$REPO_ROOT/lib/cmd_collision.sh" "$COLL" >"$lf" 2>&1; crc=$?
        v=$(grep -E 'RESULT: (PASS|FAIL)' "$lf" | tail -1)
        if grep -q 'ModuleNotFoundError\|no python runtime' "$lf"; then
            skip COLLISION "mesh stack unavailable — not failing check"
        elif [ $crc -eq 0 ]; then pass COLLISION "${v:-ok}"
        else fail COLLISION "${v:-see $lf}"; HARD_FAIL=1; fi
        echo "    (log: $lf)"
    fi
    echo
fi

# ---- SILHOUETTE (ADVISORY) -------------------------------------------------
if want silhouette; then
    echo "${BD}[SILHOUETTE]${ZZ} image-space IoU/AE vs reference (advisory)"
    if [ -z "$REF" ] || [ ! -f "$REF" ]; then
        skip SILHOUETTE "no reference (pass --ref <img>)"
    elif ! find_magick >/dev/null 2>&1; then
        skip SILHOUETTE "ImageMagick not installed"
    else
        lf="$WORK/sil.log"
        bash "$REPO_ROOT/lib/cmd_score.sh" "$ASSEMBLY" "$REF" -o "$WORK/score" \
            --cam "$CAM" --size "$SILSIZE" >"$lf" 2>&1 || true
        iou=$(grep -E '^IoU=' "$lf" | tail -1 | cut -d= -f2)
        ae=$(grep -E '^AE=' "$lf" | tail -1 | cut -d= -f2)
        if [ -n "$iou" ]; then
            info SILHOUETTE "IoU=$iou AE=${ae:-?} cam=[$CAM] $SILSIZE (ref=$(basename "$REF"))"
        else
            skip SILHOUETTE "scoring failed (see $lf)"
        fi
    fi
    echo
fi

echo "${BD}------------------------------------------------${ZZ}"
if [ "$HARD_FAIL" -eq 0 ]; then
    echo "${GR}>>> CHECK: PASS${ZZ}"
    exit 0
else
    echo "${RD}>>> CHECK: FAIL${ZZ}"
    exit 1
fi
