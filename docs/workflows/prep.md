# Model to print-ready plate with `3d prep`

The flow that replaces the Chekhov session's ~27 one-off scripts. Each box is a step with a cache entry;
the arrows are what invalidates what. Commands: [`3d prep`](../commands/prep.md),
[`3d heavy`](../commands/heavy.md), [`3d remote`](../commands/remote.md),
[`3d profiles doctor`](../commands/profiles.md). Evidence and ranked bottlenecks:
[the ledger](../research/prep-session-ledger-2026-10-20.md). U1 Orca CLI traps:
[u1-orca-cli.md](../printers/u1-orca-cli.md).

```
diagnose ─ repair ─ orient ─ adapt ─┬─ overhang
                                    ├─ fit ─ stability
                                    └─ colors ─ layers ─ project ─ slice ─ gates
                                                                      proof (reads everything)
```

## One-time set-up

```bash
uv sync --extra prep                 # pillow + scikit-image (the dev extra has them too)
3d profiles doctor                   # presets: parents exist, 0.08-0.32 band inside the machine limits
3d doctor                            # Orca / openscad / ... present?
```

## The run

```bash
3d prep "cat phone holder full.stl" --out out --spec examples/prep/chekhov/prep.yaml
```

The spec names what is specific to the object: the phones table (`fit`), the lean-seat holder, the colour
regions (`colors`), the relaxed face zone of the adaptation, the process (4 walls, 15 %, 0.08-0.32 mm).
Everything else is a flag with a default. Read `out/proof-report.md`: header verdict, one section per
step with its numbers and pictures, the honest limits at the end.

What each step decided on the Chekhov STL (411 213 faces, 62 components, 37 open edges, 87 non-manifold edges):

| Step | Result |
|---|---|
| diagnose / repair | FAIL -> one watertight shell; 61 debris shells dropped, 18 holes filled, surface moved <= 0.48 mm (WARN above 0.2) |
| orient | the upright orientation is already the best of 116 candidates |
| adapt | open hollow base filled (+164 cm3) and 45 degree wedges under the remaining overhangs; the face zone (z > 50 mm) is relaxed to 75 degrees without pillars |
| overhang | 0 islands, bridges <= 20 mm (WARN, the chin and ears) |
| fit | 29 phones x 4 cases = 116 rows: PASS / RISK, 0 FAIL |
| stability | tip angle 50 deg alone, 25-31 deg with the heaviest/lightest phone; a 2-5 N tap or pull tips the stand (safety factor < 1) — a WARN to read, not a failed print |
| colors / layers | 8 colour parts, eyes `WG`, nose `WR` mixes, every mixed layer <= 0.12 mm, no filament on adjacent layers |
| project / slice / gates | one multi-part object 3MF with a prime tower, headless slice, G-code gates |

## A K1 SE plate

`--printer k1se` takes a single part to the Creality K1 SE (one head, no colours) through the same cache: the
`project` step writes the placed STL and three flat Orca presets instead of a 3MF, `slice` runs them with
`--load-settings`, and `gates` judges the real feedrates per feature from the G-code. The soil-sensor mole plate
(2 parts, 94 x 43 x 24.6 mm, Elegoo RAPID PLA+) reproduces the owner's reference slice: 123 layers, 13.56 g, 32 min 52 s
(reference 32 min 51 s), every feature's fastest move equal to the reference. See
[K1 SE in the command doc](../commands/prep.md#k1-se) for the flags and the overlay file.

## Re-running

```bash
3d prep cat.stl --out out --spec chekhov.yaml                 # every step "cached": seconds
3d prep cat.stl --out out --spec chekhov.yaml --colors c2.yaml  # colours -> layers -> 3MF -> slice -> gates only
3d prep cat.stl --out out --spec chekhov.yaml --angle 50      # from orient on
3d prep cat.stl --out out --spec chekhov.yaml --from adapt    # redo the adaptation and everything after
3d prep status out
```

## On the heavy box

```bash
3d remote run --project cat --name prep --wait --push cat.stl --push examples/prep/chekhov --pull -- \
  3d prep cat.stl --out ../out --spec chekhov/prep.yaml --no-lock
```

The box's `heavy` wrapper holds the one-job-at-a-time lock and the memory cap, so `--no-lock` skips the
local `3d heavy` layer; on a workstation leave it out and `3d prep` takes `/tmp/3d-cli-heavy.lock` once
for the whole run (slicing inside it never waits for its own lock).

## Measured on the Chekhov STL

Original `cat phone holder full.stl` (411 213 faces), `examples/prep/chekhov/prep.yaml`, run on the 16-core
heavy box (`3d remote run ... -- 3d prep ... --no-lock`, cold cache, Snapmaker Orca 01.10.01.50). Seconds per
step from `prep.json`; the session ledger's matching phase is the right-hand column.

| Step | Seconds | Verdict | Session ledger (same job, by hand) |
|---|---:|---|---|
| diagnose | 1.1 | FAIL (informational) | phase 2: 14.6 min incl. repair |
| repair | 5.9 | WARN (surface moved 0.48 mm) | |
| orient | 1.5 | WARN (current is the best) | — |
| adapt (cavity fill + wedges) | 8.2 | PASS | phase 4: 28.5 min |
| overhang | 1.8 | WARN (10 tiny bridges, 6 mm2) | |
| fit (29 phones x 4 cases) | 1.8 | WARN (12 RISK, 0 FAIL) | phases 3, 5, 7: ~41 min |
| stability | 1.1 | WARN (tap/pull SF < 1) | |
| colors | 3.1 | PASS (8 parts, 2 mixes) | phases A-C (not run on the cat at all) |
| layers | 0.4 | PASS (688 layers, 8.6 % at 0.08) | |
| project (3MF) | 3.7 | PASS | |
| slice (Orca CLI) | 17.0 | WARN (7 slicer warnings, recorded) | never reached |
| gates (10 G-code gates) | 18.3 | PASS, 0 SKIP | never reached |
| proof (13 images + report) | 11.0 | PASS | phases 8, 10, 11: 16.6 min (+ 25.5 min deadlock) |
| **total** | **75.1** (76 s wall) | overall WARN | **2 h 22 min, no G-code** |

| Re-run | Seconds |
|---|---:|
| same command again (every step cached) | 1.0 |
| `--from colors` (colours, layers, 3MF, slice, gates, proof) | 55 |
| the whole flow, cold | 76 |

The same flow on the shared workstation under load (other agents running, heavy lock queued)
took 263 s up to the proof pack before the adaptation/fit/overhang speed-ups of the last commits, plus the
slice; the remote box is the intended place for it (`3d remote`).

What the G-code gates said about the real print: walls 687 layers with 4+ loops (3 top layers fewer), infill
estimate 0.160 vs 0.15, no supports, layer heights 0.080-0.255 mm (8.6 % at 0.08), mix layers (165 in two
mix regions) all <= 0.12 mm, no green or red layer adjacent to another inside the eye and nose boxes,
tools T0-T3 only, prime tower present, extrusion inside the 270 mm bed.
