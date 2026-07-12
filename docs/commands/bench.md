# `3d bench` — auto-scored generative-modeling benchmark

Run a suite of ModelRift-style cases (a text prompt → an OpenSCAD `.scad`) through a chosen
AI backend, gate each candidate on **build-success** (does OpenSCAD render it at all?), then
score the survivors on a metric vector and report the rates + cost/efficiency columns.

Backed by `lib/ai/bench.py`. Implements APPLY-RESEARCH **P1.3** (the ModelRift task format
with an *automated* metric vector replacing the subjective 0–5 score) and the VLM-judge
methodology (as **one advisory column**, never the only score). See
[`docs/APPLY-RESEARCH.md`](../APPLY-RESEARCH.md).

Also reachable as `3d ai bench` (same runner).

> **Honesty:** a mock run proves the **scoring harness** works — build-success gating, the
> metric-column plumbing, failure-path handling, aggregation, persistence, compare — **not**
> that any real backend scores well. A case that never renders is counted as a build-failure,
> not crashed; a refusal / non-SCAD reply is a caught failure; a budget-exceeded case records
> its `stop_reason`; the suite always completes and reports.

## Usage

```
3d bench [options]
```

| Option | Meaning |
|---|---|
| `--backend NAME` | AI backend: `claude`, `codex`, `opencode`, `ollama`, `mock`. Default: auto-pick, or the deterministic mock when `$THREED_AI_MOCK_RESPONSE` is set. |
| `--suite DIR` | Directory of `*.json` bench cases (default: the shipped `lib/data/bench`). |
| `--workdir DIR` | Scratch dir for candidate `.scad`/`.png`/`.stl` (default: a temp dir). |
| `--compare` | Print the delta versus the previous stored suite run, then exit. |
| `--no-store` | Do not append records to the metrics store. |
| `--json` | Print the full JSON report instead of the text table. |

## The pipeline (per case)

1. **Backend call** — ask the backend for a `.scad` (text mode is single-shot). Budget:
   `max_backend_calls` (0 ⇒ `budget_exhausted` before any call).
2. **Extract + safety** — pull the SCAD from a ```scad fence (or verbatim if it looks like
   SCAD). A prose refusal ⇒ `no_scad` failure. An out-of-sandbox `include`/`import` or a
   shell-injection construct ⇒ `unsafe_candidate` failure.
3. **Gate 0 — build-success** — `openscad --render` the candidate. No renderer installed ⇒
   `diagnostic` (`renderer_unavailable`; an environment gap, **not** a model failure). A
   render error ⇒ `failure` (`build_failed`).
4. **Metric vector** (only on a built model; each column degrades to `available: false` when
   its input/tool is absent — never a fake number):
   - **silhouette** IoU via `3d score` (needs a `reference_image` + ImageMagick);
   - **geometry** battery via `3d metrics geometry` (needs a `target_mesh` + trimesh/scipy);
   - **perceptual** battery via `3d metrics perceptual` (PSNR always; LPIPS/CLIP optional);
   - **judge** — the advisory VLM rubric (`lib/ai/judge.py`); a blind/mock/malformed judge is
     caught and reported unavailable, never crashes the run.

## Report columns

`build-success-rate` (Text2CAD invalidity ratio: renders that succeeded / renders attempted),
`ok-rate`, `diagnostic-rate`, `failure-rate`, `expectation-match`, `seconds/ok`, `calls/ok`,
`$/ok` (only when the backend returns a token cost — `n/a` otherwise). Each case row shows its
`ProofStatus`, `stop_reason`, `build`, and the available metric columns.

## Case format

Each case is one `*.json` file in the suite dir:

```json
{
  "id": "washer",
  "description": "A flat washer: 20 mm OD, 8 mm bore, 3 mm thick.",
  "prompt": "Model a flat washer, 20 mm OD, 8 mm bore, 3 mm thick, in OpenSCAD.",
  "expected_status": "ok",
  "budget": {"max_rounds": 1, "max_renders": 2, "max_backend_calls": 1},
  "reference_image": "refs/washer.png",
  "target_mesh": "golden/washer.stl",
  "mock_response": "```scad\ndifference(){cylinder(h=3,d=20);translate([0,0,-1])cylinder(h=5,d=8);}\n```"
}
```

`reference_image` / `target_mesh` / `golden_scad` are optional (relative paths resolve against
the suite dir). `mock_response` is the canned reply used under the mock backend — ignored for
real backends. `expected_status` is the ideal outcome (`ok` / `diagnostic` / `failure`) used
for the expectation-match column.

## Persistence

Every case (plus one suite-aggregate row) is appended to
`~/.local/share/3d-cli/metrics/bench.jsonl` (or `$XDG_DATA_HOME/3d-cli/metrics/`) **with its
convention fields**, so `--compare` shows real deltas rather than silently-drifting numbers.
Inspect the store with `3d metrics show --command bench`.

## Examples

```bash
3d bench                                   # run the shipped suite (auto/mock backend)
3d bench --backend mock                    # deterministic offline run (canned per-case replies)
3d bench --suite tests/fixtures/bench --json   # custom case dir, full JSON report
3d bench --compare                         # delta vs the previous stored run
3d ai bench --backend claude               # same runner under the `3d ai` umbrella
```

## Exit code

A benchmark **run** exits `0` — the numbers *are* the result; a per-case failure is a data
point, not a process error. Environment errors (no backend available at all, a missing suite
dir) raise a structured error and exit nonzero before the run starts.
