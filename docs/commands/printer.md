# `3d printer` — drive the printers on your LAN

`3d printer` talks to the physical printers of the fleet registry
[`fleet/printers.yaml`](../../fleet/printers.yaml) and of `printer-devices.yaml`: it reads their
state, stores G-code on them, starts an uploaded file after an explicit `--yes`, and cancels
a job. `3d printers` (plural) is a different command: it lists printer **models** and their
build volumes; `3d printer` is about the machines themselves.

Epic: [#90](https://github.com/alex-mextner/3d-cli/issues/90).

## Usage

```
3d printer list [--json]
3d printer status NAME [--json] [--secs S]
3d printer upload NAME FILE.gcode [--as REMOTE]
3d printer start NAME FILE.gcode --yes [--as REMOTE] [--source-3mf X.3mf] [--bridge-max MM] [--force] [--secs S]
3d printer cancel NAME --yes
3d printer watch NAME... | --all [--steer SESSION] [--exec CMD] [--min-severity LEVEL] ...
3d printer vision NAME [--interval S] [--once] [--dry-run] [--scene] ...  |  vision eval ...  |  vision record NAME DIR  |  vision fetch-model
3d printer bed status NAME [--json] [--max-age S]  |  bed reference NAME [--capture [--no-plate]]  |  bed monitor NAME
3d printer subscribe --session ID [--printer P,P] [--kinds K,K] [--min-severity LEVEL] ...
3d printer unsubscribe --session ID | --id SUBID | --all
3d printer subscriptions [list | test PRINTER] [--json]
3d printer agent list [--json] | stop PRINTER | handoff PRINTER --summary TEXT
3d printer todo list [PRINTER] [--all] [--json] | add PRINTER --title TEXT [options] | done ID [--note TEXT]
3d printer todo dismiss ID --reason TEXT | snooze ID [--until DATE] [--reason TEXT] | reopen ID
3d printer todo import FILE... [--dry-run] [--json]
```

| Subcommand | What |
|---|---|
| `list` | Every fleet and configured printer: owner, access, model, address, heads (an unusable head with its reason), and where the device settings came from when a user/project file overrides the fleet. |
| `status NAME` | State, current file and progress, temperatures, and (U1) what each head holds. |
| `upload NAME FILE` | Store the file on the printer and verify its size there. Never starts a print. |
| `start NAME FILE --yes` | Start a file that was uploaded before. Runs [`3d print-preflight`](print-preflight.md) first. |
| `cancel NAME --yes` | Cancel the running or paused job. |
| `watch NAME...` | Stay subscribed (read-only) and report anomalies as events — see [Watching printers](#watching-printers). |
| `vision NAME` | Watch the camera for failed prints (spaghetti, stale/dark camera, the U1's own NPU verdicts) — see [Camera failure detection](#camera-failure-detection). |
| `bed status` / `reference` / `monitor` | Is the bed clear, is the build plate on — from the camera against an empty-bed reference; also the `bed` start gate and the `bed_clear` event — see [Bed state](#bed-state). |
| `subscribe` / `unsubscribe` / `subscriptions` | Route printer alerts to an AI agent first: it looks at the frame and tells the user only if the failure is real — see [printer-subscribe.md](printer-subscribe.md). |
| `agent list` / `stop` / `handoff` | The temporary per-printer omp agent (tmux) that gets the warning+ alerts no live subscriber took — see [printer-subscribe.md#the-temporary-printer-agent](printer-subscribe.md#the-temporary-printer-agent). |
| `todo …` | The to-do list of each printer: needed calibrations, due maintenance, repairs of open faults, manual tasks — see [To-do list](#to-do-list-3d-printer-todo). |

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
- `--yes` — required by `start` and `cancel`: you confirm the physical action.
  `3d printer start u1 out/part.gcode --yes`
- `--bridge-max MM` — preflight overhangs gate: longest bridge; overrides the printer's `bridge_max_mm`.
  `3d printer start u1 out/porch.gcode --yes --bridge-max 25`
- `--force` — start even when preflight reports FAIL; the failing checks are printed as `FORCED`.
  `3d printer start k1se out/roof.gcode --yes --force`
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
3d printer upload u1 out/part.gcode
3d printer upload k1se "out/roof left.gcode" --as roof_left.gcode
3d printer start u1 out/part.gcode --yes
3d printer start u1 out/base.gcode --yes --source-3mf print/base.3mf
3d printer start k1se out/roof.gcode --yes --force
3d printer cancel k1se --yes
3d printer watch u1 --duration 20
3d printer watch u1 k1se --steer 01a12cd6
3d printer subscribe --session 01a12cd6 --printer u1,k1se
3d printer subscribe --session 01a12cd6 --printer u1 --kinds 'vision_*,printer_noodle_verdict'
3d printer subscriptions
3d printer agent list
3d printer todo list
3d printer todo add k1se --title "Check X belt tension" --type inspect --when idle --duration 15
3d printer todo done 12 --note "belts re-tensioned"
```

## To-do list (`3d printer todo`)

One list per printer of what to do on it (plan §7.5, issue
[#281](https://github.com/alex-mextner/3d-cli/issues/281)): what, why, whether it blocks printing,
when it suits (idle / before a print / any time), how long it takes, whether a human is needed,
where it comes from and its status. Served by 3d-api (`/v1/printers/{p}/tasks`, `/v1/tasks/…`,
see services/3d-api/README.md); Home Assistant shows the same list
([#285](https://github.com/alex-mextner/3d-cli/issues/285)).

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
3d printer todo import FILE... [--dry-run] [--json]
```

Where tasks come from (`source`):

| Source | Opens when | Closes when |
|---|---|---|
| `fault` | a fault is open (`3d printops maint fault …`, the U1 T2 fault seeded from the fleet file): a repair task that **blocks its head** (or the whole printer for a fault without a head) | the fault closes: fixed → done, wontfix → dismissed |
| `maintenance_rule` | a rule of `fleet/maintenance-rules.yaml` is announced due (`3d printops maint due --check`) | the rule's maintenance is logged (`3d printops maint log P[/H] --rule ID`) |
| `calibration` | a calibration kind is not current for the head and its loaded spool ([`3d calibrate plan`](calibrate.md): never done, new filament/vendor/lot/nozzle, invalidated by a nozzle swap or repair, expired) — one task per kind × head × nozzle × filament (× lot when the kind is lot-sensitive) | a record makes it current ([`3d calibrate record`](calibrate.md)) |
| `manual`, `calibration_followup` | `add` (`--followup`: a mechanical action after a calibration) | `done` |
| `import` | `import` of a JSONL buffer | `done` |

The first three are **derived**: 3d-api materialises them on every tasks request and every 15
minutes, keyed by `source_key` (the printer and the full scope, e.g.
`calib:k1se/T1/flow_ratio/nozzle:4/filament:12/lot:A7`, `rule:u1/hotend-wipe/T1/0`,
`fault:fleet:u1/T2`). `done` refuses them and names the command that records the work — the
record closes the task, otherwise the next pass would open it again. `dismiss --reason` drops one
without the work and stays dismissed for that key; a new nozzle or another spool is a new key, so a
new task. When the need goes away for another reason (another spool loaded, the head no longer
usable, the rule deleted) the task is dismissed by `derive`. A head whose spool is unknown
(Spoolman unreachable, nothing bound) keeps its calibration tasks as they are.

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
`dismiss` and `snooze` append the operation to `~/.local/share/3d/printer-todo/<printer>.jsonl`
(`$XDG_DATA_HOME/3d/printer-todo`; one line per operation, schema `printops.printer_task/1`,
`flock` + one `O_APPEND` write + `fsync`) and say so on stderr; `list` then shows the buffer
(name tasks by their uid prefix). `import` replays buffer files into 3d-api in order, each
operation once by its `uid` — importing a file twice adds nothing; a line that is not JSON or names
an unknown schema stops the import with its line number and nothing is imported. An `open` whose
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
  `3d printer todo import ~/.local/share/3d/printer-todo/k1se.jsonl --dry-run`

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

A key that is not known is left out (shown as `null`/`-`), never guessed.
`THREED_FLEET=FILE` reads another fleet file, `THREED_FLEET=` (empty) disables it.

```
$ 3d printer list
  NAME          OWNER        ACCESS    MODEL                ADDRESS                         HEADS
  a1mini        boris        offline   Bambu Lab A1 mini    - (never contacted)             T1
  k1se          own          full      Creality K1 SE       moonraker 192.168.0.27:7125     T1
  p1s           neighbour    offline   Bambu Lab P1S        - (never contacted)             T1
  u1            own          full      Snapmaker U1         moonraker 192.168.0.26:7125     T1 | T2 UNUSABLE | T3 | T4
      T2: head T2 does not work (babushkin-dom gen/queue.json: «слот 2 — заглушка, голова T2 не работает»; …)
  xecut-a1      hackerspace  offline   Bambu Lab A1         - (never contacted)             T1
  xecut-cr200b  hackerspace  offline   Creality CR-200B Pro - (never contacted)             T1
```

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
macro, so any macro in the G-code that is not listed makes preflight FAIL and `start` needs
`--force`. With `--printer A --axis-limits B` only A's list counts.

`bridge_max_mm` is the longest span this printer bridges cleanly (measure it with a bridge test
print). The preflight overhangs gate counts uncarried runs up to it as bridges; `--bridge-max`
overrides it, and without either the limit is 15 mm. The gate's line names where the limit came from.

## Adapters

| Adapter | Printers | Transport |
|---|---|---|
| `moonraker` | Klipper/Moonraker: Snapmaker U1 and others | HTTP: `/printer/objects/query`, `/server/files/upload` (`print=false`), `/printer/print/start`, `/printer/print/cancel`. On the U1, `print_task_config` gives the colour and material in each head; a head without filament (the broken T2) shows as `empty`. |
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
| `state` | info / warning / critical | Started, resumed, complete (info); paused, cancelled, job ended without completing (warning); error (critical). Found paused/in error at start: reported too. Pause/cancel/error text carries the last error seen in the 5 min before. |
| `exception` | critical (warning for level ≤ 1) | U1 `print_stats.exception` / `exception_manager` (e.g. code 2 "detected noodle" from defect detection, id 532); K1 `err.errcode`. |
| `gcode_error` | critical while a job is active, else warning | A G-code response starting with `!!` — e.g. `!! Error evaluating 'gcode_macro INNER_PREEXTRUDE_FILAMENT:gcode'`. |
| `gcode_warning` | warning | `// Warning…` / `Warning:` responses. |
| `filament` | critical while printing, warning while paused | Moonraker `filament_switch_sensor`/`filament_motion_sensor` goes from detected to not detected; K1 runout sensor reports no filament (`materialDetect` 1 and `materialStatus` 1 — the K1's own UI: «Consumable not detected»). The K1 event is held 10 s and dropped if the sensor recovers; in the start phase it waits until the start phase ends ([#242](https://github.com/alex-mextner/3d-cli/issues/242)). A runout pause is reported at once by the `state` event, which carries the reason. |
| `homing_lost` | critical | Printing/paused and `homed_axes` (K1: `autohome`) loses X, Y or Z — resuming would move unhomed axes. Held 10 s, see below. |
| `bed_off` | warning | Bed target dropped to 0 while printing/paused (or 0 at start while paused). Held 10 s, see below. |
| `temperature` | warning (critical when drifting while printing) | A heater target not reached after `--heat-timeout-min`; or, after reaching, more than 15 °C off for 60 s. |
| `stall` | warning (info when it moves again) | Printing, but file position/progress/layer unchanged for `--stall-min`. |
| `klippy` | critical (shutdown/error, or any loss during a job) / info (ready again) | Klipper shutdown, error, disconnect. |
| `connection` | warning / info | Cannot connect, connection lost (once; reconnects with 2–60 s backoff), reconnected. |
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
- tg — **emergency only**, yaml `watch.tg_fallback: true` (no CLI flag): one message per batch
  through the `tg` CLI, marked «агент недоступен», carrying only the warning+ events no agent
  could take (the printer agent did not start). The watcher never messages the user otherwise.
  The old `--tg` / `--tg-always` flags and `tg:` key are read as `tg_fallback: true` with a
  deprecation warning.
- `--dry-run` — exec/steer/subscriptions/agent/tg print what they would send to stderr instead.

Delivery rules: events below `--min-severity` (default `info`) are dropped; the same event
key within `--dedup-min` (default 10) is dropped; events arriving within 2 s go out as one
tg/steer message, most severe first; tg and steer send at most 6 messages per 10 minutes —
critical events always go through, and the next message says how many were held back.

Other options: `--all`, `--stall-min N` (15), `--heat-timeout-min N` (10), `--milestones 50,90`,
`--heartbeat FILE`, `--duration S` (stop after S seconds).

Per-printer settings can live in `printer-devices.yaml`; flags add to / override them:

```yaml
u1:
  adapter: moonraker
  host: 192.168.0.26
  watch:
    steer: 01a12cd6          # or a list; also `exec: CMD` / a list
    tg_fallback: true        # emergency: phone gets only what NO agent could take
    min_severity: info
    stall_minutes: 20
    heat_timeout_minutes: 10
    milestones: [50, 90]
    dedup_minutes: 10
```

Heartbeat: `~/.local/state/3d-cli/watch/<names>.json` (`--heartbeat` overrides), rewritten
every 15 s with each printer's connection, state, last-message age, last event and the
per-sink delivered/failed/suppressed counters.

As a user service:

```bash
3d printer watch u1 k1se --install-service
3d printer subscribe --session 01a12cd6 --printer u1,k1se
systemctl --user enable --now 3d-printer-watch@u1 3d-printer-watch@k1se
journalctl --user -fu '3d-printer-watch@*'
```

`--install-service` writes `~/.config/systemd/user/3d-printer-watch@.service`
(`ExecStart=3d printer watch %i $WATCH_ARGS`, `Restart=always`) and
`~/.config/3d-cli/watch/NAME.env` with the other flags, then runs `systemctl --user daemon-reload`.
Flags with spaces (an `--exec` command) belong in `printer-devices.yaml` instead.

## Camera failure detection

`3d printer vision NAME` (shorthand for `vision run NAME`) polls the printer camera every 10 s,
scores each frame with Obico's open spaghetti detector on the CPU, drops boxes outside the
current print's bed volume (camera calibration; the U1's ships with 3d-cli), and checks each
remaining box with a zero-shot CLIP verifier, so textured walls do not count as spaghetti
(weights downloaded on first use into `~/.cache/3d-cli/vision/models/`, sha256-pinned). It sends
`vision_spaghetti`, `vision_spaghetti_vetoed` (info), `vision_camera_stale`, `vision_camera_dark`
and `printer_noodle_verdict` events, plus the opt-in `--scene` bed-change events, through the same
dispatcher and sinks as `watch` (the device's `watch:` block). It never pauses or cancels.
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

- **Reference.** With the bed empty and the plate on, at the Z where the printer parks after a
  job (U1 `PRINT_END`/`CANCEL_PRINT`: Z 200; K1 SE `END_PRINT`: half of Z max = 125):
  `3d printer bed reference u1 --capture`. The bed moves in Z on both printers, so a reference
  only counts within `z_tol_mm` (0.5) of the current Z. `--capture --no-plate` (plate off) adds the
  bare-bed reference that makes the plate test reliable. `--capture --from FRAME.jpg --z 199.7`
  stores a saved snapshot of the empty bed taken at that Z instead. Stored in
  `~/.local/share/3d-cli/bed/NAME/`; `3d printer bed reference u1` lists them.
- **Status.** `3d printer bed status u1` — `clear` / `occupied` / `unknown` and plate `present` /
  `missing` / `unknown`, with the reason and an evidence frame (ROI cyan, changed pixels red) in
  `~/.cache/3d-cli/vision/bed/`. `--max-age S` takes the watcher's last verdict when it was
  confirmed within S seconds at the same Z, else one fresh frame is judged (it waits up to 6 s
  for the camera to deliver a new frame; without one the camera may be frozen, so a clear-looking
  frame reads `unknown`, while parts or a missing plate on it still count).
- **How.** Frames are decoded at 480 px; the current one is mapped onto the reference colours by
  a robust gain/offset fit (auto-exposure, room light) and a median detrend removes smooth light
  gradients; a changed region of ≥ 200 px (or ≥ 8 % of the bed view) is `occupied`, under 100 px
  and ≤ 3 % is `clear`, in between `unknown`. ROI: `watch.bed.roi` (a polygon of the bed, 0..1 of
  the frame), else the camera calibration (bed plus 80 mm above it at the current Z), else the
  whole frame. On the U1 at Z 200 only parts taller than a few cm show, and only the plate's front
  edge is in view (about 2 % of the frame), so the plate verdict rests on that strip; the U1's
  own `DETECT_BED_PLATE` still checks the plate at start. Below 2 % of the frame the surface
  counts as out of view (plate `unknown`, the bed check WARNs).
- **Monitor.** `3d printer watch` runs `bed monitor NAME` as a child for every Moonraker printer
  with `watch.bed.enabled: true`. Nothing is grabbed while a job runs; otherwise one snapshot per
  `interval_s` (1 s), identical bytes skipped, one reduced decode + one masked difference per new
  frame; the bed is judged only after a change has settled for 3 frames (a hand in the chamber is
  not judged), and once at start. Events (info, with `data.frame`): `bed_occupied` (parts after
  a job, or put on), `bed_clear`, `plate_missing`, `plate_present` (after missing). Subscribe an
  agent to them: `3d printer subscribe --session ID --kinds 'bed_*,plate_*' --min-severity info`.
  The last verdict is in `~/.local/state/3d-cli/bed/NAME.json` and in the watch heartbeat.
- **Start gate.** `3d printer start` and `3d print-preflight --printer` add a `bed` line: FAIL on
  objects or a missing plate, PASS on a clear bed with the plate, WARN when the camera cannot
  tell (no reference at this Z, stale, plate out of view, camera down); `--force` overrides.
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
   missing); a FAIL blocks the start unless `--force`.
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

Exit codes: 0 done; 1 refused by a safety rule (incl. offline/read-only printer), preflight FAIL,
or printer unreachable; 2 usage error, unknown printer, bad config or fleet file, or missing file.
