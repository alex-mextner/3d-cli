# `3d lint` — advisory repository lint rules

Runs lightweight advisory lint rules. This is not the full test gate; use `3d test` for
pytest and mypy.

## Usage

```
3d lint [--all | paths...] [--json] [--rule RULE]
```

| Option | What |
|---|---|
| `--all` | Scan `lib/*.py` and `lib/**/*.py`. |
| `--json` | Print machine-readable findings. |
| `--rule RULE` | Run one rule. Current accepted rule: `no-subject-leakage`. |

```bash
3d lint --all
3d lint lib/preprocess_reference.py
3d lint --all --rule no-subject-leakage --json
```

Exit codes: 0 for no findings, 1 for findings, 2 for invalid invocation.
