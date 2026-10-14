# Snapmaker U1: stock firmware → paxx12 Extended Firmware

How we move the U1 (`u1` in `~/.config/3d-cli/printer-devices.yaml`, 192.168.0.26) from Snapmaker
stock firmware to [paxx12 SnapmakerU1-Extended-Firmware](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware),
what to back up first, how to roll back, and what to check afterwards.

Prepared 2026-10-14. Every printer fact below came from read-only Moonraker `GET`s on that date.
`[INFERENCE]` marks claims nobody has checked on this printer yet.

**Do not start while the U1 prints.** On 2026-10-14 `print_stats.state` was `printing`
(`u1_walls_main_v3-…gcode`, layer 71/1193). The install reboots the printer and flashes the
toolhead MCUs.

## 1. Current state (stock)

| What | Value | Source |
|---|---|---|
| Firmware | `1.4.1`, Klipper `1.4.1.6_20260608141446` | `/machine/system_info` `product_info`, `/printer/info` |
| Moonraker | `1.4.1`, API `1.4.0` | `/server/info` |
| OS | Buildroot 2024.02, kernel 6.1.99, aarch64, 1 GB RAM | `/machine/system_info` |
| Network | `wlan0` only, MAC `40:d9:5a:e3:18:c8`, mDNS `U1`, hostname `lava` | `/machine/system_info`, `/server/config` |
| Serial | `8110026040210241WI29` | `product_info.serial_number` |
| Moonraker auth | no login; `trusted_clients` includes `192.0.0.0/8`, so the LAN needs no API key | `config/moonraker.conf` |
| File roots | `config` (r) `/oem/printer_data/config`, `logs` (r), `gcodes` (rw) `/userdata/gcodes`, `camera` (r) | `/server/files/roots` |
| DB namespaces | `webcams`, `announcements`, `gcode_metadata`, `moonraker`, `fluidd` | `/server/database/list` |
| History | 88 jobs, ~331 h of printing | `/server/history/totals` |
| Webcam entry | `U1 Camera` → HA ingress `/api/ingress/u1_printer/u1-camera/u1.mjpeg` (our u1-camera-proxy) | `/server/webcams/list` |
| `printer.cfg` | identical to the stock backup `printer-20260823_103042.cfg`, includes only `fluidd.cfg` and `xyz_offset_calibration.cfg`; nothing of ours to migrate | baseline backup |
| Defect detection | `main_enable: true`, clean_bed + noodle on (sensitivity low), residue/nozzle off | `config/snapmaker/defect_detection.json` |
| Input shaper | X mzv 50.4 Hz, Y mzv 48.4 Hz | `config/snapmaker/input_shaper.json` |

## 2. Which release

**Use the stable `v1.6.0-paxx12-22`** (GitHub "Latest", 2026-09-07). It is built on Snapmaker
stock `1.6.0` (`U1_1.6.0.267_20260815150420_upgrade.bin`, pinned in the release's `vars.mk`).

Not the rolling `v2.0.0-paxx12-develop-*`. That build sits on stock `2.0.0.205`, is overwritten
on every push and is labelled by the project itself as "not on a printer you depend on". The
develop line also ships a separate `-afc` image; the stable release has only the one `.bin`.

Compatibility with our `1.4.1`:

- The `.bin` is a full firmware image installed through the stock **Local Update** screen, so it
  does not care which stock version runs now. From `1.4.1` the install is also a stock
  `1.4.1 → 1.6.0` jump: the base OS, Klipper/Moonraker and the toolhead/main MCU firmware all change.
- paxx12 once built on exactly our base (`v1.4.1-paxx12-20` pins `U1_1.4.1.6_20260608141446`, the
  same Klipper build string as ours). That release is outdated (no SpoolLink fixes, no `CARD_EVENT_TIME`);
  do not use it.
- Snapmaker's release notes say firmware upgrades keep calibration and leveling data. paxx12's
  `data_persistence.md` says `/home/lava/printer_data` always survives. See §6.
- Later paxx12 releases install from the printer's own `http://192.168.0.26/firmware-config/`
  (upload or URL), so only the first install needs a USB stick.

## 3. Files already downloaded (`~/.cache/u1-fw/`)

All checksums verified with `sha256sum -c` on 2026-10-14.

| File | Size | SHA-256 | Use |
|---|---|---|---|
| `v1.6.0-paxx12-22/U1_extended_1.6.0-paxx12-22_upgrade.bin` | 247 180 032 | `eea907c22847b6f165640f0ae5e835ca42181f7a39b722ff062fd138d47a689c` (MD5 `a3f9cc368cf2d1c76bace3fa8a18ae19`, both match the release notes) | install |
| `stock/U1_1.6.0.267_20260815150420_upgrade.bin` | 254 649 088 | `9f446819e51a2e7a2f0516fde0c025f2b1cd9c23396301e0f5c45e7859cd8af4` | rollback, same base as the paxx12 build |
| `stock/U1_1.4.1.6_20260608141446_upgrade.bin` | 221 457 480 | `cb3d69d4e612887fb8a3234489041e7eb75ab1525744fa92abc813f31e18d3dd` | exact current stock, last-resort rollback |

Each directory has a `SHA256SUMS`; the paxx12 directory also has `MD5SUMS` and
`RELEASE_NOTES.md`. Stock images come from `https://public.resource.snapmaker.com/firmware/U1/<file>`,
the URL and SHA-256 that paxx12's own `Makefile` and `vars.mk` use.

## 4. Backup (read-only, before install day)

`scripts/u1-moonraker-backup.sh` does `GET` requests only, paced at 0.3 s, so it is safe while
a print runs:

```bash
scripts/u1-moonraker-backup.sh                    # config + DB + history + objects (~1 MB)
scripts/u1-moonraker-backup.sh --logs --camera    # + ~300 MB logs + timelapses; run when idle
```

It writes `~/.cache/u1-fw/backup-<UTC>/` with:

| Item | Why |
|---|---|
| `config/` — every file in the `config` root: `printer.cfg` (with the `SAVE_CONFIG` bed mesh block), `xyz_offset_calibration.cfg` (head XY offsets), `fluidd.cfg`, `moonraker.conf`, `factory*.cfg`, `snapmaker/*.json` (bed mesh, input shaper, PID ×4, flow calibrator, extruder base position, `defect_detection.json`, `filament_parameters.json`, print task configs), `persistent/*.json` (extruder config, homing origin) | calibrations and settings |
| `db/<namespace>.json` — webcams, fluidd, moonraker, gcode_metadata, announcements | Moonraker DB content (our webcam entry, Fluidd UI settings) |
| `history.json` — every job from `/server/history/list` | print history; Moonraker keeps it in its DB file, which no `GET` returns whole |
| `objects.json` — every Klipper object incl. `configfile.settings`, `print_task_config`, `filament_detect`, `bed_mesh`, `extruder_offset_calibration` | live state to compare against after the install |
| `machine-system_info.json`, `printer-info.json`, `server-*.json` | versions, roots, auth config |
| `SHA256SUMS` | integrity |

A baseline was taken on 2026-10-14: `~/.cache/u1-fw/backup-20261014T185352Z/` (36 config files,
90 history jobs). **Run it again right before the install**, after the current prints finish.

Not reachable read-only over the API: the Moonraker DB file itself
(`printer_data/database/`), Wi-Fi credentials, and `/userdata` outside `gcodes`. Stock already has
**Settings > Maintenance > Root Access** (SSH as `root`/`snapmaker`, per paxx12 `ssh_access.md`
"since v1.2.0") `[INFERENCE: not checked on our screen]`. To copy those too, the user has to turn
that on at the printer, which is a change to the printer and needs a separate OK. The API backup
covers everything we need to restore calibrations.

The G-code files in `gcodes` are not backed up; they can be re-uploaded from `out/`.

## 5. Install day (physical steps for the user)

Before you go to the printer:

1. `3d printer status u1`: state `standby`/`complete`, no job. Do not interrupt a job.
2. `scripts/u1-moonraker-backup.sh --logs` and keep the printed directory name.
3. Stop our watcher so it does not page you during the reboot: `systemctl --user stop 3d-printer-watch@u1`.
4. Copy `~/.cache/u1-fw/v1.6.0-paxx12-22/U1_extended_1.6.0-paxx12-22_upgrade.bin` to the
   **root** of a **FAT32** USB stick (exFAT/NTFS fail), plus
   `stock/U1_1.6.0.267_20260815150420_upgrade.bin` for a rollback. Run `sha256sum` on the stick.
5. Check that the router has a DHCP reservation `40:d9:5a:e3:18:c8 → 192.168.0.26`. Every
   consumer (3d-cli, HA, the camera proxy) uses that IP.

At the printer:

6. Plug the USB stick into the U1.
7. Touchscreen: **Settings > About > Firmware Version > Local Update**, pick
   `U1_extended_1.6.0-paxx12-22_upgrade.bin`, confirm.
8. Wait. The printer flashes, updates the MCUs and reboots, probably more than once
   `[INFERENCE: 10–20 min with the 1.4.1→1.6.0 MCU update]`. **Do not power it off.** If the splash
   screen hangs, give it time first (see [paxx12#778][p778] in §8).
9. After boot: **Settings > About** shows `1.6.0` / an extended build. If Wi-Fi did not come back,
   enter it again on the touchscreen.
10. **Settings > Maintenance > Advanced Mode → on**, then restart the printer. Without it
    `http://192.168.0.26/firmware-config/` does not open.
11. Remove the USB stick, or keep it in for a rollback.

From the desk:

12. Open `http://192.168.0.26/firmware-config/`. In Status check the base `1.6.0` and build
    `1.6.0-paxx12-22`. **Leave "Require Login" off** (§7). Leave **Firmware Upgrade Channel** at
    `none`; it only works when the printer is signed in to Snapmaker Cloud anyway.
13. Run the checklist in §10 before the first real print.

## 6. Preserved and lost

| Item | After install | Note |
|---|---|---|
| Head XY offsets (`xyz_offset_calibration.cfg`, `extruder_offset_calibration`) | preserved | lives in `printer_data/config`, which survives; Snapmaker notes say calibrations survive upgrades |
| Bed mesh (`SAVE_CONFIG` block, `snapmaker/bed_mesh_default.json`), Z offset | preserved | same; compare against the backup (§10) |
| Input shaper, PID, flow calibration, `extruder_base_position` | preserved | `snapmaker/*.json` |
| Filament settings (`filament_parameters.json`, per-head `print_task_config`) | preserved | slot colours/materials stay as last set |
| Defect detection settings | preserved | `defect_detection.json`; the stock module is not patched by paxx12 |
| Hand edits to `printer.cfg` | **lost on every firmware update** ([paxx12#766][p766]) | we have none: `printer.cfg` equals the stock backup |
| Wi-Fi | should be kept `[INFERENCE]` | stock upgrades keep it; paxx12 adds `05-persist-dhcp`. Fallback: enter it again on screen |
| Moonraker DB (history, webcams, Fluidd settings) | preserved `[INFERENCE]` | lives in `printer_data/database`; the backup has the content |
| Camera | the stock service is **replaced** by paxx12's v4l2-mpp service (WebRTC at `/webcam/`) | stock timelapse files in `camera/` stay; see §7 for `monitor.jpg` |
| Snapmaker Cloud / app pairing | unchanged | not used |
| System changes under `/etc`, SSH keys | wiped on every upgrade (and `/oem/.debug` removed) | not used today |

## 7. Compatibility with our stack

**3d-cli moonraker adapter** (`lib/printer_ops/moonraker.py`, `3d printer status|upload|start|cancel`):

- Uses `/printer/objects/query?...&print_task_config`, `/server/files/list|upload`,
  `/printer/print/start`, `/printer/print/cancel`. paxx12 does not change Moonraker's port (7125)
  or these endpoints.
- `print_task_config` stays: paxx12 only patches `SET_PRINT_PREFERENCES` (adds `FORCE=1`), and the
  fields we read (`filament_exist`, `filament_color_rgba`, `filament_type`, `filament_vendor`,
  `filament_sub_type`) are untouched. Known stock bug inherited: mixed nozzle sizes fail at print
  start ([paxx12#773][p773]); ours are all 0.4 mm.
- **Auth is the one breaking switch.** firmware-config **Require Login (Fluidd only)** writes
  `force_logins: true`. That overrides `trusted_clients`, and our adapter, the HA `moonraker`
  integration, `u1_print_inspector` and the camera proxy all call Moonraker without a key. Keep it
  **off**. If we ever want it on, the adapter needs API-key support first (it has none).
- `filament_detect`: extended adds `CARD_TYPE`, `CARD_EVENT_TIME`, `SKU`, a writable
  `POST /printer/filament_detect/set` and, with OpenRFID, many more tag formats. 3d-cli does not
  read `filament_detect` today, so nothing breaks. PrintOps code that reads it must not assume
  stock-only fields.
- **`trusted_macros`** in `printer-devices.yaml` needs no change. paxx12 keeps the vendor macros
  our G-code calls (`PRINT_START`, `PRINT_END`, `DEFECT_DETECTION_*`, `T0..T3`, …); its hooks run
  inside them. New macros (`SET_SPOOL_ID`, AFC-Lite lane macros) only need listing if our G-code
  starts calling them, for example `SET_SPOOL_ID` in the start G-code.

**`3d printer watch u1`** subscribes to `print_stats`, `toolhead`, heaters,
`filament_motion_sensor *` and similar objects by name. The new objects paxx12 adds (`AFC_lane …`,
purifier `fan_generic`) are not subscribed and do no harm. The noodle pause still comes from the
stock `defect_detection.py`.

**Camera in Home Assistant.** `u1-camera-proxy` (`scripts/u1_mjpeg_proxy.py` in the HA repo)
polls `camera/monitor.jpg` and wakes the monitor through Moonraker MQTT. paxx12 keeps the stock
camera app alive through an LD_PRELOAD frame "imposter" so AI detection still works; whether
`monitor.jpg` keeps updating is `[INFERENCE]` and the §10 check decides it. Either way, the better
path afterwards is the native stream (§11). Open upstream camera reports: [paxx12#635][p635]
(camera 502 after 1.5.x, fixed by a downgrade), [paxx12#501][p501].

## 8. Known upstream issues that touch us

| Issue | Effect on us |
|---|---|
| [#567](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/pull/567) | Old `spoolman_multi_tool`-style includes break SpoolLink (`variables.cfg` error). We have no such include (`printer.cfg` = stock). |
| [#722](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/issues/722) (open) | OpenRFID wipes a manual Spoolman lane assignment on any RFID re-read (load/unload, restart). Untagged spools from dryboxes lose their link; the stock reader does not. |
| [#734](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/issues/734) (open, reproduced on 1.6) | NTAG read only the first time; a manual re-read in Filament Manager restores it. |
| [#660](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/issues/660) (open) | `force_logins` breaks OpenRFID's webhook and more; another reason to keep login off. |
| [#761](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/issues/761) (open) | Toolhead-dock anomaly on the first swap after updating to stable; the reporter fixed it with cleaning, then went back to stock. Watch the first multi-colour print. |
| [#778](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/issues/778) (open, develop 2.0) | Splash-screen hang on boot. **Never use the touchscreen "factory reset" while extended is installed**; use the recovery files below. |
| Release notes | Old Spoolman `Lot Nr` mappings are not read; SpoolLink uses spool extra field `card_uids`. Tags carrying an OpenSpool `spool_id` are ignored until re-linked by UID. |

## 9. Rollback

Easiest first:

1. **firmware-config > Troubleshooting > Recover to Backup Firmware.** The U1 has A/B slots, so
   right after the install the other slot should still hold our stock `1.4.1`
   `[INFERENCE: what the button restores was not checked]`.
2. **USB, stock `1.6.0`** (`stock/U1_1.6.0.267_20260815150420_upgrade.bin`): same Local Update steps
   as §5. The MCUs are already on 1.6.0 firmware, so this is the low-risk path.
3. **USB, stock `1.4.1`** (`stock/U1_1.4.1.6_…`): only if 1.6.0 itself misbehaves. This downgrades
   the base and MCU firmware too; Snapmaker does not document downgrades
   `[INFERENCE: the risk is an MCU version mismatch → re-flash with 1.6.0]`.

Recovery files, without reflashing:

- `extended-recover.txt` (empty file, FAT32 USB, insert, restart): resets only the extended
  config (backed up to `extended.backup.N`).
- `full-recover.txt` (USB, insert while running): wipes printer data and overlays and rebuilds them
  from defaults. **This loses calibrations**; restore them from the backup's `config/` afterwards.

Risks: stock leaves `config/extended/` in place (harmless, unused), and `/oem/.debug` changes
vanish. If calibrations come back wrong after any of these steps, re-run the U1 XY-offset and bed
calibration on screen, or compare against the backup JSON and restore it over SSH.

## 10. Post-install checklist

- [ ] `curl -s 192.168.0.26:7125/machine/system_info | jq .result.system_info.product_info`:
      firmware `1.6.0`; `curl -s 192.168.0.26:7125/server/info | jq .result.failed_components` → `[]`.
- [ ] `3d printer status u1` works without a key, shows `standby` and all 4 slots with the right
      colours/materials.
- [ ] Run `scripts/u1-moonraker-backup.sh` again and diff the new `config/` against the
      pre-install one: `xyz_offset_calibration.cfg`, `snapmaker/bed_mesh_default.json`,
      `snapmaker/input_shaper.json`, `snapmaker/*pid*`, `defect_detection.json` unchanged. Diff
      `objects.json` `extruder_offset_calibration`, `bed_mesh.profiles` too.
- [ ] `3d print-preflight <some recent>.gcode --printer u1`: no new FAIL, `macros` PASS with the
      current `trusted_macros`.
- [ ] `systemctl --user start 3d-printer-watch@u1` and `journalctl --user -u 3d-printer-watch@u1 -n 50`:
      subscribed, no reconnect loop.
- [ ] `/printer/objects/query?defect_detection` exists; `defect_detection.json` still has noodle on;
      the touchscreen AI detection toggles show the same values.
- [ ] Camera: `http://192.168.0.26/webcam/` plays; `camera/monitor.jpg` `modified` advances while the
      camera is active (decides whether u1-camera-proxy keeps working); HA `sensor.printer_u1_*` not
      `unavailable`; go2rtc `u1_camera` plays.
- [ ] Moonraker webcam entry `U1 Camera` (HA ingress URL) still in `/server/webcams/list`; paxx12 adds
      its own `[webcam case]`, so delete the duplicate in Fluidd if both show.
- [ ] A short 2-colour test print with a toolchange ([paxx12#761][p761]) before the next long job.

## 11. What we turn on afterwards (each one a separate decision)

1. **Native camera → go2rtc.** firmware-config **Camera RTSP Stream: Enabled** gives
   `rtsp://192.168.0.26:8554/stream` (H.264, hardware-encoded). In `go2rtc.yaml` replace the ffmpeg
   chain from `u1-camera-proxy` with `u1_camera: rtsp://192.168.0.26:8554/stream`; the HA
   `custom:webrtc-camera` card stays the same. Keep the internal camera at `1080p25` or lower (fixed
   1080p is needed for AI detection; framerate can drop to `1080p15` if the CPU suffers). Then
   `u1-camera-proxy` only keeps the share/overlay features or can be retired.
2. **Spoolman via SpoolLink** (needs [#158](https://github.com/alex-mextner/3d-cli/issues/158) deployed: Spoolman 0.27 on the Odroid,
   `http://192.168.0.39:7912`). firmware-config **Snapmaker Components > Spoolman Integration:
   Enabled**, URL `http://192.168.0.39:7912`; the printer checks it can reach it. SpoolLink
   registers extra fields `card_uids` (spool) and `variant` (filament), PATCHes spools when it links
   a tag, and Moonraker's `[spoolman]` reports usage for the active spool. **This conflicts with
   our "3d-api is the single writer to Spoolman" rule (D2) and duplicates our `tag_uids` extra
   field.** Before enabling, decide one of: (a) SpoolLink owns U1 usage and `card_uids`, and 3d-api
   skips `/use` for U1 jobs and maps `tag_uids` ↔ `card_uids`; (b) keep SpoolLink off, and 3d-api
   writes U1 slot data itself through `POST /printer/filament_detect/set` (Moonraker does no
   Spoolman accounting on the printer).
3. **OpenRFID + NTAG215/OpenSpool.** firmware-config **RFID Detection System: OpenRFID** reads
   OpenSpool/OpenTag3D/TigerTag NTAGs and vendor tags. Upstream recommends the built-in **Snapmaker**
   reader for the most reliable SpoolLink UID resolution, and [paxx12#722][p722] wipes manual assignments for
   untagged drybox spools. Start with `Snapmaker` and switch to OpenRFID once all spools carry
   NTAG215 tags (`OpenSpool` JSON, linked by UID through a SpoolLink app such as SpoolPainter or
   the paxx12 SpoolLink Android app).
4. Optional: **SSH** (firmware-config), for full backups and for diagnosing; change the default
   `snapmaker` password, which needs `/oem/.debug` to persist. **Klipper Metrics Exporter** `:9101`
   for HA/Prometheus.

## Sources

- paxx12 docs at tag `v1.6.0-paxx12-22`: `install.md`, `firmware_config.md`, `firmware_upgrade.md`,
  `data_persistence.md`, `camera_support.md`, `spoolman.md`, `nfc_spoolman_setup.md`,
  `ssh_access.md`, `design/spoolman.md`, `design/filament_detect.md`; overlays
  `32-feature-klipper-tweaks/.../authorization.cfg` (`force_logins: true`),
  `36-feature-print-preferences` (the only `print_task_config` patch), `60-app-camera/apps/v4l2-imposter`.
- Release notes: [v1.6.0-paxx12-22](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/releases/tag/v1.6.0-paxx12-22),
  [rolling develop](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/releases/tag/rolling).
- Snapmaker: [U1 firmware release notes](https://wiki.snapmaker.com/en/snapmaker_u1/firmware/release_notes),
  [update procedure](https://wiki.snapmaker.com/en/snapmaker_u1/firmware_update_procedure).

[p501]: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/issues/501
[p635]: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/issues/635
[p722]: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/issues/722
[p761]: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/issues/761
[p766]: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/issues/766
[p773]: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/issues/773
[p778]: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/issues/778
