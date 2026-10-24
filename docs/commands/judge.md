# `3d judge` — VLM-as-a-judge (render vs reference)

Score a rendered model against a reference image with a multimodal-LLM judge on a fixed,
anchored rubric, with reproducibility guards. This is the "does it LOOK like the subject"
metric that pixel scores (IoU, LPIPS) miss — the no-ground-truth-mesh regime.

Backed by `lib/ai/judge.py`. Applies the VLM-judge methodology from
[`docs/APPLY-RESEARCH.md`](../APPLY-RESEARCH.md) and
[`docs/research/benchmarks-and-metrics.md`](../research/benchmarks-and-metrics.md) §2.5
(CADBench κ=0.791, FlipFlop arXiv:2311.08596, position-bias arXiv:2406.07791).

## Usage

```
3d judge <render.png|jpg> <reference.png|jpg> [options]
```

## The rubric (each dimension 0–4, plus mean)

| Dimension (`key`) | 0 | 2 | 4 |
|---|---|---|---|
| `silhouette_proportion` | wrong overall shape | right family, proportions off | silhouette + key proportions match |
| `feature_completeness` | major salient features missing | some present, some missing | all salient features present |
| `structural_correctness` | parts misplaced / wrong count | mostly right, minor errors | correct arrangement, counts, relations |
| `detail_fidelity` | flat / blocky vs reference | coarse detail | fine detail present |

The anchors are **domain-neutral**: pass a feature taxonomy with `--feature-context`
(no hardcoded "portico/dome" in core — the ROADMAP §15 leakage rule). The **mean is
always recomputed** from the per-dimension scores; a model's own "mean" field is ignored.

## Reproducibility guards

- **Temp-0 canonical + N stability samples.** One canonical score is logged; `--stability-n`
  extra samples measure agreement. If the samples disagree by more than 1 point on the mean
  **or any single dimension**, the instance is flagged `STABILITY_UNSTABLE=true` — this is
  how the FlipFlop effect is *detected*, not assumed absent.
- **≥2 distinct judges.** Pass `--backend` twice (different backend/model) for cross-judge
  agreement; a large `CROSS_JUDGE_SPREAD` marks the verdict low-confidence. A single judge
  is always `SINGLE_JUDGE=true` → `low-confidence`.
- **Blind (text-only) surfacing.** A backend whose `supports_images` is `False` cannot see
  the images. It is excluded from the visual aggregate and, if no sighted judge remains, the
  verdict is `LABEL=blind` — **never** reported as a real visual score.

> **Limitation (honest):** `ai.backends.Backend.complete()` does not yet expose a
> temperature knob, so the temp-0 / temp-0.1 split is recorded as *intended* metadata, not
> enforced at the transport. The N-sample stability machinery is wired and correct the
> moment a temperature-capable backend lands; today the stability signal reflects whatever
> nondeterminism the chosen backend shows across N calls. A judge that "runs" on a mock is
> not evidence it judges *real* renders well — that needs a human-labelled photo set.

## Options

| Option | Meaning |
|---|---|
| `--backend NAME` | Judge backend; **repeatable** for ≥2 distinct judges. Accepted: `claude`, `codex`, `opencode`, `ollama`, `mock`. Default: auto-pick. |
| `--stability-n N` | Stability samples per judge (default: `5`; `0` disables the flag). |
| `--feature-context T` | Caller-supplied salient-feature taxonomy (kept out of core). |
| `--config PATH` | Backend JSON config (default: `~/.config/3d-cli/ai.json`). |
| `--json` | Emit the full `VisualScore` as JSON (per-dim, per-judge stability, notes) plus `all_runs`, `rationale` and `gate`. |
| `--rubric FILE.json` | Your own rubric instead of the four built-in dimensions: a JSON list of `{"key", "anchor_0", "anchor_2", "anchor_4", "label"?}`, e.g. one dimension per marking region. `3d judge crop_render.png crop_ref.png --rubric nostril.json` |
| `--min-dim X` | Gate: exit 1 unless **every** dimension's mean over all reads (the canonical read and every stability sample) is ≥ `X`, e.g. `3d judge crop_render.png crop_ref.png --rubric nostril.json --min-dim 3 --stability-n 2`. A blind verdict passes nothing. |
| `--max-mean X` | Gate: exit 1 unless the mean over all reads is ≤ `X` — a negative control: `3d judge front.png back.png --max-mean 2` (two different views must score low). |

## Result labels

| `LABEL` | Meaning |
|---|---|
| `ok` | ≥2 sighted judges, stable N-samples, agreeing. |
| `low-confidence` | single judge, unstable N-samples, or large cross-judge spread. |
| `blind` | no judge backend can see images (text-only) — **not** a real visual score. |

## Output (KEY=VALUE, machine-parseable)

```
LABEL=ok
MEAN=3.0
DIM.silhouette_proportion=3.0
DIM.feature_completeness=2.0
DIM.structural_correctness=4.0
DIM.detail_fidelity=3.0
BLIND=false
SINGLE_JUDGE=false
STABILITY_UNSTABLE=false
CROSS_JUDGE_SPREAD=0.0
JUDGES=2
```

When `LABEL=blind`, the numeric fields are masked (`MEAN=NA`, `DIM.*=NA`) so a consumer
keying on `MEAN` alone cannot read a text-only verdict as a real visual score. The raw
per-judge numbers remain available in `--json`, guarded by the `blind` flags.
`CROSS_JUDGE_SPREAD` is `NA` for a single judge.

## Gates and controls

`--min-dim` and `--max-mean` turn the score into a PASS/FAIL (exit `0` / `1`, `GATE=PASS|FAIL` in the
output, `gate` in `--json`). They gate on `RUNS_DIM.*` / `RUNS_MEAN`, the means over **every** read of every
sighted judge (the canonical read plus the `--stability-n` samples): `DIM.*` and `MEAN` are the canonical
reads only, and one VLM read of one dimension moves by a point between calls. `RUNS` is the number of reads.
If the reply has a `rationale` string (ask for one in `--feature-context`) it is printed as `RATIONALE=`.

A rubric is only worth gating on when it passes its own controls: the reference against itself must score 4
everywhere, and two different views must score low.

```bash
3d judge ref.png ref.png --rubric nostril.json --min-dim 4     # control: the reference against itself must score 4
3d judge front.png back.png --max-mean 2                       # control: two different views must score low
3d judge crop_render.png crop_ref.png --rubric nostril.json --min-dim 3 --stability-n 2   # a feature passes at >= 3
```

## Examples

```bash
# Single auto-picked judge (labelled single-judge / low-confidence)
3d judge render.png photo.jpg

# Two distinct judges for cross-judge agreement
3d judge render.png photo.jpg --backend claude --backend ollama

# Full stability sweep as JSON for the metrics store
3d judge render.png photo.jpg --stability-n 5 --json

# Inject a caller feature taxonomy (no domain leakage in core)
3d judge render.png photo.jpg --feature-context "columns; pediment; dome"
```

## Library API

```python
from ai.judge import judge, judge_pairwise

# Two DISTINCT backends -> a real cross-judge verdict that can reach 'ok'.
result = judge("render.png", "photo.jpg", backend=["claude", "ollama"], stability_n=5)
print(result.label, result.mean, result.per_dim)   # 'ok' 3.0 {...}
# A single auto-resolved backend is always single-judge -> 'low-confidence', never 'ok'.
solo = judge("render.png", "photo.jpg")             # backend=None -> one judge
print(solo.label)                                   # 'low-confidence'

# A/B with the position-swap guard: one comparative prompt (reference + both renders)
# run in BOTH orders. If the two orders disagree the judge just favoured the first-shown
# render — the verdict is forced to `tie` and `position_consistent` is False.
ab = judge_pairwise("render_a.png", "render_b.png", "photo.jpg")
print(ab.winner, ab.margin, ab.position_consistent)   # 'A' 3.0 True
```
