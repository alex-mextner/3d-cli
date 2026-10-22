# Calibration workflows

What to calibrate, in which order, for every situation a printer goes through: a new printer, a
new spool, a nozzle swap, maintenance or a firmware update, slow drift, and multi-head printers.
All of them run through one pipeline: a calibration session in `3d-api`. The variants differ
only in which calibrations are missing and therefore which tests land on the plate.

This page is the overview of epic [#268][268]. The design details live in
[plan.md §7.2–§7.5 and §8](plan.md#8-защита-от-забытых-калибровок--streamline) (catalog, settings history,
printer todos, plates v2, session). Every step names its status:

| Mark | Meaning |
|---|---|
| **built** | on `main`; a number next to it is the merged PR |
| **PR #N** | implemented, in an open pull request |
| **#N** | open ticket, not implemented |

Printers in the fleet today: Creality K1 SE (rooted, Klipper + Moonraker, one head, 220×220 bed)
and Snapmaker U1 (Klipper + Moonraker, four heads, tool changer).

## 1. Common pipeline

```
bed mesh ─► plan ─► gen (dense plate on the underlay) ─► print ─► photo ─► analyze
                                                                            │
   todos ◄── history ◄── apply ◄── choose ◄── annotated image + JSON + PDF ◄┘
```

| # | Step | What happens | Status |
|---|---|---|---|
| 1 | **Bed mesh** | `BED_MESH_CALIBRATE` before every calibration plate; the mesh is fetched over Moonraker and kept in history; flatness checked (§3) | [#330][330] |
| 2 | **Plan** | only what is missing for this printer, head and spool; `--skip`/`--only`; `--max-time` budget | `3d calibrate plan` **built** (list of missing kinds, no `--skip`/`--only`); `--skip`/`--only` on `gen` PR [#288][pr288]; plate planner [#271][271] |
| 3 | **Gen** | one dense plate (at most two per spool) on a shared one-layer underlay, with label and QR, preview PNG | plate engine PR [#288][pr288], `dense`/`dense2` PR [#289][pr289]/[#314][pr314]; underlay [#320][320], label [#322][322], removable tests [#327][327], layout [#272][272] |
| 4 | **Print** | through `3d printer start` (preflight gates, idle printer, explicit confirm) | **built** |
| 5 | **Photo** | phone (HA card), Telegram, or the printer camera from above; light on, contrast checked | [#276][276], [#263][263] |
| 6 | **Analyze** | ArUco → homography → per-test analyzers → `calib-assessment/1` JSON + annotated image | [#273][273] |
| 7 | **Report** | one PDF page per plate: annotated diagram, manual steps, answer boxes, final values | [#328][328] |
| 8 | **Choose** | the user confirms or edits each proposed value; low confidence → `needs_manual` | [#277][277] |
| 9 | **Apply** | slicer profile (`profiles/` + `3d profiles sync`) and/or Klipper (`SAVE_CONFIG` only when idle) | [#274][274] |
| 10 | **History** | calibration records + append-only `settings_change` rows with old → new; rollback is a new row | records **built** ([#297][pr297]); history [#264][264] |
| 11 | **Todos** | follow-ups (shim the bed, check belts, rerun head offsets) become printer tasks | [#281][281]–[#285][285] |

### 1.1 Bed mesh first

A calibration plate reads first-layer squish, flow, fits and dimensions. On an uneven bed those
readings mix the bed's shape into the filament's values, so every calibration plate starts with
a fresh mesh ([#330][330]):

1. **Probe.** The plate's G-code runs a full `BED_MESH_CALIBRATE` (not `ADAPTIVE=1`: the point is
   the whole bed) after the bed has reached print temperature and soaked for a few minutes, before
   the first extrusion. If the printer's start macro already probes, the engine relies on it
   only when a test proves that macro does it. Preflight fails on macros a printer does not
   trust, so `BED_MESH_CALIBRATE` joins the K1 SE's `trusted_macros` (the U1 already lists it)
   in `fleet/printers.yaml` and in any `printer-devices.yaml` override, which replaces the fleet
   list ([#330][330]).
2. **Fetch.** Right after probing, `3d-api` (or the CLI) reads
   `GET /printer/objects/query?bed_mesh` from Moonraker: `profile_name`, `mesh_min`, `mesh_max`,
   `probed_matrix`, `mesh_matrix`, `profiles`, plus `probe_count` and the probe offset from
   `configfile`.
3. **Store.** A `bed_mesh` calibration record (`range_mm`, catalog kind already exists) and a
   `settings_change` row (kind `bed_mesh`, source `firmware_auto`) whose blob is the full matrix.
   The calibration session keeps the mesh id, so a result can always be read next to the bed it
   was printed on.
4. **Check.** Range, plane tilt and the worst step between neighbouring points against new
   `fleet/policy.yaml` `bed_mesh:` thresholds with defaults (the key does not exist yet;
   [#330][330] adds it with boundary tests); over a threshold → shim map and a printer task (§3).

`3d printer mesh` (planned, [#330][330]; not on `main` yet):

```
3d printer mesh PRINTER [--probe --yes] [--json] [--png FILE]     current mesh: heatmap, range, tilt, worst step
3d printer mesh history PRINTER                                    stored meshes, newest first
3d printer mesh diff ID [ID]                                       change between two meshes (default: vs latest)
3d printer mesh shims PRINTER [--mesh ID] [--layer-mm 0.06] [--max-layers N] [--target p75]
                                                                   Kapton shim map (§3.3)
```

`--probe` runs `BED_MESH_CALIBRATE` itself, so it moves the printer: idle only, explicit `--yes`,
like `3d printer start`.

### 1.2 The session

Every step above is one transition of a calibration session in `3d-api` ([#277][277]):
`plan → gen (preview) → print → photo → analyze → review/choose → apply → done`, plate 2 after
plate 1's values are applied, `abort` from any step. Details in
[plan.md §8.3.6](plan.md#836-калибровочная-сессия-и-каналы-277280-275); channels in §5 below.

## 2. Variants

A variant is not a separate code path. A trigger (new spool, maintenance record, nozzle swap)
makes some calibration records missing or stale (requirement engine, **built** [#297][pr297];
`fleet/calibrations.yaml` `invalidated_by`), and the planner builds plates from what is missing.
The tables say which kinds each variant ends up with and in which order.

Two gaps today: mechanics kinds are `baseline: assumed` (a printer without records is taken to be
at factory state), so nothing asks for them on a new printer; and PID, skew and VFA are not in the
catalog. Both are [#334][334] (a `commission` maintenance record, the missing kinds,
dependency order, named test sets `--set mechanics|filament|drift|express`).

### 2.1 New printer (mechanics)

Once per printer, and again after a factory reset. Order matters: each step assumes the ones
above it.

| # | Calibration | How | Why here | Kind / status |
|---|---|---|---|---|
| 1 | Belts, frame, gantry | belt frequency (app or meter), squareness, eccentric nuts | everything after reads through the motion system | `belt_tension` **built** (record only) |
| 2 | PID hotend + bed | `PID_CALIBRATE HEATER=extruder TARGET=<print temp>`, `HEATER=heater_bed`; `SAVE_CONFIG` | every thermal test assumes a stable temperature | not in catalog, [#334][334] |
| 3 | Extruder rotation distance (e-steps) | mark 100 mm, extrude, measure | flow calibration would otherwise absorb an extruder error | `rotation_distance` **built** (record only) |
| 4 | Z offset | Klipper probe calibrate; the K1 SE and U1 automatic routines | first layer | `z_offset` **built** (record only) |
| 5 | Bed mesh + flatness | §1.1, fix mechanically per §3 if over the threshold | first layer everywhere on the bed | `bed_mesh` **built** (record only); fetch/check/shims [#330][330] |
| 6 | Input shaper | accelerometer: `3d calibrate gen shaper` (console commands, no `SAVE_CONFIG` until checked) | needs tight belts; ringing tests need it | `input_shaper` **built** (record only); `gen shaper` PR [#249][pr249] |
| 7 | Head offsets (multi-head) | the vendor's routine (U1: Snapmaker XY/Z calibration), then a vernier check plate | per-head positions before any multi-head plate | `head_offsets` **built** (record); check [#331][331] |
| 8 | Skew, XY dimensions | 50×50 frame with diagonals on the dense plate; Klipper `SET_SKEW` or slicer XY compensation | after shaper, so ringing does not blur the edges | `xy_dimensions` [#269][269]; skew [#334][334]; frame on `dense` PR [#289][pr289] |
| 9 | VFA | wall-speed bands on the dense plate (ringing and VFA tubes) → speed ranges to avoid | after shaper and belts | tubes PR [#289][pr289]; kind [#334][334] |

Then the printer gets the new-filament workflow (§2.2) for every spool it will print.

### 2.2 New filament

Per head × nozzle × filament: a second spool of the same filament reuses its records. For a new
material type or vendor the `calibration_current` gate (`3d calibrate check`) FAILs until flow and
PA exist (policy D6); a new lot only warns, for the lot-sensitive kinds (flow, temperature). The
gate is not yet part of `3d printer start`'s preflight. Order:

| # | Calibration | Test | Plate |
|---|---|---|---|
| 1 | Temperature | temperature tower (sequential, last, alone) | 1 |
| 2 | Pressure advance | PA lines (`pa`), `pa_fine` follow-up when ambiguous | 1 (fine: 2) |
| 3 | Flow ratio | flow tiles | 1 (recheck on 2 if the chosen temperature moved ≥ 5 °C) |
| 4 | Max volumetric speed | MVS strip | 2 (depends on temperature) |
| 5 | Retraction | retraction tower, `stringing` pillars | 1, checked on 2 |
| 6 | Cooling, bridges, overhangs | bridges 8/11/14 mm, warm/cold bridge pair, overhangs 45–60° | 1, 2 |
| 7 | Fits and holes | pins in holes 0.1–0.5, slots, hinges, M3/M4 holes, Ø1–10 holes, thin walls, elephant foot | 2 |

The plates, tests, budgets and the "temperature first, temperature-dependent results are
provisional" rule are in [plan.md §8.3](plan.md#83-калибровочные-столы-v2-печатаем-только-недостающее-плотно-с-машинным-чтением-фото).
Built pieces: `gen pa|flow|combo|temp` PR [#249][pr249]/[#257][pr257], `plate1` PR [#288][pr288],
`dense`/`dense2` PR [#289][pr289]/[#314][pr314]; the express guide (one plate, PA + flow),
`docs/printops/express-calibration.md`, comes with those PRs and is not on `main` yet.

### 2.3 Nozzle change

A nozzle swap logged through maintenance ([#166][166], PR [#312][pr312]) invalidates, per catalog:

| Stale | Redo | Notes |
|---|---|---|
| `z_offset` (printer × plate) | yes, first | a new nozzle has a different length |
| `head_offsets` (multi-head) | yes, after Z | the vendor routine, then the vernier check [#331][331] |
| `flow_ratio`, `pressure_advance`, `max_volumetric_speed` (head × nozzle × filament) | for each filament that will print on this head | flow + PA + MVS on one plate; the catalog invalidates MVS on any nozzle swap, even the same type |
| `temp_tower` | no | a filament property |
| `retraction` | no, unless the hotend changed | `invalidated_by: [ptfe, hotend, extruder]` |

The mesh is not invalidated by a nozzle (only by plate, move, kinematics, repair), but it is
probed anyway before the plate (§1.1).

### 2.4 After maintenance or a firmware update

Maintenance records ([#166][166]) carry the component, and the catalog maps it to stale kinds:

| Work done | Stale kinds |
|---|---|
| belts replaced or re-tensioned | `belt_tension`, `input_shaper`; skew and VFA ([#334][334]) |
| kinematics repair | `belt_tension`, `input_shaper`, `bed_mesh`; skew and VFA ([#334][334]) |
| printer moved | `input_shaper`, `bed_mesh` |
| any `repair` | `input_shaper`, `z_offset`, `bed_mesh`; `head_offsets` (multi-head) |
| bed or build plate replaced | `z_offset`, `bed_mesh` |
| hotend replaced | `z_offset`, flow, PA, MVS, retraction; `head_offsets` (multi-head); PID hotend ([#334][334]) |
| extruder gears or motor | `rotation_distance`, PA, retraction |
| PTFE tube | PA, retraction |
| U1 head swapped | `z_offset`, `head_offsets` |

**Firmware update.** A firmware update is not a calibration trigger by itself: it is a question
of whether the settings survived. The settings history ([#264][264]) snapshots the config files
before and after (`firmware_update` maintenance) and its drift scan diffs them. Only kinds whose
values changed or vanished (the `SAVE_CONFIG` block: mesh, shaper, PID; U1
`snapmaker/*.json`; head offsets) are re-run; the rest is verified by a drift plate (§2.5).
The printer upgrade guides list what each upgrade keeps:
[K1 SE root](../printers/k1se-root-upgrade.md), [U1 extended firmware](../printers/u1-paxx12-upgrade.md).

### 2.5 Periodic drift check (express)

Values drift: belts stretch, nozzles wear, sheets warp. A short check plate confirms the current
values instead of recalibrating ([#334][334]):

- **When:** every `calibrations.drift_every` (print hours or days, `fleet/policy.yaml`); the
  idle-window notifier ([#283][283]) suggests it when the printer is free.
- **Plate (~10–15 min):** first-layer patches across the whole bed, PA lines at the current value
  ± one step, one flow tile at the current value ± 2 %, a dimension frame, plus the mesh diff
  against the stored mesh (§1.1).
- **Result:** inside tolerance → the current records get a new `valid_until`, no values change;
  outside → a printer task for each drifted kind, which the next full session picks up.

The same short plate is the "express" check after a firmware update (§2.4) and on a new lot of a
known filament (policy: warn).

### 2.6 Multi-head printers (U1)

Everything above, per head, plus the tool change itself.

**Per head.** Filament calibrations are per head: flow, PA and MVS are scoped head × nozzle ×
filament, retraction head × filament, so each loaded colour on each head needs its own values
(temperature is per filament and shared). The
planner lists them for every head a print uses; a plate can carry tests of several heads.

**First layer on one head.** The parked head oozes; a tool change on the first layer drops that
ooze into the layer the whole plate stands on ([#308][308], night print of 2026-10-15/16).
Rule: the underlay ([#320][320]) and the first layer are printed by one head (on the U1 in a
contrasting filament, [#302][302]); tests of the other heads start on layer 2, on top of the
underlay. A preflight check that FAILs a G-code with a tool change on the first layer or raft is
planned ([#308][308]); until it lands, nothing checks it automatically.

**Tool-change calibrations** ([#331][331]):

| Kind | What it decides | Test | Read as |
|---|---|---|---|
| `toolchange_prime` | volume to extrude after a change before printing parts (prime tower or purge) | strips printed right after a change with 0/5/10/20/30 mm³ prime | length of the under-extruded start of each strip |
| `standby_temp` | standby temperature drop and preheat lead time of the parked head | variants of the drop with a fixed park time | ooze blob vs. wait for reheat |
| `toolchange_retraction` | retraction when parking | variants on the same strips | strings and blobs at the change |
| `head_offset_check` | whether the vendor's XY offsets still hold | vernier lines by every head pair over the underlay | misalignment in 0.05 mm steps; over the limit → task "rerun the offset calibration" |

**Colour mixing (optional, [#333][333]).** For layer-pattern mixed colours ([#116][116]): small
tiles with short patterns (cycle 2–6 layers: A:B 1:1, 2:1, 1:2, A:B:C 1:1:1) next to solid tiles of
each filament. Vision white-balances on the solid tiles (or a reference card, [#202][202]),
measures each tile in CIELAB and reports ΔE2000 against the recipe's prediction; the fitted
per-filament correction improves later recipes. Only on request.

### 2.7 Ironing (optional, [#332][332])

For parts with visible flat tops. Never in the default plan; `--only ironing` adds it.

- **Parameters:** ironing flow (5–25 %), line spacing (0.08–0.25 mm), speed (15–80 mm/s);
  optionally pattern and inset.
- **Test:** 15×15 mm tiles, 3–4 solid layers, ironed top, per-object settings like the flow tiles.
- **Search:** plate 1 is a coarse space-filling design (9–12 tiles over the three axes); vision
  scores each top under raking light (gloss uniformity, grooves, ridges, gaps); the user confirms
  the ranking. The next plate (or free space on the next calibration plate) narrows every range
  around the best tiles and keeps the best one as a control. Stop when the score stops improving
  or the plate budget is spent.

## 3. Bed flatness

### 3.1 What the mesh shows

`BED_MESH_CALIBRATE` probes a grid (`probe_count`, e.g. 5×5 or 7×7) and Klipper interpolates
between the points. The mesh is the height of the surface the probe touches at each point: bed,
magnetic base and the removable sheet together. From it `3d printer mesh` derives:

- **range** — highest minus lowest point;
- **tilt** — the best-fit plane; a tilt is the bed's mounting, not its flatness;
- **residual** — the mesh minus the plane: the real shape (dome, bowl, saddle, twist, a corner);
- **worst step** — the largest difference between neighbouring points: a local bump or dip.

### 3.2 What the printer compensates and what it does not

Klipper's `bed_mesh` moves Z along every move to follow the mesh. That covers:

- tilt and smooth, wide shapes (dome, bowl, twisted bed) that the grid resolves;
- small ranges, where following the surface does not distort the part.

It does **not** cover:

| Problem | Why the mesh misses it | Sign |
|---|---|---|
| Large deviations (range over the `fleet/policy.yaml` `bed_mesh:` threshold, [#330][330]) | compensation keeps the first layer even, but the part copies the bed's shape: with `fade_end` unset Klipper never fades, so tops are not flat; with a fade the bottom layers vary in thickness | wavy tops, uneven bottoms, dimensions off across the bed |
| Local bumps and dips smaller than the probe spacing | a 5×5 grid on a 220 mm bed probes every ~50 mm; a 10 mm bump between points does not exist for the mesh | one spot of the first layer too thin (translucent, rough) or too thick (squished ridge) |
| Outside `mesh_min`/`mesh_max` | Klipper holds the edge values beyond the probed area | edges of the bed worse than the centre |
| A sheet that moves | the mesh is taken once; a sheet lifting at a corner, debris under it or a sheet put back differently changes the surface after probing | the problem moves or comes and goes between prints |
| Thermal change | a bed probed cold or before soaking warps further as it heats | first layer fine on one plate, off on the next |

The calibration underlay ([#320][320]) is a flatness test by itself: one layer over the whole test
area shows local bumps and dips that the mesh is too coarse to see, so the first-layer readout of
the underlay is reported next to the mesh.

### 3.3 Human fixes: Kapton shims

Shims raise low areas; they cannot lower high ones. Kapton (polyimide) tape is thin, heat-proof and
even: about **0.06 mm per layer** (measure a stack of five with calipers for your roll).

**Where:** on the magnetic base, under the removable spring-steel sheet. The steel sheet spreads
each shim over a few centimetres, so the result is smooth, and the fix is reversible. A permanent
fix goes under the magnetic sticker itself.

**Shim map** (`3d printer mesh shims`, [#330][330]):

1. Fit a plane to the mesh and take the residuals (the tilt is the mesh's job).
2. Pick the target level: by default the 75th percentile of the residuals (`--target`), so a few
   high points are left to the mesh instead of shimming the whole bed up to them.
3. For each mesh cell: `layers = clamp(round((target − residual) / layer_mm), 0, max_layers)`
   (`--layer-mm`, default 0.06; `--max-layers`). Cells above the target get 0.
4. Merge neighbouring cells with the same count into patches and stack them as a pyramid: the
   lowest layer the largest, each next layer smaller by ~5–10 mm on every side, so the sheet does
   not get a step.
5. Output: a PNG/PDF map in bed coordinates (mm from the bed's front-left corner; the probe offset
   from `configfile` applied) with the outline of each patch and its layer count, and a table
   `patch, x–x, y–y, layers`. Print it 1:1 and lay it on the bare base, or measure from the edges.

Example: a dip of 0.18 mm below the target → 3 layers there, nothing elsewhere.

**Verify:** put the sheet back, heat and soak, `3d printer mesh --probe --yes`, then
`3d printer mesh diff` against the mesh before shimming. Inside the threshold → the printer task
closes; still over it → a second shim pass with the new map. Every mesh stays in history, so a
shim that slipped shows up as a change later.

### 3.4 When to replace the sheet

Rotate the removable sheet 180° and probe again:

- **The shape turns with the sheet** → the sheet is warped. Replace it; shims under a warped sheet
  only move the problem.
- **The shape stays** → the base or the bed is the cause: shim (§3.3) or fix the bed's mounting.

Replace the sheet also when it has a kink or a permanent bend, the coating is worn or damaged where
parts print, or the residual stays over the threshold after two shim passes. A new sheet is a
`plate` change: `z_offset` and `bed_mesh` become stale and are redone first.

## 4. History and todos

- **Records:** each confirmed value is a calibration record (**built**, [#297][pr297]) scoped to
  printer, head, nozzle install, plate, filament, lot.
- **Settings history:** each applied change is a `settings_change` row ([#264][264]) with old →
  new, source (photo, vision, manual, firmware), actor, command and evidence (photo, annotated
  image, mesh, G-code sha256). Rollback is a new row (`3d settings rollback ID`). Without
  `3d-api`, the CLI appends to `~/.local/share/3d/printer-history/<printer>.jsonl` and imports it
  later.
- **Printer todos** ([#281][281]): the requirement engine turns every missing or stale kind into a
  task; follow-ups add mechanical work (shim the bed, check belts after shaper peaks, rerun head
  offsets, replace the sheet). Tasks show in the CLI (`3d printer todo`), the bot (`/todo`,
  [#284][284]) and HA (`todo.printops_<printer>`, [#285][285]). Preflight warns on open tasks and
  fails on blocking ones ([#282][282]), except calibration tasks: those are gated once, by
  `calibration_current`. The idle notifier suggests the tasks that fit now
  ([#283][283]).

## 5. Channels

The session lives in `3d-api`; the CLI, the bot and HA are equal views of it. Each shows the same
step and the same expected action ("photo of plate 1 needed"). None of the three channels exists
yet: the table is the planned interface of the tickets in its header. On `main` today
`3d calibrate` has `plan`, `record`, `ls`, `show` and `check`, and `3d printer` has no `mesh` or
`todo`.

| Step | CLI ([#278][278]) | Bot ([#279][279]) | HA card «Калибровка» ([#280][280], [#275][275]) |
|---|---|---|---|
| start, plan | `3d calibrate start --printer P [--head T] [--spool ID] [--skip/--only] [--max-time]` | `/calibrate`, buttons | printer, head, spool, checkboxes, «Не дольше» |
| bed mesh | `3d printer mesh …` | mesh heatmap in the session message | heatmap image |
| gen, preview | `3d calibrate gen --session ID` | preview PNG | «Сгенерировать», preview |
| print | `3d calibrate print --session ID --yes` | button with confirmation | «Отправить на принтер» |
| photo | `3d calibrate photo --session ID FILE` | send a photo (bound by QR or as a reply) | «Загрузить фото» (phone camera) |
| analyze, report | `3d calibrate analyze`, `3d calibrate report --pdf` | annotated image + PDF | annotated image, PDF link |
| choose | `3d calibrate choose --session ID k=v …` | Применить / Изменить / Переснять | editable value table |
| apply | `3d calibrate apply --session ID --yes`; syncs the slicer profile on this machine | Klipper with confirmation; profile change waits for the next `3d profiles sync` | «Применить» (targets: профиль, принтер), «Откатить» |
| todos | `3d printer todo` | `/todo`, done/snooze | `todo.printops_<printer>` |

Who does what best: the CLI is the only channel next to the slicer, so it finishes profile apply;
the bot gets the photo (phone in hand at the printer) and the notifications; HA is the
dashboard and the phone-camera upload.

**Resuming.** Every step is persisted in `3d-api` (SQLite) with an expected action:

- start in one channel, continue in another: CLI start → bot photo → HA apply is the epic's
  acceptance test;
- the session survives restarts of `3d-api`, the bot and HA, and days between steps (print in the
  evening, photo in the morning);
- transitions are idempotent (a repeated button press does not apply twice) and out-of-order
  calls return 409 with the expected action;
- a photo is bound by the QR on the plate's label (session, plate, generation revision); a photo
  whose QR names an old or regenerated plate is refused. Without a readable QR the photo is
  accepted only inside an explicit session context (CLI `--session`, a reply in the bot, the
  card), and then the revision cannot be checked: the user confirms which plate it is;
- `3d calibrate status --session ID` (or the card, or the bot) shows where it stopped; `abort`
  works from any step.

Without `3d-api` there is no session: `3d calibrate plan`, `record`, `ls`, `show` and `check` talk
to `3d-api` too, so only the local `3d calibrate gen` (PR [#249][pr249]) works offline, and later
the settings history buffer ([#264][264], §4).

[116]: https://github.com/alex-mextner/3d-cli/issues/116
[166]: https://github.com/alex-mextner/3d-cli/issues/166
[202]: https://github.com/alex-mextner/3d-cli/issues/202
[pr297]: https://github.com/alex-mextner/3d-cli/pull/297
[263]: https://github.com/alex-mextner/3d-cli/issues/263
[264]: https://github.com/alex-mextner/3d-cli/issues/264
[268]: https://github.com/alex-mextner/3d-cli/issues/268
[269]: https://github.com/alex-mextner/3d-cli/issues/269
[271]: https://github.com/alex-mextner/3d-cli/issues/271
[272]: https://github.com/alex-mextner/3d-cli/issues/272
[273]: https://github.com/alex-mextner/3d-cli/issues/273
[274]: https://github.com/alex-mextner/3d-cli/issues/274
[275]: https://github.com/alex-mextner/3d-cli/issues/275
[276]: https://github.com/alex-mextner/3d-cli/issues/276
[277]: https://github.com/alex-mextner/3d-cli/issues/277
[278]: https://github.com/alex-mextner/3d-cli/issues/278
[279]: https://github.com/alex-mextner/3d-cli/issues/279
[280]: https://github.com/alex-mextner/3d-cli/issues/280
[281]: https://github.com/alex-mextner/3d-cli/issues/281
[282]: https://github.com/alex-mextner/3d-cli/issues/282
[283]: https://github.com/alex-mextner/3d-cli/issues/283
[284]: https://github.com/alex-mextner/3d-cli/issues/284
[285]: https://github.com/alex-mextner/3d-cli/issues/285
[302]: https://github.com/alex-mextner/3d-cli/issues/302
[308]: https://github.com/alex-mextner/3d-cli/issues/308
[320]: https://github.com/alex-mextner/3d-cli/issues/320
[322]: https://github.com/alex-mextner/3d-cli/issues/322
[327]: https://github.com/alex-mextner/3d-cli/issues/327
[328]: https://github.com/alex-mextner/3d-cli/issues/328
[330]: https://github.com/alex-mextner/3d-cli/issues/330
[331]: https://github.com/alex-mextner/3d-cli/issues/331
[332]: https://github.com/alex-mextner/3d-cli/issues/332
[333]: https://github.com/alex-mextner/3d-cli/issues/333
[334]: https://github.com/alex-mextner/3d-cli/issues/334
[pr249]: https://github.com/alex-mextner/3d-cli/pull/249
[pr257]: https://github.com/alex-mextner/3d-cli/pull/257
[pr288]: https://github.com/alex-mextner/3d-cli/pull/288
[pr289]: https://github.com/alex-mextner/3d-cli/pull/289
[pr312]: https://github.com/alex-mextner/3d-cli/pull/312
[pr314]: https://github.com/alex-mextner/3d-cli/pull/314
