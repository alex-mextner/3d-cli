#!/bin/sh
# secret-scan.sh — generic, CI-agnostic secret scan with gitleaks.
#
# For any CI that runs a shell step (GitLab CI, Jenkins, Buildkite, CircleCI, Drone,
# bare cron, a Makefile target). On GitHub Actions prefer ./secret-scan.yml (the pinned
# action). Secret-scanning standard = gitleaks; this is the same engine, scripted.
#
# WHAT IT DOES
#   - Installs gitleaks if missing (brew | apt download | go install), else fails clearly.
#   - BLOCK tier (default): scans the repo; a high-confidence finding => exit 1 (CI red).
#   - WARN tier (SECRET_SCAN_WARN_CONFIG set): a second pass that only prints findings
#     and never fails — for surfacing low-confidence cases without blocking the pipeline.
#
# CONFIG / EXTEND
#   - Repo-root .gitleaks.toml is auto-detected by gitleaks (add [[rules]] / [allowlist]).
#   - SECRET_SCAN_CONFIG=path        -> explicit block-tier config (overrides .gitleaks.toml).
#   - SECRET_SCAN_WARN_CONFIG=path   -> enable the warn pass with this config.
#   - SECRET_SCAN_SCOPE=full|staged  -> "full" (default in CI) scans all history; "staged"
#                                       scans only staged changes (for a local/hook reuse).
#
# FALSE POSITIVES: inline `gitleaks:allow` comment, or an [allowlist] entry. Never paper
# over a real finding by deleting the step.
set -eu

GITLEAKS_VERSION="${GITLEAKS_VERSION:-8.30.1}"
SCOPE="${SECRET_SCAN_SCOPE:-full}"

log() { printf '%s\n' "secret-scan: $*" >&2; }

ensure_gitleaks() {
  if command -v gitleaks >/dev/null 2>&1; then return 0; fi
  log "gitleaks not found — attempting install (v$GITLEAKS_VERSION)."
  if command -v brew >/dev/null 2>&1; then
    brew install gitleaks && return 0
  fi
  if command -v go >/dev/null 2>&1; then
    GOBIN="${GOBIN:-$HOME/go/bin}" go install "github.com/gitleaks/gitleaks/v8@v$GITLEAKS_VERSION" \
      && export PATH="${GOBIN:-$HOME/go/bin}:$PATH" && return 0
  fi
  # Last resort: download the release tarball for linux amd64.
  if command -v curl >/dev/null 2>&1 && command -v tar >/dev/null 2>&1; then
    url="https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"
    tmp="$(mktemp -d)"
    if curl -fsSL "$url" -o "$tmp/gl.tgz" && tar -xzf "$tmp/gl.tgz" -C "$tmp" gitleaks; then
      install -m 0755 "$tmp/gitleaks" /usr/local/bin/gitleaks 2>/dev/null \
        || { mkdir -p "$HOME/.local/bin"; install -m 0755 "$tmp/gitleaks" "$HOME/.local/bin/gitleaks"; export PATH="$HOME/.local/bin:$PATH"; }
      rm -rf "$tmp"; return 0
    fi
    rm -rf "$tmp"
  fi
  log "could not install gitleaks automatically — install it and re-run: https://github.com/gitleaks/gitleaks"
  return 1
}

scan() { # $1 = optional config path
  cfg="${1:-}"
  set -- --redact --no-banner -v
  [ -n "$cfg" ] && set -- "$@" -c "$cfg"
  case "$SCOPE" in
    staged) gitleaks git --staged "$@" ;;
    *)      gitleaks git "$@" ;;          # full history
  esac
}

ensure_gitleaks

# --- WARN tier first (optional, never fails the build) ---
if [ -n "${SECRET_SCAN_WARN_CONFIG:-}" ]; then
  if ! scan "$SECRET_SCAN_WARN_CONFIG"; then
    log "WARNING — suspicious string(s) found (not failing the build). Review them above."
  fi
fi

# --- BLOCK tier (fails the build on a high-confidence finding) ---
if ! scan "${SECRET_SCAN_CONFIG:-}"; then
  log "BLOCKED — a high-confidence secret was found. Remove it, rotate it, and re-push."
  log "  false positive? add a 'gitleaks:allow' comment or an [allowlist] entry in .gitleaks.toml."
  exit 1
fi

log "clean — no high-confidence secrets found."
