# `3d metrics` — inspect persisted metrics

Reads command metrics from the JSONL store under `~/.local/share/3d-cli/metrics/`, or
`$XDG_DATA_HOME/3d-cli/metrics/` when `XDG_DATA_HOME` is set.

## Usage

```
3d metrics <subcommand>
```

| Subcommand | What |
|---|---|
| `list` | Show metric JSONL files, record counts, and latest timestamp. |
| `show [--limit N] [--command NAME]` | Print metric records as deterministic JSON lines. |

```bash
3d metrics list
3d metrics show --limit 20
3d metrics show --command render --limit 5
```
