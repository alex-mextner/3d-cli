# Critic-prompt templates for the silhouette-match loop

Reusable, copy-pasteable prompts for the vision critic that drives the
pixel-match loop (report §3.3 + Appendix E §17). The metric — not the model —
decides acceptance; the critic only **proposes** a single numeric edit, the
orchestrator measures IoU, accepts on strict improvement, and logs to the
changelog.

Placeholders use `{curly}` braces. Fill them in before sending:

| Placeholder      | Meaning                                                              |
|------------------|---------------------------------------------------------------------|
| `{iou}`          | current silhouette IoU of RENDER vs REFERENCE (0..1)                 |
| `{target}`       | target IoU threshold (e.g. `0.93`)                                   |
| `{phase}`        | current coarse->fine phase name (see "Layered matching order")       |
| `{allowed}`      | comma-separated list of `constants.scad` params the critic may edit |
| `{changelog}`    | the running changelog block (see "Changelog line format")           |
| `{per_feature}`  | optional per-feature IoU vector, e.g. `funnel:0.71 boiler:0.95 ...`  |

The three images the critic always receives:
1. **OVERLAY** — REFERENCE silhouette in RED, current RENDER silhouette in
   CYAN, matched pixels in GREY.
2. **EDGE-OVERLAY** — REFERENCE edges in red, RENDER edges in cyan.
3. (optional) the raw RENDER and REFERENCE side by side for context only.

---

## Template 1 — Ranked numeric-delta diff

The default critic. Produces machine-parseable, directional, ranked edits
instead of prose. Use it when you want a short ranked list and will apply the
top entry. (Report §3.3: "ranked numeric-delta diffs".)

```
You are a CAD silhouette critic. You see an OVERLAY image: the REFERENCE
locomotive outline in RED, the current RENDER outline in CYAN, matched regions
in GREY. You also see an EDGE-OVERLAY (red=reference edges, cyan=render edges).

Current silhouette IoU: {iou}.  Target IoU: {target}.
Current phase: {phase}.  You may ONLY edit these params: {allowed}.

TASK: List the TOP 3 mismatches between RENDER and REFERENCE. For each, output
one row of strict JSON in an array, sorted by visual magnitude (largest first):

[
  {
    "rank": 1,
    "feature":  "<funnel|boiler|cab|pilot|smokebox|dome_front|dome_rear|runningboard|buffer>",
    "param":    "<exact name in constants.scad, MUST be in the allowed list>",
    "current":  <number>,
    "target":   <number>,
    "delta_mm": <signed number, target-current, in mm (or fraction for *_frac)>,
    "confidence": <0..1>,
    "reason":   "<where CYAN sticks out beyond RED, or RED beyond CYAN, in one sentence>"
  },
  ...
]

Rules:
- Reason ONLY from where CYAN sticks out beyond RED (render too big/tall there)
  or RED sticks out beyond CYAN (render too small/short there).
- Every observation MUST be a number with units. "Too tall" is forbidden;
  "+6 mm too tall" is required.
- ONE param per row. No coupled edits. Sort by visual magnitude, not confidence.
- Only propose params in the allowed list {allowed}. Ignore mismatches you
  cannot fix this phase.
```

---

## Template 2 — Anti-oscillation / changelog-aware single edit

The workhorse for the monotonic loop. Feeds the changelog so the critic never
re-tries a reverted move (the concrete antidote to the FlipFlop / widen-narrow
-widen loop, report §3.3 + §17.1). Emits exactly ONE edit or the `CONVERGED`
token.

```
You are a CAD silhouette critic. You see an OVERLAY image: the REFERENCE
locomotive outline in RED, the current RENDER outline in CYAN, matched regions
in GREY. You also see an EDGE-OVERLAY (red=reference edges, cyan=render edges)
and a CHANGELOG of prior edits with their score impact.

Current silhouette IoU: {iou}.  Target IoU: {target}.
Current phase: {phase}.  You may ONLY edit these params: {allowed}.

CHANGELOG (do NOT propose a move already marked 'reverted'; do NOT undo a move
marked 'OK'):
{changelog}

TASK: Propose exactly ONE parameter edit that will most increase IoU. Output
strict JSON:
{
  "feature":    "<funnel|boiler|cab|pilot|smokebox|dome_front|dome_rear|runningboard|buffer>",
  "param":      "<exact name in constants.scad, MUST be in the allowed list>",
  "current":    <number>,
  "target":     <number>,
  "delta_mm":   <signed number>,
  "confidence": <0..1>,
  "reason":     "<where the cyan/red mismatch is, in one sentence>"
}

If the residual is below target AND you have no high-confidence edit, output
exactly:
{"CONVERGED": true}

Rules:
- Reason ONLY from where CYAN sticks out beyond RED (render too big/tall there)
  or RED beyond CYAN (render too small/short there).
- ONE parameter only. No coupled edits.
- Numbers in millimetres (or the fraction for *_frac params), with units
  implied by the param name.
- Honour the CHANGELOG: a param last marked 'reverted' at a given target is a
  dead move; pick a DIFFERENT param or a DIFFERENT magnitude, or back off it.
- Stay inside the allowed set {allowed}. A mismatch on a frozen param is not
  yours to fix this phase — ignore it.
```

---

## Template 3 — Silhouette / overlay critic (spatial-error reasoning)

Use when the residual is dominated by *where* the silhouette bleeds rather than
a single obvious dimension. Forces the critic to localise the error on the
overlay before naming a param. (Report §3.3: "the spatial error is made
literal"; the overlay/difference image is dramatically more reliable than two
separate pictures.) Optionally fed a per-feature IoU vector so it edits the
*right* feature (failure-playbook row 4, §17.4).

```
You are a CAD silhouette critic reasoning about a DIFFERENCE/OVERLAY image only.
In the OVERLAY: RED = REFERENCE-only pixels (render is MISSING shape there),
CYAN = RENDER-only pixels (render has EXTRA shape there), GREY = matched. The
EDGE-OVERLAY shows red reference edges vs cyan render edges.

Current silhouette IoU: {iou}.  Target IoU: {target}.
Current phase: {phase}.  You may ONLY edit these params: {allowed}.
Per-feature IoU (lower = worse, fix the worst you are allowed to touch):
{per_feature}

STEP 1 — Localise: name the ONE region with the largest contiguous colour bleed
and state its direction. Examples of valid reasoning:
  "Cyan sticks out ABOVE the funnel by ~6 mm -> funnel too tall."
  "Red band along the LOWER boiler -> render boiler diameter too small there."
  "Cyan wedge ahead of the smokebox -> pilot/buffer beam projects too far front."

STEP 2 — Map that region to exactly ONE allowed param and a signed delta in mm.

Output strict JSON:
{
  "region":     "<funnel-top|boiler-lower|cab-rear|pilot-front|dome_front|dome_rear|runningboard|buffer>",
  "bleed":      "<CYAN_EXTRA|RED_MISSING>",
  "feature":    "<funnel|boiler|cab|pilot|smokebox|dome_front|dome_rear|runningboard|buffer>",
  "param":      "<exact constants.scad name, MUST be in the allowed list>",
  "current":    <number>,
  "target":     <number>,
  "delta_mm":   <signed number>,
  "confidence": <0..1>,
  "reason":     "<the localised bleed from STEP 1, one sentence>"
}

If no colour bleed exceeds a few mm AND IoU >= target, output exactly:
{"CONVERGED": true}

Rules:
- CYAN bleed => render is TOO BIG there => reduce the param (negative delta).
- RED bleed  => render is TOO SMALL there => increase the param (positive delta).
- ONE region, ONE param, ONE delta. No coupled edits.
- Stay inside the allowed set {allowed}.
```

---

## Changelog line format

The changelog is the anti-oscillation memory. Every attempted edit appends
**one line**, regardless of outcome. The critic reads it back (Template 2/3) and
must not re-propose a `reverted` move or undo an `OK` move. The orchestrator
writes these (it owns the metric); the critic only reads them.

Canonical line:

```
<param> <old>-><new>: IoU <best_before>-><score_after> <STATUS>
```

`<STATUS>` is exactly one of:

| STATUS                  | Meaning                                                        | best/stale effect          |
|-------------------------|----------------------------------------------------------------|----------------------------|
| `OK`                    | strict improvement AND all gates pass — edit kept              | `best=score`, `stale=0`    |
| `no improve reverted`   | rendered fine but IoU did not beat `best+MARGIN` — rolled back | `stale += 1`               |
| `gate FAIL reverted`    | improved IoU but broke manifold/consistency/printability       | `stale += 1`               |
| `INVALID RENDER reverted` | render errored / blank mask (zero-reward anchor) — rolled back | `stale += 1`             |

Worked examples (verbatim format the loop produces):

```
boiler_d 56->60: IoU 0.812->0.871 OK
funnel_h 24->28: IoU 0.871->0.853 no improve reverted
funnel_h 24->22: IoU 0.871->0.889 OK
cab_h 40->46: IoU 0.889->0.901 gate FAIL reverted
pilot_h 18->0: IoU 0.901->0.000 INVALID RENDER reverted
dome_d 14->16: IoU 0.901->0.918 OK
```

Reading rules for the critic:
- A param whose **last** line is `... reverted` at target `T` is a dead move at
  `T`; do not re-propose `param->T`. Pick a different param, a different
  magnitude, or back off it entirely.
- A param whose last line is `OK` is at its accepted value — do not undo it.
- `*_frac` params log the fraction, not mm (e.g. `funnel_frac 0.27->0.30`).

---

## CONVERGED protocol (stop condition)

The loop stops on **either** trigger; whichever fires first.

1. **Critic CONVERGED token.** The critic emits exactly `{"CONVERGED": true}`
   when the residual is below `{target}` AND it has no high-confidence edit.
   The orchestrator stops the current phase on this token. It must be the
   *only* content of the JSON object — any accompanying edit fields are ignored
   and treated as "not converged".

2. **No-improvement cap (`MAX_STALE`).** Independently, the orchestrator counts
   consecutive non-`OK` rounds in `stale`. When `stale >= MAX_STALE` (the
   ReLook resample cap), the phase ends even if the critic keeps proposing.
   This bounds plateau-fiddling regardless of the model.

Orchestrator accept/reject logic (report §17.2) — the metric decides, never the
model:

```python
best = -1.0; changelog = []; stale = 0
while stale < MAX_STALE:
    crit = call_vision_critic(overlay, edge_overlay, changelog, best, TARGET,
                              allowed=phase_params)
    if crit.get("CONVERGED"):
        break                                            # stop condition 1
    if crit["param"] not in phase_params:                # phase guard
        continue
    old = read_param(crit["param"])
    write_param(crit["param"], crit["target"])           # apply ONE edit
    if not renders_ok():                                 # zero-reward anchor
        write_param(crit["param"], old)
        changelog.append(f'{crit["param"]} {old}->{crit["target"]}: '
                         f'IoU {best:.3f}->0.000 INVALID RENDER reverted')
        stale += 1; continue
    render_silhouette(); score = iou()                   # measure
    gates_ok = manifold() and consistency() and printability()
    if score > best + MARGIN and gates_ok:               # forced-monotonic accept
        changelog.append(f'{crit["param"]} {old}->{crit["target"]}: '
                         f'IoU {best:.3f}->{score:.3f} OK')
        best = score; stale = 0
    else:
        write_param(crit["param"], old)                  # revert
        tag = "gate FAIL reverted" if not gates_ok else "no improve reverted"
        changelog.append(f'{crit["param"]} {old}->{crit["target"]}: '
                         f'IoU {best:.3f}->{score:.3f} {tag}')
        stale += 1
return best, changelog
```

Three invariants that make this robust:
1. **The metric, not the model, decides acceptance** — the critic only proposes.
2. **Invalid renders score worst-possible (IoU=0)** — no reward hacking.
3. **The changelog is fed back** — the critic never amnesiacally re-tries a
   reverted move. `MARGIN` rejects noise-level "improvements"; `MAX_STALE` ends
   a plateau.

Suggested constants (tune to the per-render IoU noise floor): `TARGET=0.93`,
`MARGIN=0.005` (above per-render noise), `MAX_STALE=6`.

---

## Coarse->fine layered matching order (report §7.5 + §17.3)

Do NOT optimize all parameters at once. Match in order of visual dominance,
**freezing** each phase before moving to the next. Each phase runs the full
monotonic loop above, but the critic's `{allowed}` set lists **only** that
phase's params — lower phases stay frozen so each monotonic step stays
meaningful. A final unfrozen polish round escapes ordering artifacts.

| Phase | Name              | What it nails                          | Params (`{allowed}`)                                                                                  |
|-------|-------------------|----------------------------------------|------------------------------------------------------------------------------------------------------|
| 1     | Bounding box      | loco fills the same frame (biggest IoU)| `total_length`, `total_height`, `baseline_z`                                                          |
| 2     | Major masses      | gross silhouette shape                  | `boiler_len`, `boiler_d`, `cab_len`, `cab_h`, `smokebox_len`                                          |
| 3     | Landmark features | funnel, domes, buffers, running board   | `funnel_frac`, `funnel_h`, `funnel_flare`, `dome_f_frac`, `dome_r_frac`, `dome_d`, `pilot_h`, `buffer_z` |
| 4     | Fine details      | window, beading, headlight, chamfers    | `cab_window_w`, `cab_window_h`, `beading_r`, `headlight_x`                                            |

Rationale (§7.5): match by visual dominance — bounding box first wins the most
IoU and is the most reliable, so it earns the early monotonic steps; major
masses set the gross shape; landmarks place the recognizable features (funnel
~0.27 L from the front per the SPEC; the two domes; buffer beam; running-board
line); fine details last. Coarse-to-fine ordering ensures early single-param
moves are far from flat, which is why the loop does not plateau immediately
(§16/§9).

Phase controller (§17.3):

```python
PHASES = [
    ["total_length", "total_height", "baseline_z"],                 # 1 bounding box
    ["boiler_len", "boiler_d", "cab_len", "cab_h", "smokebox_len"], # 2 major masses
    ["funnel_frac", "funnel_h", "funnel_flare", "dome_f_frac",      # 3 landmarks
     "dome_r_frac", "dome_d", "pilot_h", "buffer_z"],
    ["cab_window_w", "cab_window_h", "beading_r", "headlight_x"],   # 4 fine details
]
for phase_params in PHASES:
    freeze_all_except(phase_params)
    best, log = match_loop(allowed=phase_params)   # §17.2 loop, restricted
unfreeze_all(); match_loop(allowed=ALL_PARAMS)     # final free polish round
```

Caveat (§7.5/§9.5): freezing the bounding box before the masses can leave a
local minimum (e.g. a boiler-length change wanted only in a later pass). The
**final unfrozen polish round** exists precisely to escape that ordering
artifact — run it after the four frozen phases, with the full param set in
`{allowed}`, until `CONVERGED` or `MAX_STALE`.

---

## Failure-signature playbook (report §17.4)

If the loop misbehaves, the signature usually points to one cause — and most
"the AI loop doesn't work" reports are a measurement bug, not a model bug. Fix
the metric before you tune the model.

| Symptom                                   | Likely cause                            | Fix                                              |
|-------------------------------------------|-----------------------------------------|--------------------------------------------------|
| Score never improves, critic keeps proposing | masks mis-registered (scale/crop)    | re-lock camera, force same `WxH!`, re-threshold  |
| Loop "converges" instantly at low IoU     | blank/invalid render scored as match    | enforce IoU=0 on all-background mask (zero-reward)|
| Oscillation despite monotonic rule        | MARGIN too small (noise accepted)       | raise MARGIN above per-render noise floor        |
| Critic edits the wrong feature            | global IoU only, no per-feature signal  | add per-feature IoU vector (Template 3 input)    |
| Good side match, wrong depth/proportion   | single view under-constrains depth      | rely on spec-pinned dims; add a second view      |
| Edits help IoU but break printability     | gate not wired into accept              | add manifold/printability to the AND in accept   |

The single most important operational takeaway: the measurement apparatus
(camera lock + mask registration + zero-reward on invalid) must be correct
**before** you trust any critic feedback. Fix the metric before you tune the
model.
