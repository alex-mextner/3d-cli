# `3d opdag` - operation graph inspection

Describes, plans, and queries a small JSON operation DAG for model build steps. This is a
dry planning surface: it validates dependency shape and prints deterministic summaries
without running geometry tools.

## Usage

```bash
3d opdag <subcommand> <graph.json> [options]
```

## Subcommands

| Subcommand | What |
|---|---|
| `describe <graph.json> [--json]` | Summarize roots, leaves, edge count, and layers. |
| `plan <graph.json> [--json]` | Print dependency-ordered build steps. |
| `query <graph.json> <node> [--json]` | Inspect one operation's dependencies, consumers, ancestors, descendants, and params. |
| `template` | Print a minimal graph JSON template. |

## Examples

```bash
3d opdag template > build.json
3d opdag describe build.json
3d opdag describe build.json --json
3d opdag plan build.json --json
3d opdag query build.json final
3d opdag query build.json final --json
```

## Graph Shape

```json
{
  "operations": [
    {"id": "base", "op": "cube", "deps": [], "params": {"size": [40, 20, 8]}},
    {"id": "cutout", "op": "difference", "deps": ["base"], "params": {"tool": "slot"}},
    {"id": "finished", "op": "union", "deps": ["cutout"], "params": {}}
  ]
}
```

Operation ids must be unique. Dependencies must point at existing ids, and cycles are
reported as structured usage errors.
