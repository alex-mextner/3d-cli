# `3d init` — scaffold a 3d project

Creates or tops up a project directory with a valid `3d.yaml`, standard folders, and
optional agent-oriented support files. Re-running is intended to be idempotent: existing
project files are not clobbered.

## Usage

```
3d init [path] [options]
```

Common options:

| Option | What |
|---|---|
| `--name NAME` | Project name written to `project.name`. |
| `--printer NAME` | Default printer name for `3d.yaml`. |
| `--material NAME` | Default material name for `3d.yaml`. |
| `--units mm|cm` | Project units. |
| `--bed X,Y,Z` | Explicit build volume. |
| `--reference PATH` | Copy a reference image into `references/`. |
| `--no-git` | Skip `git init`. |
| `--no-mcp` | Skip `.mcp.json`. |
| `--no-skills` | Skip per-project `.claude/skills/`. |
| `--no-agents` | Skip `AGENTS.md` and `CLAUDE.md` symlink. |
| `--no-hooks` | Skip the pre-commit hook. |
| `--no-input`, `--yes`, `-y` | Non-interactive mode for CI and agents. |

```bash
3d init
3d init my-bracket --no-input
3d init --name pantheon --reference pantheon.jpg --printer X1C --no-input
```

## Notes

- The generated `3d.yaml` has an empty `parts:` map; users add real parts later.
- `3d init` can register the project so `3d web` and cross-project tools can discover it.
- Printer and material names come from the merged registries exposed by `3d printers` and
  `3d materials`.
