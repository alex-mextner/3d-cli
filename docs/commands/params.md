# `3d params` — extract Customizer-style parameters

Extracts the tunable parameters declared in an `.scad` file (`name = value; // [min:max] description`) so they can be inspected, diffed, or fed into other tools.

**Why it exists.** Models often expose dozens of constants. Having a machine-readable list makes it easy to generate parameter tables, drive batch renders, or feed values into the match loop.

## Usage

```
3d params <file.scad> [--json]
```

```bash
3d params model.scad
3d params model.scad --json
```

## Output

Without `--json`, prints a human-readable list. With `--json`, emits a JSON object suitable for scripting.
