"""3d generate — text description + explicit dimensions -> VERIFIED parametric .scad.

Thin registry command: it parses argv (a description + `--dim name=value` flags and/or a
`--spec` JSON of named dimensions), then hands a `GenerateRequest` to the CADSmith-style
pipeline in `lib/ai/design.py` (generate -> validate -> render -> check -> fix). The
pipeline shells out to the real `3d` gates and returns an explicit proof label
(ok / diagnostic / failure). STDLIB-ONLY at import time; `ai.design` is lazy-imported.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from cli.registry import Command
from errors import GateFailure, InputNotFound, UsageError


@dataclass(slots=True)
class _Options:
    description: str | None = None
    dims: list[tuple[str, str]] = field(default_factory=list)
    spec: str | None = None
    backend: str | None = None
    config: str | None = None
    rounds: int = 3
    out: str = "generated.scad"
    json: bool = False

USAGE = """3d generate "<description>" [--dim name=value ...] [options]
  Turn a text description plus explicit named dimensions into a parametric OpenSCAD
  .scad that has actually been run through the gates. The backend writes the .scad;
  `3d validate` + `3d render` + `3d check` judge it; on a gate failure the exact error
  is fed back for another bounded round. The result carries an EXPLICIT proof label.

Proof labels (also the exit code):
  ok          renders + manifold + printable, and every requested dim is a constant. exit 0
  diagnostic  renders but a gate warns/skips/fails, a dim is missing, or OpenSCAD is
              absent so verification was skipped. exit 0
  failure     no valid render produced within the round budget. exit 1

Input:
  "<description>"       positional natural-language description of the part.
                        Example: 3d generate "a hollow box with a lid lip"
  --dim name=value      a required named dimension, injected as a top-level OpenSCAD
                        constant the model must declare and use. Repeatable. The value
                        is a raw OpenSCAD token (a number or a small expression).
                        Example: 3d generate "bracket" --dim width=40 --dim wall=2.4
  --spec FILE           read dims (and an optional description) from JSON. Shape:
                        {"description": "...", "dims": {"width": 40, "wall": 2.4}} — or a
                        flat object of dims. --dim flags and a positional description
                        override the spec.
                        Example: 3d generate --spec box.json -o box.scad

Backend / loop:
  --backend NAME        AI backend to write the .scad (claude, codex, opencode, ollama,
                        mock). Default: auto-pick the first available (claude-first).
                        Example: 3d generate "cube" --dim size=20 --backend codex
  --config PATH         AI config JSON (default: ~/.config/3d-cli/ai.json or
                        $THREED_AI_CONFIG). Example: 3d generate "cube" --dim size=20 --config ai.json
  --rounds N            max generate->fix rounds. Default: 3.
                        Example: 3d generate "gear" --dim teeth=12 --rounds 5

Output:
  -o, --out FILE        output .scad path. Default: generated.scad
                        Example: 3d generate "cube" --dim size=20 -o cube.scad
  --json                print the JSON summary (status, rounds, gate_results,
                        requested_dims, dims_present_in_scad) instead of the text report.
                        Example: 3d generate "cube" --dim size=20 --json

Examples:
  3d generate "a hollow box" --dim width=30 --dim depth=20 --dim height=16 --dim wall=2 -o box.scad
  3d generate "a round coaster with a rim" --dim diameter=90 --dim rim=3 --rounds 4
  3d generate --spec bracket.json -o bracket.scad --json
  # deterministic (no network): feed a known .scad via the mock backend
  THREED_AI_MOCK_RESPONSE="$(cat cube.scad)" 3d generate "cube" --dim width=20 --backend mock"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    opts = _parse_args(argv)
    description, dims = _resolve_inputs(opts)

    from ai.design import GenerateRequest, generate  # lazy: heavy-ish import graph

    result = generate(
        GenerateRequest(
            description=description,
            dims=dims,
            out_path=opts.out,
            rounds=opts.rounds,
            backend=opts.backend,
            config_path=opts.config,
        )
    )

    if opts.json:
        print(json.dumps(result.to_jsonable(), indent=2, sort_keys=True))
    else:
        _print_report(result)

    if result.status == "failure":
        raise GateFailure(">>> GENERATE: FAILURE", command="generate", silent=True)
    return 0


def _parse_args(argv: list[str]) -> _Options:
    opts = _Options()
    i, n = 0, len(argv)
    while i < n:
        a = argv[i]
        if a == "--dim":
            from ai.design import parse_dim_flag

            opts.dims.append(parse_dim_flag(_need_value(argv, i, a)))
            i += 2
        elif a == "--spec":
            opts.spec = _need_value(argv, i, a)
            i += 2
        elif a == "--backend":
            opts.backend = _need_value(argv, i, a)
            i += 2
        elif a == "--config":
            opts.config = _need_value(argv, i, a)
            i += 2
        elif a == "--rounds":
            opts.rounds = _need_int(argv, i, a)
            i += 2
        elif a in ("-o", "--out"):
            opts.out = _need_value(argv, i, a)
            i += 2
        elif a == "--json":
            opts.json = True
            i += 1
        elif a.startswith("-") and a != "-":
            print(USAGE)
            raise UsageError(f"unknown option '{a}'", command="generate")
        else:
            if opts.description is not None:
                raise UsageError(
                    "only one positional description is allowed "
                    f"(got a second: {a!r})",
                    command="generate",
                    remediation=['Quote the whole description: 3d generate "a hollow box" ...'],
                )
            opts.description = a
            i += 1
    return opts


def _resolve_inputs(opts: _Options) -> tuple[str, dict[str, str]]:
    """Merge the --spec file (if any) with CLI flags. CLI wins over the spec."""
    description = opts.description
    dims: dict[str, str] = {}

    if opts.spec is not None:
        spec_desc, spec_dims = _load_spec(opts.spec)
        if description is None:
            description = spec_desc
        dims.update(spec_dims)

    for name, value in opts.dims:
        dims[name] = value

    if not description:
        raise UsageError(
            "no description given",
            command="generate",
            remediation=['Provide a description: 3d generate "a hollow box" --dim width=30 ...'],
        )
    if not dims:
        raise UsageError(
            "no dimensions given",
            command="generate",
            remediation=["Pass at least one --dim name=value, or --spec FILE with dims."],
        )
    return str(description), dims


def _load_spec(path: str) -> tuple[str | None, dict[str, str]]:
    if not os.path.isfile(path):
        raise InputNotFound(path, command="generate")
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(
            f"could not read spec JSON: {exc}",
            command="generate",
            remediation=['Example: {"description": "a box", "dims": {"width": 30, "wall": 2}}'],
        ) from None
    if not isinstance(data, dict):
        raise UsageError("spec file must contain a JSON object", command="generate")

    description = data.get("description")
    if description is not None and not isinstance(description, str):
        raise UsageError("spec 'description' must be a string", command="generate")
    dims_field = data.get("dims")
    raw_dims: dict[str, object] = (
        dims_field if isinstance(dims_field, dict)
        else {k: v for k, v in data.items() if k != "description"}
    )
    return description, {str(k): _dim_token(str(k), v) for k, v in raw_dims.items()}


def _dim_token(name: str, value: object) -> str:
    """Convert a JSON dim value to a raw OpenSCAD token."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise UsageError(
        f"spec dim {name!r} must be a number, bool, or non-empty string",
        command="generate",
    )


def _print_report(result: object) -> None:
    from ai.design import GenerateResult

    assert isinstance(result, GenerateResult)
    tag = {"ok": "OK", "diagnostic": "DIAGNOSTIC", "failure": "FAILURE"}[result.status]
    rounds = f"rounds={result.rounds}"
    if result.winning_round != result.rounds:
        rounds += f" (best from round {result.winning_round})"
    print(f"generate: {tag}   backend={result.backend}   {rounds}")
    print(f"  scad: {result.scad_path}")
    print("  gates:")
    for gate in result.gate_results:
        detail = f"  {gate.detail}" if gate.detail else ""
        print(f"    {gate.name:<13} {gate.status.upper()}{detail}")
    print("  dimensions:")
    for name, value in result.requested_dims.items():
        mark = "present" if result.dims_present_in_scad.get(name) else "MISSING"
        print(f"    {name} = {value}   [{mark}]")
    for note in result.notes:
        print(f"  note: {note}")


def _need_value(argv: list[str], i: int, flag: str) -> str:
    if i + 1 >= len(argv):
        raise UsageError(f"option {flag} needs a value", command="generate")
    return argv[i + 1]


def _need_int(argv: list[str], i: int, flag: str) -> int:
    raw = _need_value(argv, i, flag)
    try:
        value = int(raw)
    except ValueError:
        raise UsageError(f"option {flag} needs an integer, got {raw!r}", command="generate") from None
    if value < 1:
        raise UsageError(f"option {flag} must be >= 1, got {value}", command="generate")
    return value


COMMAND = Command(
    name="generate",
    group="GEOMETRY & EXPORT",
    summary="text + dimensions -> verified parametric OpenSCAD (.scad) via an AI backend",
    usage=USAGE,
    aliases=("gen",),
    run=run,
)
