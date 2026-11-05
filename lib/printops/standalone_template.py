"""standalone_template.py — the bash text of the installer `3d init --env --spoolman script` writes.

WHAT: TEMPLATE is the whole script with three placeholders (@GENERATED_BY@, @PAYLOAD_SHA256@,
  @PAYLOAD@) that printops.standalone_script fills in. It is DATA for a user-facing bash artifact
  (copied to a server that has no 3d-cli), kept as a string because the repo's tools are Python;
  tests/test_printops_standalone_script.py runs shellcheck and `bash -n` on every generated script.
WHY a template and not generated shell: the logic (preflight, idempotent unpack, compose env, health
  waits, token mint, backup timer) is long enough that it must stay readable as bash.
"""
from __future__ import annotations

TEMPLATE = r'''#!/usr/bin/env bash
# PrintOps standalone installer: Spoolman + 3d-api in docker compose, on THIS machine.
#
# Written by `3d init --env --spoolman script` (@GENERATED_BY@): it carries the docker build context
# of Spoolman + 3d-api as a gzipped tar and needs nothing from 3d-cli: bash, docker with the
# compose plugin, tar, gzip, base64 and sha256sum (or shasum). Copy it to any server and run it as
# the user that should own the data; it is safe to re-run (every step checks before it changes
# anything).
#
#   ./spoolman-setup.sh              install / update, print the URLs and the token
#   ./spoolman-setup.sh --dry-run    check the prerequisites and print the plan, change nothing
#   ./spoolman-setup.sh --bind ADDR  listen on this host address only (same as PRINTOPS_BIND_ADDR)
#
# Environment (all optional):
#   PRINTOPS_DIR                  base dir (default ~/printops; data in DIR/data; a relative path
#                                 is taken from the directory you run this in)
#   PRINTOPS_SPOOLMAN_PORT        Spoolman port on the host (default 7912)
#   PRINTOPS_API_PORT             3d-api port on the host (default 8765)
#   PRINTOPS_BIND_ADDR            host address Spoolman and 3d-api listen on, an IP address
#                                 (default @BIND_ADDR@; 0.0.0.0 = every interface, 127.0.0.1 = this
#                                 host only: then other machines and printers cannot reach it)
#   PRINTOPS_CONTAINER_PREFIX     compose project + container name prefix (default printops)
#   PRINTOPS_TZ                   container time zone (default: this host's)
#   PRINTOPS_SPOOLMAN_CORS_ORIGIN Spoolman's allowed browser origins, comma-separated full origins
#                                 (default: Spoolman's own address only; list your Fluidd/Mainsail
#                                 origins here, never *: Spoolman has no authentication)
#   PRINTOPS_HOST                 address shown in the printed URLs (default: first host IP)
#   PRINTOPS_BACKUP_TIMER=0       do not install the daily backup timer (systemd --user)
#   SPOOLMAN_ONLY=1               start only Spoolman (no 3d-api image build, no token)
#   TOKEN_NAME                    name of the 3d-api token to mint (default cli-<hostname>)
#   WAIT_SECONDS                  how long to wait for the services to become healthy (default 300)
#
# Spoolman has no authentication: keep its port inside your LAN/VPN, never forward it; on a server
# that is reachable from the internet give --bind the one LAN/VPN address (docker's iptables rules
# bypass ufw/firewalld).
set -euo pipefail

GENERATED_BY="@GENERATED_BY@"
PAYLOAD_SHA256="@PAYLOAD_SHA256@"

DIR="${PRINTOPS_DIR:-$HOME/printops}"
# docker compose resolves bind sources against the compose file's directory, so a relative DIR
# would be created here but mounted from inside the unpacked build context
case "$DIR" in /*) ;; *) DIR="$PWD/${DIR#./}" ;; esac
PREFIX="${PRINTOPS_CONTAINER_PREFIX:-printops}"
SPOOLMAN_PORT="${PRINTOPS_SPOOLMAN_PORT:-7912}"
API_PORT="${PRINTOPS_API_PORT:-8765}"
SPOOLMAN_ONLY="${SPOOLMAN_ONLY:-0}"
WAIT_SECONDS="${WAIT_SECONDS:-300}"
COMPOSE_FILE="services/3d-api/compose.yaml"

log() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 is required: $2"; }

# normalize_bind ADDR: an IPv4/IPv6 literal in the form compose's port syntax wants (IPv6 bracketed)
normalize_bind() {
  local a="${1#\[}"
  a="${a%\]}"
  if [[ "$a" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then printf '%s' "$a"
  elif [[ "$a" == *:* && "$a" =~ ^[0-9a-fA-F:.]+$ ]]; then printf '[%s]' "$a"
  else die "--bind / PRINTOPS_BIND_ADDR must be an IP address (127.0.0.1, 192.168.0.20, 0.0.0.0, ::1), got '$1'"
  fi
}
BIND_ADDR="$(normalize_bind "${PRINTOPS_BIND_ADDR:-@BIND_ADDR@}")"

wildcard_bind() { [ "$BIND_ADDR" = 0.0.0.0 ] || [ "$BIND_ADDR" = "[::]" ]; }
# local_host: where this host's own health probes reach the services (a wildcard bind answers on loopback)
local_host() { if wildcard_bind; then printf 127.0.0.1; else printf '%s' "$BIND_ADDR"; fi; }

host_name() { hostname -s 2>/dev/null | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9._\n-' '-'; }

host_address() {
  local ip
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')" || ip=""
  if [ -z "${PRINTOPS_HOST:-}" ] && ! wildcard_bind; then ip="$BIND_ADDR"; fi
  printf '%s' "${PRINTOPS_HOST:-${ip:-$(hostname)}}"
}

detect_tz() {
  local tz
  tz="$(timedatectl show -p Timezone --value 2>/dev/null)" || tz=""
  [ -n "$tz" ] || tz="$(cat /etc/timezone 2>/dev/null)" || tz=""
  printf '%s' "${tz:-UTC}"
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  else shasum -a 256 "$1" | awk '{print $1}'
  fi
}

http_get() {
  if command -v curl >/dev/null 2>&1; then curl -fsS --max-time 5 "$1"
  elif command -v wget >/dev/null 2>&1; then wget -qO- --timeout=5 "$1"
  else python3 -c 'import sys, urllib.request; sys.stdout.write(urllib.request.urlopen(sys.argv[1], timeout=5).read().decode())' "$1"
  fi
}

# top_level_json BODY: the body's outermost object with every nested object removed, so a pattern
# sees only top-level keys ({"status":"degraded","spoolman":{"status":"ok"}} is not "ok")
top_level_json() {
  local s="${1//$'\n'/ }" prev=""
  s="${s#*\{}"
  s="${s%\}*}"
  while [ "$s" != "$prev" ]; do
    prev="$s"
    s="$(sed -E 's/\{[^{}]*\}//g' <<<"$s")"
  done
  printf '%s' "$s"
}

# wait_for NAME URL ERE: poll URL until its top-level JSON keys match the ERE
wait_for() {
  local name="$1" url="$2" pattern="$3" deadline body
  deadline=$((SECONDS + WAIT_SECONDS))
  while (( SECONDS < deadline )); do
    if body="$(http_get "$url" 2>/dev/null)" && grep -Eq "$pattern" <<<"$(top_level_json "$body")"; then
      log "$name is healthy: $url"
      return 0
    fi
    sleep 3
  done
  die "$name did not become healthy at $url within ${WAIT_SECONDS}s; look at: docker logs --tail 80 ${PREFIX}-spoolman ${PREFIX}-api"
}

preflight() {
  need docker "install Docker Engine with the compose plugin: https://docs.docker.com/engine/install/"
  docker compose version >/dev/null 2>&1 || die "the docker compose plugin is missing: https://docs.docker.com/compose/install/linux/"
  docker info >/dev/null 2>&1 || die "cannot talk to the docker daemon: start it, and add this user to the docker group (or run with sudo)"
  need tar "install tar"
  need gzip "install gzip"
  need base64 "install coreutils"
  command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 || die "sha256sum (or shasum) is required"
  if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1 && ! command -v python3 >/dev/null 2>&1; then
    die "curl, wget or python3 is required to check the services' health"
  fi
}

# src_state: what $DIR/src is right now: missing | empty | ours (carries this installer's marker) |
# foreign (somebody's own files: PRINTOPS_DIR=$HOME would otherwise make ~/src ours to delete)
src_state() {
  local src="$DIR/src"
  if [ ! -e "$src" ] && [ ! -L "$src" ]; then printf missing
  elif [ -f "$src/.payload-sha256" ]; then printf ours
  elif [ -d "$src" ] && [ -z "$(find "$src" -mindepth 1 -maxdepth 1 -print -quit)" ]; then printf empty
  else printf foreign
  fi
}

guard_src() {
  [ "$(src_state)" != foreign ] ||
    die "refusing to replace $DIR/src: it exists, is not empty and was not created by this installer (no .payload-sha256 marker); pick another base dir (PRINTOPS_DIR=...) or move it away"
}

extract_payload() {
  local src="$DIR/src" tmp="$DIR/payload.tgz.tmp" stage
  guard_src
  if [ -f "$src/.payload-sha256" ] && [ "$(cat "$src/.payload-sha256")" = "$PAYLOAD_SHA256" ]; then
    log "build context already unpacked ($src)"
    return 0
  fi
  log "unpacking the build context to $src"
  payload | base64 -d >"$tmp"
  [ "$(sha256_of "$tmp")" = "$PAYLOAD_SHA256" ] || { rm -f "$tmp"; die "the embedded payload is corrupt (was this script truncated while copying?)"; }
  # unpack beside src and rename: an interrupted unpack never leaves a half-filled src behind
  stage="$(mktemp -d "$DIR/src.new.XXXXXX")"
  tar -xzf "$tmp" -C "$stage" || { rm -rf "${stage:?}" "$tmp"; die "could not unpack the build context"; }
  rm -f "$tmp"
  printf '%s\n' "$PAYLOAD_SHA256" >"$stage/.payload-sha256"
  chmod 755 "$stage"
  rm -rf "${src:?}"
  mv "$stage" "$src"
}

compose() {
  (
    cd "$DIR/src"
    PRINTOPS_VERSION="$GENERATED_BY" \
    PRINTOPS_DATA_DIR="$DIR/data" \
    PRINTOPS_BACKUP_DIR="$DIR/backup" \
    PRINTOPS_GCODE_HOST_DIR="$DIR/gcodes" \
    PRINTOPS_BIND_ADDR="$BIND_ADDR" \
    PRINTOPS_CONTAINER_PREFIX="$PREFIX" \
    PRINTOPS_SPOOLMAN_PORT="$SPOOLMAN_PORT" \
    PRINTOPS_API_PORT="$API_PORT" \
    PRINTOPS_UID="$(id -u)" \
    PRINTOPS_GID="$(id -g)" \
    PRINTOPS_TZ="${PRINTOPS_TZ:-$(detect_tz)}" \
    PRINTOPS_SPOOLMAN_CORS_ORIGIN="${PRINTOPS_SPOOLMAN_CORS_ORIGIN:-http://$(host_address):$SPOOLMAN_PORT}" \
      docker compose -p "$PREFIX" -f "$COMPOSE_FILE" "$@"
  )
}

# The api container's token store is a file; a token is shown once, so re-runs never re-mint.
mint_token() {
  local name="$1" existing
  existing="$(docker exec "${PREFIX}-api" 3d printops token list 2>/dev/null || true)"
  if NAME="$name" awk '$1 == ENVIRON["NAME"] { found = 1 } END { exit !found }' <<<"$existing"; then
    TOKEN=""
    return 0
  fi
  TOKEN="$(docker exec "${PREFIX}-api" 3d printops token create "$name" --scopes read,write 2>/dev/null)" ||
    die "could not mint a token: docker exec ${PREFIX}-api 3d printops token create $name --scopes read,write"
}

# Every command carries `|| return 1`: the caller runs this under `if !`, where errexit is off, and a
# failure here must neither stop the script before it prints the one-time token nor go unnoticed.
install_backup_timer() {
  [ "${PRINTOPS_BACKUP_TIMER:-1}" = 1 ] || return 0
  if ! command -v systemctl >/dev/null 2>&1 || ! systemctl --user show-environment >/dev/null 2>&1; then
    log "no systemd --user here: skipping the daily backup timer (by hand: docker exec ${PREFIX}-api 3d printops backup)"
    return 0
  fi
  local unit_dir="$HOME/.config/systemd/user" docker_bin
  docker_bin="$(command -v docker)" || return 1
  mkdir -p "$unit_dir" || return 1
  cat >"$unit_dir/${PREFIX}-backup.service" <<EOF || return 1
[Unit]
Description=PrintOps: snapshot the PrintOps + Spoolman SQLite DBs into $DIR/backup

[Service]
Type=oneshot
ExecStart=$docker_bin exec ${PREFIX}-api 3d printops backup
EOF
  cat >"$unit_dir/${PREFIX}-backup.timer" <<EOF || return 1
[Unit]
Description=Daily PrintOps DB snapshot

[Timer]
OnCalendar=*-*-* 04:15:00
Persistent=true
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
EOF
  systemctl --user daemon-reload || return 1
  systemctl --user enable --now "${PREFIX}-backup.timer" >/dev/null || return 1
  log "daily backup timer installed (${PREFIX}-backup.timer)"
  warn_if_no_linger
}

# A systemd --user manager (and so the timer) stops when the last session of the user ends, unless
# lingering is on: on a headless server the nightly backup would silently never run.
warn_if_no_linger() {
  local user linger
  user="$(id -un)"
  linger="$(loginctl show-user "$user" -p Linger 2>/dev/null)" || linger=""
  [ "$linger" = "Linger=yes" ] ||
    log "warning: lingering is off for $user, so the backup timer only runs while you are logged in; enable it: sudo loginctl enable-linger $user"
}

summary() {
  local addr token_name="$1" token="$2"
  addr="$(host_address)"
  printf '\n'
  printf 'Spoolman  http://%s:%s\n' "$addr" "$SPOOLMAN_PORT"
  if wildcard_bind; then
    printf '\nSpoolman has no authentication and listens on every interface of this host. Unless it sits behind a\nrouter/VPN you trust, restrict it: %s --bind ADDR (the one LAN/VPN address; machines and printers that must\nreach it need that address).\n' "$0"
  fi
  if [ "$SPOOLMAN_ONLY" = 1 ]; then
    printf '\nSpoolman only (SPOOLMAN_ONLY=1): 3d printops needs the 3d-api; re-run without SPOOLMAN_ONLY.\n'
    return 0
  fi
  printf '3d-api    http://%s:%s\n' "$addr" "$API_PORT"
  printf '\nOn the machine that runs 3d:\n'
  if [ -n "$token" ]; then
    printf '  3d init --env --no-input --api http://%s:%s --token %s\n' "$addr" "$API_PORT" "$token"
    printf '\nor put this in ~/.config/3d-cli/printops.yaml (mode 600):\n'
    printf '  api: http://%s:%s\n  token: %s\n' "$addr" "$API_PORT" "$token"
    printf '\nThe token is shown only now (the server keeps its sha256).\n'
  else
    printf '  the token "%s" already exists and is never shown again; mint another:\n' "$token_name"
    printf '  TOKEN_NAME=%s-2 %s\n' "$token_name" "$0"
  fi
}

main() {
  # NAME_RE allows 64 characters: "cli-" + at most 60 of the host name (a longer name failed after the stack was up)
  local token_name="${TOKEN_NAME:-cli-$(host_name | cut -c1-60)}" dry=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --dry-run) dry=1 ;;
      --bind)
        [ $# -ge 2 ] || die "--bind needs an IP address"
        BIND_ADDR="$(normalize_bind "$2")"
        shift
        ;;
      -h | --help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; return 0 ;;
      *) die "unknown argument $1 (accepted: --dry-run, --bind ADDR, --help)" ;;
    esac
    shift
  done
  log "PrintOps standalone installer ($GENERATED_BY)"
  preflight
  log "base dir $DIR, project $PREFIX, Spoolman :$SPOOLMAN_PORT, 3d-api :$API_PORT, listening on $BIND_ADDR$([ "$SPOOLMAN_ONLY" = 1 ] && printf ' (Spoolman only)')"
  guard_src # a real run would refuse too: say so before creating anything
  if [ "$dry" = 1 ]; then
    log "dry run: $DIR/src is $(src_state); would unpack the build context there, run docker compose up, wait for health and mint token $token_name; nothing changed"
    return 0
  fi
  mkdir -p "$DIR/data/api" "$DIR/data/spoolman" "$DIR/backup" "$DIR/gcodes"
  extract_payload
  if [ "$SPOOLMAN_ONLY" = 1 ]; then
    compose up -d spoolman
  else
    compose up -d --build --remove-orphans
  fi
  wait_for Spoolman "http://$(local_host):${SPOOLMAN_PORT}/api/v1/health" '"status": *"healthy"'
  local token=""
  if [ "$SPOOLMAN_ONLY" != 1 ]; then
    wait_for 3d-api "http://$(local_host):${API_PORT}/v1/health" '"status": *"ok"'
    mint_token "$token_name"
    token="$TOKEN"
    if ! install_backup_timer; then
      log "warning: the daily backup timer was not installed; the stack and the token are fine (by hand: docker exec ${PREFIX}-api 3d printops backup)"
    fi
  fi
  summary "$token_name" "$token"
}

payload() {
  cat <<'PRINTOPS_PAYLOAD'
@PAYLOAD@
PRINTOPS_PAYLOAD
}

main "$@"
'''
