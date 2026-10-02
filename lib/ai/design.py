# ─────────────────────────────────────────────────────────────────────────────
# ai/design.py — the `3d generate` pipeline: text + dims -> VERIFIED parametric .scad.
#
# WHAT / WHY
#   `3d generate "a hollow box" --dim width=20 ...` turns a natural-language
#   description plus explicit named dimensions into a parametric OpenSCAD file that
#   has actually been put through the gates. This is CADSmith-style
#   generate -> validate -> render -> check -> fix: the backend writes a .scad, the
#   deterministic `3d` gates judge it, and on a gate failure the exact error text is
#   fed back into the next prompt round (bounded, monotonic — the best rendering
#   candidate is kept). The output carries an EXPLICIT proof label (ok / diagnostic /
#   failure), never a bare "it ran".
#
# HOW IT'S REACHED
#   `lib/commands/generate.py` (the thin registry command) parses argv into a
#   `GenerateRequest` and calls `generate(request)`. The heavy verification is done by
#   shelling out to `bin/3d validate|render|check` — the SAME gates a human runs — so
#   there is one source of truth for "does this pass".
#
# INVARIANTS
#   - STDLIB-ONLY at import time (plus `ai.backends`, itself stdlib-only). Importing
#     this module never drags in numpy/trimesh (the offline-help guarantee).
#   - The dims-present check is REAL: it scans the emitted .scad for top-level
#     `name = ...` assignments. A model that ignores a requested dimension is reported,
#     it is not silently patched to fake a pass.
#   - When OpenSCAD is absent the .scad is still written and the status degrades to
#     `diagnostic` (verification skipped) — the pipeline never claims a render it could
#     not perform, and never hard-fails purely because a tool is missing.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any

import ai_tools as ai_core
from ai import load_backend_config
from ai.backends import resolve_backend
from cli.env import find_openscad, repo_root
from errors import InvalidArgument, UsageError

# Reasonable per-subprocess ceiling for a gate invocation (a CGAL render can be slow).
_GATE_TIMEOUT = 600.0
# A valid OpenSCAD identifier for a top-level constant (no leading `$` special vars).
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Fenced code block the model may wrap its answer in (```scad / ```openscad / ```).
_FENCE_RE = re.compile(r"```(?:scad|openscad)?[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)

# Status ranking so the retry loop can keep the BEST candidate monotonically.
_STATUS_RANK = {"failure": 0, "diagnostic": 1, "ok": 2}

# Default visual-review pass bar on the judge's 0-4 anchored rubric mean (public: the
# generate command reads it to fill an unset --visual-threshold). A candidate
# whose render scores below this against the reference is NOT allowed to reach `ok` (its
# geometry may be valid while it looks nothing like the reference). Opt-in only.
DEFAULT_VISUAL_THRESHOLD = 3.0

_STYLE_SYSTEM = """
You are generating ONE self-contained OpenSCAD (.scad) file from a description.
HARD RULES:
- Begin the file with the exact named constant declarations you are given, verbatim,
  one per line as `name = value;`. Every requested dimension MUST appear as a
  top-level constant and MUST drive the geometry — no magic numbers duplicating a
  named dimension.
- Build parametrically in an attachment-graph style: position each feature relative to
  the named constants (translate/rotate anchored to parent faces/edges expressed from
  the dimensions), not with hardcoded offsets that would break if a dimension changes.
- Keep it printable: closed/manifold solids, sane wall thicknesses.
- Output ONLY the OpenSCAD source. No prose, no explanation, no markdown fences.
""".strip()


@dataclass(frozen=True, slots=True)
class GenerateRequest:
    """A single `3d generate` request, fully resolved from argv/spec."""

    description: str
    dims: dict[str, str]  # ordered name -> raw OpenSCAD value token (number or expr)
    out_path: str
    rounds: int = 3
    backend: str | None = None
    model: str | None = None
    config_path: str | None = None
    # Opt-in visual review (default OFF — the non-visual path is byte-identical to before).
    # When `visual_review` is True the candidate render is scored against `reference` by the
    # VLM judge; a score below `visual_threshold` blocks `ok` and its critique is fed back.
    reference: str | None = None
    visual_review: bool = False
    visual_threshold: float = DEFAULT_VISUAL_THRESHOLD


@dataclass(slots=True)
class GateResult:
    """One gate's verdict for the summary/JSON."""

    name: str
    status: str  # pass | fail | warn | skip
    detail: str = ""

    def to_jsonable(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass(slots=True)
class GenerateResult:
    """The pipeline outcome — the thing the command prints and JSON-dumps.

    `rounds` is the TOTAL number of rounds attempted; `winning_round` is the specific
    round whose .scad is the one reported/on-disk (they differ when an earlier round was
    the best candidate and later rounds failed to beat it). `scad_path` is always the
    written file — the pipeline always leaves the best candidate on disk.
    """

    status: str  # ok | diagnostic | failure
    rounds: int
    winning_round: int
    scad_path: str
    gate_results: list[GateResult]
    requested_dims: dict[str, str]
    dims_present_in_scad: dict[str, bool]
    backend: str = ""
    notes: list[str] = field(default_factory=list)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "rounds": self.rounds,
            "winning_round": self.winning_round,
            "scad_path": self.scad_path,
            "backend": self.backend,
            "requested_dims": dict(self.requested_dims),
            "dims_present_in_scad": dict(self.dims_present_in_scad),
            "gate_results": [g.to_jsonable() for g in self.gate_results],
            "notes": list(self.notes),
        }


# ── dims ─────────────────────────────────────────────────────────────────────
def parse_dim_flag(raw: str) -> tuple[str, str]:
    """Parse a single `--dim name=value` token into (name, value).

    `value` is kept as the raw OpenSCAD token (a number or a small expression) so the
    caller can inject it verbatim; only the NAME is validated as an identifier.
    """
    if "=" not in raw:
        raise UsageError(
            f"--dim expects name=value, got {raw!r}",
            command="generate",
            remediation=["Example: --dim width=20 --dim wall=2.4"],
        )
    name, value = raw.split("=", 1)
    name, value = name.strip(), value.strip()
    if not _IDENT_RE.match(name):
        raise InvalidArgument(
            "--dim name", name, ["a valid OpenSCAD identifier, e.g. width, wall_t"],
            command="generate",
        )
    if not value:
        raise UsageError(f"--dim {name} has an empty value", command="generate")
    return name, value


def render_constants_block(dims: dict[str, str]) -> str:
    """Render the required dims as an OpenSCAD top-of-file constant block."""
    lines = ["// Required parametric dimensions (declare these verbatim, then use them)."]
    lines += [f"{name} = {value};" for name, value in dims.items()]
    return "\n".join(lines)


def _strip_comments(scad: str) -> str:
    """Remove `/* ... */` block comments and `// ...` line comments so a commented-out
    assignment cannot masquerade as a real one."""
    no_block = re.sub(r"/\*.*?\*/", "", scad, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", no_block)


def dims_present_in_scad(scad: str, dims: dict[str, str]) -> dict[str, bool]:
    """REAL check: which requested dims appear as a TOP-LEVEL `name = ...` assignment.

    Comments are stripped first, and only assignments at brace depth 0 count — so a
    `name` mentioned in prose, commented out, used in a `==` comparison, or declared as a
    local inside a `module { ... }` body does NOT satisfy the parametric-constant contract.
    """
    clean = _strip_comments(scad)
    assign = {name: re.compile(rf"^[ \t]*{re.escape(name)}[ \t]*=[ \t]*[^=]") for name in dims}
    present = {name: False for name in dims}
    depth = 0
    for line in clean.splitlines():
        if depth == 0:
            for name in dims:
                if not present[name] and assign[name].search(line):
                    present[name] = True
        depth = max(0, depth + line.count("{") - line.count("}"))
    return present


# ── prompt + extraction ──────────────────────────────────────────────────────
def build_prompts(
    req: GenerateRequest,
    cfg: ai_core.AIConfig,
    *,
    previous_scad: str | None = None,
    errors: str | None = None,
) -> tuple[str, str]:
    """Build (system, user) prompts. Reuses `build_prompt_bundle` for the base design
    system prompt, then layers the generation style rules and this request's dims.

    On a repair round `previous_scad` + `errors` are appended so the model fixes the
    exact gate failure rather than starting over.
    """
    bundle = ai_core.build_prompt_bundle(
        ai_core.AIRequest(
            tool="design", operator="do",
            target=pathlib.Path(req.out_path),
            context=req.description,
        ),
        cfg,
    )
    system = f"{bundle.system_prompt}\n\n{_STYLE_SYSTEM}"
    constants = render_constants_block(req.dims)
    parts = [
        f"Model description: {req.description}",
        "",
        "Required named dimensions — declare these EXACTLY at the top, then use them:",
        constants,
        "",
        "Produce the complete .scad now (code only).",
    ]
    if previous_scad is not None and errors:
        parts += [
            "",
            "Your previous attempt did NOT pass verification.",
            "--- previous .scad ---",
            previous_scad.strip(),
            "--- verification errors ---",
            errors.strip(),
            "",
            "Fix the issues and output the corrected complete .scad. Keep the required "
            "named constants and the parametric style.",
        ]
    return system, "\n".join(parts)


def extract_scad(text: str) -> str:
    """Pull the OpenSCAD source out of a model response: prefer a fenced code block,
    else use the whole text. Always newline-terminated."""
    m = _FENCE_RE.search(text)
    body = m.group(1) if m else text
    return body.strip() + "\n"


# ── gates (shell out to the real `bin/3d`) ───────────────────────────────────
def _threed_bin() -> str:
    return os.path.join(repo_root(), "bin", "3d")


def _run_3d(args: list[str]) -> tuple[int, str]:
    """Run `bin/3d <args>` under the current interpreter; return (rc, stdout+stderr)."""
    try:
        p = subprocess.run(
            [sys.executable, _threed_bin(), *args],
            capture_output=True, text=True, timeout=_GATE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return 124, f"3d {' '.join(args)} timed out after {_GATE_TIMEOUT:g}s"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _parse_check_gates(log: str) -> list[GateResult]:
    """Parse a `3d check` breakdown into per-gate results.

    check prints lines like `  MANIFOLD     PASS  <msg>` / `PRINTABILITY SKIP ...`.
    """
    out: list[GateResult] = []
    for gate in ("MANIFOLD", "CONSISTENCY", "PRINTABILITY", "COLLISION", "SILHOUETTE"):
        m = re.search(rf"(?m)^\s*{gate}\s+(PASS|FAIL|SKIP|----)\s*(.*)$", log)
        if not m:
            continue
        verdict = {"PASS": "pass", "FAIL": "fail", "SKIP": "skip", "----": "warn"}[m.group(1)]
        out.append(GateResult(gate.lower(), verdict, m.group(2).strip()))
    return out


@dataclass(slots=True)
class _CandidateEval:
    """One round's verification outcome for a written .scad."""

    status: str
    gate_results: list[GateResult]
    error_text: str  # what to feed back to the model on the next round (empty if clean)


# ── visual review (opt-in) ───────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class _VisualReview:
    """Resolved config for the opt-in visual-review gate (built only when enabled)."""

    reference: str
    threshold: float
    backend: str | None
    config_path: str | None


@dataclass(slots=True)
class _VisualOutcome:
    """One candidate's visual verdict against the reference image."""

    passed: bool           # render meets the threshold AND a sighted judge was available
    blind: bool            # no sighted judge (or the judge could not run) — inconclusive
    gate: GateResult       # the `visual` gate row for the summary
    critique: str          # feedback for the next round (empty when passed)


def _run_visual_review(review: _VisualReview, render_png: str) -> _VisualOutcome:
    """Score the candidate render against the reference with the VLM judge (lazy import).

    Kept cheap for an inner loop (one sighted judge, no stability resampling). A judge
    failure never crashes the generate loop: it degrades to a blind/inconclusive outcome
    with a note, mirroring the advisory-judge discipline in ai.bench."""
    from ai import load_backend_config  # lazy: keep design.py import-light
    from ai.backends import resolve_backend
    from ai.judge import judge

    try:
        backend = resolve_backend(review.backend, config=load_backend_config(review.config_path))
        score = judge(render_png, review.reference, backend=backend, judges=1, stability_n=0)
    except Exception as exc:  # judge is an ADDITIONAL gate; its failure must not crash the loop
        note = f"visual review could not run: {exc}"
        return _VisualOutcome(False, True, GateResult("visual", "skip", note), "")
    return _outcome_from_score(score, review.threshold)


def _outcome_from_score(score: Any, threshold: float) -> _VisualOutcome:
    """Map a VisualScore into a pass/blind/fail outcome + a critique for the next round."""
    if score.blind:
        note = "no sighted judge (blind) — visual review inconclusive"
        return _VisualOutcome(False, True, GateResult("visual", "skip", note), "")
    passed = score.mean >= threshold
    detail = f"mean {score.mean}/4 vs threshold {threshold:g}"
    gate = GateResult("visual", "pass" if passed else "fail", detail)
    critique = "" if passed else _visual_critique(score, threshold)
    return _VisualOutcome(passed, False, gate, critique)


def _visual_critique(score: Any, threshold: float) -> str:
    """Build the feedback text the next prompt round sees from a below-threshold score."""
    lines = [
        f"VISUAL-REVIEW: the render scored mean {score.mean}/4 against the reference "
        f"(threshold {threshold:g}); it does not yet match the reference well enough.",
        "Per-dimension scores (0-4): "
        + ", ".join(f"{k}={v}" for k, v in score.per_dim.items()),
    ]
    rationales = _judge_rationales(score)
    if rationales:
        lines.append("Judge critique:")
        lines += [f"  - {r}" for r in rationales]
    lines.append("Revise the geometry so the render better reproduces the reference image.")
    return "\n".join(lines)


def _judge_rationales(score: Any) -> list[str]:
    """Pull each sighted judge's free-text `rationale` (if any) out of its raw reply."""
    import json as _json

    out: list[str] = []
    for jr in getattr(score, "judges", []):
        if getattr(jr, "blind", False):
            continue
        raw = getattr(getattr(jr, "canonical", None), "raw", "") or ""
        try:
            start, end = raw.index("{"), raw.rindex("}") + 1
            obj = _json.loads(raw[start:end])
        except (ValueError, _json.JSONDecodeError):
            continue
        rationale = obj.get("rationale") if isinstance(obj, dict) else None
        if isinstance(rationale, str) and rationale.strip():
            out.append(rationale.strip())
    return out


def _apply_visual(
    status: str, gates: list[GateResult], error_text: str, outcome: _VisualOutcome
) -> tuple[str, list[GateResult], str]:
    """Fold the visual outcome into the geometry verdict. Visual review is an ADDITIONAL
    gate: it can only DOWNGRADE `ok` (never upgrade), and it never touches the geometry
    gates. A blind/inconclusive verdict downgrades `ok` (we cannot confirm the look) but
    feeds no critique; a below-threshold verdict downgrades `ok` and feeds its critique."""
    gates = [*gates, outcome.gate]
    if outcome.passed:
        return status, gates, error_text
    if status == "ok":
        status = "diagnostic"
    if not outcome.blind:
        error_text = _join_errors(error_text, outcome.critique)
    return status, gates, error_text


def _join_errors(base: str, extra: str) -> str:
    parts = [p for p in (base.strip(), extra.strip()) if p]
    return "\n\n".join(parts)


def _build_review(req: GenerateRequest) -> _VisualReview | None:
    """Build the visual-review config from the request, or None when it is disabled.

    The reference-presence check lives in the command (exit 2 on a missing reference); by
    the time a request reaches here with `visual_review` set, `reference` is populated."""
    if not req.visual_review:
        return None
    if not req.reference:
        raise UsageError(
            "visual review requires a reference image",
            command="generate",
            remediation=["Pass --reference PATH (or a 'reference' key in --spec)."],
        )
    return _VisualReview(
        reference=req.reference,
        threshold=req.visual_threshold,
        backend=req.backend,
        config_path=req.config_path,
    )


def evaluate_scad(
    scad_path: str,
    dims: dict[str, str],
    scad_src: str,
    review: _VisualReview | None = None,
) -> _CandidateEval:
    """Validate -> render -> check the written .scad and decide its per-candidate status.

    - OpenSCAD absent  -> `diagnostic` (wrote the file, verification skipped).
    - validate/render fail -> `failure` for this candidate (no valid render).
    - renders + all HARD gates pass + all dims present -> `ok`.
    - renders but a gate warns/fails/skips or a dim is missing -> `diagnostic`.

    When `review` is set (opt-in) the candidate render is additionally scored against the
    reference by the VLM judge; a below-threshold or inconclusive verdict downgrades `ok`
    to `diagnostic` and its critique is threaded back into `error_text`. With `review` None
    the behaviour is byte-identical to before (the judge is never imported or called).
    """
    present = dims_present_in_scad(scad_src, dims)
    missing = [n for n, ok in present.items() if not ok]

    if find_openscad() is None:
        note = "openscad not installed — validate/render/check skipped"
        gates = [GateResult("verification", "skip", note)]
        if missing:
            gates.append(GateResult("dims", "warn", f"missing: {', '.join(missing)}"))
        if review is not None:
            gates.append(GateResult("visual", "skip", "no render — visual review skipped"))
        return _CandidateEval("diagnostic", gates, "")

    v_rc, v_log = _run_3d(["validate", scad_path])
    if v_rc != 0:
        return _CandidateEval("failure", [GateResult("validate", "fail", _tail(v_log))], v_log)

    visual: _VisualOutcome | None = None
    with tempfile.TemporaryDirectory(prefix="3d_generate_render.") as td:
        png = os.path.join(td, "render.png")
        r_rc, r_log = _run_3d(["render", scad_path, "--view", "iso", "--size", "480x360", "-o", png])
        rendered = r_rc == 0 and os.path.isfile(png) and os.path.getsize(png) > 0
        if rendered and review is not None:
            visual = _run_visual_review(review, png)  # judge while the PNG still exists
    if not rendered:
        return _CandidateEval("failure", [GateResult("render", "fail", _tail(r_log))], v_log + "\n" + r_log)

    c_rc, c_log = _run_3d(["check", scad_path])
    gates = _parse_check_gates(c_log) or [GateResult("check", "warn", "no gate lines parsed")]
    if missing:
        gates.append(GateResult("dims", "warn", f"requested dims missing from scad: {', '.join(missing)}"))

    status = _candidate_status(gates, missing)
    error_text = "" if status == "ok" else c_log
    if visual is not None:
        status, gates, error_text = _apply_visual(status, gates, error_text, visual)
    return _CandidateEval(status, gates, error_text)


def _candidate_status(gates: list[GateResult], missing: list[str]) -> str:
    """`ok` only if every HARD gate PASSED and no requested dim is missing."""
    by_name = {g.name: g.status for g in gates}
    manifold = by_name.get("manifold")
    printability = by_name.get("printability")
    hard_ok = manifold == "pass" and printability == "pass"
    if hard_ok and not missing:
        return "ok"
    return "diagnostic"


def _tail(text: str, n: int = 12) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


# ── the loop ─────────────────────────────────────────────────────────────────
def generate(req: GenerateRequest) -> GenerateResult:
    """Run the bounded generate->verify->fix loop and return the labelled result.

    Keeps the best rendering candidate across rounds (monotonic by status rank) and
    stops early the moment a candidate reaches `ok`. The best candidate's source is the
    one left on disk, even when a later, worse round overwrote it during the loop.
    """
    cfg = ai_core.with_cli_overrides(
        ai_core.load_config(req.config_path), backend=req.backend, model=req.model,
    )
    # Resolve the backend from the RAW ai.json (no forced-claude default): an absent
    # `backend` key must fall through to first-available, and a set $THREED_AI_MOCK_RESPONSE
    # must win — both handled inside resolve_backend when given the un-defaulted config.
    backend = resolve_backend(req.backend, config=load_backend_config(req.config_path))

    review_cfg = _build_review(req)

    best: GenerateResult | None = None
    best_src = ""
    prev_scad: str | None = None
    prev_errors: str | None = None
    rounds_run = 0

    for round_no in range(1, max(1, req.rounds) + 1):
        rounds_run = round_no
        system, user = build_prompts(req, cfg, previous_scad=prev_scad, errors=prev_errors)
        scad_src = extract_scad(backend.complete(system, user))
        _write(req.out_path, scad_src)

        ev = evaluate_scad(req.out_path, req.dims, scad_src, review=review_cfg)
        candidate = GenerateResult(
            status=ev.status, rounds=round_no, winning_round=round_no, scad_path=req.out_path,
            gate_results=ev.gate_results, requested_dims=dict(req.dims),
            dims_present_in_scad=dims_present_in_scad(scad_src, req.dims), backend=backend.name,
        )
        if _is_better(candidate, best):
            best, best_src = candidate, scad_src
        assert best is not None
        if ev.status == "ok":
            break
        prev_scad, prev_errors = scad_src, ev.error_text

    assert best is not None  # the loop runs at least once
    # `winning_round` stays the round that produced `best`; `rounds` = total attempted.
    best.rounds = rounds_run
    # A later, worse round may have overwritten the file — restore the best source so the
    # on-disk .scad matches the reported status.
    _write(req.out_path, best_src)
    if best.status == "failure":
        best.notes.append("no candidate produced a valid render within the round budget")
    return best


def _is_better(cand: GenerateResult, best: GenerateResult | None) -> bool:
    return best is None or _STATUS_RANK[cand.status] > _STATUS_RANK[best.status]


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
