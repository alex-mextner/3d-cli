# `3d events` - append-only workflow event log

Records and inspects CLI/model workflow events in a JSON Lines store under the shared
3d data directory.

## Usage

```bash
3d events <subcommand> [options]
```

Subcommands are `record`, `list`, `query`, and `path`.

## Record

```bash
3d events record --type cli.render --subject examples/cube.scad --status pass
3d events record --type model.match --source agent --message "round accepted" --data round=2 --ts 2026-06-05T12:00:00+00:00
```

| Option | Default | What |
|---|---|---|
| `--type NAME` | required | Event type such as `cli.render` or `model.match` |
| `--source NAME` | `cli` | Event source |
| `--subject VALUE` | none | File, model, project, or workflow subject |
| `--status VALUE` | none | Status such as `pass`, `fail`, `start`, `stop`, or `note` |
| `--message TEXT` | none | Human-readable detail |
| `--data key=value` | none | Structured data field; repeatable |
| `--ts ISO` | current UTC time | Explicit ISO-8601 timestamp |

## List And Query

```bash
3d events list --type cli.render --source cli --status pass --since 2026-06-05T00:00:00+00:00 --limit 10
3d events query --subject examples/cube.scad
3d events path
```

| Filter | Default | What |
|---|---|---|
| `--type NAME` | any | Match event type |
| `--source NAME` | any | Match event source |
| `--subject VALUE` | any | Match subject exactly |
| `--status VALUE` | any | Match status exactly |
| `--since ISO` | none | Only events at or after the timestamp |
| `--limit N` | `20` for `list`, unlimited for `query` | Maximum number of events |

`list` prints a table. `query` prints JSON Lines.
