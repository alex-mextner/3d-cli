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
                     [--colour-delta-e N] [--fix-colour [--fix-max-delta-e N]]
3d printer slot set NAME T<n> --material M --color #RRGGBB [--brand B] [--sub-type S] [--dry-run] [--force-official] [--secs S] [--json]
3d printer slot show NAME [--secs S] [--json]
3d printer cancel NAME --yes
3d printer evidence NAME [--job ID | --last] [--out DIR] [--gcode FILE.gcode] [--note TEXT] [--klippy-log FILE] [--job-file FILE]
3d printer guard NAME --job-id ID --yes [--interval S] [--evidence DIR]
3d printer guard NAME --job-id ID --dry-run
3d printer watch NAME... | --all [--steer SESSION] [--exec CMD] [--min-severity LEVEL] ...
3d printer supervise status [NAME...|--all] [--json] [--no-live] [--unit-dir DIR]
3d printer supervise install [NAME...|--all] [--yes] [--plan] [--repo DIR] [--unit-dir DIR]
3d printer supervise uninstall [NAME...|--all] [--yes] [--unit-dir DIR]
3d printer supervise quiet NAME [--reason TEXT] | --clear
3d printer ondemand NAME [--grace S] [--once]
3d printer vision NAME [--interval S] [--once] [--dry-run] [--scene] [--no-detach] ...  |  vision eval ...  |  vision record NAME DIR [--timeout S]  |  vision fetch-model
3d printer bed status NAME [--json] [--max-age S] [--best-view | --no-move] [--part-height MM]  |  bed reference NAME [--capture [--best-view] [--no-plate]] | bed reference NAME --refresh [--assume-empty WHY]  |  bed calibrate NAME [--z-from A --z-to B --z-step S] [--part-height MM]  |  bed monitor NAME
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
3d printer -n PRINTER macro list [--offline | --dry-run] [--all] [--json]
3d printer -n PRINTER macro run MACRO [KEY=VALUE ...] [--yes] [--dry-run] [--offline] [--timeout S]
3d printer -n PRINTER aliases [--json]
3d printer -n PRINTER ALIAS [args] [--yes] [--dry-run] [--offline] [--timeout S]   # home, level, load, preheat, ...
```

| Subcommand | What |
|---|---|
| `list` | Every fleet and configured printer: owner, access, model, address, heads (an unusable head with its reason), and where the device settings came from when a user/project file overrides the fleet. |
| `status NAME` | State, current file and progress, temperatures, and (U1) what each head holds. |
| `upload NAME FILE` | Store the file on the printer and verify its size there. Never starts a print. A FILE without its preview thumbnails gets them first, in place (old and new SHA-256 printed; see [`3d gcode-thumbnails`](gcode-thumbnails.md#layers-a-thumbnail-cannot-be-missing)). |
| `start NAME FILE --yes` | Start a file that was uploaded before. Runs [`3d print-preflight`](print-preflight.md) first. Adds missing thumbnails to FILE in place like `upload`; the fixed bytes have another content-addressed name, so upload again. |
| `cancel NAME --yes` | Cancel the running or paused job. |
| `evidence NAME --out DIR` | After EVERY print (done, cancelled, failed): save the Moonraker history job and the klippy.log lines around its pauses and errors as `DIR/<date>-<outcome>.md` + `.json`, with the layer of each stop when `--gcode` is given; GET only. See [printer-evidence.md](printer-evidence.md). |
| `guard NAME --job-id ID --yes` | Authorize one automatic native PAUSE of this exact live Moonraker job on confirmed camera failure; [policy and readiness](printer-vision.md#one-job-automatic-pause). |
| `watch NAME...` | Stay subscribed (read-only) and report anomalies as events — see [Watching printers](#watching-printers). |
| `supervise status\|install\|uninstall\|quiet` | The ON-DEMAND supervision: one systemd timer per owned printer (no process between its 30 s checks) from the fleet registry whose one-shot check starts the watch and vision systemd user units only while a print runs, running without a login, waiting for a switched-off printer, catching every print start; `status` FAILs when a printing/answering printer is unsupervised — see [Always-on supervision](#always-on-supervision). |
| `ondemand NAME [--once]` | The check the supervisor timer runs every 30 s (a one-shot, ~20 ms, no process in between): one small request to the printer, starts `3d-printer-watch@NAME` and `3d-printer-vision@NAME` while a print runs and stops them completely a grace period after it; `--once` shows what it sees — see [On-demand supervision](#on-demand-supervision-747). |
| `vision NAME` | Watch the camera for failed prints (spaghetti, stale/dark camera, the U1's own NPU verdicts) — see [Camera failure detection](#camera-failure-detection). |
| `bed status` / `reference` / `monitor` | Is the bed clear, is the build plate on — from the camera against an empty-bed reference; also the `bed` start gate and the `bed_clear` event — see [Bed state](#bed-state). |
| `subscribe` / `unsubscribe` / `subscriptions` | Route printer alerts to an AI agent first: it looks at the frame and tells the user only if the failure is real — see [printer-subscribe.md](printer-subscribe.md). |
| `agent list` / `stop` / `handoff` | The temporary per-printer omp agent (tmux) that gets the warning+ alerts no live subscriber took — see [printer-subscribe.md#the-temporary-printer-agent](printer-subscribe.md#the-temporary-printer-agent). |
| `doctor [NAME\|--all]` | Diagnose the printer's setup (Klippy, Moonraker, Spoolman link and CORS, active spool, camera, watch services, ...) and with `--fix` repair what is safe — the same command as `3d printers doctor`, see [printers-doctor.md](printers-doctor.md). |
| `macro list\|run` | The macros a Klipper printer defines, and ONE macro run with typed `KEY=VALUE` parameters — see [Macros and aliases](#macros-and-aliases-3d-printer-n-name-macro-home-load-). |
| `aliases`, `home`, `level`, `load`, `preheat`, ... | Short names for the usual operations, mapped per adapter, model and printer to the real macros — see [Macros and aliases](#macros-and-aliases-3d-printer-n-name-macro-home-load-). |
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
- `--colour-delta-e N` — `start`: the preflight `slot Tn` colour tolerance, CIEDE2000, default 10
  (see [Slot colour](#slot-colour)). `3d printer start u1 out/plate.gcode --yes --colour-delta-e 8`
- `--fix-colour` — `start`: first rewrite the file (and its project 3MF) to the colours the printer
  reports, print the old/new SHA-256, upload the fixed file, then run the preflight and start.
  `3d printer start u1 out/plate.gcode --yes --fix-colour`
- `--fix-max-delta-e N` — the largest ΔE `--fix-colour` rewrites across (default 30).
  `3d printer start u1 out/plate.gcode --yes --fix-colour --fix-max-delta-e 40`

## Slot colour

The preflight `slot Tn` line compares the colour the slicer wrote (`; filament_colour`) with the
colour the printer reports for that head by **CIEDE2000 ΔE**, default tolerance 10
(`--colour-delta-e`), and prints the distance. The same beige PETG is `#E8D8B0` in the slice and
`#FEE5A5` on the U1 screen: ΔE 6.4, PASS. A material mismatch is always a FAIL. Three ways out of a
real mismatch, none of them `--force`:

```bash
# 1. the file is wrong about the colour: rewrite it at the source (G-code + project 3MF + sidecars)
3d print-preflight out/plate.gcode --printer u1 --fix
3d printer start u1 out/plate.gcode --yes --fix-colour     # rewrite, upload the fixed file, preflight, start
# 2. the printer is wrong about what is loaded: write the head's record (idle printers only)
3d printer slot set u1 T4 --material PETG --color '#E8D8B0' --brand Elegoo --dry-run
3d printer slot set u1 T4 --material PETG --color '#E8D8B0' --brand Elegoo
# 3. load the filament the file was sliced for
```

`--fix` / `--fix-colour` set entry *n* of `filament_colour` and `extruder_colour` to the printer's
colour for every tool the G-code uses, in the G-code and in its project 3MF
(`Metadata/project_settings.config`, `plate_N.json`), keep mode and mtime, move
`NAME.slice-warnings.json` to the new G-code SHA-256 and update `NAME.manifest.json`, and print the
old and new SHA-256 of each file (the hash is part of the stored file name, hence the upload in
`start --fix-colour`). A head whose colour is further than `--fix-max-delta-e` (default 30) from the
slice is refused with nothing written: that is another filament, and relabelling it would hide a
wrong-colour print.

`slot set NAME T<n> --material M --color #RRGGBB [--brand B] [--sub-type S] [--dry-run]
[--force-official]` is `3d printers filament set NAME/T<n>` (`--brand` is its `--vendor`); `T<n>`
is the label the printer shows (U1: T1..T4 = G-code tools T0..T3). `slot show NAME` reads the heads.

**What the Snapmaker U1 accepts** (Snapmaker/u1-klipper `print_task_config.py`, issue #400): the
touchscreen's own Klipper command `SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=<i> VENDOR=… FILAMENT_TYPE=…
FILAMENT_SUBTYPE=… FILAMENT_COLOR_RGBA=RRGGBBFF`, sent through `POST /printer/gcode/script` (no
webhook endpoint exists for it) and read back from `print_task_config`. It needs vendor, type and
sub-type together (defaults Generic / Basic), persists to `print_task.json`, resets the head's
pressure advance (the answer prints the K it has now), and refuses a head with official RFID filament
unless `--force-official`. 3d refuses before sending when the fleet entry is not `access: full`
(read-only and offline printers), while printing or paused, or when Klipper is not ready. Not used:
the RFID path `FILAMENT_DT_UPDATE` / the Extended Firmware's `POST /printer/filament_detect/set`
(it marks the slot official, which locks later writes). Other adapters (Creality) have no
per-head write: `slot set` exits 2 and `slot show` is the read-only comparison.

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

## Macros and aliases (`3d printer -n NAME macro`, `home`, `load`, ...)

Issue [#718](https://github.com/alex-mextner/3d-cli/issues/718). Two ways to run what the printer's own
firmware knows, both through the adapter's G-code channel (`moonraker`: `POST /printer/gcode/script`).
The `creality-stock` adapter has no G-code channel (ws:9999 only starts and stops a file): every command
below refuses it with "does not support macros and aliases". The printer is named with `-n NAME`, like `todo`.

### `macro list` and `macro run`

```
3d printer -n u1 macro list                       # what the printer defines; * = in its trusted_macros
3d printer -n u1 macro list --offline --json      # registry data only (trusted_macros + alias targets); nothing contacted
3d printer -n u1 macro list --dry-run             # print the two GET requests, send none
3d printer -n u1 macro run SM_PRINT_AUTO_FEED EXTRUDER=1 --yes --dry-run
3d printer -n k1se macro run PAUSE
```

`macro list` reads `GET /printer/objects/list` (the `gcode_macro NAME` objects) and `GET /printer/gcode/help`
(every extended command with its description); `_private` macros need `--all`. `macro run` prints the exact
line that is sent (`gcode:` block) and the request on `--dry-run`. Gates, in this order, nothing is sent before
all passed:

1. **Typed tokens.** The name is an identifier (`[A-Za-z_][A-Za-z0-9_]*`, upper-cased); each value is one token of
   `A-Za-z0-9_.,:+/-` (at most 64 characters). A newline, `;`, space, quote, `{`, `%`, `#` or `*` is refused as an
   invalid argument, so a value can never append a second command.
2. **No emergency stop.** `M112` and `EMERGENCY_STOP`, as a name or inside a value, are refused whatever the flags:
   the printer's own stop control is the way to stop it.
3. **Access.** Only `access: full` printers; `read-only` and `offline` (never contacted) refuse, also with `--dry-run`.
4. **Defined on the printer.** The name must appear in the printer's `gcode/help`. A standard code (`G28`, `M104`, `T0`)
   is run only if the printer defines a macro of that name (the K1's `G29`) or through an alias.
5. **`--yes`** for a macro outside the printer's `trusted_macros` and for dangerous commands (`FIRMWARE_RESTART`, `RESTART`,
   `SAVE_CONFIG`, `FORCE_MOVE`, `M84`, calibrations that write config, ...), trusted or not.
6. **An idle printer** (state re-read now; printing, paused, busy and unreadable all refuse) for motion, heating and
   unknown macros. `PAUSE`, `RESUME`, `CANCEL_PRINT`, `M600`, `M220`, `M221` and the `SET_PAUSE_*` pair are allowed during a job.

A macro body is the printer's own config and is not checked, so any macro that is not known to be harmless is treated
as motion. `--offline` prints the script from registry data without contacting the printer (it cannot check 4 and 6).

**There is no raw `gcode` passthrough.** `3d printer gcode SCRIPT` does not exist: `/printer/gcode/script` runs any text, newlines
and `;` included, the model of this tool is that an agent never sends a free-text line to hardware, and a stock G-code
(`G1`, `M104`) cannot be tied to a trusted list. A command the table lacks is added as a named, typed alias in the
printer's own alias file (below) or run as a macro the printer defines.

### Aliases

`3d printer -n NAME ALIAS [args] [--yes] [--dry-run] [--offline]`. An alias is a table entry (`kind`, a `summary`, typed
`args`, `gcode` lines); its lines are vetted, so only `dangerous` ones and entries marked `yes: true` ask for `--yes`; the
idle-printer rule (6) and the definition check (4) still apply. The table is
[`lib/data/printer-aliases.yaml`](../../lib/data/printer-aliases.yaml) (schema in its header), layered per adapter, then
every model whose name contains the entry's `contains` text, then the printer's own entries in
`~/.config/3d-cli/printer-aliases.yaml` and `./printer-aliases.yaml` (the project file wins per alias):

```yaml
# ~/.config/3d-cli/printer-aliases.yaml or ./printer-aliases.yaml: printer name -> its aliases
u1:
  home: {summary: home X and Y only, kind: motion, gcode: ["G28 X Y"]}   # replaces the table's home
  nozzle-wipe: {summary: wipe the nozzle, kind: motion, gcode: [ROUGHLY_CLEAN_NOZZLE]}   # a new alias
  park: null                                                              # removed for this printer
  cooldown: {unsupported: "close the chamber door first"}                 # removed, with the reason shown
```

An alias that a model does not have says so (`alias park is not defined for k1se ...`, with the aliases it does have);
it is never guessed. `3d printer -n NAME aliases` lists what each alias sends on that printer and which are unsupported;
`3d printer ALIAS --help` shows one.

| Alias | Sends | Where |
|---|---|---|
| `home [XYZ]` | `G28` / `G28 X Y` | every `moonraker` printer |
| `level` | `BED_MESH_CALIBRATE`; Creality K1: the stock `G29` (heats, probes) | `moonraker`; K1 family |
| `qgl`, `z-tilt` | `QUAD_GANTRY_LEVEL`, `Z_TILT_ADJUST` | `moonraker`; refused with a reason on the U1 and the K1 SE (their stock `printer.cfg` define neither) and, on any printer, when the firmware does not define the command |
| `pause`, `resume` | `PAUSE`, `RESUME` (allowed during a job) | `moonraker` |
| `preheat MATERIAL` / `--nozzle C` / `--bed C` / `--head T2` | `SET_HEATER_TEMPERATURE` for the nozzle of the head (fleet `heads:`, default T1) and `heater_bed`; PLA 210/60, PETG 235/80, ABS 250/100, ASA 255/100, TPU 225/50 (nozzle/bed, typical values), nozzle 0-300, bed 0-120 | `moonraker` |
| `cooldown` | `TURN_OFF_HEATERS` | `moonraker` |
| `load`, `unload` | generic: `LOAD_FILAMENT`, `UNLOAD_FILAMENT` (Fluidd/Mainsail convention, refused when the printer has no such macro); K1: `LOAD_MATERIAL`, `QUIT_MATERIAL`; U1: `load T1..T4` = `AUTO_FEEDING EXTRUDER=n LOAD=1`, `unload T1..T4` = `AUTO_FEEDING EXTRUDER=n UNLOAD=1 STAGE=prepare` then `STAGE=doing` | per adapter/model |
| `level-heated [--bed C]` | `AUTO_BED_MESH_CALIBRATE TEMP=C` (heats the bed to 55 by default, soaks 2 min, meshes) | Snapmaker U1 |
| `clean-nozzle` | `ROUGHLY_CLEAN_NOZZLE` | Snapmaker U1 |
| `park [T1..T4]`, `idle-position [T1..T4]` | `PARK_EXTRUDER[n]`, `MOVE_TO_XY_IDLE_POSITION_EXTRUDER[n]` | Snapmaker U1 |
| `purifier idle\|cool\|preheat\|hot` | `SET_PURIFIER_MODE MODE=0..3` | Snapmaker U1 |

U1 facts come from the firmware config of [Snapmaker/u1-klipper](https://github.com/Snapmaker/u1-klipper)
(`lava/fluidd.cfg`, `lava/printer.cfg`, `klippy/extras/filament_feed.py`, `purifier.py`, `kinematics/extruder.py`); the
K1 SE ones from [CrealityOfficial/K1_Series_Klipper](https://github.com/CrealityOfficial/K1_Series_Klipper)
`config/K1_SE_CR4CU220812S12/gcode_macro.cfg`. None of them was run on a real printer by the author: the tests use
the loopback fake Moonraker only. Head arguments are the fleet labels (`T1`..), never a bare number, and an unusable
head (fleet `usable: false`) is refused with its reason.

Models without an adapter here (Bambu Lab, Prusa) have no aliases: they are `offline` in the fleet and every command above
refuses them; a Bambu/Prusa adapter would bring its own `adapters:` layer in the table.

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

### On-demand supervision (#747)

[#747](https://github.com/alex-mextner/3d-cli/issues/747): nothing may cost memory, CPU or disk while no print runs
(before it: two idle vision processes at 1.7 GiB in total, 4.6 % of a core, 104 file writes per minute). So no process
of ours lives between checks at all: a systemd **timer** per printer runs a ~20 ms one-shot check every 30 s, and the
heavy `watch` and `vision` units exist only while a print does.

| Unit | Runs | Costs |
|---|---|---|
| `3d-printer-ondemand@NAME.timer` | always armed and enabled (held by systemd, no process) | nothing |
| `3d-printer-ondemand@NAME.service` (`3d printer ondemand NAME`, `printer_ops/ondemand.py`) | one-shot, started by the timer every 30 s (30 s after the previous check finished) | ~20 ms CPU, ~15 MB for that moment, one 331-byte request, no journal line, no file write while idle |
| `3d-printer-watch@NAME`, `3d-printer-vision@NAME` | started by the check when a print is `printing` or `paused`, stopped `grace` seconds after it ends | as before, but only during a print (plus the grace period) |

The question (every timer tick): Moonraker `GET /printer/objects/query?print_stats&webhooks` over a raw socket
(`print_stats.state` is `printing` or `paused` and Klipper is `ready`: a print runs; HTTP 503 means Klipper is down: no
print); creality-stock reads the `ws:9999` state push once (state 1, 5 or 7). A websocket subscription was rejected:
Moonraker pushes `notify_proc_stat_update` to every websocket client each second, so it costs a 1 Hz wake-up and an
open connection on the printers' small boards; the printers cannot push a print start to us, and systemd path or socket
activation cannot see a remote printer. A resident polling process (the first design of #747, ~18 MB per printer and a
wake-up every 30 s) was replaced by the timer: the same checks, nothing resident.

```text
idle ──print runs──> active ──print ends / printer lost──> grace ──grace over──> idle (workers stopped)
                       ^                                      │
                       └────────────print runs again──────────┘
```

- **State between checks** is one small file per printer, `$XDG_RUNTIME_DIR/3d-cli/ondemand/NAME.json` (tmpfs: RAM,
  never the disk, gone at reboot together with the workers): the resolved printer address and the mtimes of the config
  files it came from (the heavy yaml/fleet loader runs in a one-shot child only when one changed), the phase, the
  grace deadline, the miss count and the last error. It is rewritten **only when its content changed** (a transition, a
  config change), so an idle printer writes nothing. systemd's own `Starting`/`Finished` journal lines are silenced
  with `LogLevelMax=notice`; the check logs one line per transition (print started, print ended, grace over, printer
  lost, printer back), never per check.
- **One check at a time per printer**: a check holds an exclusive non-blocking `flock` on `NAME.json.lock` (created once,
  afterwards opened read-only, so an idle check writes nothing for it). The timer never overlaps its own one-shot, but a
  hand-run `3d printer ondemand NAME` can; a check that finds the lock taken exits at once and leaves the work to the
  other one (exit 75, a failed run on purpose, so a skipped check never looks like a fresh poll in `supervise status`),
  so an older check can never stop workers a newer one just started. The cached printer address is dropped when the
  selected fleet file (`THREED_FLEET`), the config directory or the checkout changes, not only when a config file's
  mtime does.
- **A print started from anywhere** (touchscreen, Fluidd, slicer, Home Assistant, macro) is seen at the next check (at
  most about 30 s); the workers start before the first layer (heating takes minutes). The watcher then announces it as
  `print_started` with `adopted: true`.
- **The end**: the workers stay `--grace` seconds (default 300; the end-of-print events, the bed check, a print that
  starts again), then `systemctl --user stop` removes them completely: no ONNX model in memory, no sockets, no timers,
  no heartbeat file. `--grace 0` stops at the first check without a print.
- **Printer unreachable**: retried at the next tick once, then every 2 minutes (a deadline in the state file, so it holds
  for any timer interval; the timer keeps firing and the check exits at once in between, writing nothing; the state
  file is rewritten once per ask while the printer stays off); one journal line when it goes away and one when it is
  back; workers are never started for it. A printer that disappears during a print counts like an ended print
  (the grace period applies at every tick; the watcher alerts about the lost connection meanwhile).
- **Reboot, or the timer restarted**: the workers are separate units and nothing else tracks them. After a reboot the
  state file and the workers are both gone; a print already running starts them at the first check. A missing state
  file while workers run (the runtime directory was cleaned, or someone started them) gives them one fresh grace
  period.
- **A broken timer or check** shows in `supervise status`: the timer must be active and enabled, and the time of the last
  check is when the check *service* last became inactive (`InactiveEnterTimestampMonotonic`, numeric on every systemd
  version), never the timer's trigger, which also fires while the check crashes. A check that failed at its last run
  (`check=failed`) and a printer configuration the check cannot read (`config=unreadable`, also before the first
  successful check) are findings; the latter is logged once. A printer that the check's own context refuses
  (`config=refused`: the unit sees another fleet, config directory or checkout than your shell) is a finding too.
- **The backups disk is missing** (the checkout lives on a bind mount of it): the check service has
  `ConditionPathExists=` on the checkout, so every tick is skipped silently, not failed; the timer keeps running and the
  checks resume when the disk is back.
- **A worker that dies or is stopped by hand during a print** is started again at the next check.
- **A worker started by hand while the printer is idle** (or by a mount-retry timer) is found by the stray look, one
  `systemctl is-active` on about one check in twenty (every ~10 minutes at the 30 s interval, chosen at random so it
  works for any interval), and stopped after one grace period: an idle printer
  never keeps workers running by accident.
- **Bed monitor**: the watcher's camera bed monitor (`watch.bed.enabled`) only runs while the watcher does, i.e. during
  the print and the grace period; `3d printer bed status` and the start gate still take a fresh reading on demand.
  Raise the grace period when you want `bed_clear` events for longer.
- **Timing**: the check interval is the timer's: `systemctl --user edit 3d-printer-ondemand@k1se.timer` with
  `[Timer]` `OnActiveSec=` (an empty assignment resets *every* timer setting), `OnActiveSec=5s` and
  `OnUnitInactiveSec=10s`; without the reset the new interval is added next to the shipped 30 s. A shorter interval
  starts the workers sooner; a longer one delays them by as much, and `supervise status` judges with the 30 s (see above). The grace period is a drop-in on the service so the unit keeps its shipped command:
  `systemctl --user edit 3d-printer-ondemand@k1se.service` with `[Service]` `Environment=THREED_ONDEMAND_GRACE_S=900`
  (read at every check, so it applies at the next one: the only lasting setting; `--grace S` on a hand-run applies to that one check only,
  the next timer check computes the deadline from the drop-in again).
- **The workers have no `[Install]` section**: nothing starts them at boot, and `systemctl enable` on one is refused.
  They carry `SuccessExitStatus=143`: the check stops them with SIGTERM and `uv run`, the vision launcher, answers
  143; without it every stop would leave the unit `failed` and run the failure hook.
  `supervise install` enables the timer and *disables* (never stops: a print may be running) workers an older
  always-on install left enabled; the check stops those after the grace period. It also retires the long-lived
  `3d-printer-ondemand@NAME.service` process of the first #747 design (`disable --now`; its workers keep running and
  are adopted by the first check). `supervise uninstall` disables the timer and stops the workers.
- **`supervise status` / the doctor**: the timer must be active and enabled; a stopped worker is the right state while
  the printer is idle, off or unreachable and a FAIL while it prints (a WARN for the first 45 s after the last
  completed check, which is the start lag). The thresholds assume the shipped 30 s interval: with a longer one the
  start lag outlasts the 45 s and a print that has just begun is judged a FAIL until the next check.

```bash
3d printer ondemand u1 --once    # {"print_active": false, "workers": {...}, "would": "nothing"}; changes nothing
cat "$XDG_RUNTIME_DIR/3d-cli/ondemand/u1.json"    # what the last check saw (phase, seen, fails, error)
systemctl --user list-timers '3d-printer-ondemand@*'
```

### What runs while a print runs

| Process | What it does | Needs | Never needs |
|---|---|---|---|
| `3d-printer-watch@NAME` (systemd user unit, on demand) | websocket to the printer (Moonraker, creality-stock ws:9999): errors, pauses, progress, `print_started`; waits for a printer that is off (retries every ≤ 30 s, silent) | the printer's own API | Home Assistant, an omp session, a login |
| `3d-printer-vision@NAME` (only with a camera, on demand) | polls the printer's state, fetches camera frames only while it prints, resets the per-print baselines at every print start, finds spaghetti / a detached part | the printer's own camera URL (`watch.vision.camera`) | Home Assistant, go2rtc, an omp session |
| the alert chain, inside both workers | live omp subscriber → temporary omp agent → `tg` → autonomous pause (`watch.pause_without_agent`: once per job, only PAUSE, never resume or cancel) | `tg`; `tmux`+`omp` for the agent | Home Assistant |
| `loginctl enable-linger` | the supervisor timer starts at boot and survives logout | — | — |

The units are generated from `fleet/printers.yaml`: every printer with `owner: own`, `access: full` and an adapter with
a watch stream (`moonraker`, `creality-stock`) gets a supervisor timer (with its one-shot check service) and a watch unit, and a vision unit when `watch.vision.camera` (or its
model's default) exists. The workers carry no `--dry-run` (a service holding its alerts is the 2026-10-22 incident), have
`Restart=always` with `StartLimitIntervalSec=0` (a crash loop never gives up), `After=network-online.target
hsw-observer-agent.service` (ordering only — no `Wants=`, so nothing depends on that session; a drop-in with
`Wants=hsw-observer-agent.service` keeps working); only the supervisor timer has an `[Install]` section (`WantedBy=timers.target`).

```bash
3d printer supervise install --all --plan   # what it would do, changes nothing
3d printer supervise install --all --yes    # templates, daemon-reload, enable --now the timers, enable-linger
3d printer supervise status                 # exit 1 (FAIL) when a printing/answering printer is unsupervised
3d printer supervise status u1 --json
3d printer supervise uninstall k1se --yes
3d printers doctor --check printer-supervision [--fix --yes]
```

After updating the checkout (`git pull`) run `supervise install --all --yes` again: an older unit file still runs the first design's resident supervisor,
which the one-shot check cannot serve (systemd restarts it every 30 s until the install replaces the unit).

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

The supervisor timer and its one-shot check write nothing while idle (only the RAM state file on a change, see above). While a print runs each worker writes a heartbeat: `watch/NAME.json` every 15 s (`last_poll_ok`, `unreachable_since`, `job`, `quiet` per
printer) and `vision/NAME.json` every cycle (`ts`, `last_poll_ok`, `reachable`, `last_frame_ts`, `supervising`, `job`).
`supervise status` and the doctor check `printer-supervision` FAIL when a printer that **answers** (printing or idle)
has a missing/inactive supervisor timer, or is printing and a worker is missing/stopped, a heartbeat older than 75 s (watch) or max(90 s, 4 cycles) (vision), a watcher that is
disconnected for over 2 min from a printer that answers us, no camera frame for 3 min while a print is supervised,
two watchers for one printer, a `--dry-run` unit, or linger off. The same gaps on a printer that does not answer are
warnings: nothing is being missed, but the next print would be.

If a worker dies while a print runs the user gets **one** short `tg` line and systemd restarts it
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
  `~/.local/share/3d-cli/bed/NAME/`; `3d printer bed reference u1` lists them (with plate type,
  light state and age).
- **References keep themselves in order** ([#723](https://github.com/alex-mextner/3d-cli/issues/723)).
  A reference is per printer, **plate type** (`watch.bed.plate`, default `default`), **light state**
  (the printer's light `on` / `off` / `unknown` when the frame was taken) and Z, and has its time.
  A frame is judged against the reference of its own light state first; the other light state is a
  fallback that the verdict marks `reference_light_mismatch` and judges by colour *and* texture (see
  *Another light* below). A reference older than `watch.bed.reference_max_age_days` (default 30), or
  taken with another plate type, never makes the verdict `occupied`: it is `unknown`, with the
  age and the refresh command. `3d printer bed reference NAME --refresh [--assume-empty "why"]`
  re-captures from the best view with the light on and replaces the reference of the same plate
  type, light state and view. Without `--assume-empty` it stores the frame only when that very
  frame is a **confident `clear` with the plate on, in a good view (`auto_view_min`) with the
  light on** (a printer without a light needs no light); otherwise it refuses, says why and
  stores nothing (exit 1). `--assume-empty "why"` is your word instead of that verdict; the reason
  is kept in the reference (`assumed_empty`) and in the journal. With `watch.bed.auto_reference:
  true` (default off) `bed status` / `bed check`, the preflight `bed` line and the one-shot
  `bed-free` check at the end of a job do it by themselves after such a confident clear on an
  idle printer (`auto_reference` in the verdict). Every refresh is one JSON line (`ts`, `mode`
  `manual` | `assumed` | `auto`, `why`, `z`, `light`, `verdict`) in
  `~/.local/state/3d-cli/bed/NAME.refs.log`. Config: `plate`, `reference_max_age_days`,
  `auto_reference` under `watch.bed`.
- **Calibrate** (`3d printer bed calibrate NAME [--z-from A --z-to B --z-step S] [--part-height MM] [--json]`,
  [#723](https://github.com/alex-mextner/3d-cli/issues/723)). A printer without a camera calibration (the
  K1 SE) used to count the plate as «in the frame by definition» at every Z, so the check never knew a poor view
  and never moved the bed to a better one. `bed calibrate` learns it: on an **idle, homed, writable** printer
  (refused otherwise, nothing moves) the bed goes through a grid of Zs **in Z only** — the guarded move of
  `bed check`, within the clearance floor (`--part-height` + 15 mm, never below Z 10) and the axis ceiling
  (`resume.z_max_mm` - 5), `--z-from` / `--z-to` default to those limits and `--z-step` to 20 mm (at most 60
  Zs; a grid outside the limits is refused), with the printer's light on — takes one fresh frame per Z, and puts the
  bed back in a `finally` (exceptions, Ctrl-C, SIGTERM; the displaced-Z marker is the same as the auto view's).
  Then, without touching the printer, the plate is **followed** over the frames: from its outline in the
  nearest empty-bed reference (`watch.bed.plate_mask`, the alias of `roi`, drawn on it; else the reference's own
  bed surface; with neither, the largest dark low-saturation region of the frame — rough, the result says
  «check it») frame to frame by the affine transform that carries the plate onto the next frame (ECC,
  correlation based, so a light change does not move it; the polygon moves with it, so the part of the plate that
  has left the frame is still counted). The visible share of the polygon per Z, the plate polygon where most of it
  shows and the grid are stored in `~/.local/share/3d-cli/bed/NAME/calibration.json`. A plate that cannot be
  followed (too little of it in the frame, no texture, correlation under 0.5) ends the chain: those Zs are listed
  as `unfollowed` and **not stored** (no view is invented). `bed_in_view` / the auto view / `out of view` then use
  the learnt shares (linear between the Zs, unknown beyond them) for a printer that has **neither a camera
  calibration nor a `watch.bed.roi`**; `watch.bed.views: {70: 1.0, 200: 0.15}` overrides them. Limits: the
  model is affine (a big move's perspective is followed approximately), the plate must show texture, the step must
  be small enough for the plate to move a few tens of px between two Zs, and the share is relative to the plate's
  area at that Z. Exit 1 when the bed could not be put back.
- **Out of view.** The calibration also says how much of the bed is in the frame: the U1 camera
  sits about 12 mm above the bed's Z 0, so the lower the bed goes the less of it shows (Z 10: 98 %,
  Z 200: 15 %, Z 215: under 10 %, Z 275 — where a home leaves it — none). Under 10 % the state is
  `unknown`, "out of the camera's view … it sees the bed again at Z 210 or below", and no
  reference is taken there: it would read every low part as a clear bed
  ([#402](https://github.com/alex-mextner/3d-cli/issues/402)).
- **A camera that moved** (`lib/bed_pose.py`). The K1 SE camera rides with the toolhead, so after a job
  or a home the same empty plate is seen from another place: the whole picture, background included, is
  shifted and zoomed by tens of px. Against a reference taken from elsewhere that read «70 % of the bed surface
  changed» — `plate missing`, FAIL, start refused, on a plate that was on (2026-11-13). The frame is now
  *located* against its reference before it is judged, by the scene round the bed region (what a plate or a part
  cannot change). **Same view** (the edge structure of the scene correlates ≥ 0.85, or in another light the
  scene does not shift, or the SIFT homography moves the bed under 4 px — a fixed camera, in any light):
  judged as before. **Moved** (SIFT features agree on a plausible homography, after warping the scene lines up
  again, edge correlation ≥ 0.65, and ≥ 90 % of the bed region is still in the frame): the frame is warped onto
  the reference and judged there with the reference's ROI; `reason`, the metrics `pose_shift_px` /
  `pose_inliers` and `pose` in the verdict say so, the evidence frame marks the pixels on the camera frame; a
  `--no-plate` reference taken from another view is not used. **Lost** (no consistent match, the scene lines
  up only far away — the camera sits lower or closer —, or too little of the bed region is in the frame):
  pixels say nothing, anything but a clear bed with the plate on becomes `unknown` and the preflight `bed` line
  a WARN naming the fix: with the plate on and the bed empty take a reference from where the camera is now
  (`3d printer bed reference NAME --capture`), or put the toolhead back where the reference was taken;
  `bed check --assume-empty "why"` then works as for any unknown. A plate that is really off and a part that is
  really on still FAIL when the camera moved and could be aligned. A K1 SE `watch.bed.roi` polygon is a
  region of the reference's picture: redraw it when the reference is taken from another view than the one it
  was drawn on, or it covers mostly the wall (a whole-frame ROI leaves no scene to locate by: judged as before).
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
- **The light** (`watch.bed.light`, on unless `enabled: false`). A frame of a dark chamber is not
  comparable to the reference, so every camera frame of the bed — `bed check`, `bed status` when it
  takes a frame, `bed reference --capture`, the preflight `bed` line and the [job
  watch](#job-watch) frame — is taken with the printer's own light on, and the light goes back as it
  was found (in a `finally`: also after a failed frame, Ctrl-C or SIGTERM). The light is found among
  Moonraker's `printer.objects.list`: `led NAME`, `neopixel NAME`, `dotstar NAME` (switched with
  `SET_LED`) or `output_pin NAME` (`SET_PIN`) whose name says light, lamp, led or chamber (light/lamp
  first, LEDs before pins). Its state is read first (read-only `objects/query`: `value` of a pin,
  `color_data` of a LED chain); a light that is already on is not touched; after switching on it waits
  `settle_s` (1.5). Name it when the guess is wrong, or give a macro:
  `light: {object: "output_pin chamber_light", on_gcode: LIGHT_ON, off_gcode: LIGHT_OFF, settle_s: 1.5}`.
  Every `on_gcode`/`off_gcode` command must be `SET_LED`, `SET_PIN` or listed in the printer's
  `trusted_macros` (checked when the config is read, like `clear_ack`); a macro whose light state
  cannot be read is switched on and, its previous state being unknown, left on (the report says so).
  `enabled: false` opts out. **No light found:** the verdict says `no light found (...)` and the
  frame is taken as it is. **Never touched while a job prints or is paused**, nor on a printer that is
  not full-access; the one exception is the job watch, whose frame is taken during the print: only
  when the light was off, switched on for the frame and off right after. The `3d printer watch` bed
  *monitor* only reads idle frames and never touches the light. The verdict (`--json`) carries
  `light`: `found`, `object`, `was_on`, `switched`, `restored`, `on_for_frame`, `error`, `text`, and
  `check` / `status` / `reference` print it as a `light:` line. `light.on_for_frame` tells a later
  comparison whether the frame was lit.
- **Bed free, once.** `3d printer subscribe --session ID --printer u1 --once --on bed-free`: when
  the running job ends (a `3d printer watch` sees printing/paused → idle / complete / cancelled) the
  watcher waits 20 s for the end macro, runs the auto-view bed check, sends **one** `bed_free`
  event with the verdict and the evidence frame to that omp session and removes the subscription.
  See [printer-subscribe.md](printer-subscribe.md#one-shot-bed-free).
- **How.** Frames are decoded at 480 px; the current one is mapped onto the reference colours by
  a robust gain/offset fit (auto-exposure, room light) and a median detrend removes smooth light
  gradients; a changed region of ≥ 200 px (or ≥ 8 % of the bed view) is `occupied`, under 100 px
  and ≤ 3 % is `clear`, in between `unknown`. Two things keep light and registration drift from
  reading as objects ([#723](https://github.com/alex-mextner/3d-cli/issues/723): the idle U1 at
  Z 200 reported «objects on the bed 14.3 %» on an empty plate — the window light behind the
  printer had changed, a cable had moved and the picture sat a pixel or two off the October
  reference): a pixel that matches the reference anywhere within `shift_px` (3) counts as
  unchanged, and changed regions are opened with an `open_px` (5) square, so an edge outline
  or a thin line goes while a patch of new colour stays. Where the camera sees under `low_view`
  (0.5) of the bed (U1 at Z 200: 15 %) most of the ROI is the machine and the room behind the
  bed, so there only a region of `occupied_blob_low_px` (1200 px) or `occupied_min` of the ROI
  says `occupied`; a smaller change is `unknown` with «the room behind the bed? …
  `3d printer bed check NAME` looks from a better Z» and the age of the reference. A real part
  is not hidden by this (the U1 frames of parts on the bed are 2700–3900 px regions, a 16 px
  object on the K1 SE stays `occupied`); a thing smaller than that from a 15 % view is
  `unknown`, never `clear`. **Another light** (day vs night, the printer's light on vs off, a gamma
  or a white-balance shift of the camera): when the brightness of the bed at its dark, middle or
  bright end is off by more than ×0.8…×1.25 from the reference's, a pixel also needs its local
  *texture* (luminance over the spread of its neighbourhood, `lib/bed_norm.py`) to have changed —
  gain, offset and any tone curve leave the texture alone, a part does not. Pixels only the colour
  difference flags are the light: a frame that would read `occupied` by colour alone is `unknown`
  («the light, not a part», never `occupied`), a real part changes both and stays `occupied`. At a
  low view the texture channel may rescue an `occupied` but does not call the bed `clear`.
  `thresholds.light_norm: 0` turns it off for a printer. Every evidence image has the untouched camera frame beside it
  (`…-label.raw.jpg`), so a doubtful verdict can be re-judged. ROI: `watch.bed.roi` (a polygon of the bed, 0..1 of
  the frame), else the camera calibration (bed plus 80 mm above it at the current Z), else the
  whole frame. On the U1 at Z 200 only parts taller than a few cm show, and only the plate's front
  edge is in view (about 2 % of the frame), so the plate verdict rests on that strip and the
  reason says how much of the bed the camera sees; the U1's own `DETECT_BED_PLATE` still checks
  the plate at start. Below 2 % of the frame the surface counts as out of view (plate `unknown`,
  the bed check WARNs). These pixel limits are the defaults tuned on the U1 and K1 SE frames;
  `watch.bed.thresholds: {clear_max: 0.05, level: 30, ...}` overrides any of them for one printer
  (`level`, `detrend_px`, `edge_px`, `clear_blob_px`, `clear_max`, `occupied_blob_px`,
  `occupied_min`, `plate_missing_min`, `plate_tie`, `surface_min`, `change_min`, `settle_frames`,
  `match_px`, `view_min`, `shift_px`, `open_px`, `low_view`, `occupied_blob_low_px`, `light_norm`). The camera is the printer's `watch.vision.camera`; enabling the bed check
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
- **Start gate.** `3d printer start` and `3d print-preflight --printer` add a `bed` line. On an idle printer
  the line runs the **full [bed check](#3d-printer-bed-check) path itself**: a fresh frame from the best
  view (the bed moved in Z only, and back, even when `watch.bed.auto_view` is off), with the printer's
  light on (see *The light* above) and restored — never the watcher's verdict at whatever Z the bed rests at.
  `--no-bed-move` judges at the current Z (light still on). **FAIL only on a confident `occupied`**
  (a changed region of the normal occupied size with at least `watch.bed.auto_view_min` of the plate in
  the frame at the Z it was judged at; the verdict's `occupied_confident`) or a missing plate. PASS on a
  clear bed with the plate. **WARN** (never FAIL) for `unknown` (no reference at this Z, out of view,
  plate out of view, camera down) and for an occupied seen at a poor view: the U1 at Z 200 sees 15 % of
  the plate and a window light there once read as «16.0 % changed: occupied», so that line is a WARN
  that gives the reason, the share of the plate in view and `3d printer bed check NAME` to run. A
  [forced start](#forcing-a-start) overrides a FAIL.
- **Clear acknowledgement** (`watch.bed.clear_ack`, off by default): after `bed_clear` the
  printer makes one motor sound so the person at it knows the camera saw the empty bed. Neither
  printer has a beeper, so the command is `STEPPER_BUZZ STEPPER=stepper_z` (Klipper: ±1 mm ×10,
  ~10 s, net zero, works unhomed); a list of lines is allowed (the K1 SE can use shorter
  `FORCE_MOVE` pulses). Besides the auto view's Z move and the light's on/off, it is the only write the bed code makes: every command word must be in
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
      light: {object: "output_pin chamber_light", on_gcode: LIGHT_ON, off_gcode: LIGHT_OFF, settle_s: 1.5}  # optional: autodetected when absent; enabled: false opts out
      plate: textured           # plate type in use (default `default`): references are kept per plate type
      reference_max_age_days: 30  # an older reference never makes the verdict `occupied`
      auto_reference: false     # refresh the reference after a confident clear check (default false)
      views: {70: 1.0, 200: 0.15}  # a camera without a calibration: share of the plate in the frame per Z (overrides `bed calibrate`)
      camera: http://k1se:8080/?action=snapshot   # the bed camera's snapshot URL (default watch.vision.camera, else the model's)
      plate_mask: [[0.31, 0.48], [0.72, 0.27], [0.92, 0.55], [0.92, 0.97], [0.41, 0.97]]  # the plate polygon (alias of roi)
k1se:
  watch:
    bed:
      enabled: true
      roi: [[0.31, 0.48], [0.72, 0.27], [0.92, 0.55], [0.92, 0.97], [0.41, 0.97]]
```

A Home Assistant bed sensor is a follow-up: [#305](https://github.com/alex-mextner/3d-cli/issues/305).

### `3d printer bed check`

`3d printer bed check NAME [--expect empty|part] [--no-move] [--part-height MM] [--json]` is **the** tool
to look at the bed: a fresh frame judged from where the camera sees the plate. An idle printer's bed is
moved in Z only (the guarded move of auto view, even when `watch.bed.auto_view` is off), judged and put
back; `--no-move` judges at the current Z. The frame is taken with the printer's light on (switched on
before the bed moves, restored after the bed is back; see *The light* under [Bed state](#bed-state)).

- **Result:** `empty` | `objects` | `out-of-view` | `unknown` | `busy` (printing: the bed belongs to
  the job, nothing is moved; use `3d printer watch NAME --job`). `--expect part` reads the same
  pixels as `part-present` | `part-gone` — after a print, is the part still on the plate?
  `fraction` is the share of the bed view that differs from the empty-bed reference; `frame` is the
  annotated picture (ROI cyan, changed pixels red), `raw_frame` the camera frame it came from.
- **Exit codes:** 1 when `--expect` is given and the result differs (and for `unknown`,
  `out-of-view`, `busy`), else 0.
- **Reference at the best Z:** `3d printer bed reference NAME --capture --best-view` moves the bed
  to where the camera sees the plate, takes the empty-bed reference there and puts the bed back.
  `3d printer bed reference NAME --refresh [--assume-empty "why"] [--part-height MM] [--json]` is the
  same flow for replacing a stale reference (see *References keep themselves in order* under
  [Bed state](#bed-state)); examples: `3d printer bed reference u1 --refresh`,
  `3d printer bed reference u1 --refresh --assume-empty "plate wiped, I looked at it"`.

### The owner's word: `bed check --assume-empty`

`3d printer bed check NAME --assume-empty "why" [--assume-empty-ttl MIN]` is the **auditable** way out of a
camera that is wrong about an empty bed ([#723](https://github.com/alex-mextner/3d-cli/issues/723)); it replaces
`3d printer start --force` for that case. It runs the normal check, then records your confirmation:

- `assume-empty-<ts>.json` in `~/.cache/3d-cli/vision/bed/` next to the frames: who (`$USER`), when, why, the
  printer, the camera's verdict, the annotated and raw frame paths, Z, the light and the job it is bound to;
- the state record `~/.local/state/3d-cli/bed/NAME.assumed.json` (`ts`, `why`, `ttl`, `job_marker`).

The `bed` line of `3d print-preflight --printer` / `3d printer start` is then **PASS** with the note «owner
confirmed empty at <time>: <why>» (the camera is not asked) while the record is younger than
`--assume-empty-ttl` minutes (default 60) **and no job has started since**: the record carries Moonraker's newest
history job id (and `print_stats.filename`), any other one later ends it (without the history component only a
different file name does; the ttl ends the rest). It never overrides a printing/paused printer, nor a camera that
sees the plate missing; `why` must be non-empty (exit 2 otherwise). The preflight's FAIL / WARN lines of the bed
name this command. Exit 0 when the confirmation was recorded, 1 when it was refused (printing printer,
unreadable printer, plate missing). Examples: `3d printer bed check u1 --assume-empty "plate wiped, I looked"`,
`3d printer bed check u1 --assume-empty "empty" --assume-empty-ttl 15 --no-move`.

## Job watch

Why: on 2026-11-04 a part came off the plate during a print and nobody noticed, the printer went on
extruding into thin air. `3d printer watch NAME --job --yes` guards **one** running print and stops
it when it is lost. It is not the always-on [`3d printer watch`](#watching-printers) (read-only,
alerts an agent): this one *acts*, so it needs `--yes` — you authorise a pause/cancel of this job.

```
3d printer watch NAME --job --yes [--on-fail auto|pause|cancel] [--gcode FILE.gcode] [--out DIR]
    [--interval S] [--checkpoints L1@25%,L1@50%,L1@100%,25%,50%,100%] [--dry-run | --tg-dry-run]
    [--fixture SCENARIO.json] [--duration S] [--wait-start S] [--no-quality]
```

Start it right after `3d printer start NAME FILE --yes` (or give `start` [`--watch`](#start---watch));
it follows that job until it ends.

**What is checked**, every `--interval` (60 s; 15 s while layer 1 prints) and at the checkpoints (a
quarter, half and all of the first layer — the first-layer marks need `--gcode` — and 25/50/100 %):

- *Camera.* The plate against an empty-plate baseline taken at the start of the job. The part's
  footprint is learnt from layer 3 on, then `part_gone` (little of it left), `part_moved`,
  `spaghetti` (much new material beyond it) and the `arm` state. A finding needs **2 readings of the
  last 4**: a toolhead passing over the part is not a finding. With the plate out of view, no
  baseline, or a plate that cannot be followed (no camera calibration: see
  [below](#job-watch-no-calibration)) the camera says `blind` — **never "gone"**. Every
  frame is taken with the printer's light on when it was off (and off again right after: the only
  write the watcher makes besides pause/cancel; `watch.bed.light`, see *The light* under [Bed state](#bed-state)).
- *Signals.* The same Moonraker events as the always-on watch (filament runout, heater loss, bed off,
  exception, G-code error, homing lost), and the printer's own defect detection (`noodle` verdicts
  with p >= 0.8, read from klippy.log).
- *Print quality* ([below](#print-quality-monitor)). Is the print GOOD, not just still there: stringing
  debris, a lost bead pattern (stripes, roughness), an uneven first layer. Warnings only, never a stop.

**Actions on a critical finding**, in this order, each step runs even if an earlier one failed:

1. save the frames and `finding.json`;
2. **stop** the job (after re-reading the printer and binding it to the watched file — never another
   job, never an idle printer; once per job, verified in the printer state);
3. `tg call me --say "<russian text>"` rings the owner;
4. the klippy.log lines of the job via `3d printer evidence` (GET only) into the same folder;
5. `tg --photo FRAME "<text>"` with the frame that showed it.

A warning (part moved, nothing seen, a heater drift) is only a tg message with the frame; nothing is
touched and nobody is called.

<a id="print-quality-monitor"></a>
**The print-quality monitor** (#733; `lib/print_quality.py` measures, `printer_ops/quality.py` decides) runs
on the same frame as the part check, in the plate region at the print's own Z. Why it exists: on 2026-11-05 a
10 h sheet of small tags stringed from layer 2 on — white thread debris all over the part — while the part
check (it only asks "is the part where it was") and the spaghetti model (no stringing class: p 0.0 on all
12 frames) stayed silent.

| number | what it is |
|---|---|
| `fleck_frac` | share of the plate region covered by small bright **neutral** specks and strands (the top-hat of the minimum colour channel: high only where R, G and B are all high, i.e. white debris — not beige part, not golden specular grain) |
| `order` | structure-tensor coherence: 1 = every neighbourhood has one dominant bead direction, lower = debris, webs, broken or striped lines |

The judge compares them with the **job's own first layers**: the first 4 readings with the part on the plate
(bed_part says so) are the baseline, never judged against anything but a floor; a clean reading drifts it a
little (bounded at 1.3x debris / 0.04 order), a bad one never does. Issues: `stringing` (debris against the
baseline AND an absolute level), `texture` (the pattern got lost with no debris: stripes, roughness,
under-/over-extrusion), `first_layer` (an absolute floor on `order` while layer <= 3; ON by default only for the U1, whose camera it was calibrated on — `first_layer_order: 0.6` in `watch.quality` turns it on elsewhere, a K1 SE view of a part reads 0.5 normally). Levels: `notice`
(report only) < `warning` < `severe`; a level is reported when 2 of the last 4 readings reach it (3 of 4
for severe), it is latched and clears after 6 clean readings. A debris trend (ratio to the baseline gained
per 10 min) lifts a notice to a warning.

A quality finding is **never critical**: one `tg --photo FRAME "<russian text: what is seen, what to
change>"` per issue and level (the frame has the debris marked in red), nobody is rung, the job is not
touched. Part gone, spaghetti and the noodle verdict still act as above. `--no-quality`, or
`<printer>.watch.quality: false`, turns it off; `<printer>.watch.quality: {baseline_n: 4, warning_ratio: 2.0,
warning_abs: 0.035, ...}` tunes any `QualityConfig` field (a wrong key is an error naming the valid ones).

**The monitoring report.** When the watch ends — the job completed, a lost print stopped it, or `--duration`
ran out — `DIR/<date>-NAME-monitor-report/` holds `report.json` (readings, counts per severity and per issue,
the alerts that were sent, the baseline, the worst reading, the timeline), `report.md` (the same for a human)
and `worst-frame.jpg`. A job with nothing wrong still gets one ("0 warnings in 41 judged readings"): silence
then means *checked and fine*, not *never ran*. Calibration, limits and the numbers on the real frames:
[print-quality-monitor.md](../research/print-quality-monitor.md).

**`--on-fail`.** `auto` (default): CANCEL when the part is gone, or on a camera finding in the first
layers (layer <= 3); PAUSE for everything else (runout, heater, later spaghetti: resumable).
`pause` / `cancel`: always that. Defaults can also be set in `<printer>.watch.job` (`on_fail`,
`interval_s`, `min_view`, `confirm`, `window`, `arm_layer`, `out_dir`, …); flags win.

**It never starts a job and never moves an axis.** The only printer writes are PAUSE and CANCEL; no
G-code, no resume, no start. In particular **the bed is not moved during a print**: the U1 camera sees
98 % of the plate up to Z 30 and 77 % at Z 100, so the watcher looks at the print's own Z. Above about
Z 130 the plate is mostly out of view and the camera is `blind` (`min_view` 0.5): the signals keep
running, the part check does not.

<a id="job-watch-no-calibration"></a>
**A printer without a camera calibration (the K1 SE, a `watch.bed.roi` polygon) follows the plate.**
Nothing says how the bed's move in Z shows in its picture, and the old rule (judge the part only within
`z_tol_mm` = 2 mm of the baseline's Z) blinded the camera after ~10 layers. The job watch now *chains*
the baseline:

- Every healthy reading (label `present`, overlap >= `heal_overlap`, outside < 0.3) saves, next to the
  part's footprint, the affine transform that carries the empty baseline's plate onto that frame
  (`track.json` in the watcher's working folder).
- The next reading starts from that transform and refines it by ECC (OpenCV `findTransformECC`:
  correlation based, so a global light change does not move it) on the plate pixels only, **without the
  part's footprint neighbourhood**: the part is excluded from the measurement of the drift. Layer to
  layer the bed moves 0.2 mm (about a pixel), so the chain never has to jump, and every step is anchored
  to the true empty first-layer baseline, so errors do not accumulate. The footprint is carried along by
  the change of transform, the baseline is warped onto the frame, the compared region is the plate as it
  moved.
- **Gone** is then the footprint of the current frame matching the empty plate again whatever the Z:
  a vanished part reads as overlap ~0, a part that stands as overlap ~1, a lighting change as neither.
- Within `z_tol_mm` of the baseline's Z a failed tracking falls back to the baseline as it stands (the
  old behaviour); beyond it the camera is `blind` with the reason (`... the plate cannot be followed on
  the picture: ...`), never `gone`.

*Limits.* The plate must show texture (a blank plate cannot be followed). The transform may not jump
more than `TRACK_STEP_PX` (12 px of the 480-px frame, a few mm of bed) between two healthy readings (the
camera was bumped, the watch was blind for a long time while the bed went far, a pause with a big Z
hop): `blind`. An ECC correlation under 0.6 is `blind`. The model is affine, the real bed move is a
perspective one: approximate over a big drift, absorbed by the comparison's shift tolerance. A part so
tall that its top leaves the plate polygon, or spaghetti over most of the plate, may make the plate
untrackable: `blind`, the signals keep running. A toolhead or a hand over the plate for one reading
only lowers the correlation of that reading.

**Exit codes.** 0 the job ended, nothing lost; 3 a lost print was found and the stop attempted;
1 it could not run.

**Testing without risk.** `--dry-run`: nothing is sent to the printer or the phone, the stop and both
tg commands are printed. `--tg-dry-run`: the job is really stopped, the tg commands are only printed.
`--fixture SCENARIO.json`: no network at all; the printer and the camera come from a scenario file,
with saved frames (implies `--dry-run` for the phone; the fixture printer records the stop it is
given):

```json
{"printer": "k1se", "file": "part.gcode", "steps": [
  {"t": 0, "state": "printing", "layer": 1, "progress": 0.01, "z": 0.2, "frame": "empty.jpg"},
  {"t": 600, "state": "printing", "layer": 40, "progress": 0.4, "z": 8.0, "frame": "gone.jpg",
   "events": [{"kind": "filament", "severity": "critical", "text": "..."}], "noodles": [["noodle", 0.9]]}]}
```

Frame paths are relative to the scenario file; a step holds until the next step's `t` (seconds on
the virtual clock).

### `start --watch`

`3d printer start NAME FILE --yes --watch` launches `3d printer watch NAME --job --yes --gcode FILE`
detached once the printer acknowledged the start and its state was printed: it prints the pid, the log
(`~/.local/state/3d-cli/jobwatch/NAME.log`) and how to stop it (`kill PID`). It is **opt-in** because
the watcher has the authority to cancel the job. Without `--watch`, `start` prints one line,
`NOT WATCHED: a lost print runs on unseen. Guard it (it may pause/cancel the job): 3d printer watch
NAME --job --yes --gcode FILE`, with the estimated duration when the G-code header carries
`; estimated printing time (normal mode) = 1h 32m 10s`, and the advice that jobs of 30 min or more
should be watched.

### Evidence folder

On a lost print `DIR/<date>-NAME-jobwatch/` (default `./print/runs`, else
`~/.local/state/3d-cli/jobwatch/NAME/runs`) holds the frames, `finding.json`, the klippy.log lines of
the job and `outcome.json` (what was sent, what was verified, which step failed). The watcher's
working files (baseline, footprint) live in `~/.local/state/3d-cli/jobwatch/NAME/`.

### Open questions

- Camera latency: how stale a frame can be when a part comes off is not measured.
- Per-model Z positions: the 98 % / 77 % / blind-above-Z-130 numbers are the U1's; other models need
  their own calibration.
- Printers without a camera calibration (K1 SE): the chained baseline ([above](#job-watch-no-calibration))
  was validated on frames of the real K1 SE bed moved by a synthetic affine drift, not on a real print of
  many layers; the real picture's correlation (noise, the toolhead, shadows) may sit lower than the
  fixtures' and `TRACK_MIN_CORR` (0.6) / `TRACK_STEP_PX` (12) may need tuning from the first real run.
- Black-on-black parts (dark filament on a dark plate) give the footprint too little contrast.

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
- If the daemon could not even post the card (`tg-ctl ask-forward not-posted … daemon declined:
  reason=send-failed`: its Telegram `sendMessage` failed, 2026-11-04 it was `getaddrinfo EAI_AGAIN
  api.telegram.org`, a resolver blip while plain `tg` worked) nothing was posted, so the ask is
  repeated after 3, 10 and 30 s (four requests in all, each reported on stderr). If it still
  fails the start is refused with the way around Telegram: the file is already stored on the
  printer, so start it **from the printer screen** (Files, then Print) — the screen does not run
  these checks, do it only when the listed FAILs are understood — and with the log to read
  (`~/.config/tg-cli/tg-ctl.*.log`, lines `sendMessage failed` / `ask-forward dropped`).
  The daemon itself sends once and never retries (tg-cli `forwardAndPostQuestion`), which is why
  the retry lives here.
- Without `--force`, without the variable, denied, timed out, or without a trusted `tg-ctl` /
  helper (`~/dev/agent-tools`, then `~/xp/agent-tools`, owned by you or root and not
  group/world-writable): exit 1, nothing is sent to the printer.
- Only the owner can approve. After every forced start, analyse the FAIL it overrode and fix the
  root cause (the gate or the slice) — the next print of the same kind must not need a force.

Exit codes: 0 done; 1 refused by a safety rule (incl. offline/read-only printer), preflight FAIL,
or printer unreachable; 2 usage error, unknown printer, bad config or fleet file, or missing file.

### Managed agent receivers

`watch.agent_fallback: false` disables automatic temporary-agent creation when a registered subscriber is unavailable; registered subscribers and stdout remain active. The default is true for backwards compatibility. Combine with `watch.tg_fallback: false` when raw external messaging is not authorized (the default `tg_fallback: true` otherwise messages the user when no subscriber is live). Only actual booleans are accepted. A missing subscriber is still a delivery failure, not success.
