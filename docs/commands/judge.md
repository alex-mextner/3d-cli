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
  verdict is `LABEL=blind` — **never** reported as a real visual score. When there is **no
  selection at all** (no `--backend` *and* no `backend` set in `ai.json`) the auto-pick now
  **prefers a sighted backend** over a text-only one (it only lands blind if none is
  installed, and says so on stderr). An explicit `--backend` **or** a configured `ai.json`
  `backend` is honored verbatim — a blind choice is still surfaced by the `blind` label.

> **Temperature (now threaded to the transport):** `ai.backends.Backend.complete()` takes a
> `temperature`, and the harness sends the canonical read at **temp 0** and the N stability
> samples at **temp 0.1**. This is honored by temperature-capable backends (**Ollama**, via
> the HTTP `options` block); the **CLI backends** (`claude`, `codex`, `opencode`) expose no
> temperature flag, so they accept-but-ignore it — the value is never faked. On those
> backends the stability signal still reflects only whatever nondeterminism they show across
> N calls. A judge that "runs" on a mock is not evidence it judges *real* renders well —
> that needs a human-labelled photo set.

## Options

| Option | Meaning |
|---|---|
| `--backend NAME` | Judge backend; **repeatable** for ≥2 distinct judges. Accepted: `claude`, `codex`, `opencode`, `ollama`, `mock`. Default: auto-pick. |
| `--stability-n N` | Stability samples per judge (default: `5`; `0` disables the flag). |
| `--feature-context T` | Caller-supplied salient-feature taxonomy (kept out of core). |
| `--config PATH` | Backend JSON config (default: `~/.config/3d-cli/ai.json`). |
| `--json` | Emit the full `VisualScore` as JSON (per-dim, per-judge stability, notes). |

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
