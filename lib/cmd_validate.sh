#!/usr/bin/env bash
# 3d validate — fast syntax check (no render).
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

usage() {
cat <<EOF
3d validate <file.scad>
  Parse-only syntax check (exports echo; no geometry render). Exit 0 = OK, 1 = error.
EOF
}
[ $# -lt 1 ] && { usage; exit 1; }
case "$1" in -h|--help) usage; exit 0 ;; esac
require_openscad

INPUT="$1"
[ -f "$INPUT" ] || { echo "validate: file not found: $INPUT" >&2; exit 2; }

echo "validate: $INPUT"
TMP="$(mktemp /tmp/3d_validate.XXXXXX.echo)"
trap 'rm -f "$TMP"' EXIT
if OUT=$("$OPENSCAD" -o "$TMP" --export-format=echo "$INPUT" 2>&1); then
    echo "  syntax OK"
    [ -s "$TMP" ] && { echo "  echo output:"; sed 's/^/    /' "$TMP"; }
    # echo export still exits 0 on a failed assert; surface ERROR: lines.
    if printf '%s\n' "$OUT" | grep -q 'ERROR:'; then
        echo "  WARNING: openscad emitted ERROR: lines:"
        printf '%s\n' "$OUT" | grep 'ERROR:' | sed 's/^/    /'
        exit 1
    fi
    exit 0
else
    echo "  validation FAILED" >&2
    printf '%s\n' "$OUT" | sed 's/^/    /' >&2
    exit 1
fi
