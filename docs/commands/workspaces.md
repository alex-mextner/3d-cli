# `3d workspaces`

`3d workspaces` creates and reads named workspace metadata for the local web dashboard.
A workspace is a scan root plus optional known project directories. The command writes
only local JSON under `~/.config/3d-cli/workspaces.json`.

## Usage

```bash
3d workspaces <subcommand> [options]
```

Subcommands:

- `list [--json]` shows workspace names, roots, and project counts.
- `create <name> [--root DIR] [--project DIR ...] [--json]` creates a workspace rooted
  at `DIR`; the current directory is used when `--root` is omitted.
- `show <name> [--json]` shows one workspace and its project metadata.

## Examples

Create a workspace for a shop model directory:

```bash
3d workspaces create shop --root ~/models --project ~/models/bracket
```

Save workspace metadata as JSON:

```bash
3d workspaces show shop --json > workspace.json
```

Inspect configured workspace names in a shell pipeline:

```bash
3d workspaces list --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["workspaces"][0]["name"])'
```

List workspaces for a quick terminal check:

```bash
3d workspaces list
```

