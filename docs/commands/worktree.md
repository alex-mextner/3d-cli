# `3d worktree`

`3d worktree` creates agent git worktrees with the project dev environment already
bootstrapped. It exists because raw `git worktree add` leaves a fresh checkout without the
right `.venv`, so pre-commit hooks and `3d test` can fail before `ruff`, `pytest`, or
`mypy` are even available.

## Usage

```bash
3d worktree <subcommand> [options]
```

Subcommands:

- `create <branch> [--path DIR] [--base REF] [--json] [--no-sync]` creates a worktree and
  runs `uv sync --extra dev` in it.
- `doctor [DIR] [--json]` verifies that a worktree has `.venv/bin/ruff`,
  `.venv/bin/pytest`, and `.venv/bin/mypy`.
- `list [--json]` lists git worktrees known to this repository.

## Examples

Create a default agent worktree under `~/.config/superpowers/worktrees/3d-cli/`:

```bash
3d worktree create roadmap/e2e-expansion --base main
```

Create a worktree at an explicit path:

```bash
3d worktree create roadmap/fit-camera-video --path /tmp/3d-cli-fit-video
```

Check a worktree before handing it to an agent:

```bash
3d worktree doctor /tmp/3d-cli-fit-video --json
```

List active worktrees as JSON for cleanup scripts:

```bash
3d worktree list --json
```

## Agent rule

Agents must use this command for new worktrees. Do not run raw `git worktree add` unless
you are repairing this command itself. The expected bootstrap is:

```bash
3d worktree create <branch> --base main
cd ~/.config/superpowers/worktrees/3d-cli/<branch-with-slashes-replaced>
3d worktree doctor .
```
