"""3d judge — score a rendered model against a reference image with a VLM-as-a-judge.

WHAT: runs the anchored VLM rubric (silhouette/proportion, feature completeness,
  structural correctness, detail fidelity — each 0-4 + mean) with reproducibility guards
  (temp-0 canonical + N stability samples + >=2 distinct judges) and prints
  machine-parseable KEY=VALUE lines + a plain result label (ok / low-confidence / blind).

WHY: pixel metrics (IoU, LPIPS) miss "does it LOOK like the subject". CADBench (κ=0.791)
  shows a well-anchored VLM rubric is reliable enough to ship — but ONLY with the guards,
  which is why a single-judge or text-only (BLIND) verdict is labelled, never passed off as
  a clean visual score.

STDLIB-ONLY at top level: the judge library (lib/ai/judge.py) is import-light, but it is
  still reached lazily inside run() to keep this module's surface minimal (the command
  contract). No heavy dep and no network at import time.

Examples:
  3d judge render.png photo.jpg
  3d judge render.png photo.jpg --backend claude --backend ollama   # two distinct judges
  3d judge render.png photo.jpg --stability-n 5 --json
  3d judge render.png photo.jpg --feature-context "columns; pediment; dome"
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from cli.registry import Command
from errors import GateFailure, InputNotFound, UsageError

USAGE = """3d judge <render.png|jpg> <reference.png|jpg> [options]
  VLM-as-a-judge: score the render against the reference on the anchored rubric
  (silhouette/proportion, feature-completeness, structural-correctness, detail-fidelity;
  each 0-4 + mean) with reproducibility guards. Prints KEY=VALUE lines + a result label.

Options:
  --backend NAME        judge backend (repeatable for >=2 distinct judges;
                        accepted: claude, codex, opencode, ollama, mock). Default: auto-pick.
  --stability-n N       stability samples per judge (default: 5; 0 disables the flag)
  --feature-context T   caller-supplied salient-feature taxonomy (kept out of core)
  --config PATH         backend JSON config (default: ~/.config/3d-cli/ai.json)
  --json                emit the full VisualScore as JSON (per-dim, stability, transcript)
  --rubric FILE.json    your own rubric: a JSON list of {"key","anchor_0","anchor_2","anchor_4","label"?}
                        instead of the built-in four dimensions (one dimension per marking region, say)
  --min-dim X           gate: exit 1 unless EVERY dimension's mean over all reads (canonical + stability
                        samples) is >= X (e.g. 3: a feature passes). A blind verdict passes nothing
  --max-mean X          gate: exit 1 unless the mean over all reads is <= X (a negative control: two
                        different views must score low)

Result labels:
  ok               >=2 sighted judges, stable, agreeing.
  low-confidence   single judge, unstable N-samples, or large cross-judge spread.
  blind            no judge backend can SEE images (text-only) — NOT a real visual score.

Examples:
  3d judge render.png photo.jpg
  3d judge render.png photo.jpg --backend claude --backend ollama
  3d judge render.png photo.jpg --stability-n 5 --json
  3d judge render.png photo.jpg --feature-context "columns; pediment; dome"
  3d judge crop_render.png crop_ref.png --rubric nostril.json --min-dim 3 --stability-n 2
  3d judge ref.png ref.png --rubric nostril.json --min-dim 4     # control: the reference against itself must score 4
  3d judge front.png back.png --max-mean 2                       # control: two different views must score low"""


def _print_usage() -> None:
    print(USAGE)


def _need_value(argv: list[str], i: int, flag: str) -> str:
    if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
        raise UsageError(f"option {flag} needs a value", command="judge")
    return argv[i + 1]


def _existing(raw: str) -> pathlib.Path:
    path = pathlib.Path(raw).expanduser()
    if not path.is_file():
        raise InputNotFound(raw, command="judge")
    return path.resolve()


class _Opts:
    def __init__(self) -> None:
        self.backends: list[str] = []
        self.stability_n: int = 5
        self.feature_context: str | None = None
        self.config: str | None = None
        self.as_json: bool = False
        self.rubric: str | None = None
        self.min_dim: float | None = None
        self.max_mean: float | None = None


def _parse_opts(argv: list[str]) -> _Opts:
    opts = _Opts()
    i = 0
    while i < len(argv):
        arg = argv[i]
        value: str | None = None
        if arg.startswith("--") and "=" in arg:
            arg, value = arg.split("=", 1)
            if value == "":
                raise UsageError(f"option {arg} needs a value", command="judge")
        if arg == "--backend":
            opts.backends.append(
                value if value is not None else _need_value(argv, i, arg)
            )
            i += 1 if value is not None else 2
        elif arg == "--stability-n":
            raw = value if value is not None else _need_value(argv, i, arg)
            try:
                opts.stability_n = max(0, int(raw))
            except ValueError as exc:
                raise UsageError(
                    f"--stability-n must be an integer, got {raw!r}", command="judge"
                ) from exc
            i += 1 if value is not None else 2
        elif arg == "--feature-context":
            opts.feature_context = (
                value if value is not None else _need_value(argv, i, arg)
            )
            i += 1 if value is not None else 2
        elif arg == "--config":
            opts.config = value if value is not None else _need_value(argv, i, arg)
            i += 1 if value is not None else 2
        elif arg == "--json":
            opts.as_json = True
            i += 1
        elif arg == "--rubric":
            opts.rubric = value if value is not None else _need_value(argv, i, arg)
            i += 1 if value is not None else 2
        elif arg == "--min-dim":
            opts.min_dim = _number(
                value if value is not None else _need_value(argv, i, arg), arg
            )
            i += 1 if value is not None else 2
        elif arg == "--max-mean":
            opts.max_mean = _number(
                value if value is not None else _need_value(argv, i, arg), arg
            )
            i += 1 if value is not None else 2
        else:
            raise UsageError(f"unknown option '{arg}'", command="judge")
    return opts


def _number(raw: str, flag: str) -> float:
    try:
        return float(raw)
    except ValueError as exc:
        raise UsageError(
            f"{flag} must be a number, got {raw!r}", command="judge"
        ) from exc


def _load_rubric(path: str) -> list[Any]:
    """A rubric file: a JSON list of {key, anchor_0, anchor_2, anchor_4, label?} -> RubricDimension list."""
    from ai.judge import RubricDimension

    file = pathlib.Path(path).expanduser()
    if not file.is_file():
        raise InputNotFound(path, command="judge")
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"{path}: not valid JSON ({exc})", command="judge") from exc
    needed = ("key", "anchor_0", "anchor_2", "anchor_4")
    if not isinstance(raw, list) or not raw:
        raise UsageError(
            f"{path}: the rubric must be a non-empty JSON list of dimensions",
            command="judge",
        )
    dims: list[Any] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or any(
            not isinstance(item.get(k), str) or not item[k] for k in needed
        ):
            raise UsageError(
                f"{path}: dimension {i} needs non-empty string fields {', '.join(needed)} (label is optional)",
                command="judge",
            )
        dims.append(
            RubricDimension(
                key=item["key"],
                label=str(item.get("label") or item["key"]),
                anchor_0=item["anchor_0"],
                anchor_2=item["anchor_2"],
                anchor_4=item["anchor_4"],
            )
        )
    keys = [d.key for d in dims]
    if len(set(keys)) != len(keys):
        raise UsageError(f"{path}: duplicate dimension keys in {keys}", command="judge")
    return dims


def _resolve_backends(opts: _Opts) -> list[Any]:
    from ai import load_backend_config
    from ai.backends import resolve_backend

    cfg = load_backend_config(opts.config)
    if not opts.backends:
        return [resolve_backend(config=cfg)]
    return [resolve_backend(name, config=cfg) for name in opts.backends]


def _na(value: object) -> str:
    """None -> NA so a KEY=VALUE consumer never trips over the literal 'None'."""
    return "NA" if value is None else str(value)


def _all_runs(result: Any) -> dict[str, Any]:
    """Per-dimension mean over EVERY read (canonical + stability samples) of every judge that could see the
    images (all judges when none could): one VLM read is too noisy to gate on, `per_dim` is the canonical reads only."""
    judges = [j for j in result.judges if not j.blind] or list(result.judges)
    reads = [r for j in judges for r in [j.canonical, *j.stability_samples]]
    keys = list(result.per_dim)
    per_dim = (
        {k: round(sum(r.dims[k] for r in reads) / len(reads), 4) for k in keys}
        if reads
        else {}
    )
    mean = round(sum(per_dim.values()) / len(per_dim), 4) if per_dim else 0.0
    return {"runs": len(reads), "per_dim": per_dim, "mean": mean}


def _rationale(result: Any) -> str | None:
    """The `rationale` string of the first judge's canonical reply, when the caller's feature context asked for one."""
    from ai.judge import JudgeError, _extract_json_object

    for j in result.judges:
        try:
            text = _extract_json_object(j.canonical.raw).get("rationale")
        except JudgeError:
            continue
        if isinstance(text, str) and text:
            return text
    return None


def _gated(opts: _Opts) -> bool:
    return opts.min_dim is not None or opts.max_mean is not None


def _gate_failures(result: Any, runs: dict[str, Any], opts: _Opts) -> list[str]:
    """Why the verdict misses --min-dim / --max-mean (empty = pass or no gate). A blind verdict passes nothing."""
    if not _gated(opts):
        return []
    if result.blind:
        return ["blind verdict (no judge could see the images) cannot satisfy a gate"]
    out: list[str] = []
    if opts.min_dim is not None:
        low = {k: v for k, v in runs["per_dim"].items() if v < opts.min_dim}
        out.extend(f"{k} {v} < --min-dim {opts.min_dim:g}" for k, v in low.items())
    if opts.max_mean is not None and runs["mean"] > opts.max_mean:
        out.append(f"mean {runs['mean']} > --max-mean {opts.max_mean:g}")
    return out


def _render_text(
    result: Any, runs: dict[str, Any], rationale: str | None, failures: list[str] | None
) -> str:
    lines = [f"LABEL={result.label}"]
    if result.blind:
        # A blind (text-only) verdict is NOT a real visual score: mask the numeric fields
        # so a downstream consumer keying on MEAN (and ignoring LABEL) cannot swallow it as
        # a genuine score. The real numbers stay in --json, guarded by the blind flags.
        lines.append("MEAN=NA")
        lines.extend(f"DIM.{key}=NA" for key in result.per_dim)
        lines.append("RUNS_MEAN=NA")
        lines.extend(f"RUNS_DIM.{key}=NA" for key in result.per_dim)
    else:
        lines.append(f"MEAN={result.mean}")
        for key, val in result.per_dim.items():
            lines.append(f"DIM.{key}={val}")
        lines.append(f"RUNS_MEAN={runs['mean']}")
        for key, val in runs["per_dim"].items():
            lines.append(f"RUNS_DIM.{key}={val}")
    lines.append(f"RUNS={runs['runs']}")
    lines.append(f"BLIND={str(result.blind).lower()}")
    lines.append(f"SINGLE_JUDGE={str(result.single_judge).lower()}")
    lines.append(f"STABILITY_UNSTABLE={str(result.stability_unstable).lower()}")
    lines.append(f"CROSS_JUDGE_SPREAD={_na(result.cross_judge_spread)}")
    lines.append(f"JUDGES={len(result.judges)}")
    if rationale:
        lines.append(f"RATIONALE={rationale}")
    if failures is not None:
        lines.append(f"GATE={'FAIL' if failures else 'PASS'}")
    lines.extend(f"NOTE: {note}" for note in result.notes)
    return "\n".join(lines)


def run(argv: list[str]) -> int:
    if not argv:
        _print_usage()
        return 1
    if argv[0] in ("-h", "--help", "help"):
        _print_usage()
        return 0
    if len(argv) < 2:
        raise UsageError("judge needs <render> <reference>", command="judge")

    render_img = _existing(argv[0])
    reference_img = _existing(argv[1])
    opts = _parse_opts(argv[2:])
    rubric = _load_rubric(opts.rubric) if opts.rubric else None

    from ai.judge import judge

    backends = _resolve_backends(opts)
    result = judge(
        render_img,
        reference_img,
        backend=backends,
        rubric=rubric,
        judges=max(len(backends), 2),
        stability_n=opts.stability_n,
        feature_context=opts.feature_context,
    )
    runs = _all_runs(result)
    failures = _gate_failures(result, runs, opts)

    if opts.as_json:
        payload = result.to_jsonable()
        payload["all_runs"] = runs
        payload["rationale"] = _rationale(result)
        payload["gate"] = (
            {"status": "FAIL" if failures else "PASS", "failures": failures}
            if _gated(opts)
            else None
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            _render_text(
                result, runs, _rationale(result), failures if _gated(opts) else None
            )
        )
    if failures:
        raise GateFailure(
            ">>> JUDGE: FAIL — " + "; ".join(failures), command="judge", silent=False
        )
    return 0


COMMAND = Command(
    name="judge",
    group="QA & GATES",
    summary="VLM-as-a-judge: score a render vs a reference on an anchored rubric",
    usage=USAGE,
    run=run,
)
