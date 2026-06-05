# `3d validate` — fast parse-only syntax check

Checks whether an `.scad` file parses without error. No geometry is rendered, so it is fast and lightweight — ideal for editor-on-save hooks or CI linting.

**Why it exists.** A full CGAL render can take minutes on complex models. `validate` only runs the parser, so it catches syntax errors, missing includes, and broken `assert()` calls in seconds.

## Usage

```
3d validate <file.scad>
```

```bash
3d validate model.scad
```

## Exit codes

- `0` — syntax OK (and no `ERROR:` lines in output)
- `1` — parse error or `ERROR:` emitted

## Implementation notes

Uses `--export-format=echo` to force a parse without rendering. If the file contains `echo()` calls, their output is printed as a confirmation that the file was actually parsed.
