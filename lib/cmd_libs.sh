#!/usr/bin/env bash
# 3d libs — manage OpenSCAD libraries (clone into the repo's libs/, print OPENSCADPATH).
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

LIBS_DIR="$REPO_ROOT/libs"

# name -> git URL  (parallel arrays for bash 3 compatibility)
NAMES="bosl2 nopscadlib"
url_for() {
    case "$1" in
        bosl2)      echo "https://github.com/BelfrySCAD/BOSL2.git" ;;
        nopscadlib) echo "https://github.com/nophead/NopSCADlib.git" ;;
        *) return 1 ;;
    esac
}
dir_for() {
    case "$1" in
        bosl2)      echo "BOSL2" ;;
        nopscadlib) echo "NopSCADlib" ;;
        *) return 1 ;;
    esac
}

usage() {
cat <<EOF
3d libs <subcommand>
  install bosl2|nopscadlib|all   clone OpenSCAD libraries into libs/
  path                           print the OPENSCADPATH line to export
  list                           show installed libraries

Notes:
  After installing, run:  export \$(3d libs path)
  (or add it to your shell profile) so 'include <BOSL2/std.scad>' resolves.

Examples:
  3d libs install bosl2
  3d libs install all
  export \$(3d libs path)
EOF
}

[ $# -lt 1 ] && { usage; exit 1; }
sub="$1"; shift || true
case "$sub" in -h|--help|help) usage; exit 0 ;; esac

install_one() {
    local name="$1" url dir dst
    url="$(url_for "$name")" || { echo "libs: unknown library '$name' (known: $NAMES)" >&2; return 2; }
    dir="$(dir_for "$name")"
    dst="$LIBS_DIR/$dir"
    mkdir -p "$LIBS_DIR"
    command -v git >/dev/null 2>&1 || { echo "libs: git not found" >&2; return 127; }
    if [ -d "$dst/.git" ]; then
        echo "libs: $name already present at $dst — pulling latest"
        git -C "$dst" pull --ff-only 2>&1 | sed 's/^/  /' || echo "  (pull skipped)"
    else
        echo "libs: cloning $name -> $dst"
        git clone --depth 1 "$url" "$dst" 2>&1 | sed 's/^/  /'
    fi
    [ -d "$dst" ] && echo "libs: $name ready ($dst)"
}

case "$sub" in
    install)
        target="${1:-}"
        [ -n "$target" ] || { echo "libs install: name required (bosl2|nopscadlib|all)" >&2; exit 2; }
        rc=0
        if [ "$target" = all ]; then
            for n in $NAMES; do install_one "$n" || rc=$?; done
        else
            install_one "$target" || rc=$?
        fi
        echo
        echo "Set the path:  export $($0 path 2>/dev/null || true)"
        exit $rc ;;
    path)
        # OPENSCADPATH lets `include <BOSL2/std.scad>` resolve from libs/.
        # Append any pre-existing OPENSCADPATH so user libs still work.
        if [ -n "${OPENSCADPATH:-}" ]; then
            echo "OPENSCADPATH=$LIBS_DIR:$OPENSCADPATH"
        else
            echo "OPENSCADPATH=$LIBS_DIR"
        fi
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
            [ $found -eq 0 ] && echo "  (none — run '3d libs install all')"
        else
            echo "  (none — run '3d libs install all')"
        fi
        echo
        echo "To use:  export \$(3d libs path)" ;;
    *)
        echo "libs: unknown subcommand '$sub'" >&2; usage; exit 2 ;;
esac
