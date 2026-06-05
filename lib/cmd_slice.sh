#!/usr/bin/env bash
# 3d slice — slice a model to G-code using whichever slicer CLI is installed.
#
# Preference: OrcaSlicer > Bambu Studio > PrusaSlicer. They share heritage but the
# CLIs DIVERGED, so each gets its own invocation function:
#   PrusaSlicer:           prusa-slicer -g <in> --load <profile.ini> --output <out.gcode>
#   OrcaSlicer/BambuStudio: <bin> --slice 0 [--load-settings "a.json;b.json"] \
#                                  --outputdir <dir> <input.3mf|stl>
# A .scad input is exported to STL first via `3d export`.
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

usage() {
cat <<EOF
3d slice <model.stl|.3mf|.scad> [options]
  Slice a model to G-code with the installed slicer (OrcaSlicer > Bambu Studio >
  PrusaSlicer; auto-detected on PATH and macOS app bundles). A .scad input is
  exported to STL first (via '3d export').

Options:
  -o, --out PATH      output .gcode (default: <model>.gcode). Orca/Bambu write into
                      its parent dir; the result is moved to this path.
  --profile FILE      a profile/config file: .ini (Prusa) or .json (Orca/Bambu),
                      repeatable via comma for Orca/Bambu ("machine.json,process.json")
  --check             sliceability GATE: slice, report OK/FAIL + est. time/filament,
                      then DISCARD the produced G-code (it's a pass/fail oracle, not an
                      artifact you keep). Nonzero exit on failure. Without --check the
                      G-code is kept at -o; both modes exit nonzero if slicing fails.
  --printer NAME      printer/machine preset (best-effort, slicer-flag UNVERIFIED — the
                      flag name differs per slicer and may be ignored; prefer --profile).
  -D k=v              pass-through define for .scad export (repeatable)

Env: SLICER=/path/to/binary forces a specific slicer.

Examples:
  3d slice part.stl -o part.gcode
  3d slice part.scad --check
  3d slice part.3mf --profile "machine.json,process.json"
EOF
}
[ $# -lt 1 ] && { usage; exit 1; }
case "$1" in -h|--help) usage; exit 0 ;; esac

INPUT="$1"; shift
OUT=""; PRINTER=""; PROFILE=""; CHECK=0; DEFS=()
while [ $# -gt 0 ]; do
    case "$1" in
        -o|--out) OUT="$2"; shift 2 ;;
        --printer) PRINTER="$2"; shift 2 ;;
        --profile) PROFILE="$2"; shift 2 ;;
        --check) CHECK=1; shift ;;
        -D) DEFS+=("-D" "$2"); shift 2 ;;
        *) echo "slice: unknown option '$1'" >&2; usage; exit 1 ;;
    esac
done
[ -f "$INPUT" ] || { echo "slice: file not found: $INPUT" >&2; exit 2; }

# ---- locate a slicer --------------------------------------------------------
if ! SL="$(find_slicer)"; then
    OS="$(detect_os)"
    echo "slice: no slicer found (OrcaSlicer / Bambu Studio / PrusaSlicer)." >&2
    case "$OS" in
        macos) echo "  Install: brew install --cask orcaslicer   (or run '3d setup')" >&2 ;;
        linux-*) echo "  Install: OrcaSlicer AppImage from https://github.com/SoftFever/OrcaSlicer/releases (or '3d setup')" >&2 ;;
        *) echo "  Install a slicer, then re-run (or '3d setup')." >&2 ;;
    esac
    exit 127
fi
SLICER_KIND="${SL%%|*}"; SLICER_BIN="${SL#*|}"

# Scratch dir for intermediate artifacts (temp STL, temp --check output). Cleaned on
# exit so we NEVER overwrite/delete a user's sibling model.stl or an existing -o file.
TMPDIR_SLICE="$(mktemp -d -t 3dslice.XXXXXX)"

# ---- if .scad, export to STL first (into the scratch dir, not next to input) ----
ext="${INPUT##*.}"; ext="$(printf '%s' "$ext" | tr 'A-Z' 'a-z')"
WORK_INPUT="$INPUT"
if [ "$ext" = scad ]; then
    TMP_STL="$TMPDIR_SLICE/$(basename "${INPUT%.scad}").stl"
    echo "slice: .scad input — exporting STL via '3d export' -> $TMP_STL"
    if ! bash "$REPO_ROOT/lib/cmd_export.sh" "$INPUT" -o "$TMP_STL" ${DEFS[@]+"${DEFS[@]}"}; then
        echo "slice: STL export failed — aborting" >&2
        exit 1
    fi
    WORK_INPUT="$TMP_STL"
    ext="stl"
fi

[ -z "$OUT" ] && OUT="${INPUT%.*}.gcode"
FINAL_OUT="$OUT"

# Always slice INTO the (empty) scratch dir, then move the result to $FINAL_OUT on
# success. The scratch dir starts empty, so whatever G-code lands there is THIS run's
# output — no timestamps, snapshots, or freshness heuristics needed, and a user's
# existing model.stl / model.gcode is never touched until the final atomic move.
OUTDIR="$TMPDIR_SLICE"
OUT="$TMPDIR_SLICE/out.gcode"     # Prusa writes here; Orca/Bambu write <model>.gcode into OUTDIR

echo "================================================================"
echo "slice: $(basename "$INPUT")  via ${SLICER_KIND}  ($SLICER_BIN)"
CHECK_NOTE=""; [ "$CHECK" -eq 1 ] && CHECK_NOTE="   (--check: gate only, G-code discarded)"
echo "  -> ${FINAL_OUT}${CHECK_NOTE}${PRINTER:+   printer=$PRINTER}${PROFILE:+   profile=$PROFILE}"
echo "================================================================"

LOG="$(mktemp -t 3dslicelog.XXXXXX)"
trap 'rm -f "$LOG"; [ -n "${TMPDIR_SLICE:-}" ] && rm -rf "$TMPDIR_SLICE"' EXIT

RC=1
# Core flags below are the SOLID part of each slicer's contract:
#   PrusaSlicer:            -g --output <file> [--load <ini>...]
#   OrcaSlicer/BambuStudio: --slice 0 --outputdir <dir> [--load-settings "a;b"]
# --printer is BEST-EFFORT: there is no single agreed printer-preset CLI flag across
# these slicers, so we route it through the same profile-load mechanism (a named
# preset config) rather than guessing a dedicated flag. Prefer --profile for control.
# ---- PrusaSlicer lineage -----------------------------------------------------
slice_prusa() {
    local args=( -g --output "$OUT" )
    if [ -n "$PROFILE" ]; then
        # split on COMMA only (paths may contain spaces) — IFS=',', not word-splitting.
        local _profs; IFS=',' read -r -a _profs <<< "$PROFILE"
        local p; for p in "${_profs[@]}"; do [ -n "$p" ] && args+=( --load "$p" ); done
    fi
    [ -n "$PRINTER" ] && args+=( --load "$PRINTER" )   # best-effort: a printer-preset .ini
    "$SLICER_BIN" "${args[@]}" "$WORK_INPUT" >"$LOG" 2>&1
}
# ---- OrcaSlicer / Bambu Studio lineage ---------------------------------------
slice_orca() {
    local args=( --slice 0 --outputdir "$OUTDIR" )
    local loads="$PROFILE"
    # fold a best-effort --printer preset into the ';'-joined --load-settings list.
    [ -n "$PRINTER" ] && loads="${loads:+$loads,}$PRINTER"
    [ -n "$loads" ] && args+=( --load-settings "${loads//,/;}" )
    "$SLICER_BIN" "${args[@]}" "$WORK_INPUT" >"$LOG" 2>&1
}

case "$SLICER_KIND" in
    prusa)        slice_prusa; RC=$? ;;
    orca|bambu)   slice_orca;  RC=$? ;;
    custom)
        # Unknown binary forced via $SLICER — try prusa-style first, then orca-style.
        slice_prusa; RC=$?
        [ "$RC" -ne 0 ] && { slice_orca; RC=$?; } ;;
    *) echo "slice: unknown slicer kind '$SLICER_KIND'" >&2; RC=2 ;;
esac

# The scratch dir started empty, so any G-code in it is this run's output. Prusa wrote
# $OUT directly; Orca/Bambu wrote <model>.gcode — pick whichever non-empty file exists.
PRODUCED=""
if [ "$RC" -eq 0 ]; then
    if [ -s "$OUT" ]; then
        PRODUCED="$OUT"
    else
        PRODUCED="$(find "$TMPDIR_SLICE" -maxdepth 1 \( -name '*.gcode' -o -name '*.gcode.3mf' \) -size +0c 2>/dev/null | head -1 || true)"
    fi
fi

echo "--- slicer log (tail) ---"
tail -n 20 "$LOG" | sed 's/^/  /'
echo "-------------------------"

# ---- sliceability verdict / --check gate ------------------------------------
EST_TIME="$(grep -ioE 'estimated printing time[^0-9]*[0-9hms: ]+' "$LOG" | head -1 || true)"
EST_FIL="$(grep -ioE 'filament used[^0-9]*[0-9.]+ ?[gm]+' "$LOG" | head -1 || true)"

if [ "$RC" -eq 0 ] && [ -n "$PRODUCED" ] && [ -s "$PRODUCED" ]; then
    SIZE="$(ls -lh "$PRODUCED" | awk '{print $5}')"
    if [ "$CHECK" -eq 1 ]; then
        # pass/fail ORACLE: the EXIT trap wipes the scratch dir; no artifact kept, and
        # the real path $FINAL_OUT was never touched.
        echo "STATUS: PASS — sliceable ($SIZE produced, discarded; --check is a gate)"
    else
        mkdir -p "$(dirname "$FINAL_OUT")"
        mv -f "$PRODUCED" "$FINAL_OUT"
        echo "STATUS: PASS — sliced OK -> $FINAL_OUT ($SIZE)"
    fi
    [ -n "$EST_TIME" ] && echo "  $EST_TIME"
    [ -n "$EST_FIL"  ] && echo "  $EST_FIL"
    echo "================================================================"
    exit 0
else
    echo "STATUS: FAIL — slicer did not produce G-code (rc=$RC)"
    echo "  (check the profile/printer preset; some slicers REQUIRE --load <profile>)"
    echo "================================================================"
    # --check is a gate; plain slice also exits nonzero on a real failure.
    exit 1
fi
