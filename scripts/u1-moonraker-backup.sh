#!/usr/bin/env bash
# Read-only backup of a Snapmaker U1 over the Moonraker HTTP API (GET requests only).
# Safe while a print runs: it never POSTs, never writes to the printer, and paces requests.
#   scripts/u1-moonraker-backup.sh [HOST] [--logs] [--camera]
# Used before a firmware change: docs/printers/u1-paxx12-upgrade.md.
# Output: ~/.cache/u1-fw/backup-<UTC timestamp>-XXXX/ (0700) with config/, logs/ (opt), camera/ (opt),
# db/<namespace>.json, history.json, objects.json, server-*.json, and SHA256SUMS.
set -euo pipefail

host=192.168.0.26
logs=0
camera=0
for a in "$@"; do
  case "$a" in
    --logs) logs=1 ;;
    --camera) camera=1 ;;
    -*) echo "unknown option $a" >&2; exit 2 ;;
    *) host=$a ;;
  esac
done
base="http://$host:7125"
umask 077   # config, history, logs and camera frames are private to the caller
mkdir -p "$HOME/.cache/u1-fw"
# mktemp gives every run its own directory, so two runs in the same second never mix files.
out=$(mktemp -d "$HOME/.cache/u1-fw/backup-$(date -u +%Y%m%dT%H%M%SZ)-XXXX")
mkdir -p "$out"/{config,db}

# Names come from the printer over plain HTTP: refuse anything that could leave $out.
safe_rel() {
  case "/$1/" in
    *"/../"*|*"/./"*|//*) echo "unsafe path from printer: $1" >&2; exit 3 ;;
  esac
}

get() { curl -fsS -m 120 "$base$1"; sleep 0.3; }   # pacing: the printer may be printing
urlenc() { python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$1"; }

for ep in machine/system_info printer/info server/info server/config server/files/roots \
          server/database/list server/webcams/list server/history/totals; do
  get "/$ep" > "$out/$(echo "$ep" | tr / -).json"
done

# Every Klipper object (configfile carries the parsed printer.cfg incl. SAVE_CONFIG values).
objs=$(get /printer/objects/list | python3 -c 'import json,sys,urllib.parse
print("&".join(urllib.parse.quote(o) for o in json.load(sys.stdin)["result"]["objects"]))')
get "/printer/objects/query?$objs" > "$out/objects.json"

# Moonraker database namespaces (webcams, fluidd, moonraker, gcode_metadata, ...).
for ns in $(python3 -c 'import json,sys;print(" ".join(json.load(open(sys.argv[1]))["result"]["namespaces"]))' \
            "$out/server-database-list.json"); do
  case "$ns" in *[!A-Za-z0-9_.-]*|.|..) echo "unsafe namespace from printer: $ns" >&2; exit 3 ;; esac
  get "/server/database/item?namespace=$(urlenc "$ns")" > "$out/db/$ns.json"
done

# Print history (all jobs, paged).
python3 - "$base" > "$out/history.json" <<'PY'
import json, sys, time, urllib.request
base, jobs, start = sys.argv[1], [], 0
while True:
    with urllib.request.urlopen(f"{base}/server/history/list?limit=50&start={start}&order=asc", timeout=60) as r:
        page = json.load(r)["result"]["jobs"]
    jobs += page
    if len(page) < 50:
        break
    start += 50
    time.sleep(0.3)
json.dump({"count": len(jobs), "jobs": jobs}, sys.stdout, indent=1)
PY

dump_root() {   # $1 = Moonraker root
  local root=$1
  mkdir -p "$out/$root"
  get "/server/files/list?root=$root" > "$out/$root.list.json"
  python3 -c 'import json,sys;[print(f["path"]) for f in json.load(open(sys.argv[1]))["result"]]' \
    "$out/$root.list.json" | while IFS= read -r p; do
      safe_rel "$p"
      mkdir -p "$out/$root/$(dirname "$p")"
      get "/server/files/$root/$(urlenc "$p" | sed 's#%2F#/#g')" > "$out/$root/$p"
    done
}
dump_root config
[ "$logs" = 1 ] && dump_root logs
[ "$camera" = 1 ] && dump_root camera

sums=$(cd "$out" && find . -type f -print0 | sort -z | xargs -0 sha256sum)
printf '%s\n' "$sums" > "$out/SHA256SUMS"
echo "$out"
