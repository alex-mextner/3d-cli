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

# =============================================================================
# OS / package-manager detection + dependency table — shared by `3d doctor`
# and `3d setup` so the two CANNOT drift (doctor reports the exact command that
# setup runs). detect-only here; mutation lives in cmd_setup.sh.
# =============================================================================

# detect_os -> echoes one of: macos linux-apt linux-dnf linux-pacman linux-unknown other
detect_os() {
    case "$(uname -s)" in
        Darwin) echo "macos" ;;
        Linux)
            if   command -v apt-get >/dev/null 2>&1; then echo "linux-apt"
            elif command -v dnf     >/dev/null 2>&1; then echo "linux-dnf"
            elif command -v pacman  >/dev/null 2>&1; then echo "linux-pacman"
            else echo "linux-unknown"; fi ;;
        *) echo "other" ;;
    esac
}

# sudo_prefix -> "sudo " if not root and sudo exists, else "".
sudo_prefix() {
    if [ "$(id -u 2>/dev/null)" = "0" ]; then echo ""; return; fi
    command -v sudo >/dev/null 2>&1 && echo "sudo " || echo ""
}

# find_slicer -> echoes "<name>|<path>" for the FIRST slicer found, preference:
# OrcaSlicer > Bambu Studio > PrusaSlicer. Empty + return 1 if none.
# Each slicer is checked across BOTH PATH and macOS app bundles BEFORE falling to the
# next, so a PATH-installed lower-priority slicer never beats a bundle-installed
# higher-priority one (preference must hold regardless of install location).
find_slicer() {
    local c p
    if [ -n "${SLICER:-}" ] && [ -x "${SLICER}" ]; then echo "custom|$SLICER"; return 0; fi
    # OrcaSlicer (PATH then bundle)
    for c in orca-slicer OrcaSlicer; do command -v "$c" >/dev/null 2>&1 && { echo "orca|$(command -v "$c")"; return 0; }; done
    for p in /Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer; do [ -x "$p" ] && { echo "orca|$p"; return 0; }; done
    # Bambu Studio (PATH then bundle)
    for c in bambu-studio BambuStudio bambu-studio-cli; do command -v "$c" >/dev/null 2>&1 && { echo "bambu|$(command -v "$c")"; return 0; }; done
    for p in /Applications/BambuStudio.app/Contents/MacOS/BambuStudio \
             "/Applications/Bambu Studio.app/Contents/MacOS/BambuStudio"; do [ -x "$p" ] && { echo "bambu|$p"; return 0; }; done
    # PrusaSlicer (PATH then bundle)
    for c in prusa-slicer PrusaSlicer prusa-slicer-console; do command -v "$c" >/dev/null 2>&1 && { echo "prusa|$(command -v "$c")"; return 0; }; done
    for p in /Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer \
             "/Applications/Original Prusa Drivers/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"; do [ -x "$p" ] && { echo "prusa|$p"; return 0; }; done
    return 1
}

# Python mesh-stack import names checked by doctor/setup (pip names differ; see PYPKG_*).
# rtree backs the broad-phase in check/printability/collision; missing it fails at runtime.
PY_MESH_MODULES="trimesh manifold3d numpy scipy rtree PIL cv2"
# Map import-name -> pip package name where they differ.
pypkg_for() {
    case "$1" in
        PIL) echo "pillow" ;;
        cv2) echo "opencv-python-headless" ;;
        *)   echo "$1" ;;
    esac
}

# py_has_module <import-name> : 0 if importable by the resolved python, else 1.
# Uses the same runtime resolution as lib/pyrun (venv > uv > system) but only for
# the venv/system case; uv-on-the-fly is reported separately by doctor.
py_has_module() {
    local mod="$1" py
    py="$(resolve_python)" || return 1
    "$py" -c "import importlib,sys; importlib.import_module('$mod')" >/dev/null 2>&1
}

# resolve_python -> echoes the python that pyrun's venv/system tiers would use.
resolve_python() {
    if [ -x "$REPO_ROOT/.venv/bin/python" ]; then echo "$REPO_ROOT/.venv/bin/python"; return 0; fi
    command -v python3 >/dev/null 2>&1 && { echo python3; return 0; }
    return 1
}
