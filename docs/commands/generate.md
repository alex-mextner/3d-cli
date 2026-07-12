# `3d generate` - text + dimensions -> verified parametric OpenSCAD

Turn a natural-language description plus explicit named dimensions into a parametric
OpenSCAD `.scad` that has actually been put through the gates. Unlike `fit-niche` (pure
parametric geometry), `generate` calls an AI backend to write the model, then runs the
CADSmith-style loop **generate -> validate -> render -> check -> fix**: the backend emits a
`.scad`, the deterministic `3d` gates judge it, and on a gate failure the exact error text
is fed back into the next bounded round (the best rendering candidate is kept). The output
carries an **explicit proof label** — `ok`, `diagnostic`, or `failure` — never a bare "it
ran".

## Usage

```bash
3d generate "<description>" [--dim name=value ...] [options]
```

The description says what the part is; each `--dim name=value` is a required dimension that
is injected into the prompt as a top-level OpenSCAD constant the model must declare and use.
After generation the command verifies that every requested dimension actually appears as a
constant assignment in the emitted `.scad` (a real scan, not a promise).

## Proof labels

The label is both printed and used as the process exit code:

| Label | Meaning | Exit |
|---|---|---|
| `ok` | Renders, manifold + printable both PASS, and every requested dim is a constant. | 0 |
| `diagnostic` | Renders but a gate warns/skips/fails, a requested dim is missing, or OpenSCAD is absent so verification was skipped. | 0 |
| `failure` | No valid render produced within the round budget. | 1 |

## Options

| Option | Default | What |
|---|---|---|
| `--dim name=value` | none | A required named dimension, injected as a top-level constant. Repeatable. Value is a raw OpenSCAD token (a number or a small expression). |
| `--spec FILE` | none | Read dims (and an optional description) from JSON. `--dim` flags and a positional description override the spec. |
| `--backend NAME` | auto (claude-first) | AI backend that writes the `.scad`: `claude`, `codex`, `opencode`, `ollama`, `mock`. |
| `--config PATH` | `~/.config/3d-cli/ai.json` | AI config JSON (also `$THREED_AI_CONFIG`). |
| `--rounds N` | `3` | Maximum generate->fix rounds. |
| `-o, --out FILE` | `generated.scad` | Output `.scad` path. |
| `--json` | off | Print the JSON summary instead of the text report. |

## JSON summary

`--json` prints a stable object:

```json
{
  "status": "ok",
  "rounds": 1,
  "winning_round": 1,
  "scad_path": "box.scad",
  "backend": "mock",
  "requested_dims": {"width": "30", "depth": "20", "height": "16", "wall": "2"},
  "dims_present_in_scad": {"width": true, "depth": true, "height": true, "wall": true},
  "gate_results": [
    {"name": "manifold", "status": "pass", "detail": "1 file(s) clean (mesh-verified)"},
    {"name": "printability", "status": "pass", "detail": "PRINTABILITY: PASS"}
  ],
  "notes": []
}
```

`rounds` is the total number of rounds attempted; `winning_round` is the specific round
whose `.scad` is the one reported and left on disk (they differ when an earlier round was
the best candidate and later rounds failed to beat it). `dims_present_in_scad` reflects a
real scan: a dimension only counts when it appears as a **top-level** constant assignment
(not commented out, not a `==` comparison, not a local inside a `module { ... }` body).

## Examples

```bash
3d generate "a hollow box" --dim width=30 --dim depth=20 --dim height=16 --dim wall=2 -o box.scad
3d generate "a round coaster with a rim" --dim diameter=90 --dim rim=3 --rounds 4
3d generate --spec bracket.json -o bracket.scad --json
3d generate "cube" --dim size=20 --backend codex
```

A `--spec` JSON file supplies the dims (and an optional description); `--dim` flags and a
positional description override it:

```json
{
  "description": "a hollow box",
  "dims": {"width": 30, "depth": 20, "height": 16, "wall": 2}
}
```

## Deterministic / offline runs

Tests and reproducible runs use the **mock backend** instead of a real model — set
`$THREED_AI_MOCK_RESPONSE` to a known `.scad` and pass `--backend mock`:

```bash
THREED_AI_MOCK_RESPONSE="$(cat cube.scad)" 3d generate "cube" --dim width=20 --backend mock -o cube.scad
```

The mock never touches the network and returns its canned response verbatim, so the loop,
the gate wiring, and the dims-present check can be exercised without an API key or a live
model.

## What the mock path proves (and what it does not)

The deterministic mock path proves the **plumbing**: a `.scad` is written, `validate` +
`render` + `check` run, the per-gate breakdown is parsed, the dims-present check is real,
and the correct status label is emitted. It does **not** prove that a real backend produces
a *good* model from an arbitrary description — that depends on the backend and is what the
`ok`/`diagnostic`/`failure` label exists to report honestly on each real run.

## See also

- [`validate`](validate.md) - the parse gate the loop runs first.
- [`render`](render.md) - the render gate.
- [`check`](check.md) - the manifold/printability master gate.
- [`fit-niche`](fit-niche.md) - pure parametric insert generation (no AI backend).
