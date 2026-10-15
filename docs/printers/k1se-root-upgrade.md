# Creality K1 SE: stock → root + Creality Helper Script (Moonraker, Fluidd, USB camera)

This runbook covers the K1 SE (`k1se` in `~/.config/3d-cli/printer-devices.yaml`, 192.168.0.27):
enabling root on the stock firmware, installing [Guilouz Creality Helper Script](https://github.com/Guilouz/Creality-Helper-Script)
for Moonraker, Fluidd and a USB webcam, then switching 3d-cli, Home Assistant and go2rtc over.
It also lists what to back up first, how to roll back, and what to check afterwards.

Prepared 2026-10-15. All printer facts below come from read-only requests made that day:
`GET /info` on :80, a `get ReqPrinterPara` on ws :9999, `GET` on :4408, and TCP connects.
`[INFERENCE]` marks claims that have not been checked on this printer yet.

**Do not start while the K1 prints.** On 2026-10-15 it was idle (`state 0`, `deviceState 0`,
bed clear).

## 1. Current state (stock)

| What | Value | Source |
|---|---|---|
| Firmware | **`1.3.5.22`** (`DWIN sw ver:1.3.5.22`), screen hw `CR4CU220812S11` | ws `ReqPrinterPara` → `modelVersion` |
| Model / host | `K1 SE`, hostname `K1SE-85B6`, MAC `FC:EE:28:06:85:B6` | `/info`, ws `hostname` |
| Stock web + ws | `:80` and `:9999` both answer `Server: libhv/1.3.3`, which is Creality's `web-server` process | response headers |
| Preinstalled by Creality | nginx 1.17.7 on `:4408` serving **Fluidd v1.34.1**; `:4409` gives 404. `/server/*` gives **502**, so Moonraker is not running. `:7125` is closed | `GET :4408/release_info.json`, `/server/info` |
| Other ports | `:8000` open (Creality camera/WebRTC service, empty 200); `:22` accepts TCP but sends no SSH banner (sshd not running before root `[INFERENCE]`); `:8080` closed | TCP connect |
| Camera | ws `video: 0`, `video1: 0`, so stock does not use the UVC webcam in the front USB port | ws `ReqPrinterPara` |
| Tuning now | `pressureAdvance 0.04`, `smoothTime 0.04`, `curZOffset 0.000`, `velocityLimits 600`, `accelerationLimits 20000` | ws `ReqPrinterPara` |
| Files on printer | 107 G-code files | ws `reqGcodeFile` → `retGcodeFileInfo2` |
| Clock | HTTP `Date` matches real UTC (TLS/git on the printer will work) | `:80` response header |

Stock config reference (mainboard): [CrealityOfficial K1_Series_Klipper `K1_SE_CR4CU220812S12`](https://github.com/CrealityOfficial/K1_Series_Klipper/tree/master/config/K1_SE_CR4CU220812S12).
It has `stepper_x` −5…229, `stepper_y` −0.5…227 (our `axis_limits`), `[adxl345]` +
`[resonance_tester]` (input shaper can be calibrated), and `[prtouch_v2] z_offset`.

**Firmware gate.** The Helper Script supports K1-series firmware **1.3.3.5 and later**, and
**Settings → Root account information** exists on 1.3.x. With `1.3.5.22` we qualify and do
not need to update or downgrade. Some 2025 K1C units ship without the root menu item ([HelixScreen K1C guide](https://helixscreen.org/guide/creality-k1c-setup/)).
If our screen does not have it, stop and report back. Do not try community exploits without
another OK.

## 2. Files already downloaded (`~/.cache/k1-root/`)

| Path | What |
|---|---|
| `Creality-Helper-Script/` | git clone, `main` @ `b46787a` (2025-05-21, last upstream push; the repo has no tagged releases) |
| `helper-script-b46787a.tar.gz` | `git archive` of that commit with prefix `helper-script/`, for the offline install (§4.2). SHA-256 `62206424b71d9015d72fc18d760c51bcc45dbbb73f2f2169643bb6bb787f2612` |
| `fluidd-v1.37.6.zip` | Fluidd latest (2026-09-23). The Helper Script downloads this from GitHub; this copy is for an offline printer. SHA-256 `9fe5bcd4a443f4cccd20cef222e70cc334a874070b21d116097a28405b0ed456` |
| `wiki/` | [Helper Script Wiki](https://guilouz.github.io/Creality-Helper-Script-Wiki/) source (2026-04-14) |
| `stock-baseline-20261014T233804Z/` | read-only snapshot from §3.1 |
| `SHA256SUMS` | checksums for the two archives |

**The printer needs internet for the install.** The Moonraker and Nginx builds ship inside the
Helper Script repo (`files/moonraker/{moonraker,nginx}.tar.gz`), but the Moonraker step then
runs `git checkout master; git pull` in `/usr/data/moonraker/moonraker` under `set -e`.
Fluidd/Mainsail come from GitHub releases; Entware and mjpg-streamer come from
`bin.entware.net` via opkg. The offline copies here only cover the clone and Fluidd (for a flaky
link or GitHub TLS trouble). They are not a fully offline path.

## 3. Backup

### 3.1 Read-only, before root (done; repeat right before install day)

The stock firmware has no API that returns `printer.cfg`. `:80/downloads/` lists only
`defData`, `humbnail`, `original`, `video`. What we can save now is the printer's own view of
its tuning, plus the file list:

```bash
cd ~/xp/3d-cli
D=~/.cache/k1-root/stock-baseline-$(date -u +%Y%m%dT%H%M%SZ); mkdir -p "$D"
curl -s http://192.168.0.27/info > "$D/info.json"
curl -s http://192.168.0.27:4408/release_info.json > "$D/fluidd-release_info.json"
uv run python - "$D" <<'EOF'
import sys, json, time; sys.path.insert(0, "lib")
from printer_ops.ws import WebSocket
with WebSocket.connect("192.168.0.27", 9999, timeout=5) as ws:
    for p in ("ReqPrinterPara", "reqGcodeFile"):
        ws.send_text(json.dumps({"method": "get", "params": {p: 1}}))
    out, t = {}, time.time()
    while time.time() - t < 8:
        m = ws.recv(1.0)
        if m:
            try: d = json.loads(m)
            except ValueError: continue
            if isinstance(d, dict): out.update(d)
with open(sys.argv[1] + "/ws-ReqPrinterPara.json", "w") as f:
    json.dump(out, f, indent=1, sort_keys=True)
EOF
(cd "$D" && sha256sum * > SHA256SUMS)
```

Both are `get` requests only. Baseline: `~/.cache/k1-root/stock-baseline-20261014T233804Z/`.

### 3.2 Full backup over SSH (right after root, before running the Helper Script)

The K1 runs dropbear, which has no `sftp-server`, so `scp` needs `-O` (legacy protocol).
`tar` over `ssh` works without it. Password: `creality_2023`.

```bash
( set -euo pipefail    # subshell: a failed step aborts the backup, not your shell
K=root@192.168.0.27
B=~/.cache/k1-root/backup-$(date -u +%Y%m%dT%H%M%SZ); mkdir -p "$B"
ssh $K 'cat /usr/data/printer_data/config/printer.cfg' > "$B/printer.cfg"
grep -q '^#\*# \[' "$B/printer.cfg"                     # SAVE_CONFIG block present (mesh/shaper/PID)
ssh $K 'cd /usr/data && ls -d printer_data/config creality/userdata 2>/dev/null' > "$B/paths.txt"
ssh $K "tar czf - -C /usr/data $(tr '\n' ' ' < "$B/paths.txt")" > "$B/usr-data-config.tgz"
tar tzf "$B/usr-data-config.tgz" > "$B/usr-data-config.list"   # fails on a broken archive
grep -qx 'printer_data/config/printer.cfg' "$B/usr-data-config.list"
grep -qx 'printer_data/config/gcode_macro.cfg' "$B/usr-data-config.list"
ssh $K 'ls -la /usr/data /usr/data/printer_data /etc/init.d; df -h; uname -a; cat /etc/os-release; v4l2-ctl --list-devices; ls /dev/video*' > "$B/inventory.txt" 2>&1 || true
(cd "$B" && sha256sum * > SHA256SUMS)
echo "backup OK: $B ($(wc -l < "$B/usr-data-config.list") entries; paths: $(tr '\n' ' ' < "$B/paths.txt"))"
)
```

Do not start §4.2 unless the last line prints `backup OK`. A stock K1 SE that finished its
first self-check has a `#*# SAVE_CONFIG` block. If the `grep` stops the backup there, look at
`printer.cfg` before going on. `paths.txt` records whether `creality/userdata` exists on this
firmware. If it is missing, the screen Z-offset lives elsewhere: note the value shown on the
screen by hand.

| Item | Why |
|---|---|
| `printer_data/config/printer.cfg` | the `#*# SAVE_CONFIG` block at the bottom holds **bed mesh**, **input shaper** (`[input_shaper]` freq/type) and the PID values |
| `printer_data/config/gcode_macro.cfg`, `printer_params.cfg`, `sensorless.cfg`, `factory_printer.cfg` | stock macros (`START_PRINT`, `END_PRINT`, `PAUSE`, our `trusted_macros`) and printer parameters |
| `creality/userdata/` | Creality UI state: screen settings and the screen's **Z-offset**, which is stored outside `printer.cfg` `[INFERENCE]` |
| `inventory.txt` | what Creality preinstalled (`/usr/data/moonraker`? `/usr/data/fluidd`? `/etc/init.d/S50nginx`?) and which `/dev/videoN` the webcam got; §4 depends on both |

If ssh fails with `no matching host key type`, add `-o HostKeyAlgorithms=+ssh-rsa
-o PubkeyAcceptedAlgorithms=+ssh-rsa` `[INFERENCE: old dropbear]`.

G-code files (`/usr/data/printer_data/gcodes`) are not backed up; re-upload them from `out/`.

After Moonraker is installed, the Helper Script menu **Backup & Restore → Backup Klipper
configuration files** writes `config/backup_config.tar.gz`, which can be restored the same way.

## 4. Install day

### 4.1 At the printer: enable root (user, ~2 min)

1. `3d printer status k1se` must show `idle`. Do not interrupt a print.
2. Touchscreen: **Settings** (gear) → **Root account information**.
3. Read the disclaimer, tick the agreement box, **wait 30 s** until **OK** becomes active, then press **OK**.
4. The next dialog says root is enabled and shows the login `root` / **`creality_2023`**. Press **OK**.
   If the dialog shows a different password, send a photo of it. Everything below assumes `creality_2023`.
5. Leave the webcam in the front USB port.

Root has to be enabled again after a full factory reset.

### 4.2 From the PC: back up, then install the Helper Script

1. `ssh root@192.168.0.27` (password `creality_2023`), then run the §3.2 backup. Dropbear takes
   ~28 s to send its banner, so set `ConnectTimeout` ≥ 60. Use one multiplexed connection
   (`ControlMaster=auto`, `ControlPersist`) so each later command does not wait again. Without
   `sshpass`, use `SSH_ASKPASS=<script echoing the password> SSH_ASKPASS_REQUIRE=force`.
2. Check that the printer has internet and the Creality preinstalls are in place:
   ```sh
   ping -c1 github.com && date
   ls -d /usr/data/moonraker /usr/data/nginx /usr/data/fluidd /usr/share/fluidd /etc/init.d/S50nginx /etc/init.d/S56moonraker_service 2>&1
   ```
3. Put the script in `/usr/data/helper-script` (the path is hard-coded in the script):
   - **Online:** `git clone --depth 1 https://github.com/Guilouz/Creality-Helper-Script.git /usr/data/helper-script`.
     The wiki suggests `git config --global http.sslVerify false` on an SSL error. **Don't do that:**
     the script runs as root and its Moonraker step `git pull`s again, so this would let anyone on
     the path swap the code. Check `date` first (a wrong clock is the usual cause). If TLS still
     fails, use the offline archive below; its SHA-256 is in §2.
   - **Offline:** `scp -O ~/.cache/k1-root/helper-script-b46787a.tar.gz root@192.168.0.27:/usr/data/`, check
     `sha256sum /usr/data/helper-script-b46787a.tar.gz` against §2, then
     `cd /usr/data && tar xzf helper-script-b46787a.tar.gz`. The script then has no `.git`, so it skips self-update.
4. `sh /usr/data/helper-script/helper.sh`. It detects the model through
   `get_sn_mac.sh model`, matching `K1`, and opens the **K1 SERIES** menus.
5. **Our factory image already contains a Helper install.** On 2026-10-15 the K1 SE had an old
   `/usr/data/helper-script` (`40887ce`, 2025-01-26), Moonraker, nginx, Fluidd 1.34.1, Entware
   (`/opt` → `/usr/data/opt`) and the `sudo`/`systemctl` shims, all from 2025-04-01. Moonraker was
   down because `printer_data/config/moonraker.conf` was missing (`moonraker.log`:
   `Configuration File Not Found`). The install menu refuses "already installed" components, so:
   update the script (`cd /usr/data/helper-script && git fetch origin main && git reset --hard origin/main`),
   then **Remove → Fluidd** and **Remove → Moonraker and Nginx**, then install fresh. The wiki says
   the same ("If you have already installed Moonraker, Fluidd or Mainsail provided by Creality,
   please uninstall them first"). Leave Entware in place.
   The wiki also recommends a factory reset before the first run. **We skip it:** a full reset
   wipes calibration, Wi-Fi and root.
6. The menus read their answers from stdin, so they can be scripted. Source the same files that
   `helper.sh` sources, then call the functions directly. We used `/usr/data/k1drive.sh`:
   ```sh
   #!/bin/sh
   set -e
   HELPER_SCRIPT_FOLDER=/usr/data/helper-script
   for s in "$HELPER_SCRIPT_FOLDER"/scripts/*.sh "$HELPER_SCRIPT_FOLDER"/scripts/menu/*.sh "$HELPER_SCRIPT_FOLDER"/scripts/menu/K1/*.sh; do . "$s"; done
   set_paths; set_permissions; model=K1
   for f in "$@"; do echo "=== $f"; "$f"; done
   ```
   Example: `printf 'y\ny\n' | /usr/data/k1drive.sh remove_fluidd remove_moonraker_nginx`. After
   that came `install_moonraker_nginx`, `install_fluidd`, `install_gcode_shell_command`,
   `install_usb_camera` and `install_save_zoffset_macros`.

Install, in this order (Install menu numbers as of `b46787a`):

| # | Component | Install? | Notes |
|---|---|---|---|
| 1 | **Moonraker and Nginx** | **yes** | Extracts the bundled tarballs, then `git pull`s Moonraker (internet). Moonraker on `:7125`, nginx `:4408` (Fluidd) / `:4409` (Mainsail), `/webcam/` → `127.0.0.1:8080`. `trusted_clients` includes `192.168.0.0/16`, so the LAN needs no API key. Also installs host-control shims (`sudo`, `systemctl`, `supervisorctl`) so Moonraker can reboot the machine. Does **not** touch Creality `web-server`. |
| 2 | **Fluidd (port 4408)** | **yes** | Same UI as on the U1. Needs GitHub. **Then run `chmod -R a+rX /usr/data/fluidd`.** The v1.37.6 zip unpacks as 0600/0700 and nginx (user `www-data`) answers 403 until you do. Offline: `scp -O ~/.cache/k1-root/fluidd-v1.37.6.zip root@…:/usr/data/`, then `mkdir -p /usr/data/fluidd && unzip /usr/data/fluidd-v1.37.6.zip -d /usr/data/fluidd && chmod -R a+rX /usr/data/fluidd && /etc/init.d/S50nginx restart` |
| 3 | Mainsail (4409) | no | one UI is enough |
| 4 | **Entware** | **yes** | prerequisite for the camera (`opkg`); needs internet. Already present on our image |
| 5 | **Klipper Gcode Shell Command** | **yes** | lets a macro restart the camera (§4.4) |
| 10 | Improved Shapers Calibrations | optional, later | the K1 SE has an ADXL345; nice-to-have graphs, not needed for us |
| 12 | **Save Z-Offset Macros** | **yes** (installed 2026-10-15) | Adds `[include Helper-Script/save-zoffset.cfg]` and renames `SET_GCODE_OFFSET` to `_SET_GCODE_OFFSET`, so offsets persist across reboots |
| 16 | Moonraker Timelapse | no | |
| 17 | Camera Settings Control | no | for Creality cameras only |
| 18 | **USB Camera Support** | **yes** | see §4.3 |
| 6–9, 11, 13–15, 19–24 | KAMP, buzzer, fans, macros, M600, Git backup, OctoEverywhere, Obico, … | no | out of scope; KAMP changes `START_PRINT` behaviour |

**Do NOT use** Customize → **Remove Creality Web Interface**. It renames `/usr/bin/web-server`
and `/usr/bin/Monitor` to `*.disabled`, kills them, and puts Fluidd on `:80`. `:80` and `:9999`
are both served by that `web-server` (same `Server: libhv/1.3.3` header). Removing it breaks the
3d-cli `creality-stock` adapter, HA `ha_creality_ws` and Creality Print uploads. Also **do not
install Guppy Screen** (it stops the same stock stack). Both are the only Helper scripts that
touch `web-server` (`grep web-server scripts/*.sh`), so with the selection above **ws :9999 stays up**.

### 4.3 USB camera (mjpg-streamer)

On the K1 model the Helper Script copies `files/services/S50usb_camera-dual` to
`/etc/init.d/S50usb_camera` without asking for a resolution. That file **hard-codes
`/dev/video4`, 1280×720, 15 fps, `:8080`** (and `/dev/video6` → `:8081` for a second camera).
Then it runs `opkg install mjpg-streamer mjpg-streamer-input-http mjpg-streamer-input-uvc mjpg-streamer-output-http mjpg-streamer-www`.
**As of 2026-10-15 that fails** with `Unknown package 'mjpg-streamer'`: the packages are gone from
the `mipselsf-k3.4` index, but the `.ipk` files are still on the server. Install them by file;
opkg resolves the dependencies (`libjpeg-turbo`, `libv4l`, `libiconv-full`) from the index:
```sh
cd /tmp && U=http://bin.entware.net/mipselsf-k3.4
for p in mjpg-streamer mjpg-streamer-input-uvc mjpg-streamer-input-http mjpg-streamer-output-http mjpg-streamer-www; do
  wget -q "$U/${p}_1.0.0-6_mipsel-3.4.ipk"; done
/opt/bin/opkg install /tmp/mjpg-streamer*_1.0.0-6_mipsel-3.4.ipk && rm -f /tmp/mjpg-streamer*.ipk
/etc/init.d/S50usb_camera start
```
camera-streamer is not offered and has no mipsel build, so mjpg-streamer is the option here.
Our webcam enumerates as `GENERAL WEBCAM` on `/dev/video4` and offers MJPG up to 2560×1440,
so the defaults (`/dev/video4`, 1280×720, 15 fps) work unchanged.

After the install, check it against `inventory.txt`:

```sh
v4l2-ctl --list-devices                     # which /dev/videoN is the UVC webcam?
v4l2-ctl -d /dev/videoN --list-formats-ext  # MJPG offered? which sizes?
```

Edit `/etc/init.d/S50usb_camera` if any of these apply:
- the webcam is not `/dev/video4`: change `-d`;
- it has no `MJPG`, only `YUYV`: add `-y` to `input_uvc.so`. Raw YUYV costs CPU on the
  MIPS SoC, so also drop to `-r 640x480 -f 10`;
- 720p stutters or loads the CPU: use `-r 640x480`. The script itself warns about 1080p.

Then `/etc/init.d/S50usb_camera restart` and check `http://192.168.0.27:8080/?action=snapshot`
and `http://192.168.0.27:4408/webcam/?action=stream`.

Register it with Moonraker so Fluidd shows it: uncomment the `[webcam Camera]` block in
`/usr/data/printer_data/config/moonraker.conf` and set
`stream_url: http://192.168.0.27:8080/?action=stream` and
`snapshot_url: http://192.168.0.27:8080/?action=snapshot` (we also set `aspect_ratio: 16:9` for
1280×720), then `/etc/init.d/S56moonraker_service restart`. The factory image's Moonraker
database had a stale `K1 SE Camera` entry pointing at `192.168.0.34`. Check
`GET /server/webcams/list` and remove leftovers with `DELETE /server/webcams/item?uid=<uid>`.

### 4.4 Remote reboot and camera restart

- Klipper restart: `POST http://192.168.0.27:7125/printer/firmware_restart` (or Fluidd).
- Whole printer: `POST /machine/reboot` through the shims installed in step 1 `[INFERENCE: verify once while idle]`.
- Camera: put this in `printer_data/config/camera-control.cfg` and add `[include camera-control.cfg]`
  after the other includes in `printer.cfg` (needs Gcode Shell Command). Then `POST /printer/restart`:
  ```ini
  [gcode_shell_command restart_camera]
  command: /etc/init.d/S50usb_camera restart
  timeout: 15
  verbose: False

  [gcode_macro RESTART_CAMERA]
  gcode:
    RUN_SHELL_COMMAND CMD=restart_camera
  ```
  Call it with `POST /printer/gcode/script?script=RESTART_CAMERA`, from an HA button, or by
  `ssh root@192.168.0.27 /etc/init.d/S50usb_camera restart`. Verified 2026-10-15: `{"result":"ok"}`,
  stream back after 2 s.

## 5. After install: 3d-cli, Home Assistant, go2rtc, watcher

**3d-cli.** `k1se` lives in the repo's `fleet/printers.yaml` (since 3d 0.14.1). Change only
the connection there (`adapter: moonraker`, `port: 7125`). Everything else stays: `axis_limits`,
`trusted_macros`, `watch`, `heads`, …

Until that PR is merged, use a **temporary full-entry override** in
`~/.config/3d-cli/printer-devices.yaml`. An entry there *replaces* the fleet's device settings
(model and access are kept), so copy every device key, not just the changed ones:

```yaml
k1se:
  model: Creality K1 SE
  adapter: moonraker
  host: 192.168.0.27
  port: 7125
  axis_limits: {x: [-5, 229], y: [-0.5, 227]}
  trusted_macros: [START_PRINT, END_PRINT, PAUSE]
  watch:
    tg_fallback: true
```

Delete the override as soon as the fleet change is on `main`.

Klipper reports the same limits (checked 2026-10-15):
`curl -s 'http://192.168.0.27:7125/printer/objects/query?configfile=settings' | jq '.result.status.configfile.settings|{stepper_x,stepper_y}|map_values({position_min,position_max})'`
→ X −5…229, Y −0.5…227. The stock path keeps working, so a rollback is just
`adapter: creality-stock` again with `port` removed. `trusted_macros` needs nothing new: none
of the installed components puts a macro into our G-code. Save Z-Offset only wraps
`SET_GCODE_OFFSET`, a Klipper command.

**Home Assistant.** **Keep `ha_creality_ws`**: ws :9999 stays (§4.2), so every
`*.k1se_85b6_*` entity and the `3d_printers.yaml` package keep working unchanged. Moonraker
does add something HA lacks today: a real e-stop. `3d_printers.yaml` notes that
`ha_creality_ws` "exposes no true e-stop". Add a `rest_command` for
`POST http://192.168.0.27:7125/printer/emergency_stop`, plus `RESTART_CAMERA`, following the
U1 `rest_command.printer_u1_*` pattern. Moving all K1 entities to a Moonraker integration
would rename them and break the dashboards, for no gain while ws :9999 lives.

**go2rtc.** `integrations/homeassistant/go2rtc/go2rtc.yaml` (on the HA-move branch)
already lists `http://192.168.0.27:8080/?action=stream` first, so it works once the camera
runs. Its fallback `http://192.168.0.27/webcam/?action=stream` points at `:80`, which stays
Creality `web-server` and has no `/webcam/`. Change it to
`http://192.168.0.27:4408/webcam/?action=stream`. To match `u1_camera`, an H.264 variant is
`ffmpeg:http://192.168.0.27:8080/?action=stream#video=h264`.

**Watcher.** `systemctl --user stop 3d-printer-watch@k1se` before root/install (reboots page
otherwise). After the yaml switch: `systemctl --user restart 3d-printer-watch@k1se`. The unit
runs `3d printer watch k1se $WATCH_ARGS` and picks up the new adapter from the yaml.

## 6. Risks, rollback, warranty

| Risk | Mitigation / rollback |
|---|---|
| Creality's root disclaimer: Creality does not cover problems caused by root use; treat it as **voiding warranty support** for software issues `[INFERENCE: exact wording on our screen]` | User approved rooting 2026-10-15. A full reset (below) disables root again (the wiki says root must be re-enabled after a factory reset) |
| Helper install fails halfway | Per-component **Remove menu** in the Helper Script restores its changes. Nothing above edits `web-server` |
| Camera service loads the CPU during prints | `/etc/init.d/S50usb_camera stop`, or lower resolution/fps (§4.3) |
| `S50usb_camera stop` also does `killall cam_app cx_ai_middleware` (Creality camera/AI daemons) | Harmless here: the K1 SE has no Creality camera and stock never saw the webcam (`video: 0`) |
| Screen **Settings → Factory reset** | Only a **partial** reset per the wiki: it does not remove Helper Script components. Use the full reset below for a clean stock state |
| Full rollback to stock | `ssh root@192.168.0.27 'echo "all" | nc -U /var/run/wipe.sock'` wipes `/usr/data` to factory, which also wipes calibration, Wi-Fi and root. Or, with the Helper installed, `/etc/init.d/S58factoryreset reset`, which keeps preferences and network. Then reconnect Wi-Fi on the screen, re-run **Self-check**, and restore calibration from the §3.2 backup |
| Bricked boot | Wiki `firmwares/firmware-recovery-k1.md` (USB `.img` reflash) |
| IP change | Reserve `FC:EE:28:06:85:B6 → 192.168.0.27` in the router; 3d-cli, HA and go2rtc all use the IP |

## 7. Verification checklist

- [ ] Screen shows root enabled; `ssh root@192.168.0.27` works; §3.2 backup has `printer.cfg` with a `SAVE_CONFIG` block and `SHA256SUMS`
- [ ] `curl -s http://192.168.0.27:7125/server/info | jq .result.klippy_state` → `"ready"`
- [ ] `http://192.168.0.27:4408` loads Fluidd, connects, shows temperatures; Fluidd version ≥ 1.37
- [ ] **Stock stack still up:** `curl -s http://192.168.0.27/info` returns the JSON from §1, and a ws `ReqPrinterPara` still answers (`3d printer status k1se` with `adapter: creality-stock`, before the yaml switch)
- [ ] HA `sensor.k1se_85b6_nozzle_temperature` still updates
- [ ] Camera: `curl -so /tmp/k1.jpg -w '%{http_code} %{content_type}\n' 'http://192.168.0.27:8080/?action=snapshot'` → `200 image/jpeg`; the `/webcam/` path on `:4408` works too; Fluidd shows the camera
- [ ] go2rtc `k1se_camera` plays in HA
- [ ] `RESTART_CAMERA` brings the stream back within ~5 s
- [ ] After the yaml switch: `3d printer status k1se` → `moonraker  192.168.0.27:7125`, state `standby`, temperatures plausible; file count from `/server/files/list` matches the 107 from the baseline `[INFERENCE: Moonraker's gcodes root is the stock upload dir]`
- [ ] `3d print-preflight` on a K1 G-code reports limits `x[-5,229] y[-0.5,227]` from `printer-devices.yaml`
- [ ] Test print: `3d printer upload k1se out/<small>.gcode`. Then check that the Z-offset on the screen equals the backup value. Only then `3d printer start k1se out/<small>.gcode --yes`, and check the first-layer height
- [ ] `systemctl --user status 3d-printer-watch@k1se` is active, no error events
- [ ] Reboot the printer once (power switch): Moonraker, nginx, camera and ws :9999 all come back without manual steps
