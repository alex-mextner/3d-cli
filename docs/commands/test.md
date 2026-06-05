# `3d test` — run the test gate

Runs the project's test suite: `ruff check lib/ tests/`, then `pytest` (unit tests + CLI smoke harness), then `mypy` (type checking over `bin/3d + lib/ + tests/`). All three must pass for exit 0.

**Why it exists.** The CLI is a single codebase with many moving parts (registry, pyrun, imaging, gates). A single `3d test` command guarantees that new commands, aliases, and refactors do not break the help surface, lint gate, or type system.

## Usage

```
3d test [pytest-args...]
```

Extra arguments are forwarded to `pytest`.

```bash
3d test
3d test -k registry           # only the registry tests
3d test -x -q
```

## Implementation notes

Delegates to `tests/run_gate.py` through `pyrun` so `ruff`, `pytest`, and `mypy` resolve via the same `.venv` / `uv` / system tiers as every other Python tool. The gate also needs `fastapi`, `uvicorn`, `markdown`, `pyyaml`, and `httpx` importable for the web tests and mypy coverage of `lib/web/`.
