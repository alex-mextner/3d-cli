# Why vision did not see the part come off the plate (U1, 2026-10-22)

Incident write-up and the fix: `vision_part_detached` ([`lib/vision_detach.py`](../../lib/vision_detach.py)).
Companion of [printer-vision.md](printer-vision.md) (what the detectors are) and
[printer-vision-geometry.md](printer-vision-geometry.md) (the camera calibration it uses). Everything
below was read from the printer and this workstation read-only on 2026-10-22 (Moonraker object
queries, `/server/history/*`, `klippy.log`/`moonraker.log` over HTTP, `journalctl --user`); what
could not be proven is marked **[INFERENCE]**.

## The case

Snapmaker U1, job `u1-two-panel-032-adhesion-retry-af5030448507598d.gcode` (history `00006E`,
started 17:44:44 local = 15:44:44 UTC, 53 layers). At 20:22:59 local the print was PAUSED at layer 17/53
(20.2 %, gcode Z 5.32 mm; the pause lift parks the bed at Z 10.45). The camera frame
(`~/.cache/3d-fixes/u1_monitor.jpg`, fixture
[`tests/data/vision/u1_part_detached_layer17.jpg`](../../tests/data/vision/u1_part_detached_layer17.jpg))
shows the white honeycomb panel with its 5 mm of printed layers floating above the zig-zag rim that
stayed stuck to the plate; bare textured-PEI plate is visible in the gap, and the other tools' colours
are not yet printed. With the camera calibration drawn at the parked Z (bed 10.45, part 5.32 mm tall) the
footprint on the plate lands exactly on the stuck rim and the expected top of the part sits tens of pixels
below where the slab is.

## Why nothing fired — five independent causes

| # | cause | evidence |
|---|---|---|
| 1 | **No detector of this class ran.** Vision's default detectors are the Obico spaghetti model + CLIP verifier and the camera-health checks; the bed-change detectors (`vision_part_moved`, `vision_foreign_object`, `vision_no_growth`) exist but are **off unless `--scene`**. | `lib/vision_detect.py` `build_scorer(..., scene=False)` and `bed_change = scene or cfg.get("scene", False)`; docs/commands/printer-vision.md "What it detects". The u1 unit runs `3d printer vision u1 --dry-run --verbose` (no `--scene`). |
| 2 | **Even with `--scene`, `part_moved` forgets slowly-developing changes.** Its reference is the median of frames 3-12 min old; "once the change is older than the reference window it becomes the new normal and the finding clears". A warp that lifts a corner over many layers is never a 3-minute change, and a lift older than 12 min is the reference. It also needs the part to *move in a few minutes*, not to be *wrong against the plate*. | `lib/vision_scene.py` lines 7-19 (design) and `SceneParams` `ref_min_age_s=180`, `ref_max_age_s=720`. |
| 3 | **The printer's own AI looks for spaghetti only.** `defect_detection.json`: `main_enable true`, `clean_bed` on (start only), `noodle` on (sensitivity low, threshold 0.83), `residue` **off**, `nozzle` off. A part that peeled but still prints neatly on top of itself is not "noodle". klippy.log shows 18 noodle verdicts for this job (ids 104 and 116-132), every one `obj_probs 0.0` (first 15:45:07Z, last 18:17:20Z; one per 5-9 min). | `GET /server/files/config/snapmaker/defect_detection.json` (the same answer as `/printer/objects/query?defect_detection`), `klippy.log` `[defect_detection] response info`. |
| 4 | **The spaghetti detector correctly saw nothing.** p ≤ 0.222, q ≤ 0.011, trend 0.0 for all 949 frames of the print (every 10 s from 17:44:51 to 20:22:50 local) — a lifted slab is neither filament nor speckle. | `journalctl --user -u 3d-printer-vision@u1 -o short-iso`: the JSON rows `{"p":..,"q":..,"trend":..}`. |
| 5 | **Delivery was held.** Both services run with `--dry-run` ("delivery held" in the unit description): `ExecStart=… 3d printer vision %i --dry-run --verbose` and `… watch u1 --dry-run`. Events that would have been raised are printed to the journal; nothing goes to `tg` or the agent. So the only event of the whole print is the *state* event for the 20:22:59 pause, printed as "[3d printer alert -> temporary u1 agent]" at 20:23:02 with `frames: none attached`. This applies to *every* event kind, including spaghetti. | `systemctl --user cat 3d-printer-vision@.service`; `journalctl --user -u 3d-printer-watch@u1`. A deployment fact, not code: the repo cannot fix it; `--dry-run` has to be dropped on the units once the owner trusts the events. |

Not a cause (checked): the camera feed was healthy (no `vision_camera_stale`/`dark`; the keepalive
`camera.start_monitor` arrived every minute from the vision host in `moonraker.log`); the print
context poll worked (`z` present in every row); guard (`3d printer guard`) was not armed and would not
have paused anyway — it acts only on **confirmed spaghetti** (`lib/printer_ops/guard.py`
`CONFIRM_FRAMES = 2`, `level == CRITICAL and positive`).

## What paused the printer, and when

`moonraker.log`: `2026-10-22 18:22:59,598 klippy_apis.py:pause_print() - Requesting job pause...` and
`klippy.log`: `18:22:59.599 [pause_resume] request pause` / `Pausing...` / `Exiting SD card print,
lines=121847 … file_position=2719723` (UTC; local 20:22:59). Not the firmware's defect detector: no
`exception`, no code 1/2/3, `print_stats.message` empty, and the last verdict 5 min earlier is a
0.0 noodle. A Moonraker API pause by a client (Fluidd/app/agent): the POST's origin is not logged
at that level, so *who* paused is **[INFERENCE]: the owner, on seeing the camera** — the pause lands 10 s
after a vision keepalive and within 0.1 s of the vision loop's 10 s grid, which is also consistent with
an automatic caller, but vision and guard cannot pause (guard not running; vision has no write path).

## When did it come off? — not provable

No frame of this print was archived: the vision loop keeps frames only for events, there was no
timelapse (the printer's camera root has no file for jobs `00006D`/`00006E`), and the bed monitor only
stores empty-plate references. The z progression from the vision journal bounds the layers (first layer
at Z 0.2 from 17:50 to ~18:15; Z 2.44 at 18:50; 5.32 at 20:20) but not the lift. **[INFERENCE]** The
upper layers separated from the rim gradually (warp/curl) while the nozzle still printed on the lifted
surface, since a sudden 5 mm lift would have been a nozzle crash or a pause. "Long ago" cannot be
confirmed; the fix therefore also makes the next such print date itself (an event saves its frame).

## The fix: `vision_part_detached`

Tier (a), OpenCV only, no model, no reference frame from the past:

1. The camera calibration projects each part footprint's prism (bed → current layer) into the frame.
   From the low chamber camera its **near side wall** is the band between the base edge (on the plate)
   and the top edge (newest layer): `vision_geometry.wall_mask`.
2. The plate colour is learnt **from the same frame** (bed pixels outside the part, median CIE (a, b)),
   so exposure and white balance cancel; the check abstains when the plate has chroma < 25 (grey plate under
   cold light), when fewer than 1 % of the frame is plate outside the part, or below 1.5 mm.
3. `gap` = share of the eroded wall band (25 px at 1920, the calibration residual) that has the plate
   colour. Warning when `gap ≥ 0.25` (absolute, a part that lifted before any baseline) or when it rose
   ≥ 0.12 above the lowest gap of this print (relative, so a concave part whose convex-hull footprint
   always shows some plate stays quiet), for 3 consecutive frames (median of the last three). Critical when
   the rise stays ≥ 0.25 for 12 frames (2 min). It never pauses anything; guard stays spaghetti-only.
4. On the incident frame: `gap` **0.52** (full size 0.5222 live, 0.5201 on the 960×540 fixture); the same
   frame with the wall band painted part-white (synthetic "still attached" twin) 0.00; the warning fires at the
   3rd consecutive detached frame, critical at the 12th.

Run `3d printer vision u1 --once` on a printing U1 to read the raw `detach_gap` / `part_detached`.
Config: `watch.vision.detach: false | {gap_abs, gap_rise, gap_crit, persist, ...}`; `--no-detach`.

### Limits (honest)

- **One real positive.** Thresholds (0.25 / 0.12 / 0.25, 3 / 12 frames) are set from one real frame and
  the synthetic twin: **[INFERENCE]** for everything but that frame. **No healthy frame with a footprint and this
  plate lighting exists** in `~/.cache/3d-cli/vision/data` (the 870-frame `u1rec_walls` recording is the
  green-wall job under a darker, almost grey plate: the check abstained on all 44 sampled frames), so the
  false-positive rate is unmeasured. Record healthy prints with `3d printer vision record` and run
  `vision eval --neg` before trusting it unattended.
- Convex-hull footprints (`exclude_object`) over-cover concave, hollow and lattice parts: a stable gap is
  handled by the relative rule, but a part that starts ≥ 25 % bare in its near wall band alerts through the
  absolute rule on every print — set `detach: {gap_abs: 1.0}` for such jobs.
- Detects a lift of at least ~½ of the printed height plus the 25 px erosion (about 3-4 mm at Z 5 from the
  U1 camera); a 1-2 mm corner curl is invisible to a camera at this angle. Walls facing away from the
  camera are not judged.
- Needs the calibration and the footprints (`exclude_object` in the G-code). Without a calibration (K1 SE) only
  the opt-in `fixed_bed` fallback exists (learnt footprint, warning only, synthetic tests only); on a bed-moves-Z
  printer without a calibration nothing honest is possible, because the footprint moves in the picture with every
  layer and cannot be learnt from a frame. A plate colour close to the filament colour is treated as plate
  (orange on a gold plate): the part looks "detached" from the start and only the relative rule helps.
- While PAUSED the vision loop fetches no frames and the reported Z includes the pause lift, so the live check
  runs only while printing; `vision --once` on a paused printer reads a lifted Z (tests use bed Z 10.45 and
  part height 5.32 explicitly).

## Slow drift: the second reference of `part_moved`

`SceneWatch` (lib/vision_scene.py, only with `--scene`) compared the frame with the median of frames 3-12 min
old, so a part that lifts a little every few minutes was never different from its reference. It now also keeps a
**slow reference** (15-60 min old, up to 2.5 mm lower bed travel, at most 16 frames in the median): the same
Lab-difference test, a larger minimum area (0.6 % of the frame) and 6 checks in a row, reported as
`vision_part_moved` with `data.reference: "slow"`. Test: a synthetic part that creeps one pixel row every 5 min is
invisible to the 3-12 min reference over 90 min and found by the slow one only after 15 min
([tests/test_vision_scene.py](../../tests/test_vision_scene.py)). Limit: the reference must be at the same Z within
2.5 mm, so on a fast-growing print (> ~3 mm per 15 min) there is no slow reference and the check stays silent;
the detached-part check above has no such limit (it compares against the plate in the same frame).

## `vision record` returned nothing (root cause)

`vision record u1 OUT --max 1` ran while the U1 was PAUSED: `run_record` saved frames only when the state
was `printing`, never wrote anything about it, and `--max 1` can never be reached on an idle/paused
printer, so it looped until the caller's 60 s timeout, leaving an empty `meta.jsonl`
(`~/.cache/3d-fixes/u1_now/meta.jsonl`, 0 bytes). Fixed: it records while printing **or paused**, logs the
state change to stderr, adds `state` to each row, and `--timeout S` fails with exit 1 when no frame was saved
in S seconds ([tests/test_vision_record.py](../../tests/test_vision_record.py), a loopback Moonraker fake).

## What I could not verify

- When the part detached (no frames), and who sent the pause.
- The false-positive rate of the new check on healthy gold-plate prints, and its recall below ~3 mm of lift.
- K1 SE: no calibration, so only the unvalidated `fixed_bed` fallback; `~/.cache/3d-fixes/k1_now.jpg` is a truncated
  JPEG (no EOI; OpenCV decodes it with a warning, the image reader tool rejects it).
