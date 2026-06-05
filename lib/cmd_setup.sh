#!/usr/bin/env bash
# 3d setup — actually INSTALL missing dependencies, OS-aware and idempotent.
# Shares the OS/package-map logic with `3d doctor` (lib/common.sh) so they cannot
# drift. Never hard-fails the whole run if one optional thing fails — reports it
# and continues. Python deps go into the repo .venv, never global (unless --global).
set -uo pipefail
source "$REPO_ROOT/lib/common.sh"

usage() {
cat <<EOF
3d setup [--yes] [--dry-run] [--no-slicer] [--global]
  Install the missing dependencies for the \`3d\` pipeline, OS-aware:
    macOS  -> Homebrew (formulae + casks: openscad, imagemagick, orcaslicer, ...)
    Linux  -> apt / dnf / pacman (auto-detected), with sudo; OpenSCAD via distro
              package, slicer via AppImage hint.
    Python -> the repo .venv (uv venv / python -m venv) + pip install -r requirements.txt.

  Idempotent: skips what is already present. Never global-installs python deps
  unless --global. One failed optional item does not abort the rest.

Options:
  --yes        non-interactive (assume yes to every prompt)
  --dry-run    print the exact commands without running them
  --no-slicer  skip slicer install (the heaviest cask/AppImage)
  --global     pip-install python deps into the active python instead of .venv
EOF
}
case "${1:-}" in -h|--help) usage; exit 0 ;; esac

YES=0; DRY=0; NO_SLICER=0; GLOBAL=0
while [ $# -gt 0 ]; do
    case "$1" in
        --yes|-y)    YES=1; shift ;;
        --dry-run)   DRY=1; shift ;;
        --no-slicer) NO_SLICER=1; shift ;;
        --global)    GLOBAL=1; shift ;;
        *) echo "setup: unknown option '$1'" >&2; usage; exit 1 ;;
    esac
done

OS="$(detect_os)"
SUDO="$(sudo_prefix)"

if [ -t 1 ]; then GRN=$'\e[32m'; RED=$'\e[31m'; YEL=$'\e[33m'; B=$'\e[1m'; DIM=$'\e[2m'; Z=$'\e[0m'
else GRN=""; RED=""; YEL=""; B=""; DIM=""; Z=""; fi

OK=(); SKIP=(); FAIL=()

confirm() { # confirm "prompt" -> 0 yes / 1 no
    [ "$YES" -eq 1 ] && return 0
    [ "$DRY" -eq 1 ] && return 0
    printf "  %s? %s [Y/n] " "$YEL" "$1$Z"
    read -r ans </dev/tty 2>/dev/null || { echo "(no tty — use --yes)"; return 1; }
    case "$ans" in n|N|no|NO) return 1 ;; *) return 0 ;; esac
}

# run a command (or echo it under --dry-run). Records OK/FAIL into the summary.
run() { # run <label> <cmd...>
    local label="$1"; shift
    echo "  ${B}-> $label${Z}"
    echo "     ${DIM}\$ $*${Z}"
    if [ "$DRY" -eq 1 ]; then OK+=("$label (dry-run)"); return 0; fi
    if "$@"; then OK+=("$label"); return 0
    else echo "     ${RED}failed${Z} (continuing)"; FAIL+=("$label"); return 1; fi
}
# same but the command is a shell string (for pipes / casks / multi-word).
run_sh() { # run_sh <label> "<shell string>"
    local label="$1"; shift
    echo "  ${B}-> $label${Z}"
    echo "     ${DIM}\$ $*${Z}"
    if [ "$DRY" -eq 1 ]; then OK+=("$label (dry-run)"); return 0; fi
    if bash -c "$*"; then OK+=("$label"); return 0
    else echo "     ${RED}failed${Z} (continuing)"; FAIL+=("$label"); return 1; fi
}

echo "${B}3d setup${Z}  —  OS=$OS  (dry-run=$DRY, yes=$YES)"
echo

# =============================================================================
# Package-manager bootstrap
# =============================================================================
PM=""; PM_INSTALL=""
case "$OS" in
    macos)
        if command -v brew >/dev/null 2>&1; then
            PM="brew"
        else
            # Leave PM empty so ensure_pkg takes its clean no-brew failure path instead
            # of firing 'brew install ...' command-not-found errors per package.
            echo "${RED}Homebrew not found.${Z} Install it first:"
            echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
            FAIL+=("homebrew (prerequisite)")
        fi ;;
    linux-apt)    PM="apt";    PM_INSTALL="${SUDO}apt-get install -y";       run_sh "apt-get update" "${SUDO}apt-get update" ;;
    linux-dnf)    PM="dnf";    PM_INSTALL="${SUDO}dnf install -y" ;;
    linux-pacman) PM="pacman"; PM_INSTALL="${SUDO}pacman -S --noconfirm" ;;
    *)            echo "${YEL}Unsupported OS for auto-install (OS=$OS). Use '3d doctor' for manual commands.${Z}" ;;
esac

# install one formula/distro package if its binary is absent.
# args: <present-test-cmd> <label> <macos-spec> <apt-pkg> <dnf-pkg> <pacman-pkg>
# macos-spec may be "formula:NAME" or "cask:NAME".
ensure_pkg() {
    local test_cmd="$1" label="$2" mac="$3" apt="$4" dnf="$5" pac="$6"
    if eval "$test_cmd" >/dev/null 2>&1; then
        echo "  ${GRN}skip${Z} $label (present)"; SKIP+=("$label"); return 0
    fi
    case "$OS" in
        macos)
            [ -z "${PM:-}" ] && { FAIL+=("$label (no brew)"); return 1; }
            local kind="${mac%%:*}" name="${mac#*:}"
            if [ "$kind" = cask ]; then run_sh "$label (brew cask)" "brew install --cask $name"
            else run_sh "$label (brew)" "brew install $name"; fi ;;
        linux-apt)    run_sh "$label (apt)"    "$PM_INSTALL $apt" ;;
        linux-dnf)    run_sh "$label (dnf)"    "$PM_INSTALL $dnf" ;;
        linux-pacman) run_sh "$label (pacman)" "$PM_INSTALL $pac" ;;
        *) echo "  ${YEL}no map for $label on OS=$OS${Z}"; FAIL+=("$label (unsupported OS)") ;;
    esac
}

echo "${B}[1/4] Core binaries${Z}"
ensure_pkg 'find_openscad' 'openscad' 'cask:openscad' 'openscad' 'openscad' 'openscad'
# OpenSCAD on Linux: if the distro package failed (older repos), suggest AppImage.
if [ "$OS" != macos ] && ! find_openscad >/dev/null 2>&1 && [ "$DRY" -eq 0 ]; then
    echo "  ${YEL}OpenSCAD still absent — fallback: download the AppImage:${Z}"
    echo "     https://openscad.org/downloads.html  (chmod +x OpenSCAD-*.AppImage, put on PATH as 'openscad')"
fi
ensure_pkg 'find_magick' 'imagemagick' 'formula:imagemagick' 'imagemagick' 'ImageMagick' 'imagemagick'
ensure_pkg 'command -v python3' 'python3' 'formula:python' 'python3 python3-venv python3-pip' 'python3 python3-pip' 'python python-pip'

echo
echo "${B}[2/4] uv (per-call python dep resolver)${Z}"
if command -v uv >/dev/null 2>&1; then
    echo "  ${GRN}skip${Z} uv (present)"; SKIP+=("uv")
elif [ "$OS" = macos ] && command -v brew >/dev/null 2>&1; then
    run_sh "uv (brew)" "brew install uv"
else
    if confirm "install uv via the astral install script"; then
        run_sh "uv (astral script)" "curl -LsSf https://astral.sh/uv/install.sh | sh"
    else SKIP+=("uv (declined)"); fi
fi

echo
echo "${B}[3/4] Python mesh stack -> repo .venv${Z}"
if [ "$GLOBAL" -eq 1 ]; then
    # pyrun ALWAYS prefers $REPO_ROOT/.venv when it exists — a --global install then
    # won't be seen by the CLI. Warn so the user isn't misled into a false success.
    if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
        echo "  ${YEL}WARNING:${Z} a repo .venv exists at $REPO_ROOT/.venv — pyrun will keep using it,"
        echo "           NOT your global install. Remove .venv (or drop --global) for --global to take effect."
    fi
    PIP="$(command -v pip3 || command -v pip || true)"
    if [ -z "$PIP" ]; then echo "  ${RED}no pip for --global${Z}"; FAIL+=("python deps (no pip)")
    else run_sh "pip install (GLOBAL)" "$PIP install -r '$REPO_ROOT/requirements.txt'"; fi
else
    VENV="$REPO_ROOT/.venv"
    if [ -x "$VENV/bin/python" ]; then
        echo "  ${GRN}skip${Z} .venv (exists at $VENV)"; SKIP+=(".venv create")
    elif command -v uv >/dev/null 2>&1; then
        run_sh "create .venv (uv)" "uv venv '$VENV'"
    elif command -v python3 >/dev/null 2>&1; then
        run_sh "create .venv (python -m venv)" "python3 -m venv '$VENV'"
    else
        echo "  ${RED}no python3/uv to create .venv${Z}"; FAIL+=(".venv create")
    fi
    if [ "$DRY" -eq 1 ] || [ -x "$VENV/bin/python" ]; then
        if command -v uv >/dev/null 2>&1; then
            run_sh "pip install -r requirements.txt (uv)" "uv pip install --python '$VENV/bin/python' -r '$REPO_ROOT/requirements.txt'"
        else
            run_sh "pip install -r requirements.txt" "'$VENV/bin/pip' install -r '$REPO_ROOT/requirements.txt'"
        fi
    fi
fi

echo
echo "${B}[4/4] Slicer (OrcaSlicer preferred)${Z}"
if [ "$NO_SLICER" -eq 1 ]; then
    echo "  ${YEL}skip${Z} slicer (--no-slicer)"; SKIP+=("slicer (--no-slicer)")
elif find_slicer >/dev/null 2>&1; then
    s="$(find_slicer)"; echo "  ${GRN}skip${Z} slicer (${s%%|*} present at ${s#*|})"; SKIP+=("slicer")
else
    case "$OS" in
        macos)
            if confirm "install OrcaSlicer (brew cask, ~heavy download)"; then
                run_sh "OrcaSlicer (brew cask)" "brew install --cask orcaslicer" \
                    || run_sh "PrusaSlicer (brew cask, fallback)" "brew install --cask prusaslicer"
            else SKIP+=("slicer (declined)"); fi ;;
        linux-pacman)
            run_sh "orca-slicer (pacman/AUR)" "$PM_INSTALL orca-slicer" \
                || echo "  ${YEL}AppImage fallback:${Z} https://github.com/SoftFever/OrcaSlicer/releases" ;;
        linux-apt|linux-dnf)
            echo "  ${YEL}OrcaSlicer ships as an AppImage on this distro (no apt/dnf package).${Z}"
            echo "     Download: https://github.com/SoftFever/OrcaSlicer/releases"
            echo "     chmod +x OrcaSlicer-*.AppImage and put it on PATH as 'orca-slicer'."
            SKIP+=("slicer (manual AppImage)") ;;
        *) echo "  ${YEL}no slicer map for OS=$OS${Z}"; SKIP+=("slicer (unsupported OS)") ;;
    esac
fi

# =============================================================================
echo
echo "${B}================== setup summary ==================${Z}"
[ ${#OK[@]}   -gt 0 ] && { echo "${GRN}installed/ran:${Z}"; printf '  + %s\n' "${OK[@]}"; }
[ ${#SKIP[@]} -gt 0 ] && { echo "${DIM}skipped (already present / declined):${Z}"; printf '  = %s\n' "${SKIP[@]}"; }
[ ${#FAIL[@]} -gt 0 ] && { echo "${RED}failed (continued past):${Z}"; printf '  ! %s\n' "${FAIL[@]}"; }
echo
echo "Verify with: ${B}3d doctor${Z}"

# Exit status: optional misses (slicer, uv, pyvista) NEVER fail the run (per spec). But
# if a REQUIRED dep is still absent after setup, exit nonzero so automation/CI notices.
# Required = openscad, imagemagick, python3, and a usable python mesh stack (venv/uv/system).
if [ "$DRY" -eq 1 ]; then exit 0; fi
MISSING_REQ=()
find_openscad >/dev/null 2>&1 || MISSING_REQ+=("openscad")
find_magick   >/dev/null 2>&1 || MISSING_REQ+=("imagemagick")
command -v python3 >/dev/null 2>&1 || MISSING_REQ+=("python3")
# python mesh stack usable — checked along pyrun's ACTUAL resolution order (venv > uv >
# system). Crucially: if a .venv exists, pyrun uses it and never falls back to uv, so a
# .venv that can't import the stack is a FAILURE even when uv is on PATH.
PYOK=0
if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    # venv wins in pyrun — it alone decides; uv cannot rescue an incomplete venv.
    "$REPO_ROOT/.venv/bin/python" -c 'import trimesh,manifold3d,numpy,scipy,rtree' >/dev/null 2>&1 && PYOK=1
elif command -v uv >/dev/null 2>&1 && [ -z "${PY3D_NO_UV:-}" ]; then PYOK=1
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import trimesh,manifold3d,numpy,scipy,rtree' >/dev/null 2>&1; then PYOK=1; fi
[ "$PYOK" -eq 1 ] || MISSING_REQ+=("python-mesh-stack (repo .venv present but incomplete? — pyrun uses it, not uv)")
if [ ${#MISSING_REQ[@]} -gt 0 ]; then
    echo "${RED}>>> SETUP: required deps still missing:${Z} ${MISSING_REQ[*]}  (exit 1)"
    exit 1
fi
echo "${GRN}>>> SETUP: required deps present.${Z}"
exit 0
