#!/usr/bin/env bash
# lib/common.sh — shared helpers for the `3d` CLI.
#
# Sourced by every bash helper in lib/. Provides:
#   - REPO_ROOT          : the 3d-cli repo root, resolved THROUGH the bin/3d symlink.
#   - find_openscad      : locate the openscad binary (PATH + common Homebrew paths).
#   - find_magick        : locate ImageMagick (magick, fallback convert).
#   - require_openscad / require_magick : hard-fail with an install hint if missing.
#
# Robust symlink resolution: `3d` is invoked via ~/.files/bin/3d which is a SYMLINK
# into this repo. `dirname "$0"` would point at ~/.files/bin and miss lib/, libs/,
# .venv. macOS `readlink` has no -f, so we walk the symlink chain by hand.

# If the dispatcher already exported REPO_ROOT, trust it; else resolve from this file.
if [ -z "${REPO_ROOT:-}" ]; then
    _src="${BASH_SOURCE[0]}"
    while [ -h "$_src" ]; do
        _dir="$(cd -P "$(dirname "$_src")" && pwd)"
        _src="$(readlink "$_src")"
        [[ "$_src" != /* ]] && _src="$_dir/$_src"
    done
    # this file lives in <repo>/lib/common.sh
    REPO_ROOT="$(cd -P "$(dirname "$_src")/.." && pwd)"
fi
export REPO_ROOT

find_openscad() {
    if [ -n "${OPENSCAD:-}" ] && command -v "$OPENSCAD" >/dev/null 2>&1; then
        echo "$OPENSCAD"; return 0
    fi
    if command -v openscad >/dev/null 2>&1; then echo "openscad"; return 0; fi
    for p in \
        /Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD \
        /opt/homebrew/bin/openscad \
        /usr/local/bin/openscad; do
        [ -x "$p" ] && { echo "$p"; return 0; }
    done
    return 1
}

require_openscad() {
    OPENSCAD="$(find_openscad)" || {
        echo "Error: OpenSCAD not found on PATH or common locations." >&2
        echo "  Install: brew install --cask openscad   (or openscad@snapshot for newest)" >&2
        exit 127
    }
    export OPENSCAD
}

find_magick() {
    if command -v magick >/dev/null 2>&1; then echo "magick"; return 0; fi
    for p in /opt/homebrew/bin/magick /usr/local/bin/magick; do
        [ -x "$p" ] && { echo "$p"; return 0; }
    done
    # IM6 legacy: 'convert' exists but no 'magick'
    if command -v convert >/dev/null 2>&1; then echo "convert"; return 0; fi
    return 1
}

require_magick() {
    MAGICK="$(find_magick)" || {
        echo "Error: ImageMagick not found." >&2
        echo "  Install: brew install imagemagick" >&2
        exit 127
    }
    export MAGICK
    # IM7 ships `magick compare`; IM6 ships a separate `compare` binary.
    if [ "$MAGICK" = "magick" ]; then COMPARE="magick compare"; else COMPARE="compare"; fi
    export COMPARE
}
