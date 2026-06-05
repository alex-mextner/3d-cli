#!/usr/bin/env bash
# 3d doctor — detect & report which dependencies are present/missing, with the
# EXACT install command for the current OS. Detect-only: never mutates anything.
# `3d setup` runs the same table to actually install. Never hard-fails; reports.
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

usage() {
cat <<EOF
3d doctor
  Report which dependencies are present/missing for the full \`3d\` pipeline,
  with the exact install command for THIS OS. Read-only — installs nothing.
  Run \`3d setup\` to install the missing ones.

  Checks: openscad, imagemagick, python3, uv/pip, the python mesh stack
  (trimesh, manifold3d, numpy, scipy, pillow, opencv), and a slicer
  (OrcaSlicer / Bambu Studio / PrusaSlicer).
EOF
}
case "${1:-}" in -h|--help) usage; exit 0 ;; esac

OS="$(detect_os)"
SUDO="$(sudo_prefix)"

if [ -t 1 ]; then GRN=$'\e[32m'; RED=$'\e[31m'; YEL=$'\e[33m'; DIM=$'\e[2m'; B=$'\e[1m'; Z=$'\e[0m'
else GRN=""; RED=""; YEL=""; DIM=""; B=""; Z=""; fi

MISSING=0

pass() { printf "  %sPASS%s    %-22s %s\n" "$GRN" "$Z" "$1" "${2:-}"; }
miss() { printf "  %sMISSING%s %-22s %sinstall:%s %s\n" "$RED" "$Z" "$1" "$DIM" "$Z" "$2"; MISSING=$((MISSING+1)); }
warn() { printf "  %sWARN%s    %-22s %s\n" "$YEL" "$Z" "$1" "${2:-}"; }

# brew/apt/dnf/pacman install command for a logical tool name.
install_cmd() {
    local tool="$1"
    case "$OS" in
        macos)
            case "$tool" in
                openscad)    echo "brew install --cask openscad" ;;
                imagemagick) echo "brew install imagemagick" ;;
                python3)     echo "brew install python" ;;
                uv)          echo "brew install uv  # or: curl -LsSf https://astral.sh/uv/install.sh | sh" ;;
                slicer)      echo "brew install --cask orcaslicer  # or bambu-studio / prusaslicer" ;;
            esac ;;
        linux-apt)
            case "$tool" in
                openscad)    echo "${SUDO}apt-get install -y openscad" ;;
                imagemagick) echo "${SUDO}apt-get install -y imagemagick" ;;
                python3)     echo "${SUDO}apt-get install -y python3 python3-venv python3-pip" ;;
                uv)          echo "curl -LsSf https://astral.sh/uv/install.sh | sh" ;;
                slicer)      echo "download OrcaSlicer AppImage from github.com/SoftFever/OrcaSlicer/releases" ;;
            esac ;;
        linux-dnf)
            case "$tool" in
                openscad)    echo "${SUDO}dnf install -y openscad" ;;
                imagemagick) echo "${SUDO}dnf install -y ImageMagick" ;;
                python3)     echo "${SUDO}dnf install -y python3 python3-pip" ;;
                uv)          echo "curl -LsSf https://astral.sh/uv/install.sh | sh" ;;
                slicer)      echo "download OrcaSlicer AppImage from github.com/SoftFever/OrcaSlicer/releases" ;;
            esac ;;
        linux-pacman)
            case "$tool" in
                openscad)    echo "${SUDO}pacman -S --noconfirm openscad" ;;
                imagemagick) echo "${SUDO}pacman -S --noconfirm imagemagick" ;;
                python3)     echo "${SUDO}pacman -S --noconfirm python python-pip" ;;
                uv)          echo "${SUDO}pacman -S --noconfirm uv  # or astral install script" ;;
                slicer)      echo "${SUDO}pacman -S --noconfirm orca-slicer  # (AUR) or AppImage" ;;
            esac ;;
        *)
            echo "(no package map for OS=$OS — install '$tool' manually)" ;;
    esac
}

echo "${B}3d doctor${Z}  —  OS=$OS"
echo

# ---- core binaries ----------------------------------------------------------
echo "${B}Core${Z}"
if OSC="$(find_openscad)"; then pass openscad "$OSC"; else miss openscad "$(install_cmd openscad)"; fi
if MGK="$(find_magick)";  then pass "imagemagick (magick)" "$MGK"; else miss "imagemagick (magick)" "$(install_cmd imagemagick)"; fi
if command -v python3 >/dev/null 2>&1; then pass python3 "$(command -v python3)"; else miss python3 "$(install_cmd python3)"; fi

# ---- python runtime path (uv OR a venv) -------------------------------------
echo
echo "${B}Python runtime (need uv OR a .venv OR importable system deps)${Z}"
HAS_UV=0; HAS_VENV=0
# PY3D_NO_UV=1 makes pyrun SKIP uv — so doctor must not credit uv in that case either,
# or it would report PASS while the real runtime path (venv/system) lacks the deps.
if [ -n "${PY3D_NO_UV:-}" ]; then
    warn uv "disabled by PY3D_NO_UV=1 (pyrun will not use it)"
elif command -v uv >/dev/null 2>&1; then pass uv "$(command -v uv) — resolves deps on the fly"; HAS_UV=1
else warn uv "not found — $(install_cmd uv)"; fi
if command -v pip3 >/dev/null 2>&1 || command -v pip >/dev/null 2>&1; then pass pip "$(command -v pip3 || command -v pip)"; else warn pip "not found (bundled with python3 -m venv)"; fi
if [ -x "$REPO_ROOT/.venv/bin/python" ]; then pass ".venv" "$REPO_ROOT/.venv (pyrun prefers this)"; HAS_VENV=1
else warn ".venv" "absent — \`3d setup\` creates it (else uv is used per-call)"; fi

# ---- python mesh stack ------------------------------------------------------
# Subtlety: pyrun prefers .venv > uv > system. If a .venv EXISTS, uv is NEVER used,
# so a module absent from the .venv is a genuine MISSING (uv can't rescue it). The
# "uv resolves it per-call" downgrade is only valid when there is NO .venv.
echo
echo "${B}Python mesh stack (mesh / check / printability / collision / preprocess)${Z}"
[ "$HAS_VENV" -eq 1 ] && echo "  ${DIM}(.venv present — pyrun uses it, NOT uv; missing modules are real)${Z}"
if PY="$(resolve_python)"; then
    for mod in $PY_MESH_MODULES; do
        if py_has_module "$mod"; then
            pass "py:$mod" "importable by $PY"
        else
            pkg="$(pypkg_for "$mod")"
            # uv only saves us if it would actually run: no .venv and uv on PATH.
            if [ "$HAS_VENV" -eq 0 ] && [ "$HAS_UV" -eq 1 ]; then
                warn "py:$mod" "not in $PY (ok: uv resolves '$pkg' per-call; \`3d setup\` adds to .venv)"
            else
                miss "py:$mod" "pip install $pkg   (or \`3d setup\`)"
            fi
        fi
    done
    # pyvista is collision --viz only — report it, but its absence isn't fatal to
    # the core stack (every other command works without it).
    if py_has_module pyvista; then
        pass "py:pyvista" "importable by $PY (collision --viz available)"
    elif [ "$HAS_VENV" -eq 0 ] && [ "$HAS_UV" -eq 1 ]; then
        warn "py:pyvista" "not in $PY (collision --viz: uv resolves per-call; \`3d setup\` adds it)"
    else
        warn "py:pyvista" "absent — only 'collision --viz' needs it (\`3d setup\` adds it)"
    fi
else
    miss "python mesh stack" "no python3 — $(install_cmd python3)"
fi

# ---- slicer -----------------------------------------------------------------
echo
echo "${B}Slicer (3d slice)${Z}"
if S="$(find_slicer)"; then
    pass "slicer (${S%%|*})" "${S#*|}"
else
    miss "slicer" "$(install_cmd slicer)   [OrcaSlicer > Bambu Studio > PrusaSlicer]"
fi

echo
echo "${B}OpenSCAD libraries${Z}"
if [ -d "$REPO_ROOT/libs/BOSL2" ]; then pass "libs/BOSL2" "$REPO_ROOT/libs/BOSL2"; else warn "libs/BOSL2" "absent — \`3d libs install bosl2\`"; fi

echo
if [ "$MISSING" -eq 0 ]; then
    echo "${GRN}>>> DOCTOR: PASS${Z} — all required dependencies present."
    exit 0
else
    echo "${YEL}>>> DOCTOR: $MISSING MISSING${Z} — run '3d setup' to install (or use the per-item commands above)."
    # doctor is informational: nonzero so scripts/CI can gate, but never crashes.
    exit 1
fi
