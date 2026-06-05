#!/usr/bin/env bash
# 3d acceptance — MASTER acceptance gate (report §7.6 / §8.1).
#
# Runs every sub-gate on the passed assembly and prints a per-gate breakdown plus a
# single verdict line:
#     >>> ACCEPTANCE: PASS   (all HARD gates PASS)
#     >>> ACCEPTANCE: FAIL   (one or more HARD gates FAIL)
#
# Hard gates:   MANIFOLD, CONSISTENCY, PRINTABILITY
# Optional (run only when configured): COLLISION (--collision cfg.json),
#                                      SILHOUETTE (--ref image; advisory)
#
# GENERIC: operates on the single assembly file given. Extra printable parts can be
# added with --part FILE (repeatable). No project directory layout assumed.
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

usage() {
cat <<EOF
3d acceptance <assembly.scad> [options]
  Master gate: MANIFOLD + CONSISTENCY + PRINTABILITY (+ COLLISION/SILHOUETTE if configured).

Options:
  --part FILE           additional printable part to gate (repeatable)
  --collision CFG.json  run the collision engine on this config (HARD)
  --ref IMAGE           reference image -> SILHOUETTE IoU/AE line (advisory)
  --cam ex,..,cz        6-param vector camera for the silhouette render
  --size WxH            silhouette render size (default 1100x480)

Exit 0 = PASS, 1 = FAIL.

Examples:
  3d acceptance assembly.scad
  3d acceptance assembly.scad --part parts/a.scad --ref ref.jpg --collision verify/collision.json
EOF
}
[ $# -lt 1 ] && { usage; exit 1; }
case "$1" in -h|--help) usage; exit 0 ;; esac
require_openscad

ASSEMBLY="$1"; shift
PARTS=(); COLL=""; REF=""; CAM="125,-330,52,125,28,44"; SILSIZE="1100,480"
while [ $# -gt 0 ]; do
    case "$1" in
        --part) PARTS+=("$2"); shift 2 ;;
        --collision) COLL="$2"; shift 2 ;;
        --ref) REF="$2"; shift 2 ;;
        --cam) CAM="$2"; shift 2 ;;
        --size) SILSIZE="${2/x/,}"; shift 2 ;;
        *) echo "acceptance: unknown option '$1'" >&2; usage; exit 1 ;;
    esac
done
[ -f "$ASSEMBLY" ] || { echo "acceptance: assembly not found: $ASSEMBLY" >&2; exit 2; }

if [ -t 1 ]; then GR=$'\e[32m'; RD=$'\e[31m'; YL=$'\e[33m'; BD=$'\e[1m'; ZZ=$'\e[0m'
else GR=""; RD=""; YL=""; BD=""; ZZ=""; fi
pass(){ printf "  ${GR}%-12s PASS${ZZ}  %s\n" "$1" "${2:-}"; }
fail(){ printf "  ${RD}%-12s FAIL${ZZ}  %s\n" "$1" "${2:-}"; }
skip(){ printf "  ${YL}%-12s SKIP${ZZ}  %s\n" "$1" "${2:-}"; }
info(){ printf "  ${BD}%-12s ----${ZZ}  %s\n" "$1" "${2:-}"; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/3d_accept.XXXXXX")"
HARD_FAIL=0
FILES=("$ASSEMBLY" ${PARTS[@]+"${PARTS[@]}"})

echo "${BD}=== acceptance gate ===${ZZ}"
echo "  assembly: $ASSEMBLY   parts: ${#PARTS[@]}   logs: $WORK"
echo

# ---- 1. MANIFOLD (HARD) ----------------------------------------------------
echo "${BD}[MANIFOLD]${ZZ} render --render, grep WARNING:/ERROR:"
man_bad=0; man_n=0; man_list=""
for f in "${FILES[@]}"; do
    [ -f "$f" ] || continue
    man_n=$((man_n+1))
    lf="$WORK/man_$(echo "$f" | tr '/.' '__').log"
    "$OPENSCAD" --render --export-format binstl -o "$WORK/out.stl" "$f" >"$lf" 2>&1 || echo "RENDER-ERROR" >>"$lf"
    if grep -Eq 'WARNING:|ERROR:|Assertion|RENDER-ERROR' "$lf"; then
        man_bad=$((man_bad+1)); man_list="$man_list $f"
        printf "    ${RD}x${ZZ} %s\n" "$f"
        grep -E 'WARNING:|ERROR:|Assertion|RENDER-ERROR' "$lf" | head -3 | sed 's/^/        /'
    fi
done
if [ $man_n -eq 0 ]; then skip MANIFOLD "no scad files"
elif [ $man_bad -eq 0 ]; then pass MANIFOLD "$man_n file(s) clean"
else fail MANIFOLD "$man_bad/$man_n with warnings:$man_list"; HARD_FAIL=1; fi
echo

# ---- 2. CONSISTENCY (HARD) -------------------------------------------------
echo "${BD}[CONSISTENCY]${ZZ} assert() checks (grep ERROR:/Assertion)"
con_files=0; con_bad=0; assert_files=""
for f in "${FILES[@]}"; do
    [ -f "$f" ] || continue
    grep -Eq '\bassert\s*\(' "$f" && assert_files="$assert_files $f"
done
if [ -z "$assert_files" ]; then
    skip CONSISTENCY "no assert() in inputs (nothing to check)"
else
    for f in $assert_files; do
        con_files=$((con_files+1))
        lf="$WORK/con_$(echo "$f" | tr '/.' '__').log"
        "$OPENSCAD" -o "$WORK/con.csg" "$f" >"$lf" 2>&1 || true
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

# ---- 3. PRINTABILITY (HARD, degrade->SKIP) ---------------------------------
echo "${BD}[PRINTABILITY]${ZZ} walls/overhangs/watertight"
lf="$WORK/print.log"
bash "$REPO_ROOT/lib/cmd_printability.sh" "${FILES[@]}" >"$lf" 2>&1; prc=$?
if grep -q 'ModuleNotFoundError\|No module named\|no python runtime' "$lf"; then
    skip PRINTABILITY "mesh stack unavailable (install trimesh) — not failing acceptance"
elif [ $prc -eq 0 ]; then
    pass PRINTABILITY "$(grep -E '>>> PRINTABILITY:' "$lf" | tail -1 | sed 's/>>> //')"
else
    fail PRINTABILITY "$(grep -E '>>> PRINTABILITY:|FAIL' "$lf" | tail -1)"
    HARD_FAIL=1
fi
echo "    (log: $lf)"
echo

# ---- 4. COLLISION (HARD, only if configured) -------------------------------
echo "${BD}[COLLISION]${ZZ} overlap/penetration (only if --collision given)"
if [ -n "$COLL" ]; then
    if [ -f "$COLL" ]; then
        lf="$WORK/coll.log"
        bash "$REPO_ROOT/lib/cmd_collision.sh" "$COLL" >"$lf" 2>&1; crc=$?
        v=$(grep -E 'RESULT: (PASS|FAIL)' "$lf" | tail -1)
        if grep -q 'ModuleNotFoundError\|no python runtime' "$lf"; then
            skip COLLISION "mesh stack unavailable — not failing acceptance"
        elif [ $crc -eq 0 ]; then pass COLLISION "${v:-ok}"
        else fail COLLISION "${v:-see $lf}"; HARD_FAIL=1; fi
        echo "    (log: $lf)"
    else
        fail COLLISION "config not found: $COLL"; HARD_FAIL=1
    fi
else
    skip COLLISION "not configured (pass --collision cfg.json)"
fi
echo

# ---- 5. SILHOUETTE (ADVISORY) ----------------------------------------------
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

echo "${BD}---------------------------------------------${ZZ}"
if [ "$HARD_FAIL" -eq 0 ]; then
    echo "${GR}>>> ACCEPTANCE: PASS${ZZ}"; RC=0
else
    echo "${RD}>>> ACCEPTANCE: FAIL${ZZ}"; RC=1
fi
echo "    (logs in $WORK)"
exit $RC
