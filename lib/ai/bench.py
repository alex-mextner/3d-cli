# ─────────────────────────────────────────────────────────────────────────────
# ai/bench.py — reproducible, auto-scored benchmark for the generative-modeling
# cases (APPLY-RESEARCH P1.3, the ModelRift image->OpenSCAD->iterate task format with
# an AUTOMATED metric vector instead of a subjective 0-5 score).
#
# WHAT / WHY
#   A suite of bench CASES, each a text prompt for an OpenSCAD model plus an expected
#   ProofStatus and per-case budget ceilings. Per case the harness:
#     1. asks the selected AI backend for a `.scad` (one shot — text mode is single-shot
#        per the design brainstorm; the loop tax is for the fit/image regimes);
#     2. GATE 0 = build-success: does OpenSCAD render it at all? (Text2CAD "Invalidity
#        Ratio" / the render-success gate — the first thing any auto-scored CAD-LLM
#        benchmark measures.) A model that does not render cannot be scored further;
#     3. the metric vector: silhouette IoU (cli.imaging via `3d score`), the geometry
#        battery (lib.geometry.mesh_metrics via `3d metrics geometry`, only when a target
#        mesh exists), the perceptual battery (lib.perceptual_metrics), and the VLM judge
#        (lib.ai.judge) as ONE ADVISORY column — never the only score (APPLY-RESEARCH
#        "VLM-judge as one column").
#   Then it reports the cost/efficiency columns the brainstorm demands ($/ok, seconds/ok,
#   backend-calls/ok, diagnostic-rate, build-success-rate) and persists one record per
#   case to the longitudinal store (registries.metrics) WITH its convention fields, so
#   `--compare` shows real deltas rather than silently-drifting numbers.
#
# HONESTY
#   This harness proves the SCORING pipeline works; it does not prove any real backend
#   scores WELL. A case that never renders is counted as a build-failure, NOT crashed; a
#   refusal / non-SCAD reply is a caught FAILURE; a budget-exceeded case records its
#   stop_reason and the suite still completes and reports. Missing tools (OpenSCAD,
#   ImageMagick, trimesh, torch) degrade a column to `available: false` — never a faked
#   number.
#
# DETERMINISM
#   With `$THREED_AI_MOCK_RESPONSE` set (or backend "mock") the whole suite runs offline
#   against canned per-case responses (`BenchCase.mock_response`) — no network, no real
#   model. That is what the test-suite drives.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ai.backends import Backend, MockBackend, resolve_backend
from errors import InvalidArgument, ThreeDError

MOCK_RESPONSE_ENV = "THREED_AI_MOCK_RESPONSE"
DEFAULT_RENDER_TIMEOUT = 120.0
STORE_COMMAND = "bench"


# ── Status / stop-reason vocab ───────────────────────────────────────────────
class ProofStatus(str, Enum):
    """Product-facing verdict for one case. SEPARATE from `stop_reason` (the machine
    detail) so "why did it stop" and "is the artifact usable" never get conflated."""

    OK = "ok"                # rendered, gates clean — a usable, verified model
    DIAGNOSTIC = "diagnostic"  # an artifact exists but could not be fully verified
    FAILURE = "failure"      # no usable artifact (no SCAD, unsafe, or did not render)


# Machine-readable reasons the case stopped where it did (brainstorm ProofStatus contract).
STOP_BUDGET = "budget_exhausted"
STOP_NO_SCAD = "no_scad"
STOP_UNSAFE = "unsafe_candidate"
STOP_WRITE_ERROR = "write_error"
STOP_BACKEND_ERROR = "backend_error"
STOP_BUILD_FAILED = "build_failed"
STOP_RENDERER_MISSING = "renderer_unavailable"
STOP_HARNESS_ERROR = "harness_error"


# ── Case + result models (stdlib dataclasses — the import-light rule) ─────────
@dataclass(frozen=True)
class Budget:
    """Per-case ceilings. A ceiling of 0 means "not allowed even once" — used to
    deterministically exercise the budget-exhausted path in tests."""

    max_rounds: int = 1
    max_renders: int = 2
    max_backend_calls: int = 1

    @classmethod
    def from_mapping(cls, raw: Any) -> "Budget":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            max_rounds=_int_or(raw.get("max_rounds"), 1),
            max_renders=_int_or(raw.get("max_renders"), 2),
            max_backend_calls=_int_or(raw.get("max_backend_calls"), 1),
        )


@dataclass(frozen=True)
class BenchCase:
    """One ModelRift-style task. `mock_response` is the canned backend reply used when the
    suite runs under the deterministic MockBackend; it is ignored for real backends."""

    id: str
    description: str
    prompt: str
    expected_status: ProofStatus
    budget: Budget
    reference_image: pathlib.Path | None = None
    target_mesh: pathlib.Path | None = None
    golden_scad: pathlib.Path | None = None
    mock_response: str | None = None
    dims: dict[str, Any] = field(default_factory=dict)


@dataclass
class MetricColumn:
    """One metric column result. `available` False means the tool/input was absent — the
    column is honestly blank, not a fabricated number."""

    name: str
    available: bool
    value: dict[str, Any] | None = None
    reason: str | None = None

    def to_jsonable(self) -> dict[str, Any]:
        return {"name": self.name, "available": self.available,
                "value": self.value, "reason": self.reason}


@dataclass
class CaseResult:
    """Everything one case produced. Persisted as one metrics record."""

    case_id: str
    status: ProofStatus
    expected_status: ProofStatus
    stop_reason: str | None
    build_success: bool | None  # None = build not attempted (no renderer)
    backend_calls: int
    renders: int
    rounds: int
    wall_time: float
    tokens: int | None
    cost: float | None
    columns: dict[str, MetricColumn]
    scad_path: str | None
    error: str | None = None

    @property
    def matched_expectation(self) -> bool:
        return self.status == self.expected_status

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "status": self.status.value,
            "expected_status": self.expected_status.value,
            "matched_expectation": self.matched_expectation,
            "stop_reason": self.stop_reason,
            "build_success": self.build_success,
            "backend_calls": self.backend_calls,
            "renders": self.renders,
            "rounds": self.rounds,
            "wall_time": round(self.wall_time, 4),
            "tokens": self.tokens,
            "cost": self.cost,
            "scad_path": self.scad_path,
            "error": self.error,
            "columns": {k: v.to_jsonable() for k, v in self.columns.items()},
        }


@dataclass
class SuiteReport:
    """Aggregate over all cases: the rates + efficiency columns the brainstorm demands."""

    backend: str
    suite_signature: str
    n_cases: int
    build_attempted: int
    build_success_rate: float | None
    ok_rate: float
    diagnostic_rate: float
    failure_rate: float
    expectation_match_rate: float
    seconds_per_ok: float | None
    calls_per_ok: float | None
    renders_per_ok: float | None
    cost_per_ok: float | None
    total_wall_time: float
    results: list[CaseResult]

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "suite_signature": self.suite_signature,
            "n_cases": self.n_cases,
            "build_attempted": self.build_attempted,
            "build_success_rate": self.build_success_rate,
            "ok_rate": self.ok_rate,
            "diagnostic_rate": self.diagnostic_rate,
            "failure_rate": self.failure_rate,
            "expectation_match_rate": self.expectation_match_rate,
            "seconds_per_ok": self.seconds_per_ok,
            "calls_per_ok": self.calls_per_ok,
            "renders_per_ok": self.renders_per_ok,
            "cost_per_ok": self.cost_per_ok,
            "total_wall_time": round(self.total_wall_time, 4),
            "cases": [r.to_jsonable() for r in self.results],
        }


# ── Small helpers ────────────────────────────────────────────────────────────
def _int_or(value: Any, default: int) -> int:
    try:
        if isinstance(value, bool) or value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def default_suite_dir() -> pathlib.Path:
    """The shipped golden-case directory (lib/data/bench)."""
    return pathlib.Path(__file__).resolve().parent.parent / "data" / "bench"


# ── Case loading ─────────────────────────────────────────────────────────────
def _status_from(raw: Any, *, case_id: str) -> ProofStatus:
    try:
        return ProofStatus(str(raw))
    except ValueError as exc:
        raise InvalidArgument(
            "expected_status", str(raw), [s.value for s in ProofStatus],
            command="bench", extra=f"in bench case {case_id!r}",
        ) from exc


def _path_field(raw: Any, base: pathlib.Path) -> pathlib.Path | None:
    if not raw:
        return None
    p = pathlib.Path(str(raw)).expanduser()
    return p if p.is_absolute() else (base / p)


# A case id becomes a workdir directory + a `.scad` filename, so it MUST be a safe basename:
# a hostile suite with `id: "../../etc/foo"` would otherwise write outside `--workdir`.
_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate_case_id(case_id: str) -> str:
    if ".." in case_id or "/" in case_id or "\\" in case_id or not _CASE_ID_RE.match(case_id):
        raise InvalidArgument(
            "case id", case_id,
            ["a safe basename: letters/digits then letters/digits/._- (no '/', '\\', or '..')"],
            command="bench",
        )
    return case_id


def parse_case(raw: dict[str, Any], base: pathlib.Path) -> BenchCase:
    """Turn one parsed JSON object into a BenchCase. `base` roots relative asset paths."""
    if "id" not in raw or "description" not in raw:
        raise InvalidArgument(
            "case", json.dumps(raw)[:80], ["objects with 'id' and 'description'"],
            command="bench",
        )
    case_id = _validate_case_id(str(raw["id"]))
    dims_raw = raw.get("dims")
    dims: dict[str, Any] = dims_raw if isinstance(dims_raw, dict) else {}
    return BenchCase(
        id=case_id,
        description=str(raw["description"]),
        prompt=str(raw.get("prompt") or raw["description"]),
        expected_status=_status_from(raw.get("expected_status", "ok"), case_id=case_id),
        budget=Budget.from_mapping(raw.get("budget")),
        reference_image=_path_field(raw.get("reference_image"), base),
        target_mesh=_path_field(raw.get("target_mesh"), base),
        golden_scad=_path_field(raw.get("golden_scad"), base),
        mock_response=raw.get("mock_response"),
        dims=dims,
    )


def load_cases(suite_dir: pathlib.Path | None = None) -> list[BenchCase]:
    """Load every `*.json` bench case from `suite_dir` (default: the shipped suite),
    sorted by id for a deterministic run order."""
    base = suite_dir or default_suite_dir()
    if not base.is_dir():
        raise InvalidArgument(
            "suite", str(base), ["an existing directory of *.json cases"],
            command="bench",
        )
    cases: list[BenchCase] = []
    for path in sorted(base.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidArgument(
                "case-file", path.name, ["valid JSON"], command="bench",
                extra=f"could not parse {path}: {exc}",
            ) from exc
        cases.append(parse_case(raw, base))
    if not cases:
        raise InvalidArgument(
            "suite", str(base), ["a directory containing at least one *.json case"],
            command="bench",
        )
    return cases


# ── SCAD extraction + safety ─────────────────────────────────────────────────
_FENCE_RE = re.compile(r"```(?:scad|openscad)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
# Tokens that make a blob of text plausibly OpenSCAD rather than a prose refusal.
_SCAD_TOKENS_RE = re.compile(
    r"\b(cube|sphere|cylinder|polyhedron|linear_extrude|rotate_extrude|"
    r"translate|rotate|module|union|difference|intersection|hull|minkowski)\s*\(",
    re.IGNORECASE,
)


def extract_scad(text: str) -> str | None:
    """Pull SCAD source out of a backend reply. Prefers a fenced ```scad block; else, if
    the whole reply looks like SCAD (has a primitive/transform call), returns it verbatim;
    else None (a prose refusal / "I can't make that" yields no SCAD — a caught failure,
    not a crash)."""
    if not text or not text.strip():
        return None
    fences = _FENCE_RE.findall(text)
    for block in fences:
        if block.strip():
            return block.strip()
    if _SCAD_TOKENS_RE.search(text):
        return text.strip()
    return None


# A candidate is rejected before it ever reaches OpenSCAD if it tries to escape the temp
# sandbox: absolute / parent-relative include|use|import, or a shell-ish injection. This is
# the "unsafe include" failure path (brainstorm risk #4 — a tempdir is a courtesy, not a
# sandbox). NOT a full sandbox; a first, cheap, deterministic gate.
_UNSAFE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(?:include|use)\s*<\s*/", "absolute include/use path escapes the sandbox"),
    (r"(?:include|use)\s*<[^>]*\.\.", "parent-relative include/use path escapes the sandbox"),
    (r"""import\s*\(\s*["'][/~]""", "absolute import() path escapes the sandbox"),
    (r"""import\s*\(\s*["'][^"']*\.\.""", "parent-relative import() path escapes the sandbox"),
    (r"\$\(|`|\bsystem\s*\(", "shell-injection-like construct"),
)


def scad_safety_check(scad: str) -> tuple[bool, str | None]:
    """Return (safe, reason). A first cheap gate against candidates that would read
    outside the temp workdir or smell like shell injection."""
    for pattern, reason in _UNSAFE_PATTERNS:
        if re.search(pattern, scad, re.IGNORECASE):
            return False, reason
    return True, None


# ── Subprocess plumbing (drive the real `3d` subcommands + OpenSCAD) ──────────
@dataclass
class _Proc:
    rc: int
    out: str
    err: str


def _run(cmd: list[str], *, timeout: float, env: dict[str, str] | None = None) -> _Proc:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return _Proc(rc=124, out="", err=f"timed out after {timeout:g}s")
    except FileNotFoundError as exc:
        return _Proc(rc=127, out="", err=str(exc))
    return _Proc(rc=p.returncode, out=p.stdout or "", err=p.stderr or "")


def _bin3d() -> pathlib.Path:
    from cli import env

    return pathlib.Path(env.repo_root()) / "bin" / "3d"


def _find_openscad() -> str | None:
    from cli import env

    return env.find_openscad()


def _score_iou(scad_png: pathlib.Path, ref: pathlib.Path) -> MetricColumn:
    """Silhouette IoU column via `3d score <render.png> <ref>` (ImageMagick only)."""
    proc = _run([str(_bin3d()), "score", str(scad_png), str(ref)], timeout=120.0)
    if proc.rc == 127:
        return MetricColumn("silhouette", available=False,
                            reason="ImageMagick missing (score exit 127)")
    if proc.rc != 0:
        return MetricColumn("silhouette", available=False,
                            reason=f"score failed: {(proc.err or proc.out).strip()[:160]}")
    parsed = _parse_kv(proc.out)
    iou = parsed.get("IoU")
    if iou is None:
        return MetricColumn("silhouette", available=False, reason="score produced no IoU")
    return MetricColumn("silhouette", available=True,
                        value={"iou": float(iou), "ae": parsed.get("AE"),
                               "sense": "higher_better", "frame": parsed.get("FRAME")})


def _geometry_column(cand_stl: pathlib.Path, target: pathlib.Path) -> MetricColumn:
    """Geometry battery via `3d metrics geometry <cand> <target> --json --no-store`."""
    proc = _run(
        [str(_bin3d()), "metrics", "geometry", str(cand_stl), str(target),
         "--json", "--no-store"], timeout=600.0,
    )
    if proc.rc == 127:
        return MetricColumn("geometry", available=False,
                            reason="trimesh/scipy runtime unavailable")
    if proc.rc != 0:
        return MetricColumn("geometry", available=False,
                            reason=f"geometry battery failed: {(proc.err or proc.out).strip()[:160]}")
    try:
        value = json.loads(proc.out)
    except json.JSONDecodeError:
        return MetricColumn("geometry", available=False, reason="geometry battery gave no JSON")
    return MetricColumn("geometry", available=True, value=value)


def _perceptual_column(scad_png: pathlib.Path, ref: pathlib.Path) -> MetricColumn:
    """Perceptual battery via `3d metrics perceptual <render.png> <ref> --json --no-store`."""
    proc = _run(
        [str(_bin3d()), "metrics", "perceptual", str(scad_png), str(ref),
         "--json", "--no-store"], timeout=600.0,
    )
    if proc.rc not in (0, 127):
        return MetricColumn("perceptual", available=False,
                            reason=f"perceptual battery failed: {(proc.err or proc.out).strip()[:160]}")
    try:
        value = json.loads(proc.out)
    except json.JSONDecodeError:
        return MetricColumn("perceptual", available=False, reason="perceptual battery gave no JSON")
    # PSNR is always available (ImageMagick/numpy); LPIPS/CLIP degrade individually.
    any_available = any(
        isinstance(v, dict) and v.get("available") for k, v in value.items() if k != "convention"
    )
    return MetricColumn("perceptual", available=any_available, value=value,
                        reason=None if any_available else "no perceptual channel available")


def advisory_judge(
    render_png: pathlib.Path, ref: pathlib.Path,
    backend: "Backend | str | None" = None, *, judges: int = 1, stability_n: int = 0,
) -> MetricColumn:
    """The VLM-judge column — ADVISORY ONLY (one column, never the pass/fail gate). Every
    judge failure mode (a blind/mock backend, a malformed non-rubric reply, a missing
    backend) is CAUGHT here and reported as an unavailable column, never a crash. This is
    exactly the "malformed backend JSON -> caught" failure path. `backend` is a resolved
    Backend, a backend name, or None (auto-resolve); `judges`/`stability_n` tune the guards
    (kept cheap by default — this is advisory, not the gate)."""
    from ai import judge as judge_mod

    try:
        verdict = judge_mod.judge(render_png, ref, backend=backend,
                                  judges=judges, stability_n=stability_n)
    except ThreeDError as exc:
        return MetricColumn("judge", available=False, reason=f"judge unavailable: {exc.message}")
    except Exception as exc:  # a judge crash must never fail the suite — it is advisory
        return MetricColumn("judge", available=False, reason=f"judge error: {exc}")
    if verdict.blind:
        return MetricColumn("judge", available=False, value=verdict.to_jsonable(),
                            reason="blind backend (no image support) — not a real visual score")
    return MetricColumn("judge", available=True, value=verdict.to_jsonable())


def _parse_kv(text: str) -> dict[str, str]:
    """Parse the KEY=VALUE lines `3d score` prints."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


# ── Backend selection (per-case mock determinism) ────────────────────────────
def _is_mock_mode(backend_name: str | None) -> bool:
    import os

    if backend_name == "mock":
        return True
    return backend_name is None and os.environ.get(MOCK_RESPONSE_ENV) is not None


def make_backend(backend_name: str | None, *, mock_response: str | None = None) -> Backend:
    """Resolve a backend. In mock mode a supplied `mock_response` wins, so a suite can hold a
    known-good case AND a known-bad case and score both offline from one run without any
    network. Decoupled from BenchCase so any caller can spin one up with just a name."""
    if _is_mock_mode(backend_name) and mock_response is not None:
        return MockBackend(mock_response)
    from ai import load_backend_config

    return resolve_backend(backend_name, config=load_backend_config())


# ── The per-case pipeline ────────────────────────────────────────────────────
def _fail(case: BenchCase, stop: str, *, calls: int, wall: float,
          error: str | None = None) -> CaseResult:
    # build_success is None, NOT False: these failures happen BEFORE any render is
    # attempted, so they must not count in the build-success-rate denominator (which is
    # the Text2CAD "invalidity ratio" = renders that succeeded / renders attempted). Only a
    # genuine gate-0 render failure sets build_success=False.
    return CaseResult(
        case_id=case.id, status=ProofStatus.FAILURE, expected_status=case.expected_status,
        stop_reason=stop, build_success=None, backend_calls=calls, renders=0, rounds=1,
        wall_time=wall, tokens=None, cost=None, columns={}, scad_path=None, error=error,
    )


def _gate0_render(scad_path: pathlib.Path, workdir: pathlib.Path,
                  openscad: str) -> tuple[bool, pathlib.Path | None, str | None]:
    """GATE 0: render the .scad to a PNG. Returns (build_success, png_path, error)."""
    png = workdir / f"{scad_path.stem}.png"
    proc = _run([openscad, "--render", "-o", str(png), str(scad_path)],
                timeout=DEFAULT_RENDER_TIMEOUT)
    if proc.rc != 0 or not png.is_file():
        return False, None, (proc.err or proc.out).strip()[:200]
    return True, png, None


def _render_stl(scad_path: pathlib.Path, workdir: pathlib.Path,
                openscad: str) -> pathlib.Path | None:
    """Best-effort STL export for the geometry battery (only when a target mesh exists)."""
    stl = workdir / f"{scad_path.stem}.stl"
    proc = _run([openscad, "-o", str(stl), str(scad_path)], timeout=DEFAULT_RENDER_TIMEOUT)
    return stl if proc.rc == 0 and stl.is_file() else None


def _score_columns(case: BenchCase, png: pathlib.Path, scad_path: pathlib.Path,
                   workdir: pathlib.Path, openscad: str,
                   backend_name: str | None) -> dict[str, MetricColumn]:
    """Run the metric vector on a successfully-built model. Every column degrades to
    `available: false` when its input/tool is absent — never a fabricated number."""
    columns: dict[str, MetricColumn] = {}
    if case.reference_image and case.reference_image.is_file():
        columns["silhouette"] = _score_iou(png, case.reference_image)
        columns["perceptual"] = _perceptual_column(png, case.reference_image)
        columns["judge"] = advisory_judge(png, case.reference_image, backend_name)  # name -> auto-resolve
    if case.target_mesh and case.target_mesh.is_file():
        stl = _render_stl(scad_path, workdir, openscad)
        if stl is not None:
            columns["geometry"] = _geometry_column(stl, case.target_mesh)
        else:
            columns["geometry"] = MetricColumn(
                "geometry", available=False, reason="candidate STL export failed")
    return columns


def _obtain_scad(case: BenchCase, backend_name: str | None, calls: int, start: float,
                 ) -> tuple[str | None, CaseResult | None, int]:
    """Budget-check + call the backend + extract/safety-check the SCAD. Returns
    (scad, early_failure_result_or_None, backend_calls)."""
    if case.budget.max_backend_calls < 1:
        return None, _fail(case, STOP_BUDGET, calls=0, wall=time.time() - start), 0
    try:
        backend = make_backend(backend_name, mock_response=case.mock_response)
        reply = backend.complete(_SYSTEM_PROMPT, case.prompt, timeout=DEFAULT_RENDER_TIMEOUT)
    except ThreeDError as exc:
        return None, _fail(case, STOP_BACKEND_ERROR, calls=calls,
                           wall=time.time() - start, error=exc.message), calls
    calls += 1
    scad = extract_scad(reply)
    if scad is None:
        return None, _fail(case, STOP_NO_SCAD, calls=calls, wall=time.time() - start,
                           error="backend reply contained no SCAD (refusal or prose)"), calls
    safe, reason = scad_safety_check(scad)
    if not safe:
        return None, _fail(case, STOP_UNSAFE, calls=calls, wall=time.time() - start,
                           error=reason), calls
    return scad, None, calls


_SYSTEM_PROMPT = (
    "You are an expert OpenSCAD modeler. Given a description, output ONLY a complete, "
    "self-contained OpenSCAD program that renders the described object. Do not read or "
    "include any external file. Wrap the code in a ```scad fenced block."
)


def run_case(case: BenchCase, *, backend_name: str | None,
             workdir: pathlib.Path) -> CaseResult:
    """Run ONE case end to end. Every failure mode is turned into a labelled CaseResult —
    this function does not raise for a model/render/tool failure (only a genuine harness
    bug would propagate, and run_suite catches even that)."""
    start = time.time()
    workdir.mkdir(parents=True, exist_ok=True)
    scad, early, calls = _obtain_scad(case, backend_name, 0, start)
    if early is not None:
        return early
    assert scad is not None
    scad_path = workdir / f"{case.id}.scad"
    try:
        scad_path.write_text(scad, encoding="utf-8")
    except OSError as exc:
        return _fail(case, STOP_WRITE_ERROR, calls=calls, wall=time.time() - start,
                     error=f"could not write candidate .scad: {exc}")

    openscad = _find_openscad()
    if openscad is None:
        return CaseResult(
            case_id=case.id, status=ProofStatus.DIAGNOSTIC,
            expected_status=case.expected_status, stop_reason=STOP_RENDERER_MISSING,
            build_success=None, backend_calls=calls, renders=0, rounds=1,
            wall_time=time.time() - start, tokens=None, cost=None, columns={},
            scad_path=str(scad_path),
            error="OpenSCAD not installed — SCAD is safe but unverified (environment, not model, failure)",
        )
    if case.budget.max_renders < 1:
        return CaseResult(
            case_id=case.id, status=ProofStatus.DIAGNOSTIC,
            expected_status=case.expected_status, stop_reason=STOP_BUDGET,
            build_success=None, backend_calls=calls, renders=0, rounds=1,
            wall_time=time.time() - start, tokens=None, cost=None, columns={},
            scad_path=str(scad_path), error="render budget exhausted before gate 0",
        )
    built, png, err = _gate0_render(scad_path, workdir, openscad)
    if not built or png is None:
        return CaseResult(
            case_id=case.id, status=ProofStatus.FAILURE,
            expected_status=case.expected_status, stop_reason=STOP_BUILD_FAILED,
            build_success=False, backend_calls=calls, renders=1, rounds=1,
            wall_time=time.time() - start, tokens=None, cost=None, columns={},
            scad_path=str(scad_path), error=f"gate 0 (build-success) failed: {err}",
        )
    columns = _score_columns(case, png, scad_path, workdir, openscad, backend_name)
    return CaseResult(
        case_id=case.id, status=ProofStatus.OK, expected_status=case.expected_status,
        stop_reason=None, build_success=True, backend_calls=calls, renders=1, rounds=1,
        wall_time=time.time() - start, tokens=None, cost=None, columns=columns,
        scad_path=str(scad_path),
    )


# ── Suite runner + aggregation ───────────────────────────────────────────────
def _safe_run_case(case: BenchCase, backend_name: str | None,
                   workdir: pathlib.Path) -> CaseResult:
    """Wrap run_case so even a genuine harness bug on ONE case becomes a labelled FAILURE
    record and the suite keeps going (brainstorm: the suite must complete and report even
    when individual cases fail)."""
    started = time.time()
    try:
        return run_case(case, backend_name=backend_name, workdir=workdir / case.id)
    except Exception as exc:  # last-resort net: a harness bug is a case failure, not a crash
        return _fail(case, STOP_HARNESS_ERROR, calls=0, wall=time.time() - started,
                     error=f"{type(exc).__name__}: {exc}")


def _rate(n: int, total: int) -> float:
    return round(n / total, 4) if total else 0.0


def _mean_over_ok(results: list[CaseResult], pick: Any) -> float | None:
    oks = [r for r in results if r.status == ProofStatus.OK]
    vals = [pick(r) for r in oks if pick(r) is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def suite_signature(backend: str, results: list[CaseResult]) -> str:
    """A stable identity for one suite run = backend + a hash of the sorted case-id set. Two
    runs are comparable (`--compare`) ONLY when their signatures match, so a delta never
    silently mixes different backends or different case sets."""
    import hashlib

    ids = ",".join(sorted(r.case_id for r in results))
    digest = hashlib.sha256(ids.encode("utf-8")).hexdigest()[:12]
    return f"{backend}:{len(results)}:{digest}"


def aggregate(results: list[CaseResult], backend: str) -> SuiteReport:
    """Roll per-case results into the rates + efficiency columns the brainstorm demands."""
    total = len(results)
    attempted = [r for r in results if r.build_success is not None]
    built_ok = [r for r in attempted if r.build_success]
    n_ok = sum(1 for r in results if r.status == ProofStatus.OK)
    n_diag = sum(1 for r in results if r.status == ProofStatus.DIAGNOSTIC)
    n_fail = sum(1 for r in results if r.status == ProofStatus.FAILURE)
    matched = sum(1 for r in results if r.matched_expectation)
    return SuiteReport(
        backend=backend,
        suite_signature=suite_signature(backend, results),
        n_cases=total,
        build_attempted=len(attempted),
        build_success_rate=_rate(len(built_ok), len(attempted)) if attempted else None,
        ok_rate=_rate(n_ok, total),
        diagnostic_rate=_rate(n_diag, total),
        failure_rate=_rate(n_fail, total),
        expectation_match_rate=_rate(matched, total),
        seconds_per_ok=_mean_over_ok(results, lambda r: r.wall_time),
        calls_per_ok=_mean_over_ok(results, lambda r: r.backend_calls),
        renders_per_ok=_mean_over_ok(results, lambda r: r.renders),
        cost_per_ok=_mean_over_ok(results, lambda r: r.cost),
        total_wall_time=round(sum(r.wall_time for r in results), 4),
        results=results,
    )


def _persist(report: SuiteReport, *, data_dir: pathlib.Path | None) -> None:
    """Append one metrics record per case + one suite-aggregate record, WITH the convention
    fields (per APPLY-RESEARCH — the store is worthless if the convention drifts). A store
    failure (full disk, read-only dir) NEVER fails the benchmark: the measurement stands on
    its own; only the persistence is best-effort."""
    try:
        from registries.metrics import append_record
    except Exception:
        return
    convention = {"harness": "ai.bench", "gate0": "openscad_render_success",
                  "judge": "advisory_only", "backend": report.backend}
    for r in report.results:
        try:
            append_record(
                command=STORE_COMMAND, tool="ai.bench", backend=report.backend,
                inputs={"case_id": r.case_id, "expected_status": r.expected_status.value},
                metrics={**r.to_jsonable(), "convention": convention},
                wall_time=r.wall_time, data_dir=data_dir,
            )
        except Exception:
            pass  # per-case store failure must not abort the suite
    try:
        append_record(
            command=STORE_COMMAND, tool="ai.bench", backend=report.backend,
            inputs={"suite": True, "n_cases": report.n_cases},
            metrics={**report.to_jsonable(), "suite": True, "convention": convention},
            wall_time=report.total_wall_time, data_dir=data_dir,
        )
    except Exception:
        pass


def run_suite(cases: list[BenchCase], *, backend_name: str | None,
              workdir: pathlib.Path, store: bool = True,
              data_dir: pathlib.Path | None = None,
              progress: Any = None) -> SuiteReport:
    """Run every case, aggregate, and (optionally) persist. Never raises for a case
    failure — the returned report always covers all cases.

    PRE-FLIGHT: a genuinely-unavailable or unknown backend is an ENVIRONMENT error, not a
    per-case data point. It is validated ONCE up front so `--backend bogus` / no-backend
    raises (MissingDependency / InvalidArgument -> nonzero exit) instead of silently
    producing an all-failed report with exit 0. Mock mode skips this (mock is always up)."""
    if not _is_mock_mode(backend_name):
        from ai import load_backend_config

        resolve_backend(backend_name, config=load_backend_config())  # raises if unavailable/unknown
    results: list[CaseResult] = []
    for i, case in enumerate(cases, start=1):
        if progress is not None:
            progress(i, len(cases), case)
        results.append(_safe_run_case(case, backend_name, workdir))
    backend_label = backend_name or ("mock" if _is_mock_mode(backend_name) else "auto")
    report = aggregate(results, backend_label)
    if store:
        _persist(report, data_dir=data_dir)
    return report


# ── Longitudinal compare ─────────────────────────────────────────────────────
def _suite_aggregates(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in records:
        metrics = rec.get("metrics")
        if isinstance(metrics, dict) and metrics.get("suite"):
            out.append({"timestamp": rec.get("timestamp"), **metrics})
    return out


_COMPARE_KEYS = ("build_success_rate", "ok_rate", "diagnostic_rate", "failure_rate",
                 "expectation_match_rate", "seconds_per_ok", "calls_per_ok", "cost_per_ok")


def compare_last_two(data_dir: pathlib.Path | None = None) -> dict[str, Any] | None:
    """Return the delta between the two most recent suite runs that share the LATEST run's
    `suite_signature` (same backend + same case set), or None if fewer than two such runs
    exist. Filtering by signature stops `--compare` from claiming a like-for-like delta
    across different backends or suites (Codex review finding)."""
    from registries.metrics import read_records

    records = read_records(command=STORE_COMMAND, data_dir=data_dir)
    suites = _suite_aggregates([dict(r) for r in records])
    if len(suites) < 2:
        return None
    latest_sig = suites[-1].get("suite_signature")
    same = [s for s in suites if s.get("suite_signature") == latest_sig]
    if len(same) < 2:
        return None
    prev, curr = same[-2], same[-1]
    deltas: dict[str, Any] = {}
    for key in _COMPARE_KEYS:
        a, b = prev.get(key), curr.get(key)
        deltas[key] = {"prev": a, "curr": b,
                       "delta": round(b - a, 4) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None}
    return {"suite_signature": latest_sig,
            "previous_timestamp": prev.get("timestamp"),
            "current_timestamp": curr.get("timestamp"), "deltas": deltas}
