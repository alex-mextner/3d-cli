# ─────────────────────────────────────────────────────────────────────────────
# ai/judge.py — VLM-as-a-judge: score a rendered model against a reference image on
# a fixed, anchored rubric, with reproducibility guards.
#
# WHAT / WHY
#   Pixel metrics (IoU, LPIPS) miss "does it LOOK like the subject". A multimodal LLM
#   scores render-vs-reference on four anchored 0-4 dimensions. CADBench (arXiv:2412.14203,
#   κ=0.791) shows a well-anchored VLM rubric is reliable enough to ship — but ONLY with
#   guards, otherwise a judge is a mood, not a metric. The guards here (from
#   docs/APPLY-RESEARCH.md "VLM-judge methodology" + benchmarks-and-metrics §2.5):
#     - temp-0 canonical logged score for determinism;
#     - N=5 stability samples (intended temp 0.1) — flag if they disagree by >1 point
#       (this is how the FlipFlop effect, arXiv:2311.08596, is DETECTED not ignored);
#     - >=2 distinct judges (different backend/model) — large cross-judge spread = low
#       confidence;
#     - pointwise rubric scoring for the logged metric; position-swap + average for any
#       pairwise A/B (position bias, arXiv:2406.07791).
#
# HOW IT'S REACHED
#   `judge(render, reference, backend=..., judges=2, stability_n=5)` -> VisualScore.
#   Vision calls go through `ai.backends` (resolve_backend / a passed Backend). A backend
#   whose `supports_images` is False is a BLIND (text-only) judge: it is NEVER silently
#   reported as a real visual score — it is excluded from the sighted aggregate and the
#   result is labelled `blind`.
#
# INVARIANTS
#   - The mean is ALWAYS recomputed from the per-dimension scores; the model's own "mean"
#     field (if any) is ignored.
#   - Parsing, stability, and cross-judge math are PURE functions (no backend, no I/O) so
#     they are unit-testable in isolation.
#   - CAVEAT (honest): `ai.backends.Backend.complete()` does not (yet) expose a temperature
#     knob, so the temp-0 / temp-0.1 split is recorded as INTENDED metadata, not enforced
#     at the transport. The N-sample stability machinery is wired and correct the moment a
#     temperature-capable backend arrives; today the stability signal reflects whatever
#     nondeterminism the chosen backend exhibits across N identical calls. See
#     docs/commands/judge.md "Limitations".
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import json
import pathlib
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from ai.backends import Backend, resolve_backend
from errors import ThreeDError

# ── Rubric ───────────────────────────────────────────────────────────────────
# Each dimension is scored 0-4 against explicit anchors so the score is reproducible,
# not vibes (benchmarks-and-metrics §2.5). The anchors are DOMAIN-NEUTRAL: the caller
# supplies any feature taxonomy via `feature_context` — no hardcoded "portico/dome" in
# core (the ROADMAP §15 leakage rule).
SCORE_MIN = 0
SCORE_MAX = 4

CANONICAL_TEMP = 0.0
STABILITY_TEMP = 0.1
DEFAULT_STABILITY_N = 5
DEFAULT_JUDGES = 2
# Two samples that differ by more than this (per dimension) mark the instance unstable.
STABILITY_THRESHOLD = 1.0
# Judge means further apart than this mark low cross-judge agreement.
CROSS_JUDGE_THRESHOLD = 1.0


@dataclass(frozen=True)
class RubricDimension:
    key: str
    label: str
    anchor_0: str
    anchor_2: str
    anchor_4: str


DEFAULT_RUBRIC: tuple[RubricDimension, ...] = (
    RubricDimension(
        key="silhouette_proportion",
        label="Silhouette / proportion fidelity",
        anchor_0="wrong overall shape",
        anchor_2="right family, proportions off",
        anchor_4="silhouette + key proportions match",
    ),
    RubricDimension(
        key="feature_completeness",
        label="Feature completeness",
        anchor_0="major salient features missing",
        anchor_2="some present, some missing",
        anchor_4="all salient features present",
    ),
    RubricDimension(
        key="structural_correctness",
        label="Structural / spatial correctness",
        anchor_0="parts misplaced / wrong count",
        anchor_2="mostly right, minor errors",
        anchor_4="correct arrangement, counts, relations",
    ),
    RubricDimension(
        key="detail_fidelity",
        label="Detail fidelity",
        anchor_0="flat / blocky vs reference",
        anchor_2="coarse detail",
        anchor_4="fine detail present",
    ),
)


class JudgeError(ThreeDError):
    """A VLM judge could not produce a usable rubric score (unparseable response,
    empty judge set). Exit 1 — distinct from MissingDependency (no backend at all)."""

    exit_code = 1


# ── Result model ─────────────────────────────────────────────────────────────
@dataclass
class RubricScore:
    """One parsed rubric read: per-dimension 0-4 scores + the recomputed mean + the raw
    backend text it was parsed from (audit trail)."""

    dims: dict[str, float]
    mean: float
    raw: str = ""

    def to_jsonable(self) -> dict[str, Any]:
        return {"dims": dict(self.dims), "mean": self.mean, "raw": self.raw}


@dataclass
class JudgeResult:
    """One judge (one backend/model): its canonical temp-0 score, the N stability
    samples, the stability verdict, and whether it could actually SEE the images."""

    backend_name: str
    model: str | None
    canonical: RubricScore
    stability_samples: list[RubricScore]
    stability_unstable: bool
    stability_mean_range: float
    per_dim_range: dict[str, float]
    supports_images: bool
    blind: bool

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "backend": self.backend_name,
            "model": self.model,
            "canonical": self.canonical.to_jsonable(),
            "stability_samples": [s.to_jsonable() for s in self.stability_samples],
            "stability_unstable": self.stability_unstable,
            "stability_mean_range": self.stability_mean_range,
            "per_dim_range": dict(self.per_dim_range),
            "supports_images": self.supports_images,
            "blind": self.blind,
            # Intended temperatures for the canonical vs stability runs. Recorded for audit;
            # NOT yet enforced at the transport (Backend.complete has no temperature knob).
            "intended_canonical_temp": CANONICAL_TEMP,
            "intended_stability_temp": STABILITY_TEMP,
        }


@dataclass
class VisualScore:
    """Top-level judge verdict: per-dimension aggregate + mean over the SIGHTED judges,
    stability + cross-judge diagnostics, a plain result label, and the full per-judge
    audit trail. `blind` is True when NO judge could see the images (text-only)."""

    per_dim: dict[str, float]
    mean: float
    label: str
    stability_unstable: bool
    cross_judge_spread: float | None
    cross_judge_low_agreement: bool
    blind: bool
    single_judge: bool
    judges: list[JudgeResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "per_dim": dict(self.per_dim),
            "mean": self.mean,
            "label": self.label,
            "stability_unstable": self.stability_unstable,
            "cross_judge_spread": self.cross_judge_spread,
            "cross_judge_low_agreement": self.cross_judge_low_agreement,
            "blind": self.blind,
            "single_judge": self.single_judge,
            "notes": list(self.notes),
            "judges": [j.to_jsonable() for j in self.judges],
        }


@dataclass
class PairwiseResult:
    """A/B pairwise verdict with the position-bias guard applied. Unlike pointwise scoring,
    this is a genuine COMPARATIVE judgement — both renders + the reference in one prompt —
    run in BOTH presentation orders. If the two orders pick DIFFERENT actual renders the
    verdict is position-biased (the judge just favoured whatever was shown first, the
    documented failure mode arXiv:2406.07791): `position_consistent` is False and the
    winner is forced to `tie` (low confidence). `margin` (0-4) is the averaged confidence
    gap only when the two orders AGREE."""

    winner: str  # "A" | "B" | "tie"
    margin: float
    position_consistent: bool
    order_verdicts: dict[str, dict[str, Any]]
    blind: bool = False
    notes: list[str] = field(default_factory=list)

    def to_jsonable(self) -> dict[str, Any]:
        return asdict(self)


# ── Prompt construction ──────────────────────────────────────────────────────
def build_system_prompt(rubric: Sequence[RubricDimension]) -> str:
    """The fixed, anchored rubric instruction. Same text every run so the score is a
    metric, not a mood. Demands a strict JSON object keyed by the dimension keys."""
    lines = [
        "You are a meticulous visual judge for 3D-model reconstruction. You are shown a",
        "REFERENCE image and a RENDER of a candidate 3D model. Score how well the RENDER",
        f"reproduces the REFERENCE on each dimension below, each an INTEGER {SCORE_MIN}-{SCORE_MAX}",
        "using the anchors. Judge only what is visible; do not reward or penalise style,",
        "colour, or lighting that the reference does not constrain.",
        "",
        "Dimensions (key: 0 anchor / 2 anchor / 4 anchor):",
    ]
    for d in rubric:
        lines.append(f"  - {d.key} ({d.label}): 0={d.anchor_0}; 2={d.anchor_2}; 4={d.anchor_4}")
    keys = ", ".join(f'"{d.key}"' for d in rubric)
    lines += [
        "",
        "Respond with ONLY a single JSON object, no prose, no markdown fences, with these",
        f"integer keys: {keys}. You MAY add a \"rationale\" string. Do NOT include a mean;",
        "the harness computes it.",
    ]
    return "\n".join(lines)


def build_user_prompt(feature_context: str | None) -> str:
    """The per-instance instruction. Any feature taxonomy is injected by the caller;
    core stays domain-neutral (§15 leakage rule)."""
    parts = [
        "Score the RENDER against the REFERENCE on the rubric. The first image is the",
        "REFERENCE; the second is the RENDER. Return only the JSON object.",
    ]
    if feature_context and feature_context.strip():
        parts.append("")
        parts.append("Salient features to weigh (caller-supplied taxonomy):")
        parts.append(feature_context.strip())
    return "\n".join(parts)


def build_pairwise_prompt(
    rubric: Sequence[RubricDimension], feature_context: str | None
) -> tuple[str, str]:
    """System+user for a COMPARATIVE A/B judgement: reference + two candidate renders in
    one prompt, pick which render better reproduces the reference across the same rubric
    dimensions. Comparative (not two independent pointwise scores) so the position-swap
    guard actually has a first-slot preference to cancel."""
    dims = "; ".join(d.label for d in rubric)
    margin_hint = f"an integer {SCORE_MIN}-{SCORE_MAX}"
    system = "\n".join(
        [
            "You are a meticulous visual judge for 3D-model reconstruction. You are shown a",
            "REFERENCE image and TWO candidate renders, FIRST and SECOND. Decide which render",
            f"better reproduces the REFERENCE across: {dims}.",
            "",
            'Respond with ONLY a single JSON object with keys "winner" (one of "first",',
            '"second", "tie") and "margin" (' + margin_hint + " — how decisively the winner",
            "is better: 0 = essentially equal, 4 = decisively better). You MAY add a",
            '"rationale" string. No prose, no markdown fences.',
        ]
    )
    parts = [
        "The first image is the REFERENCE; the second is candidate FIRST; the third is",
        "candidate SECOND. Return only the JSON object.",
    ]
    if feature_context and feature_context.strip():
        parts += ["", "Salient features to weigh (caller-supplied taxonomy):", feature_context.strip()]
    return system, "\n".join(parts)


# ── Pure parsing / math (no backend, no I/O — unit-testable in isolation) ─────
def _extract_json_object(text: str) -> dict[str, Any]:
    """Pull the first balanced top-level JSON object out of a possibly-noisy backend
    reply (markdown fences, leading prose, trailing tokens). Braces INSIDE string
    literals (a rationale like "marked with { here") do not count, and backslash-escapes
    are respected, so a valid object with a brace in a string still parses. Raises
    JudgeError if none parses."""
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    chunk = text[start : i + 1]
                    try:
                        obj = json.loads(chunk)
                    except json.JSONDecodeError:
                        start = -1
                        continue
                    if isinstance(obj, dict):
                        return obj
    raise JudgeError(
        "judge response contained no parseable JSON object",
        command="judge",
        remediation=["The backend did not return the required rubric JSON; retry or pick another backend."],
    )


def _coerce_score(value: Any, key: str) -> float:
    # A bool is almost always a malformed response, not a deliberate 0/1 — float(True)==1.0
    # would silently accept it, so reject it like any other non-number.
    if isinstance(value, bool):
        raise JudgeError(f"rubric dimension {key!r} was a boolean, not a number: {value!r}", command="judge")
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise JudgeError(
            f"rubric dimension {key!r} was not a number: {value!r}",
            command="judge",
        ) from exc
    # Clamp into the anchored range — a judge that says 5 or -1 is out of rubric.
    return float(max(SCORE_MIN, min(SCORE_MAX, num)))


def parse_rubric_response(text: str, rubric: Sequence[RubricDimension]) -> RubricScore:
    """Parse one backend reply into a RubricScore. The mean is ALWAYS recomputed from the
    per-dimension scores (the model's own mean is ignored). Every rubric key must be
    present, else JudgeError."""
    obj = _extract_json_object(text)
    dims: dict[str, float] = {}
    for d in rubric:
        if d.key not in obj:
            raise JudgeError(
                f"judge response missing rubric dimension {d.key!r}",
                command="judge",
                remediation=[f"The backend must score every dimension: {[x.key for x in rubric]}."],
            )
        dims[d.key] = _coerce_score(obj[d.key], d.key)
    mean = sum(dims.values()) / len(dims) if dims else 0.0
    return RubricScore(dims=dims, mean=round(mean, 4), raw=text)


def stability_report(
    samples: Sequence[RubricScore],
    rubric: Sequence[RubricDimension],
    *,
    threshold: float = STABILITY_THRESHOLD,
) -> tuple[bool, float, dict[str, float]]:
    """Given N stability samples, return (unstable, mean_range, per_dim_range).

    `unstable` is True if the mean disagrees by more than `threshold` across samples OR
    any single dimension does — this is the FlipFlop detector: a judge that flips a
    dimension between calls is low-confidence even if the mean looks steady."""
    if not samples:
        return False, 0.0, {d.key: 0.0 for d in rubric}
    means = [s.mean for s in samples]
    mean_range = max(means) - min(means)
    per_dim: dict[str, float] = {}
    for d in rubric:
        vals = [s.dims[d.key] for s in samples if d.key in s.dims]
        per_dim[d.key] = (max(vals) - min(vals)) if vals else 0.0
    unstable = mean_range > threshold or any(r > threshold for r in per_dim.values())
    return unstable, round(mean_range, 4), {k: round(v, 4) for k, v in per_dim.items()}


def aggregate_dims(
    scores: Sequence[RubricScore], rubric: Sequence[RubricDimension]
) -> dict[str, float]:
    """Mean per dimension across a set of canonical scores (one per sighted judge)."""
    out: dict[str, float] = {}
    for d in rubric:
        vals = [s.dims[d.key] for s in scores if d.key in s.dims]
        out[d.key] = round(sum(vals) / len(vals), 4) if vals else 0.0
    return out


def cross_judge_spread(scores: Sequence[RubricScore]) -> float | None:
    """Range (max-min) of the per-judge canonical means. None for <2 judges (no spread
    is meaningful with a single judge)."""
    if len(scores) < 2:
        return None
    means = [s.mean for s in scores]
    return round(max(means) - min(means), 4)


def parse_pairwise_response(text: str) -> tuple[str, float]:
    """Parse a comparative A/B reply into (slot, margin). `slot` is one of
    'first'/'second'/'tie'; `margin` is clamped to the score range. Raises JudgeError on a
    missing/unknown winner."""
    obj = _extract_json_object(text)
    slot = str(obj.get("winner", "")).strip().lower()
    if slot not in ("first", "second", "tie"):
        raise JudgeError(
            f"pairwise response 'winner' must be first/second/tie, got {slot!r}",
            command="judge",
        )
    margin = _coerce_score(obj.get("margin", 0), "margin") if slot != "tie" else 0.0
    return slot, margin


# ── Backend plumbing ─────────────────────────────────────────────────────────
def _score_once(
    backend: Backend,
    system: str,
    user: str,
    images: list[pathlib.Path],
    rubric: Sequence[RubricDimension],
    timeout: float,
) -> RubricScore:
    text = backend.complete(system, user, images=images, timeout=timeout)
    return parse_rubric_response(text, rubric)


def _run_judge(
    backend: Backend,
    render_img: pathlib.Path,
    reference_img: pathlib.Path,
    rubric: Sequence[RubricDimension],
    *,
    feature_context: str | None,
    stability_n: int,
    timeout: float,
) -> JudgeResult:
    """Run one judge end-to-end: canonical score + N stability samples + verdict.

    A blind (supports_images=False) backend is still scored so its answer is on record,
    but it is flagged `blind` and the caller excludes it from the sighted aggregate."""
    system = build_system_prompt(rubric)
    user = build_user_prompt(feature_context)
    # REFERENCE first, RENDER second — matches the prompt's stated ordering.
    images = [reference_img, render_img]
    blind = not backend.supports_images

    canonical = _score_once(backend, system, user, images, rubric, timeout)
    samples: list[RubricScore] = []
    for _ in range(max(0, stability_n)):
        samples.append(_score_once(backend, system, user, images, rubric, timeout))
    # The canonical (logged) score is part of the stability set: a canonical that
    # diverges from the samples IS instability, and this also stops stability_n=1 from
    # trivially reporting "stable" off a single sample compared to nothing.
    unstable, mean_range, per_dim_range = stability_report([canonical, *samples], rubric)
    model = getattr(backend, "model", None)
    return JudgeResult(
        backend_name=backend.name,
        model=model if isinstance(model, str) else None,
        canonical=canonical,
        stability_samples=samples,
        stability_unstable=unstable,
        stability_mean_range=mean_range,
        per_dim_range=per_dim_range,
        supports_images=backend.supports_images,
        blind=blind,
    )


def _resolve_judges(
    backend: Backend | str | Sequence[Backend | str] | None,
) -> list[Backend]:
    """Normalise the `backend` argument into a concrete judge list. A single/None backend
    yields one judge; a sequence is truncated/kept as the distinct judge panel. A `str`
    (or a sequence of str) is a backend NAME resolved via `resolve_backend` — without this
    guard a bare `"claude"` would be iterated character by character (str is a Sequence)."""
    if backend is None:
        return [resolve_backend()]
    if isinstance(backend, str):
        return [resolve_backend(backend)]
    if isinstance(backend, Backend):
        return [backend]
    resolved = [resolve_backend(b) if isinstance(b, str) else b for b in backend]
    if not resolved:
        raise JudgeError(
            "no judge backends supplied",
            command="judge",
            remediation=["Pass at least one Backend, or None to auto-resolve."],
        )
    # Dedupe by (name, model): two copies of one model trivially "agree", which would fake
    # a high cross-judge confidence — the whole point is DISTINCT judges. An explicitly
    # supplied panel is otherwise honoured in full (never silently truncated to `judges`,
    # which is only the target count used for labelling).
    seen: set[tuple[str, str | None]] = set()
    panel: list[Backend] = []
    for b in resolved:
        model = getattr(b, "model", None)
        key = (b.name, model if isinstance(model, str) else None)
        if key in seen:
            continue
        seen.add(key)
        panel.append(b)
    return panel


def _label_and_notes(
    sighted: list[JudgeResult],
    blind_judges: list[JudgeResult],
    stability_unstable: bool,
    spread: float | None,
    low_agreement: bool,
    requested_judges: int,
) -> tuple[str, bool, list[str]]:
    """Derive the plain result label + single-judge flag + human notes."""
    notes: list[str] = []
    single_judge = len(sighted) < 2
    if not sighted:
        notes.append(
            "BLIND: no judge backend can see images (supports_images=False); "
            "this is NOT a real visual score."
        )
        return "blind", single_judge, notes
    if blind_judges:
        names = ", ".join(sorted({j.backend_name for j in blind_judges}))
        notes.append(f"excluded blind (text-only) judge(s) from the visual aggregate: {names}")
    if single_judge:
        notes.append(
            f"single sighted judge (requested {requested_judges}, >=2 distinct recommended): "
            "cross-judge agreement cannot be measured — treat as lower confidence."
        )
    if stability_unstable:
        notes.append("stability: N-sample scores disagree by more than the threshold (low confidence).")
    if low_agreement:
        notes.append(f"cross-judge spread {spread} exceeds threshold (low agreement).")
    if stability_unstable or single_judge or low_agreement:
        return "low-confidence", single_judge, notes
    return "ok", single_judge, notes


def judge(
    render_img: str | pathlib.Path,
    reference_img: str | pathlib.Path,
    *,
    backend: Backend | str | Sequence[Backend | str] | None = None,
    rubric: Sequence[RubricDimension] | None = None,
    judges: int = DEFAULT_JUDGES,
    stability_n: int = DEFAULT_STABILITY_N,
    feature_context: str | None = None,
    timeout: float = 1200.0,
) -> VisualScore:
    """Score a render against a reference on the anchored rubric, with reproducibility
    guards (temp-0 canonical + N stability samples + >=2 distinct judges).

    `backend` is a single Backend/name, a sequence of judge backends/names, or None
    (auto-resolve ONE — which is single-judge, hence at best `low-confidence`, never `ok`).
    An explicit panel is deduped by (name, model) so two copies of one model cannot fake
    cross-judge agreement. `judges` is only the target distinct-judge count used for
    labelling. Returns a VisualScore whose `.blind` / `.label` make a blind or
    low-confidence verdict impossible to mistake for a clean visual score."""
    render_path = pathlib.Path(render_img).expanduser()
    reference_path = pathlib.Path(reference_img).expanduser()
    rub = tuple(rubric) if rubric else DEFAULT_RUBRIC

    panel = _resolve_judges(backend)
    results = [
        _run_judge(
            b, render_path, reference_path, rub,
            feature_context=feature_context, stability_n=stability_n, timeout=timeout,
        )
        for b in panel
    ]

    sighted = [r for r in results if not r.blind]
    blind_judges = [r for r in results if r.blind]
    scoring = sighted if sighted else blind_judges  # fall back so a mean still exists
    canon = [r.canonical for r in scoring]

    per_dim = aggregate_dims(canon, rub)
    mean = round(sum(per_dim.values()) / len(per_dim), 4) if per_dim else 0.0
    spread = cross_judge_spread(canon)
    low_agreement = spread is not None and spread > CROSS_JUDGE_THRESHOLD
    stability_unstable = any(r.stability_unstable for r in scoring)

    label, single_judge, notes = _label_and_notes(
        sighted, blind_judges, stability_unstable, spread, low_agreement, judges
    )
    return VisualScore(
        per_dim=per_dim,
        mean=mean,
        label=label,
        stability_unstable=stability_unstable,
        cross_judge_spread=spread,
        cross_judge_low_agreement=low_agreement,
        blind=not sighted,
        single_judge=single_judge,
        judges=results,
        notes=notes,
    )


def _pairwise_pref(
    backend: Backend,
    system: str,
    user: str,
    reference_img: pathlib.Path,
    first: pathlib.Path,
    second: pathlib.Path,
    first_label: str,
    second_label: str,
    timeout: float,
) -> tuple[str, float, str]:
    """One comparative call. Returns (actual_winner_label, margin, raw_slot) where the raw
    slot ('first'/'second'/'tie') is mapped back to the caller's A/B label so the two
    presentation orders can be compared on the SAME axis."""
    text = backend.complete(system, user, images=[reference_img, first, second], timeout=timeout)
    slot, margin = parse_pairwise_response(text)
    actual = {"first": first_label, "second": second_label, "tie": "tie"}[slot]
    return actual, margin, slot


def judge_pairwise(
    render_a: str | pathlib.Path,
    render_b: str | pathlib.Path,
    reference_img: str | pathlib.Path,
    *,
    backend: Backend | str | None = None,
    rubric: Sequence[RubricDimension] | None = None,
    feature_context: str | None = None,
    timeout: float = 1200.0,
) -> PairwiseResult:
    """A/B: which render better reproduces the reference, with the position-swap guard. A
    single COMPARATIVE prompt (reference + both renders) is run in BOTH presentation orders
    (A,B) and (B,A). If the two orders name DIFFERENT actual renders the judge merely
    favoured the first-shown one (position bias, arXiv:2406.07791): the verdict is forced to
    `tie` and `position_consistent` is False. `margin` (0-4) is the averaged confidence gap
    only when the orders agree."""
    b = resolve_backend(backend) if isinstance(backend, str) else (backend or resolve_backend())
    rub = tuple(rubric) if rubric else DEFAULT_RUBRIC
    ref = pathlib.Path(reference_img).expanduser()
    a_path = pathlib.Path(render_a).expanduser()
    b_path = pathlib.Path(render_b).expanduser()
    system, user = build_pairwise_prompt(rub, feature_context)

    pref1, m1, slot1 = _pairwise_pref(b, system, user, ref, a_path, b_path, "A", "B", timeout)
    pref2, m2, slot2 = _pairwise_pref(b, system, user, ref, b_path, a_path, "B", "A", timeout)

    notes: list[str] = []
    order_verdicts = {
        "order_ab": {"first": "A", "second": "B", "slot": slot1, "winner": pref1, "margin": m1},
        "order_ba": {"first": "B", "second": "A", "slot": slot2, "winner": pref2, "margin": m2},
    }
    if pref1 == pref2 and pref1 in ("A", "B"):
        winner, margin, consistent = pref1, round((m1 + m2) / 2, 4), True
    elif pref1 == "tie" and pref2 == "tie":
        winner, margin, consistent = "tie", 0.0, True
    elif {pref1, pref2} == {"A", "B"}:
        winner, margin, consistent = "tie", 0.0, False
        notes.append(
            f"position bias: order A,B chose {pref1} but order B,A chose {pref2} — the judge "
            "favoured the first-shown render; verdict forced to tie (low confidence)."
        )
    else:
        # One order named a winner, the other said tie: order-dependent disagreement, not a
        # clean A<->B flip. Still low-confidence, but don't mislabel it as position bias.
        winner, margin, consistent = "tie", 0.0, False
        notes.append(
            f"order-dependent disagreement: order A,B chose {pref1}, order B,A chose {pref2} "
            "— inconsistent across presentation order; verdict forced to tie (low confidence)."
        )
    if not b.supports_images:
        # A text-only backend NEVER yields a real visual winner (the module invariant),
        # even if both orders happen to "agree" — force tie and surface it.
        winner, margin, consistent = "tie", 0.0, False
        notes.append("BLIND backend (supports_images=False): pairwise verdict is NOT a real visual comparison.")
    return PairwiseResult(
        winner=winner,
        margin=margin,
        position_consistent=consistent,
        order_verdicts=order_verdicts,
        blind=not b.supports_images,
        notes=notes,
    )
