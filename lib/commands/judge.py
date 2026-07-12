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
from errors import InputNotFound, UsageError

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

Result labels:
  ok               >=2 sighted judges, stable, agreeing.
  low-confidence   single judge, unstable N-samples, or large cross-judge spread.
  blind            no judge backend can SEE images (text-only) — NOT a real visual score.

Examples:
  3d judge render.png photo.jpg
  3d judge render.png photo.jpg --backend claude --backend ollama
  3d judge render.png photo.jpg --stability-n 5 --json
  3d judge render.png photo.jpg --feature-context "columns; pediment; dome" """


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
            opts.backends.append(value if value is not None else _need_value(argv, i, arg))
            i += 1 if value is not None else 2
        elif arg == "--stability-n":
            raw = value if value is not None else _need_value(argv, i, arg)
            try:
                opts.stability_n = max(0, int(raw))
            except ValueError as exc:
                raise UsageError(f"--stability-n must be an integer, got {raw!r}", command="judge") from exc
            i += 1 if value is not None else 2
        elif arg == "--feature-context":
            opts.feature_context = value if value is not None else _need_value(argv, i, arg)
            i += 1 if value is not None else 2
        elif arg == "--config":
            opts.config = value if value is not None else _need_value(argv, i, arg)
            i += 1 if value is not None else 2
        elif arg == "--json":
            opts.as_json = True
            i += 1
        else:
            raise UsageError(f"unknown option '{arg}'", command="judge")
    return opts


def _resolve_backends(opts: _Opts) -> list[Any]:
    from ai import load_backend_config
    from ai.backends import resolve_backend

    cfg = load_backend_config(opts.config)
    # `judge` always attaches images, so the auto-pick (no --backend) prefers a sighted
    # backend over a text-only one. An explicit --backend is honored verbatim (a blind
    # choice is still surfaced by the judge's BLIND label).
    if not opts.backends:
        return [resolve_backend(config=cfg, prefer_vision=True)]
    return [resolve_backend(name, config=cfg) for name in opts.backends]


def _na(value: object) -> str:
    """None -> NA so a KEY=VALUE consumer never trips over the literal 'None'."""
    return "NA" if value is None else str(value)


def _render_text(result: Any) -> str:
    lines = [f"LABEL={result.label}"]
    if result.blind:
        # A blind (text-only) verdict is NOT a real visual score: mask the numeric fields
        # so a downstream consumer keying on MEAN (and ignoring LABEL) cannot swallow it as
        # a genuine score. The real numbers stay in --json, guarded by the blind flags.
        lines.append("MEAN=NA")
        for key in result.per_dim:
            lines.append(f"DIM.{key}=NA")
    else:
        lines.append(f"MEAN={result.mean}")
        for key, val in result.per_dim.items():
            lines.append(f"DIM.{key}={val}")
    lines.append(f"BLIND={str(result.blind).lower()}")
    lines.append(f"SINGLE_JUDGE={str(result.single_judge).lower()}")
    lines.append(f"STABILITY_UNSTABLE={str(result.stability_unstable).lower()}")
    lines.append(f"CROSS_JUDGE_SPREAD={_na(result.cross_judge_spread)}")
    lines.append(f"JUDGES={len(result.judges)}")
    for note in result.notes:
        lines.append(f"NOTE: {note}")
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

    from ai.judge import judge

    backends = _resolve_backends(opts)
    result = judge(
        render_img,
        reference_img,
        backend=backends,
        judges=max(len(backends), 2),
        stability_n=opts.stability_n,
        feature_context=opts.feature_context,
    )

    if opts.as_json:
        print(json.dumps(result.to_jsonable(), indent=2, sort_keys=True))
    else:
        print(_render_text(result))
    return 0


COMMAND = Command(
    name="judge",
    group="QA & GATES",
    summary="VLM-as-a-judge: score a render vs a reference on an anchored rubric",
    usage=USAGE,
    run=run,
)
