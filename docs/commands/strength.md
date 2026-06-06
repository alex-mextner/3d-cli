# `3d strength`

`3d strength` validates a structural load case and prints the deterministic dry-run report
that a future solver will consume. It does not mesh, solve, or claim pass/fail structural
fitness yet; the current value is a safe, reviewable contract for agents, scripts, and CI.

The command also has the alias `3d structural-check`.

## Usage

```bash
3d strength <file.scad|.stl|.3mf> --material NAME --load NEWTONS [options]
```

Required:

- `--material NAME` resolves a material from `3d materials list`.
- `--load NEWTONS` sets a positive force in newtons.

Options:

- `--axis X|Y|Z` sets the load axis. Default: `Z`.
- `--fixture TYPE` sets the load fixture: `cantilever`, `simple`, or `compression`.
- `--safety-factor N` records the target safety factor metadata. Default: `2`.
- `--dry-run` makes the dry-run mode explicit. This is currently the only mode.
- `--json` emits the structured report as JSON.

## Examples

Print a readable dry-run report for a bracket:

```bash
3d strength bracket.scad --material PLA --load 25 --axis Z
```

Use the compatibility alias and a simple support fixture:

```bash
3d structural-check part.stl --material PETG --load 12.5 --fixture simple --json
```

Save a report before handing the load case to another tool:

```bash
3d strength bracket.scad --material PLA --load 25 --json > strength-report.json
```

Inspect the conservative material number in a shell pipeline:

```bash
cat strength-report.json | python3 -c 'import json,sys; print(json.load(sys.stdin)["material"]["controlling_strength_mpa"])'
```

Record a compression load case with a higher target factor:

```bash
3d strength spacer.3mf --material ABS --load 40 --fixture compression --safety-factor 3 --json
```

## Output

Text output is a readable checklist with the normalized input path, material, load axis,
fixture, target safety factor, conservative controlling strength, and planned checks.

JSON output includes:

- `status`: currently `DRY-RUN`.
- `verdict`: currently `NOT_EVALUATED`.
- `request`: file, material, load, axis, fixture, safety factor, and dry-run flag.
- `material`: material registry name, yield strength, layer-adhesion factor, and the
  conservative controlling strength.
- `steps`: the ordered solver stages the current dry run would feed into.
- `checks`: the planned solver stages, all marked `planned`.

The controlling strength is `yield_mpa * layer_adhesion`, matching the material registry's
cross-layer FDM knockdown. It is metadata for planning, not a solver result.
