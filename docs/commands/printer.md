# `3d printer` — drive the printers on your LAN

`3d printer` talks to the physical printers of the fleet registry
[`fleet/printers.yaml`](../../fleet/printers.yaml) and of `printer-devices.yaml`: it reads their
state, stores G-code on them, starts an uploaded file after an explicit `--yes`, and cancels
a job. `3d printers` (plural) is a different command: it lists printer **models** and their
build volumes; `3d printer` is about the machines themselves. For everything about one machine on
one screen — model spec, address and settings, the web UIs that answer (Fluidd/Mainsail/Moonraker),
live status, job queue and job history — use [`3d printers show NAME`](printers.md#show-name)
(`--json`, `--no-live`, `--history N`, `--secs S`).

Epic: [#90](https://github.com/alex-mextner/3d-cli/issues/90).

## Usage

```
3d printer list [--json]
3d printer status NAME [--json] [--secs S]
3d printer upload NAME FILE.gcode [--as REMOTE]
3d printer start NAME FILE.gcode --yes [--as REMOTE] [--source-3mf X.3mf] [--bridge-max MM] [--force] [--secs S] [--no-bed-move]
3d printer cancel NAME --yes
3d printer evidence NAME [--job ID | --last] [--out DIR] [--gcode FILE.gcode] [--note TEXT] [--klippy-log FILE] [--job-file FILE]
3d printer guard NAME --job-id ID --yes [--interval S] [--evidence DIR]
3d printer guard NAME --job-id ID --dry-run
3d printer watch NAME... | --all [--steer SESSION] [--exec CMD] [--min-severity LEVEL] ...
3d printer supervise status [NAME...|--all] [--json] [--no-live] [--unit-dir DIR]
3d printer supervise install [NAME...|--all] [--yes] [--plan] [--repo DIR] [--unit-dir DIR]
3d printer supervise uninstall [NAME...|--all] [--yes] [--unit-dir DIR]
3d printer supervise quiet NAME [--reason TEXT] | --clear
3d printer vision NAME [--interval S] [--once] [--dry-run] [--scene] [--no-detach] ...  |  vision eval ...  |  vision record NAME DIR [--timeout S]  |  vision fetch-model
3d printer bed status NAME [--json] [--max-age S] [--best-view | --no-move] [--part-height MM]  |  bed reference NAME [--capture [--no-plate]]  |  bed monitor NAME
3d printer subscribe --session ID [--printer P,P] [--kinds K,K] [--min-severity LEVEL] ...
3d printer subscribe --session ID --printer P --once --on bed-free
3d printer unsubscribe --session ID | --id SUBID | --all
3d printer subscriptions [list | test PRINTER] [--json]
3d printer agent list [--json] | stop PRINTER | handoff PRINTER --summary TEXT
3d printer doctor [NAME ...|--all] [--fix] [--yes] [--json]
3d printer todo list [PRINTER] [--all] [--json] | add PRINTER --title TEXT [options] | done ID [--note TEXT]
3d printer todo dismiss ID --reason TEXT | snooze ID [--until DATE] [--reason TEXT] | reopen ID
3d printer todo import [FILE...] [--dry-run] [--json]
3d printer -n PRINTER issues|todo list|add|done|dismiss|snooze|reopen|import …
```

| Subcommand | What |
|---|---|
| `list` | Every fleet and configured printer: owner, access, model, address, heads (an unusable head with its reason), and where the device settings came from when a user/project file overrides the fleet. |
| `status NAME` | State, current file and progress, temperatures, and (U1) what each head holds. |
| `upload NAME FILE` | Store the file on the printer and verify its size there. Never starts a print. |
| `start NAME FILE --yes` | Start a file that was uploaded before. Runs [`3d print-preflight`](print-preflight.md) first. |
| `cancel NAME --yes` | Cancel the running or paused job. |
| `evidence NAME --out DIR` | After EVERY print (done, cancelled, failed): save the Moonraker history job and the klippy.log lines around its pauses and errors as `DIR/<date>-<outcome>.md` + `.json`, with the layer of each stop when `--gcode` is given; GET only. See [printer-evidence.md](printer-evidence.md). |
| `guard NAME --job-id ID --yes` | Authorize one automatic native PAUSE of this exact live Moonraker job on confirmed camera failure; [policy and readiness](printer-vision.md#one-job-automatic-pause). |
| `watch NAME...` | Stay subscribed (read-only) and report anomalies as events — see [Watching printers](#watching-printers). |
| `supervise status\|install\|uninstall\|quiet` | The ALWAYS-ON supervision: one watch + one vision systemd user unit per owned printer from the fleet registry, running without a login, waiting for a switched-off printer, catching every print start; `status` FAILs when a printing/answering printer is unsupervised — see [Always-on supervision](#always-on-supervision). |
| `vision NAME` | Watch the camera for failed prints (spaghetti, stale/dark camera, the U1's own NPU verdicts) — see [Camera failure detection](#camera-failure-detection). |
| `bed status` / `reference` / `monitor` | Is the bed clear, is the build plate on — from the camera against an empty-bed reference; also the `bed` start gate and the `bed_clear` event — see [Bed state](#bed-state). |
| `subscribe` / `unsubscribe` / `subscriptions` | Route printer alerts to an AI agent first: it looks at the frame and tells the user only if the failure is real — see [printer-subscribe.md](printer-subscribe.md). |
| `agent list` / `stop` / `handoff` | The temporary per-printer omp agent (tmux) that gets the warning+ alerts no live subscriber took — see [printer-subscribe.md#the-temporary-printer-agent](printer-subscribe.md#the-temporary-printer-agent). |
| `doctor [NAME\|--all]` | Diagnose the printer's setup (Klippy, Moonraker, Spoolman link and CORS, active spool, camera, watch services, ...) and with `--fix` repair what is safe — the same command as `3d printers doctor`, see [printers-doctor.md](printers-doctor.md). |
| `todo …` (alias `issues`) | The to-do list of each printer: needed calibrations, due maintenance, repairs of open faults, manual tasks — see [To-do list](#to-do-list-3d-printer-todo). A printer's problems are tasks of this list, not GitHub tickets. |

Options:

- `--json` — `list` as JSON (one object per printer: `name`, `model`, `owner`, `access`, `adapter`,
  `address`, `settings`, `fleet`, `heads[]` with `nozzle`/`usable`/`reason`, `ams`, `enclosure`,
  `chamber_heater`, `plates`; `null` = not known), or `status` as JSON: normalized `state`,
  `raw_state`, `filename`, `progress` (0..1), `temps`, `slots`, and the raw firmware payload.
  `3d printer list --json | jq '.[] | {name, access, heads: [.heads[] | select(.usable | not)]}'`
  `3d printer status u1 --json | jq '.slots[] | {label, color, material}'`
- `--secs S` — Creality printers push their state over a WebSocket; listen this long (default 3).
  `3d printer status k1se --secs 5`
- `--as REMOTE` — the name stem on the printer (default: the local name with spaces turned into `_`).
  The stored name always gets the first 16 hex digits of the file's SHA-256 appended
  (`roof_left-1a2b3c4d5e6f7a8b.gcode`), so `start` finds exactly the bytes that were uploaded; pass the
  same `--as` to `start`.
  `3d printer upload k1se "out/roof left.gcode" --as roof_left.gcode`
- `--yes` — required by `start`, `cancel` and `guard`: you confirm the physical action.
  `3d printer start u1 out/part.gcode --yes`
- `--bridge-max MM` — preflight overhangs gate: longest bridge; overrides the printer's `bridge_max_mm`.
  `3d printer start u1 out/porch.gcode --yes --bridge-max 25`
- `--force` — ask to start despite preflight FAILs. It is **not an override by itself**: the start
  needs the owner's one-time Telegram approval (see [Forcing a start](#forcing-a-start)); without it
  the start is refused with exit 1. After an approved start the failing checks are printed as `FORCED`.
  `RIG_HATCH_REQUEST_3D_PRINTER_START_FORCE="why this FAIL is acceptable" 3d printer start k1se out/roof.gcode --yes --force`
- `--source-3mf FILE` — the project 3MF the G-code was sliced from; preflight then runs the
  head-sequence check on it and `3d halves-check` (without it, the head-sequence check uses
  `NAME.3mf` next to `NAME.gcode` when present).
  `3d printer start u1 out/base.gcode --yes --source-3mf print/base.3mf`

## Examples

```bash
3d printer list
3d printer list --json | jq '.[] | {name, access, heads: [.heads[] | select(.usable | not)]}'
3d printer status u1
3d printer status k1se --secs 5
3d printer status u1 --json | jq '.slots[] | {label, color, material}'
3d printers show u1                    # the full dossier: machine, web UIs, status, queue, history
3d printers show k1se --history 10 --secs 3
3d printer upload u1 out/part.gcode
3d printer upload k1se "out/roof left.gcode" --as roof_left.gcode
3d printer start u1 out/part.gcode --yes
3d printer start u1 out/base.gcode --yes --source-3mf print/base.3mf
RIG_HATCH_REQUEST_3D_PRINTER_START_FORCE="roof overhang FAIL: stale map, issue #N" 3d printer start k1se out/roof.gcode --yes --force
3d printer cancel k1se --yes
3d printer watch u1 --duration 20
3d printer watch u1 k1se --steer 01a12cd6
3d printer subscribe --session 01a12cd6 --printer u1,k1se
3d printer supervise install --all --plan      # what the always-on units would do; then --yes
3d printer supervise status                    # exit 1 when a printing/answering printer is unsupervised
3d printer supervise quiet k1se --reason "off for the week"
3d printer subscribe --session 01a12cd6 --printer u1 --kinds 'vision_*,printer_noodle_verdict'
3d printer subscriptions
3d printer agent list
3d printer todo list
3d printer todo add k1se --title "Check X belt tension" --type inspect --when idle --duration 15
3d printer todo done 12 --note "belts re-tensioned"
3d printer -n u1 issues list
```

## To-do list (`3d printer todo`)

One list per printer of what to do on it (plan §7.5, issue
[#281](https://github.com/alex-mextner/3d-cli/issues/281)): what, why, whether it blocks printing,
when it suits (idle / before a print / any time), how long it takes, whether a human is needed,
where it comes from and its status. Served by 3d-api (`/v1/printers/{p}/tasks`, `/v1/tasks/…`,
see services/3d-api/README.md); Home Assistant shows the same list
([#285](https://github.com/alex-mextner/3d-cli/issues/285)).

`3d printer issues …` is the same command. A printer's problems (a broken head, a squeaking
belt, a wrong slot label) are **tasks of this list, not GitHub tickets**: GitHub issues are for
the 3d-cli software. `-n PRINTER` / `--name PRINTER` before `todo`/`issues` names the printer
(its fleet name) instead of the PRINTER argument — `3d printer -n u1 issues list` equals
`3d printer todo list u1`; with `done`/`dismiss`/`snooze`/`reopen` it refuses a task of another
printer; with `import` and no FILE it imports only that printer's buffer files. Both forms stay.

```
3d printer todo list [PRINTER] [--all] [--json]
3d printer todo add PRINTER --title TEXT [--why TEXT] [--type T] [--head H] [--slot S] [--component C]
                   [--blocks none|printer|head[:H]|slot[:S]] [--when idle|before_print|any] [--when-note TEXT]
                   [--duration MIN] [--no-human] [--depends-on ID,..] [--ref R,..] [--kinds K,..] [--spool ID]
                   [--followup] [--json]
3d printer todo done ID [--note TEXT]
3d printer todo dismiss ID --reason TEXT
3d printer todo snooze ID [--until DATE] [--reason TEXT]
3d printer todo reopen ID
3d printer todo import [FILE...] [--dry-run] [--json]
3d printer -n PRINTER issues list|add|done|… (the same, PRINTER named up front)
```

Where tasks come from (`source`):

| Source | Opens when | Closes when |
|---|---|---|
| `fault` | a fault is open (`3d printops maint fault …`, the U1 T2 fault seeded from the fleet file): a repair task that **blocks its head** (or the whole printer for a fault without a head) | the fault closes: fixed → done, wontfix → dismissed |
| `maintenance_rule` | a rule of `fleet/maintenance-rules.yaml` is announced due (`3d printops maint due --check`) | the rule's maintenance is logged (`3d printops maint log P[/H] --rule ID`) |
| `calibration` | a calibration kind is not current for the head and its loaded spool ([`3d calibrate plan`](calibrate.md): never done, new filament/vendor/lot/nozzle, invalidated by a nozzle swap or repair, expired) — **one task per head** listing every missing kind with its spool and reason (`requirements`) | nothing of the head is missing any more and a record ([`3d calibrate record`](calibrate.md)) satisfied what it covered; each record drops its kind from the task |
| `manual`, `calibration_followup` | `add` (`--followup`: a mechanical action after a calibration) | `done` |
| `import` | `import` of a JSONL buffer | `done` |

The first three are **derived**: 3d-api materialises them on every tasks request and every 15
minutes, keyed by `source_key` (`calib:k1se/T1` — one per head, `rule:u1/hotend-wipe/T1/0`,
`fault:fleet:u1/T2`). `done` refuses them and names the command that records the work — the
record closes the task, otherwise the next pass would open it again. `dismiss --reason` drops one
without the work and stays dismissed for that need. A calibration task covers requirement keys
with the full scope of the record (`calib:k1se/T1/flow_ratio/nozzle:4/filament:12/lot:A7`), and its
dismissal sticks for exactly those keys: a new nozzle, another spool or lot, or an expiry after a
record opens a new task with the new keys only. When the need goes away for another reason (the
head no longer usable, the rule deleted) the task is dismissed by `derive`; another spool rewrites
the head's calibration task. A kind the engine cannot decide (spool unknown: Spoolman unreachable,
nothing bound) keeps its entry as it was. The per-kind calibration tasks of the first version
(#281) are closed on the next pass: done when recorded, otherwise dismissed as `superseded`; a task
that depended on one now depends on the head's task (a snooze on them is not carried over).

`list` shows open tasks first (blocking ones on top), then snoozed ones; a task with an open
dependency (`--depends-on`) is `WAITING`. `--all` adds done and dismissed ones. A snoozed task
opens again at `--until`; without `--until` it waits for `reopen`.

```
$ 3d printer todo list u1
u1: 2 open, 2 blocking
  #1     OPEN      u1/T2      repair      Repair u1/T2: head T2 does not work
         why: open fault #1 since 2026-10-15; prints using this head are refused until it is fixed
         BLOCKS head T2 · any time · human · from fault — closes with: 3d printops maint fault close 1 --fixed-by MAINT_ID (after `3d printops maint log …`)
  #11    OPEN      u1/T3      setup       Fix slot T3 filament label: shows '#FFFFFF NONE', loaded spool is green #1B5E20
         BLOCKS slot T3 · any time · human · from import
```

**Without 3d-api** (no `PRINTOPS_API`/`api:` configured, or it does not answer) `add`, `done`,
`dismiss` and `snooze` append the operation to `~/.local/share/3d-cli/printer-todo/<printer>.jsonl`
(`$XDG_DATA_HOME/3d-cli/printer-todo`, next to the other 3d-cli state files; one
line per operation, schema `printops.printer_task/1`, `flock` + one `O_APPEND` write + `fsync`)
and say so on stderr; `list` then shows the buffer (name tasks by their uid prefix). `import`
without FILE takes every file of that directory (task buffers and the `<printer>.notices.jsonl`
idle-notice journals). The first buffers (2026-10-15/16) were written to `~/.local/share/3d/printer-todo/`
(`$XDG_DATA_HOME/3d/printer-todo`); the first `3d printer todo` that touches the buffer moves that
directory, notice journals included, to the new place once, under `flock`, and says so on stderr.
If both directories hold something, the command stops (exit 1) and lists the `3d printer todo
import FILE` commands that merge both into 3d-api (by uid, nothing doubles; an explicit FILE never
triggers the move, so these run), any non-buffer entry to check by hand, and the `mv` that sets the
old directory aside afterwards (nothing is deleted). If neither directory exists, nothing happens.
`import` replays buffer files into 3d-api in order, each operation once by its `uid` — importing a
file twice adds nothing. Each file is one transaction: a line that is not JSON or names an unknown
schema stops that file's import with its line number and nothing of that file is imported; with
several files, the ones before it stay imported (a re-run skips them by uid). An `open` whose
`source_key` matches an open task (e.g. `fault:fleet:u1/T2`) merges into it (the buffer uid joins
`refs`); a spool named only by its filament name is bound to the one Spoolman spool of exactly
that filament name, else to the one whose filament name contains it (`Snapdragon` →
`жёлтый Snapdragon`, reported as a warning); otherwise it stays unbound with a warning.
`--dry-run` runs the import and rolls it back. The same command moves
idle notices (`printops.idle_notice/1`, #283) into 3d-api.

- `--blocks head:T2` — what the task keeps from printing (`head`/`slot` take the head/slot from the
  option or `--head`/`--slot`); the print preflight check that enforces it is #282.
  `3d printer todo add u1 --title "Fix T3 label" --type setup --blocks slot:T3 --head T3`
- `--depends-on ID,..` — tasks that must be done first.
  `3d printer todo add k1se --title "Cold SHAPER_CALIBRATE X" --type calibration --depends-on 9 --no-human`
- `--until DATE` — ISO date or date-time (a date = its midnight UTC).
  `3d printer todo snooze 4 --until 2026-10-25 --reason "after the house queue"`
- `--json` — the API objects (`list`: `{printer, summary, tasks, sync}` for one printer, `{tasks}` for all).
  `3d printer todo list u1 --json | jq '.tasks[] | select(.blocks != "none") | {id, title, blocks}'`
- `--dry-run` — `import` only: report what would happen, change nothing.
  `3d printer todo import ~/.local/share/3d-cli/printer-todo/k1se.jsonl --dry-run`

## Fleet registry

[`fleet/printers.yaml`](../../fleet/printers.yaml) in the 3d-cli repo lists every printer we
print on — ours, the neighbour's, Boris's, the hackspace's — and is checked by
[`3d lint --printops`](lint.md) (issue [#157](https://github.com/alex-mextner/3d-cli/issues/157)).
Per printer:

| Key | Values |
|---|---|
| `model` | a model from [`3d printers list`](printers.md) |
| `owner` | `own`, `neighbour`, `boris`, `hackerspace` |
| `access` | `full` (3d may upload/start/cancel), `read-only` (status only: `upload`, `start`, `cancel` are refused), `offline` (never contacted: no device settings allowed; every contacting command refuses it) |
| `adapter`, `host`, `port`, `ws_port`, `axis_limits`, `trusted_macros`, `bridge_max_mm`, `resume` | the device settings below; `adapter` and `host` are required unless `offline` |
| `heads` | in tool order: `label` T1..Tn ↔ `index` 0..n-1, `extruder` (Klipper object), `nozzle` {`diameter_mm`, `material` brass/stainless/hardened/ruby/tungsten, `type` std/hf}, `usable` (false needs `reason`) |
| `ams` | units feeding the heads `{id, kind ams/ams_lite/ams_2_pro/ams_ht/cfs/other, slots}`; `[]` = none |
| `enclosure`, `chamber_heater` | `none`/`partial`/`full`; `true`/`false` |
| `plates` | `smooth_pei`, `textured_pei`, `cool_plate`, `engineering_plate`, `high_temp_plate`, `epoxy`, `glass` |
| `notes`, `sources` | free text; where the facts come from |
| `watch` | `3d printer watch/vision/guard/bed` settings (keys: `printer_ops/watch_run.py`); `watch.vision.camera` is where the frames come from, see [Cameras](#cameras-watchvisioncamera) |
| `photo` | top-camera photo settings for `3d calibrate photo`: camera, lock, frames, light G-code, pose, scale reference ([calibrate.md](calibrate.md#photo--the-top-camera-photo-mode-342)) |

A key that is not known is left out (shown as `null`/`-`), never guessed.
`THREED_FLEET=FILE` reads another fleet file, `THREED_FLEET=` (empty) disables it.

```
$ 3d printer list
  NAME          OWNER        ACCESS    MODEL                ADDRESS                         HEADS
  a1mini        boris        offline   Bambu Lab A1 mini    - (never contacted)             T1
  k1se          own          full      Creality K1 SE       moonraker 192.168.0.27:7125     T1
  p1s           neighbour    offline   Bambu Lab P1S        - (never contacted)             T1
  u1            own          full      Snapmaker U1         moonraker 192.168.0.26:7125     T1 | T2 | T3 | T4
  xecut-a1      hackerspace  offline   Bambu Lab A1         - (never contacted)             T1
  xecut-cr200b  hackerspace  offline   Creality CR-200B Pro - (never contacted)             T1
```

### Cameras (`watch.vision.camera`)

`3d printer vision`, `guard`, the bed monitor (`3d printer bed`, `watch.bed`), the dossier and the
Home Assistant generator (`3d ha vision`) all read the camera of a printer from ONE place,
`printer_ops/camera.py`; nothing in them carries a printer's camera path. `3d lint --printops` and
the loader check the block.

```yaml
k1se:
  watch:
    vision:
      camera:
        kind: mjpg-streamer      # moonraker-monitor | mjpg-streamer | snapshot-url | none
        port: 8080               # mjpg-streamer: snapshot http://HOST:8080/?action=snapshot
        streams: ["http://192.168.0.27:8080/?action=stream"]   # live URLs for go2rtc / Home Assistant
```

| `kind` | Keys | What it is |
|---|---|---|
| `moonraker-monitor` | `keepalive` | Snapmaker U1 stock firmware: no stream, Moonraker serves `/server/files/camera/monitor.jpg` only while `camera.start_monitor` is renewed (sent automatically, once a minute) |
| `mjpg-streamer` | `url`, `port` (8080), `streams`, `keepalive` | the Creality Helper Script webcam (K1 SE); `url` replaces the derived snapshot URL, `streams` the derived `?action=stream` |
| `snapshot-url` | `url` (required), `streams`, `keepalive` | any camera that answers a JPEG GET; `url` is absolute or a path on the printer's Moonraker |
| `none` | - | the printer has no camera: `vision`, `guard` and `bed` say `no camera` (exit 2) |

`watch.vision.snapshot: URL` stays as the shorthand for `camera: {kind: snapshot-url, url: URL}`
(give one of the two); `watch.vision.keepalive: false` switches a keepalive off. A device that sets
neither gets its MODEL's default from `lib/data/vision/cameras.yaml` (`Snapmaker U1` →
`moonraker-monitor`), so a `printer-devices.yaml` override that drops the `watch:` block keeps its
camera; any other printer has no camera until one is set. Offline printers (Bambu P1S, A1 mini, the
hackspace machines) carry no `watch:` and are never contacted, so they have none by construction.
Two more `watch.vision` settings are per printer too: `npu: true|false` (read the printer's own
camera detector from klippy.log; default on only for a `moonraker-monitor` camera, so the K1 SE does
not poll for verdicts it can never have) and `limits: {stale_s, dark_luma, npu_threshold,
npu_quiet_s}` (the frozen/dark/NPU thresholds, measured on the U1, replaced per printer).
The camera calibration that turns a frame into bed coordinates (`3d printer vision` ROI, the bed
check, the ghost overlay) is separate: the bundled one of the model (`lib/data/vision/`), else
`~/.config/3d-cli/vision/NAME-camera.yaml`; without it the bed check falls back to
`watch.bed.roi` and vision to the unmasked frame.

## Configuring printers

The device settings of the fleet's `full`/`read-only` printers are the base layer;
`~/.config/3d-cli/printer-devices.yaml` (honours `XDG_CONFIG_HOME`) and then a
`./printer-devices.yaml` in the current directory replace a printer's device settings by name
(whole entry). Only printers listed in these files are ever contacted — there is no network
discovery. An override never loosens the fleet's `access`: an entry named like an offline printer is
ignored, and one named like a read-only printer stays read-only. An override without `model:`
keeps the fleet's model (`3d printer vision` picks its bundled camera calibration by it).

```yaml
u1:
  adapter: moonraker        # Klipper + Moonraker (Snapmaker U1, Voron, ...)
  host: 192.168.0.26
  port: 7125                # default 7125
  model: Snapmaker U1
k1se:
  adapter: creality-stock   # Creality K1 family on stock firmware (the fleet's rooted K1 SE uses
  host: 192.168.0.27        # moonraker:7125; this whole entry replaces it, dropping its watch.vision)
  model: Creality K1 SE     # port defaults to 80, ws_port to 9999
  axis_limits: {x: [-5, 229], y: [-0.5, 227]}
  trusted_macros: [START_PRINT, END_PRINT, PAUSE]
  bridge_max_mm: 15         # longest clean bridge, from a bridge test print
```

`axis_limits` is the real X/Y travel (Klipper `stepper_x`/`stepper_y` `position_min`/`position_max`;
for the K1 SE from CrealityOfficial K1_Series_Klipper `config/K1_SE_CR4CU220812S12/printer.cfg`).
[`3d gcode-bounds`](gcode-bounds.md) and the preflight check every move against it. Moonraker
printers without `axis_limits` report them from their own Klipper config.

`resume:` holds the printer-specific values of [`3d gcode-resume`](gcode-resume.md) (off-bed
purge macro and lengths, travel clearance, start commands); see that page for the keys.

`trusted_macros` names the macros (vendor start/end, pause, toolchange `T0`…`Tn`, timelapse
commands) whose bodies you know keep the head on the bed and leave the coordinate modes
(G90/G91, G92, SET_GCODE_OFFSET) as they found them. The bounds check cannot see inside a
macro, so any macro in the G-code that is not listed makes preflight FAIL and `start` is
refused (add the macro to `trusted_macros`; a [forced start](#forcing-a-start) needs the owner's
Telegram approval). With `--printer A --axis-limits B` only A's list counts.

`bridge_max_mm` is the longest span this printer bridges cleanly (measure it with a bridge test
print). The preflight overhangs gate counts uncarried runs up to it as bridges; `--bridge-max`
overrides it, and without either the limit is 15 mm. The gate's line names where the limit came from.

## Adapters

| Adapter | Printers | Transport |
|---|---|---|
| `moonraker` | Klipper/Moonraker: Snapmaker U1 and others | HTTP: `/printer/objects/query`, `/server/files/upload` (`print=false`), `/printer/print/start`, `/printer/print/cancel`. On the U1, `print_task_config` gives the colour and material in each head; a head without filament (the broken T2) shows as `empty`. Write (only from `3d printops spool load`, [#400](https://github.com/alex-mextner/3d-cli/issues/400)): `SET_PRINT_FILAMENT_CONFIG` sets a head's colour/material/vendor/sub-type, read back; the firmware then resets that head's pressure advance, which is read and, with a current calibration, set again by `SET_PRESSURE_ADVANCE` (read back). |
| `creality-stock` | Creality K1 / K1 SE on stock firmware (no Moonraker) | HTTP `POST /upload/<name>` with only the `file` field; WebSocket `:9999` for state, file list, `opGcodeFile` start and `stop`. The heartbeat is answered automatically. States: 0 idle, 1 printing, 2 complete (seen on our K1 SE); 5 paused, 4 stopped, 7 cancel finishing, `withSelfTest` 1–99 self-test (from the [ha_creality_ws](https://github.com/3dg1luk43/ha_creality_ws/blob/a8e5491809227ff3ce72bc6b317cc75075e19b9f/custom_components/ha_creality_ws/utils.py#L640-L690) integration). A non-zero `err` code shows as `error` unless the printer is busy. |

The Creality upload sends **only** the `file` field: with an extra `path` field the K1 SE
stored 165–168 bytes more than it received
([#99](https://github.com/alex-mextner/3d-cli/issues/99)). The stored size is always compared
with the local file.

A rooted K1 with the Creality Helper Script runs Moonraker on `:7125` and can use the
`moonraker` adapter instead. The stock `:80`/`:9999` stack keeps running next to it. See
[K1 SE: root + Helper Script](../printers/k1se-root-upgrade.md).

## Watching printers

`3d printer watch` keeps a WebSocket open to each printer and turns what it pushes into
events ([#145](https://github.com/alex-mextner/3d-cli/issues/145)). It only reads: the
frames it sends are Moonraker's `server.info`, `printer.objects.list`,
`printer.objects.subscribe` and `server.files.metadata` (once per job file), and the `ok` the
K1 expects for its heartbeat.

Moonraker progress is measured over the G-code body (the file metadata's `gcode_start_byte` ..
`gcode_end_byte`), as Mainsail and Fluidd show it: `virtual_sdcard` counts the whole file, so a
slicer's config footer put `END_PRINT` of a small K1 print at 94.6 %
([#255](https://github.com/alex-mextner/3d-cli/issues/255)). Without metadata the whole-file
progress is used.

```
3d printer watch NAME... | --all [options]
```

| Event `kind` | Severity | When |
|---|---|---|
| `snapshot` | info | First reading after start: state, file, progress, heated heaters. |
| `print_started` | info | The printer went from standby/complete/cancelled/error to `printing` — whoever started it (CLI, Fluidd/Mainsail, touchscreen, slicer upload, Home Assistant, macro) and also after a power cycle; `data.job_started`, `data.adopted`. A job already printing when the watcher starts or reconnects is announced once with `adopted: true`. |
| `state` | info / warning / critical | Resumed, complete (info); paused, cancelled, job ended without completing (warning); error (critical). Found paused/in error at start: reported too. Pause/cancel/error text carries the last error seen in the 5 min before. |
| `exception` | critical (warning for level ≤ 1) | U1 `print_stats.exception` / `exception_manager` (e.g. code 2 "detected noodle" from defect detection, id 532); K1 `err.errcode`. |
| `gcode_error` | critical while a job is active, else warning | A G-code response starting with `!!` — e.g. `!! Error evaluating 'gcode_macro INNER_PREEXTRUDE_FILAMENT:gcode'`. |
| `gcode_warning` | warning | `// Warning…` / `Warning:` responses. |
| `filament` | critical while printing, warning while paused | Moonraker `filament_switch_sensor`/`filament_motion_sensor` goes from detected to not detected; K1 runout sensor reports no filament (`materialDetect` 1 and `materialStatus` 1 — the K1's own UI: «Consumable not detected»). The K1 event is held 10 s and dropped if the sensor recovers; in the start phase it waits until the start phase ends ([#242](https://github.com/alex-mextner/3d-cli/issues/242)). A runout pause is reported at once by the `state` event, which carries the reason. |
| `homing_lost` | critical | Printing/paused and `homed_axes` (K1: `autohome`) loses X, Y or Z — resuming would move unhomed axes. Held 10 s, see below. |
| `bed_off` | warning | Bed target dropped to 0 while printing/paused (or 0 at start while paused). Held 10 s, see below. |
| `temperature` | warning (critical when drifting while printing) | A heater target not reached after `--heat-timeout-min`; or, after reaching, more than 15 °C off for 60 s. |
| `stall` | warning (info when it moves again) | Printing, but file position/progress/layer unchanged for `--stall-min`. |
| `klippy` | critical (shutdown/error, or any loss during a job) / info (ready again) | Klipper shutdown, error, disconnect. |
| `connection` | info / warning | The printer is unreachable (once per outage; reconnects every 2–30 s, silent): info, warning only when a job was printing or paused when the link dropped; silent with the `NAME.intentional-off` marker; one info line when it is back (which clears the marker). |
| `milestone` | info | Progress passed a `--milestones` percentage. |

End macros (`END_PRINT`, `PRINT_END`, `CANCEL_PRINT`) switch the bed off and disable the
motors (`M84`) while the printer still reports printing or paused. So `bed_off` and
`homing_lost` that appear during a job are held for 10 s and dropped if the job ends
(complete, cancelled, idle) or the condition clears meanwhile; at 99 % progress or more (body
progress on Moonraker, see above) they are ignored
([#190](https://github.com/alex-mextner/3d-cli/issues/190),
[#255](https://github.com/alex-mextner/3d-cli/issues/255)). Found already at
watch start (a paused job), they are reported at once. Start macros do the same in reverse —
the K1's stock `START_PRINT` zeroes the bed and nozzle targets for about 30 s while probing — so
in the start phase (the first 10 minutes after the watcher saw the job start; for a job already
running when the watcher started, progress at most 1 %) the hold is 90 s, and a restored target
or re-homed axes drop the event.

Each event is one JSON line on stdout:
`{"ts", "printer", "kind", "severity", "text", "key", "data"}` — `text` is one phone-first line
(what happened, where in the job, what to decide), `data` has state, file, progress, layer and
the kind's details.

Sinks — several at once:

- stdout JSONL (default; `--no-stdout` turns it off).
- `--exec CMD` — `sh -c CMD` per event; the event JSON on stdin and `PRINTER_EVENT_PRINTER`,
  `_KIND`, `_SEVERITY`, `_TEXT` in the environment. Repeatable.
- `--steer TARGET` — one message per batch (the same evidence-aware text a
  [subscriber](printer-subscribe.md#what-the-agent-receives) gets, every frame path included) into a live omp session through
  `tg-ctl omp-live send` (the bridge `omp-steer` uses: a busy session gets it as a steer, an
  idle one as an aside that starts a turn). TARGET is a sessionId or its prefix (survives
  `omp -r`), a pid, or a unique title substring; several matches are refused, `all` broadcasts.
  `omp-steer list` shows the sessions. Repeatable.
- [Subscriptions](printer-subscribe.md) — no flag: every watcher also delivers to the agent
  sessions registered with `3d printer subscribe` (re-read when the registry changes).
- [The temporary printer agent](printer-subscribe.md#the-temporary-printer-agent) — no flag:
  warning+ events that no live agent (`--steer`, a subscriber) took go to a per-printer omp
  agent in tmux, which judges them and decides itself whether to tell the user.
- tg — **emergency**, on unless yaml `watch.tg_fallback: false` (no CLI flag): ONE short message
  per incident through the `tg` CLI (≤ 5 lines: `printer: <worst event>`, `+N related: kinds`, the
  pause outcome; the frame as `--photo`), carrying only the warning+ events nobody could judge — no
  live subscriber, and the printer agent did not start (3 tries) or was not up within
  `watch.agent_ack_seconds` (default 90) even after one silent restart. This is the only case
  where the watcher messages the user itself ([When nobody can
  judge](printer-subscribe.md#when-nobody-can-judge)). After a message, further alerts of the
  same or lower severity within `watch.message_cooldown_minutes` (default 10) are folded into one
  `+N more: kinds` line sent when it ends; a higher severity passes once.
  The old `--tg` / `--tg-always` flags and `tg:` key are read as `tg_fallback: true` with a
  deprecation warning.
  At start the watcher (and `3d printer vision`) sends ONE `[TEST]` message through the same `tg`
  command line an alert uses and asserts tg's exit code, on its own thread (a hung tg never delays the
  watching) and at most once an hour per service across restarts (a service restarting in a loop must
  not spam; `watch` and `vision` each prove their own tg): a tg that refuses every message is otherwise silent
  until the first real alert fails (Chekhov 2026-10-23: `tg --tag answer` without `--reply-to`, the
  error buried in a log, a paused print waited 6 h). A refusal is an `ERROR: the Telegram test message
  FAILED` line on stderr (the journal of the service) and, for `watch`, `failed` / `last_error` of the `tg`
  sink in the heartbeat file (`vision` has no heartbeat: the log line is its only surface); the watcher
  keeps running, its other sinks may still reach someone, and the next start tries again at once. A state
  directory the service cannot write only turns the once-an-hour throttle off. Not sent with `--dry-run`.
- pause — before that message, a high-confidence critical finding pauses the print itself when
  `watch.pause_without_agent` is `critical-only` (default for own, `access: full` printers with a
  camera): [Pause without an agent](printer-vision.md#pause-without-an-agent). The stream itself
  stays read-only; the pause goes through the adapter, once per job, never resume or cancel.
- `--dry-run` — exec/steer/subscriptions/agent/tg print what they would send to stderr instead.
  Refused in a service (stderr is the journal): a service never holds alerts.

Delivery rules: events below `--min-severity` (default `info`) are dropped; the same event
key within `--dedup-min` (default 10) is dropped; events arriving within 2 s go out as one
tg/steer message, most severe first; steer sends at most 6 messages per 10 minutes (critical
events always go through, and the next message says how many were held back); tg sends one
message per incident and a cooldown per printer (above).

Other options: `--all`, `--stall-min N` (15), `--heat-timeout-min N` (10), `--milestones 50,90`,
`--heartbeat FILE`, `--duration S` (stop after S seconds).

Per-printer settings can live in `printer-devices.yaml`; flags add to / override them:

```yaml
u1:
  adapter: moonraker
  host: 192.168.0.26
  watch:
    steer: 01a12cd6          # or a list; also `exec: CMD` / a list
    tg_fallback: true        # emergency (the default; false = nobody is told when no agent can judge)
    agent_ack_seconds: 90    # the temporary agent must show up in omp-live within this (one silent restart), else tg (+ pause)
    message_cooldown_minutes: 10   # per printer: same-or-lower severity alerts after a tg message are folded into one line
    pause_without_agent: critical-only   # or off; default critical-only for own/full/camera printers
    min_severity: info
    stall_minutes: 20
    heat_timeout_minutes: 10
    milestones: [50, 90]
    dedup_minutes: 10
```

Heartbeat: `~/.local/state/3d-cli/watch/<names>.json` (`--heartbeat` overrides), rewritten
every 15 s with each printer's connection, state, last-message age, last event and the
per-sink delivered/failed/suppressed counters. Per printer it also has `last_poll_ok` (epoch of the last data
from the printer), `unreachable_since` (epoch while no session is up), `job` (`{file, started, adopted}` while a
job prints or is paused) and `quiet` (unreachable with its intentional-off marker set); `supervise status`
and the doctor read them ([Liveness](#liveness)).

As a user service use the shipped units — `3d printer supervise install --all --yes`, then
`3d printer subscribe --session 01a12cd6 --printer u1,k1se` and `journalctl --user -fu '3d-printer-watch@*'`
([Always-on supervision](#always-on-supervision)). `--install-service` is gone (it wrote the same
`3d-printer-watch@.service` with another definition); units it installed earlier, with `$WATCH_ARGS` in an
`EnvironmentFile`, still run, and `supervise install --yes` replaces the template (a `.bak` copy stays).
Flags with spaces (an `--exec` command) belong in `printer-devices.yaml` `<name>.watch`.

## Always-on supervision

`3d printer supervise status|install|uninstall|quiet` ([#639](https://github.com/alex-mextner/3d-cli/issues/639),
[#640](https://github.com/alex-mextner/3d-cli/issues/640), [#641](https://github.com/alex-mextner/3d-cli/issues/641),
[#642](https://github.com/alex-mextner/3d-cli/issues/642), [#643](https://github.com/alex-mextner/3d-cli/issues/643))
installs, checks and repairs the AI supervision of the owned printers.

### Requirement (the owner, 2026-10-24)

> AI control of printers must ALWAYS work while a print runs. It must also work when the printer is powered off
> between prints, and it must catch the start of a print however it was started — with Home Assistant and without it.

That sentence is the test of every change to `watch`, `vision`, the alert chain and these units. "However it was
started" means our CLI, Fluidd/Mainsail, the printer's touchscreen, an Orca/Creality Print upload, Home Assistant or a
G-code macro: the watcher sees only the printer's own state edge, never who caused it.

### What runs always

| Process | What it does | Needs | Never needs |
|---|---|---|---|
| `3d-printer-watch@NAME` (systemd user unit) | websocket to the printer (Moonraker, creality-stock ws:9999): errors, pauses, progress, `print_started`; waits for a printer that is off (retries every ≤ 30 s, silent) | the printer's own API | Home Assistant, an omp session, a login |
| `3d-printer-vision@NAME` (only with a camera) | polls the printer's state, fetches camera frames only while it prints, resets the per-print baselines at every print start, finds spaghetti / a detached part | the printer's own camera URL (`watch.vision.camera`) | Home Assistant, go2rtc, an omp session |
| the alert chain, inside both | live omp subscriber → temporary omp agent → `tg` → autonomous pause (`watch.pause_without_agent`: once per job, only PAUSE, never resume or cancel) | `tg`; `tmux`+`omp` for the agent | Home Assistant |
| `loginctl enable-linger` | the units start at boot and survive logout | — | — |

The units are generated from `fleet/printers.yaml`: every printer with `owner: own`, `access: full` and an adapter with
a watch stream (`moonraker`, `creality-stock`) gets a watch unit, and a vision unit when `watch.vision.camera` (or its
model's default) exists. They carry no `--dry-run` (a service holding its alerts is the 2026-10-22 incident), have
`Restart=always` with `StartLimitIntervalSec=0` (a crash loop never gives up), `After=network-online.target
hsw-observer-agent.service` (ordering only — no `Wants=`, so nothing depends on that session; a drop-in with
`Wants=hsw-observer-agent.service` keeps working) and `WantedBy=default.target`.

```bash
3d printer supervise install --all --plan   # what it would do, changes nothing
3d printer supervise install --all --yes    # templates, daemon-reload, enable --now, enable-linger
3d printer supervise status                 # exit 1 (FAIL) when a printing/answering printer is unsupervised
3d printer supervise status u1 --json
3d printer supervise uninstall k1se --yes
3d printers doctor --check printer-supervision [--fix --yes]
```

`install` never touches a file you made without `--yes`: a hand-made `3d-printer-watch@.service` (for example the
`--dry-run` one), and a `ConditionPathExists=!…intentional-off` drop-in, are listed as `[needs --yes]`, nothing at all
is changed, and with `--yes` the old file is kept next to the new one as `….bak-<time>` / `….conf.retired-<time>`.
Other drop-ins (`live.conf`, `live-observer.conf`, anything with `After=`/`Wants=`) are left alone and shown. A running
unit is restarted by `install` only when its running definition still carries `--dry-run`; otherwise restart it
yourself when the printer is idle (`systemctl --user restart 3d-printer-watch@NAME`). Run `install` from the main
checkout: the units run from the checkout it is started in (it warns when that is a worktree; `--repo DIR` overrides).

### The printer is switched off between prints

An unreachable printer is the normal state, not an incident. The watcher and the vision loop poll it at most every
30 s (2 s, 4 s, … capped at 30 s), log one line when it goes away and one when it is back, raise **no** alert for a
printer that was idle (a `connection` warning only when a job was active when the link dropped), and supervise it
the moment it answers. A print that starts after a power cycle is a new print even when it is the same file name
(see below).

**The `NAME.intentional-off` marker** (`~/.local/state/3d-cli/NAME.intentional-off`) means exactly one thing: *silence
the "printer unreachable" events while it is unreachable*. It is **not** a gate: no unit is conditioned on it, the
units are always up. The first successful answer from the printer clears it, so "switched off once" never outlives a
power cycle. Before [#640](https://github.com/alex-mextner/3d-cli/issues/640) the owner's hand-made
`intentional-off.conf` drop-ins (`ConditionPathExists=!<marker>`) kept both units stopped while the file existed, so a
printer switched on again and printed from its touchscreen was unwatched.

| | Where |
|---|---|
| Writers | `3d printer supervise quiet NAME [--reason TEXT]` (`printer_ops/intentional_off.py`); by hand: `echo '{}' > ~/.local/state/3d-cli/NAME.intentional-off` |
| Readers | `watch_run.py` (silences the unreachable event, clears the marker on the first answer), `vision_cycle.py` (the same for its log lines), `supervise status` (shows it) |
| Clear | the first answer of the printer; `3d printer supervise quiet NAME --clear` |
| Retired | the systemd `ConditionPathExists` gate; `supervise install --yes` retires such a drop-in, `status`/doctor warn about it |

### A print starts, whoever started it

The watcher emits a `print_started` event on every transition to `printing` from a non-job state (standby, complete,
cancelled, error), from the Moonraker websocket and the creality-stock `ws:9999` path alike. A job that is already
printing when the watcher starts, or when it reconnects after an outage, is announced once as `print_started` with
`adopted: true`. The vision loop polls the state itself, so it needs no start event: it resets the tracker and the
detached-part baseline when the state turns `printing`, also after a power cycle with the same file name (a gap ≥ 90 s
in which the printer did not answer, followed by a file that changed, a progress that went back by more than 2 %, or a
state that was not a job in between). The automatic pause binds the job from the printer's own history, once per job
(`<state>/guard/NAME-JOBID.json`), so it covers every job without a per-job `printer guard`. Nothing in
`watch`, `vision` or the pause reads state that `3d printer start` writes.

### Liveness

Each unit writes a heartbeat: `watch/NAME.json` every 15 s (`last_poll_ok`, `unreachable_since`, `job`, `quiet` per
printer) and `vision/NAME.json` every cycle (`ts`, `last_poll_ok`, `reachable`, `last_frame_ts`, `supervising`, `job`).
`supervise status` and the doctor check `printer-supervision` FAIL when a printer that **answers** (printing or idle)
has a missing/stopped unit, a heartbeat older than 75 s (watch) or max(90 s, 4 cycles) (vision), a watcher that is
disconnected for over 2 min from a printer that answers us, no camera frame for 3 min while a print is supervised,
two watchers for one printer, a `--dry-run` unit, or linger off. The same gaps on a printer that does not answer are
warnings: nothing is being missed, but the next print would be.

If a supervisor dies while a print runs the user gets **one** short `tg` line and systemd restarts it
(`Restart=always`). The units run `3d printer supervise notify-failure NAME KIND` as `ExecStopPost=`: systemd never
puts a `Restart=always` unit into the `failed` state, so `OnFailure=` would never fire, while `ExecStopPost=` runs after
every exit. The hook is silent for a clean stop (`SERVICE_RESULT=success`, `systemctl restart`), silent when the
heartbeat says no job was running, and sends one line per unit per 30 min through the delivery ledger (a crash loop
does not repeat it; a refused `tg` frees the slot). A *hung* process that is alive but stopped writing is not restarted
by systemd; it is what the stale-heartbeat FAIL is for.

### Migrating the hand-made units (2026-10-24)

On the owner's workstation the templates ended in `--dry-run` and live behaviour came from drop-ins
(`3d-printer-*@u1.service.d/live-observer.conf`, `…@k1se.service.d/live.conf` + `live-observer.conf`) plus
`intentional-off.conf` on `k1se`. The migration is `install --all --plan`, then `install --all --yes`: the templates are
replaced (copies kept), the `intentional-off.conf` gates retired, the `live*.conf` drop-ins stay (they set the same
`ExecStart` and the `After=`/`Wants=hsw-observer-agent.service` ordering). The `k1se` marker file itself is left
where it is: it now only silences its unreachable alarms and is removed when the K1 answers.

### Home Assistant

Nothing above reads Home Assistant, go2rtc or an ingress URL: the camera snapshot comes from the printer
(`/server/files/camera/monitor.jpg` on its Moonraker origin, or its own mjpg-streamer), the state from its own API.
`tests/test_printer_supervision_ha_free.py` fails when a module of the supervision path imports or names Home
Assistant, and checks that every fleet camera is read from the printer's host. HA stays an optional consumer of the
`vision/NAME.json` status file ([Status for Home Assistant](printer-vision.md#status-for-home-assistant)).

## Camera failure detection

`3d printer vision NAME` (shorthand for `vision run NAME`) polls the printer camera every 10 s,
scores each frame with Obico's open spaghetti detector on the CPU, drops boxes outside the
current print's bed volume (camera calibration; the U1's ships with 3d-cli), and checks each
remaining box with a zero-shot CLIP verifier, so textured walls do not count as spaghetti
(weights downloaded on first use into `~/.cache/3d-cli/vision/models/`, sha256-pinned). It sends
`vision_spaghetti`, `vision_spaghetti_vetoed` (info), `vision_camera_stale`, `vision_camera_dark`
and `printer_noodle_verdict` events, plus the opt-in `--scene` bed-change events, through the same
dispatcher and sinks as `watch` (the device's `watch:` block). It never pauses or cancels.
For an explicitly authorized automatic pause, start the separate job-bound guard:
`3d printer guard u1 --job-id 00006D --yes`. It bypasses notification routing and saves
its decisions locally. [Read the guard policy and readiness contract](printer-vision.md#one-job-automatic-pause).
`vision record` saves frames with print context, `vision eval` replays footage and prints
precision/recall, and `vision fetch-model` downloads the weights. Python deps come from the
`vision` extra (numpy, OpenCV, onnxruntime).

U1 camera in short: no stream on stock firmware (`/webcam/` → 502); `unisrv` rewrites
`/server/files/camera/monitor.jpg` at ~2 fps while a client sends `camera.start_monitor` (vision
does, once a minute); the Fluidd camera entry `/api/ingress/u1_printer/u1-camera/...` is a Home
Assistant ingress path that works only through HA, served by `u1-camera-proxy` on the Odroid
(needs a font present on that host). Details: [printer-vision.md](printer-vision.md); research
and numbers on real U1 footage: [docs/research/printer-vision.md](../research/printer-vision.md),
verifier and threshold tuning: [printer-vision-tuning.md](../research/printer-vision-tuning.md).

## Bed state

([#303](https://github.com/alex-mextner/3d-cli/issues/303)) After a job the parts stay on the bed
until someone takes them off. The camera can see that, so nobody has to tell an agent «стол
свободен», and a start onto old parts or onto the bare magnetic sheet is refused.

- **Reference.** With the bed empty and the plate on, at each Z where the printer rests after a
  job (U1: Z 200 after `PRINT_END`/`CANCEL_PRINT`, Z 10 after a `G28`; K1 SE `END_PRINT`: half of
  Z max = 125): `3d printer bed reference u1 --capture`. The bed moves in Z on both printers. With
  a camera calibration (the U1) a reference stands for any Z where the calibrated bed picture
  moved under 1.5 px of the 480-px frame — the U1's `PRINT_END` park reads toolhead Z 199.7 or
  200.3 depending on the job's mesh and Z offset, the same picture; without one (`watch.bed.roi`,
  the K1 SE) only within `z_tol_mm` (0.5) of the current Z. `--capture --no-plate` (plate off)
  adds the bare-bed reference that makes the plate test reliable. `--capture --from FRAME.jpg --z
  199.7` stores a saved snapshot of the empty bed taken at that Z instead. Stored in
  `~/.local/share/3d-cli/bed/NAME/`; `3d printer bed reference u1` lists them.
- **Out of view.** The calibration also says how much of the bed is in the frame: the U1 camera
  sits about 12 mm above the bed's Z 0, so the lower the bed goes the less of it shows (Z 10: 98 %,
  Z 200: 15 %, Z 215: under 10 %, Z 275 — where a home leaves it — none). Under 10 % the state is
  `unknown`, "out of the camera's view … it sees the bed again at Z 210 or below", and no
  reference is taken there: it would read every low part as a clear bed
  ([#402](https://github.com/alex-mextner/3d-cli/issues/402)).
- **Status.** `3d printer bed status u1` — `clear` / `occupied` / `unknown` and plate `present` /
  `missing` / `unknown`, with the reason and an evidence frame (ROI cyan, changed pixels red) in
  `~/.cache/3d-cli/vision/bed/`. `--max-age S` takes the watcher's last verdict when it was
  confirmed within S seconds at the same Z, else one fresh frame is judged (it waits up to 6 s
  for the camera to deliver a new frame; without one the camera may be frozen, so a clear-looking
  frame reads `unknown`, while parts or a missing plate on it still count).
- **Auto view** (`watch.bed.auto_view`: off unless the printer's entry sets it; `fleet/printers.yaml` sets it for the U1). At its
  Z 200 rest the U1 camera sees 15 % of the plate, and a window light there once read as «15.7 % of
  the view differs: occupied» and a print was started with `--force`. So when the share of the plate
  in the frame at the current Z is under `auto_view_min` (0.85), the printer is idle / complete /
  cancelled with Z homed, and an empty-bed reference stands at a better Z, `bed status` (and the
  `bed` preflight line, `printer start`) moves the **bed in Z only** to that Z, waits for the camera,
  judges a fresh frame there and puts the bed back, so the printer is left as found. The Z is picked
  by `lib/bed_view.py`: candidates are the Zs of the references plus a 5 mm grid; one qualifies with
  ≥ 85 % of the plate in the frame, a reference standing for it, inside the clearance floor and the
  axis ceiling; views within 10 points of the best are equal and the **nearest to the current Z**
  wins (least motion, the bed stays as far from the nozzles as it can). On the U1 references at Z
  70 / 150 / 175 / 199.7 give **Z 70** (89 % of the plate in view). If no Z qualifies the verdict
  stays the old one, with the reason (`no Z shows that much`, `no empty-bed reference stands there:
  … bed reference NAME --capture`, …): the camera never invents a view, and `unknown` is never
  read as clear. `--best-view` moves even when `auto_view` is off, `--no-move` (`--no-bed-move` on
  `print-preflight` / `printer start`) never moves, `--part-height MM` declares the tallest thing that
  may stand on the bed for one call. The verdict (and `--json`) carries `view`: `action` (`moved` /
  `not moved` / `none`), `moved_from`, `z_used`, `restored`, `pick`, and the reason names the Z
  judged at; the preflight line reads «judged at Z 70.0 after moving the bed from Z 200.0 and back».
  What it **never** does:
  - move while the printer is `printing`, `paused` or `busy`: nothing is sent and the camera is not
    asked; the verdict is `printing` («printing: bed not moved, view not judged», WARN in the
    preflight, never clear). Every send, the restore included, re-reads the printer first and
    refuses unless it reads idle / complete / cancelled with Klipper ready and Z homed, so a print
    that starts during the check gets no move;
  - move X or Y, home, or run a macro: the whole write is `G90`, `G1 Z<z> F600`, `M400` (G1 in
    gcode coordinates, so the Z offset of the last job is kept and the bed returns to exactly where
    it was). An unhomed printer is «cannot move: not homed» with the old verdict: no printer
    profile marks `G28 Z` safe with parts on the bed, so it is not offered;
  - raise the bed into a part. The bed goes toward the nozzles, so the floor is the tallest thing
    that may stand on it + 15 mm (never below Z 10): `--part-height`, `watch.bed.auto_view_part_mm`,
    else the last job's `object_height` from Moonraker's file metadata. Unknown height: no move
    («the height of what may stand on the bed is unknown»). The ceiling is `resume.z_max_mm` - 5.
    After a tall job the floor can exclude every good Z: then take the part off and run
    `bed status NAME --best-view --part-height 0`;
  - leave the bed away on failure: the restore is in a `finally` (exceptions, Ctrl-C, SIGTERM); a
    marker `~/.local/state/3d-cli/bed/NAME.view.json` records the displaced Z until it is back, and
    the next check puts the bed back first. A restore that fails is reported (`restored: false`,
    preflight WARN), never swallowed. One view check per printer at a time (flock).
  The `3d printer watch` monitor never moves the bed, and judges nothing (no verdict, no event, no
  ack) while a check has the bed away from where it was found. Config: `watch.bed.auto_view: true|false`,
  `auto_view_min`, `auto_view_part_mm` (see below).
- **Bed free, once.** `3d printer subscribe --session ID --printer u1 --once --on bed-free`: when
  the running job ends (a `3d printer watch` sees printing/paused → idle / complete / cancelled) the
  watcher waits 20 s for the end macro, runs the auto-view bed check, sends **one** `bed_free`
  event with the verdict and the evidence frame to that omp session and removes the subscription.
  See [printer-subscribe.md](printer-subscribe.md#one-shot-bed-free).
- **How.** Frames are decoded at 480 px; the current one is mapped onto the reference colours by
  a robust gain/offset fit (auto-exposure, room light) and a median detrend removes smooth light
  gradients; a changed region of ≥ 200 px (or ≥ 8 % of the bed view) is `occupied`, under 100 px
  and ≤ 3 % is `clear`, in between `unknown`. ROI: `watch.bed.roi` (a polygon of the bed, 0..1 of
  the frame), else the camera calibration (bed plus 80 mm above it at the current Z), else the
  whole frame. On the U1 at Z 200 only parts taller than a few cm show, and only the plate's front
  edge is in view (about 2 % of the frame), so the plate verdict rests on that strip and the
  reason says how much of the bed the camera sees; the U1's own `DETECT_BED_PLATE` still checks
  the plate at start. Below 2 % of the frame the surface counts as out of view (plate `unknown`,
  the bed check WARNs). These pixel limits are the defaults tuned on the U1 and K1 SE frames;
  `watch.bed.thresholds: {clear_max: 0.05, level: 30, ...}` overrides any of them for one printer
  (`level`, `detrend_px`, `edge_px`, `clear_blob_px`, `clear_max`, `occupied_blob_px`,
  `occupied_min`, `plate_missing_min`, `plate_tie`, `surface_min`, `change_min`, `settle_frames`,
  `match_px`, `view_min`). The camera is the printer's `watch.vision.camera`; enabling the bed check
  on a printer without one is refused when the config is read.
- **Monitor.** `3d printer watch` runs `bed monitor NAME` as a child for every Moonraker printer
  with `watch.bed.enabled: true`. Nothing is grabbed while a job runs; otherwise one snapshot per
  `interval_s` (1 s), identical bytes skipped, one reduced decode + one masked difference per new
  frame; the bed is judged only after a change has settled for 3 frames (a hand in the chamber is
  not judged), and once at start. Events (info, with `data.frame`): `bed_occupied` (parts after
  a job, or put on), `bed_clear`, `plate_missing`, `plate_present` (after missing); and the
  warning `bed_unknown` when the first verdict after a job cannot tell (out of view, no
  reference at that Z, a small part or a shadow) — once per job, never silence. Warnings reach
  the subscribed agent first and tg only as the watcher's no-subscriber fallback
  (`watch.tg_fallback`). Subscribe an agent to them:
  `3d printer subscribe --session ID --kinds 'bed_*,plate_*' --min-severity info`.
  The last verdict is in `~/.local/state/3d-cli/bed/NAME.json` and in the watch heartbeat.
- **Start gate.** `3d printer start` and `3d print-preflight --printer` add a `bed` line: FAIL on
  objects or a missing plate, PASS on a clear bed with the plate, WARN when the camera cannot
  tell (no reference at this Z, out of view, stale, plate out of view, camera down); a [forced start](#forcing-a-start) overrides it.
  On an idle printer with `watch.bed.auto_view` the line is judged at the best Z (the bed moved in Z
  only, and back; `--no-bed-move` judges at the current Z), see *Auto view* above.
- **Clear acknowledgement** (`watch.bed.clear_ack`, off by default): after `bed_clear` the
  printer makes one motor sound so the person at it knows the camera saw the empty bed. Neither
  printer has a beeper, so the command is `STEPPER_BUZZ STEPPER=stepper_z` (Klipper: ±1 mm ×10,
  ~10 s, net zero, works unhomed); a list of lines is allowed (the K1 SE can use shorter
  `FORCE_MOVE` pulses). It is the only write the bed code makes: every command word must be in
  `trusted_macros`, it is sent only when the printer reads idle/complete/cancelled right before,
  once per `bed_clear`, at most once per `debounce_minutes` (10), never below `min_z_mm` (10).

```yaml
u1:
  trusted_macros: [..., STEPPER_BUZZ]
  watch:
    bed:
      enabled: true
      clear_ack: {enabled: true, command: STEPPER_BUZZ STEPPER=stepper_z, debounce_minutes: 10, min_z_mm: 10}
      auto_view: true           # move the bed in Z to where the camera sees the whole plate (fleet U1: true; default false)
      auto_view_min: 0.85       # share of the plate in the frame below which it is moved
      auto_view_part_mm: 60     # optional: the tallest thing that may stand on the bed (else the last job's object_height)
k1se:
  watch:
    bed:
      enabled: true
      roi: [[0.31, 0.48], [0.72, 0.27], [0.92, 0.55], [0.92, 0.97], [0.41, 0.97]]
```

A Home Assistant bed sensor is a follow-up: [#305](https://github.com/alex-mextner/3d-cli/issues/305).

## Safety

([#95](https://github.com/alex-mextner/3d-cli/issues/95))

1. `upload` only stores the file. On Moonraker it sends `print=false` and fails loudly if the
   reply says a print was started or queued.
2. `start` and `cancel` need `--yes`. Without it nothing is sent to the printer.
3. `start` refuses while the printer is printing, paused, busy, or its state cannot be read.
   The file name on the printer carries the content hash, so a re-sliced local file never
   matches a stale upload; a missing file or a size difference refuses the start. Where the
   firmware can return the file (Moonraker), `start` reads it back and compares the SHA-256;
   different bytes refuse the start. The state is read again right before the start request.
4. `start` runs `3d print-preflight` — every G-code gate: the move-bounds check against the
   printer's axis limits and the macro check ([`3d gcode-bounds`](gcode-bounds.md)), the
   overhang check ([`3d gcode-overhangs`](gcode-overhangs.md)), the slicer-warnings record
   next to the G-code ([`3d slice-warnings`](slice-warnings.md)), the head-sequence check
   (project 3MF from `--source-3mf` or next to the G-code), thumbnails, the PrintOps
   material gates of `3d print-preflight --printer NAME` (live 3d-api state from
   `$PRINTOPS_API` / `printops.yaml`, else fleet facts only), and on printers with
   `watch.bed.enabled` the camera [bed check](#bed-state) (objects on the bed or the plate
   missing); a FAIL blocks the start unless the owner approves a [forced start](#forcing-a-start).
5. `upload` refuses while a Creality printer is busy (the firmware does not protect the
   running job), and on any printer refuses to overwrite the file being printed.
6. `start` succeeds only when the printer acknowledges it: Moonraker's HTTP reply, or on
   Creality a pushed state naming this file as running within 15 s. No acknowledgement, a
   failed request, or an unreadable state right after an acknowledged start is reported as
   UNKNOWN; uploads that time out are checked against the printer's file list. Nothing is
   resent automatically: check `3d printer status NAME` first.
7. An `offline` fleet printer (someone else's) is never contacted: `status`, `upload`, `start`
   and `cancel` refuse it by name. Its static profile (`axis_limits`, `trusted_macros`,
   `bridge_max_mm` in the fleet file) still serves the G-code gates:
   `3d print-preflight X.gcode --printer a1mini` (state checks are left out) and
   `--axis-limits a1mini`. A `read-only` printer answers `status` but refuses `upload`, `start`
   and `cancel` before any request is sent.

### Forcing a start

A preflight FAIL is either a real defect (do not print) or a false positive of a gate (fix the gate
and open an issue for it). `--force` therefore never starts such a print by itself; it only
**asks**, and the owner answers on Telegram, through the shared hatch helper the agent-hooks and
`gh ship --skip-ci` use (agent-tools `lib/agenttools_hatch_escalation`, hook id
`3d-printer-start-force`, [`lib/printer_ops/force_hatch.py`](../../lib/printer_ops/force_hatch.py)):

```bash
RIG_HATCH_REQUEST_3D_PRINTER_START_FORCE="loaded_filament_match: slice has no colour, issue #N" \
  3d printer start k1se out/part.gcode --yes --force
```

- The written justification (at least 8 characters, not a bare `1`) opens one Telegram prompt with
  Approve / Deny: the card names the printer, the file, its SHA-256, the exact command and **every
  failing check with its detail**. The command waits for the tap (no local timeout); an Approve
  tapped after the process was interrupted is a grant that the same command, rerun with
  `RIG_HATCH_REQUEST_3D_PRINTER_START_FORCE=approved:hgr-…`, redeems.
- Approved: the start continues, prints `FORCED: starting despite preflight FAIL (...)`, and the
  helper appends the use to `~/.config/agent-tools/overrides.log`.
- Without `--force`, without the variable, denied, timed out, or without a trusted `tg-ctl` /
  helper (`~/dev/agent-tools`, then `~/xp/agent-tools`, owned by you or root and not
  group/world-writable): exit 1, nothing is sent to the printer.
- Only the owner can approve. After every forced start, analyse the FAIL it overrode and fix the
  root cause (the gate or the slice) — the next print of the same kind must not need a force.

Exit codes: 0 done; 1 refused by a safety rule (incl. offline/read-only printer), preflight FAIL,
or printer unreachable; 2 usage error, unknown printer, bad config or fleet file, or missing file.

### Managed agent receivers

`watch.agent_fallback: false` disables automatic temporary-agent creation when a registered subscriber is unavailable; registered subscribers and stdout remain active. The default is true for backwards compatibility. Combine with `watch.tg_fallback: false` when raw external messaging is not authorized (the default `tg_fallback: true` otherwise messages the user when no subscriber is live). Only actual booleans are accepted. A missing subscriber is still a delivery failure, not success.
