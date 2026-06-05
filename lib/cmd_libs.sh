#!/usr/bin/env bash
# 3d libs — INFO only: print the OPENSCADPATH and list installed OpenSCAD libraries.
# Installation is handled automatically by the first-run bootstrap (see lib/common.sh
# maybe_bootstrap); there is no `libs install` anymore.
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

LIBS_DIR="$REPO_ROOT/libs"

usage() {
cat <<EOF
3d libs <subcommand>   (info only — libraries auto-install on first run)
  path                           print the OPENSCADPATH line to export
  list                           show installed libraries

Notes:
  OpenSCAD libraries (BOSL2, NopSCADlib) are cloned into libs/ automatically on the
  first \`3d\` invocation, and OPENSCADPATH is auto-exported by the CLI — so
  'include <BOSL2/std.scad>' just resolves. \`libs path\` prints the line if you want it
  in your own (non-3d) shell. To re-install, remove ~/.config/3d/.bootstrapped and rerun.

Examples:
  3d libs list
  export \$(3d libs path)
EOF
}

[ $# -lt 1 ] && { usage; exit 1; }
sub="$1"; shift || true
case "$sub" in -h|--help|help) usage; exit 0 ;; esac

case "$sub" in
    install)
        echo "libs: 'install' was removed — libraries auto-install on first run." >&2
        echo "      To force a re-install: rm ~/.config/3d/.bootstrapped && 3d help" >&2
        exit 2 ;;
    path)
        # common.sh already prepended $LIBS_DIR to OPENSCADPATH (dedup-guarded), so emit
        # that as-is — it lets `include <BOSL2/std.scad>` resolve and keeps user libs.
        echo "OPENSCADPATH=${OPENSCADPATH:-$LIBS_DIR}"
        ;;
    list)
        echo "Installed OpenSCAD libraries in $LIBS_DIR:"
        if [ -d "$LIBS_DIR" ]; then
            found=0
            for d in "$LIBS_DIR"/*/; do
                [ -d "$d" ] || continue
                found=1
                echo "  - $(basename "$d")"
            done
            [ $found -eq 0 ] && echo "  (none — re-run after removing ~/.config/3d/.bootstrapped)"
        else
            echo "  (none — re-run after removing ~/.config/3d/.bootstrapped)"
        fi
        echo
        echo "To use:  export \$(3d libs path)" ;;
    *)
        echo "libs: unknown subcommand '$sub'" >&2; usage; exit 2 ;;
esac
