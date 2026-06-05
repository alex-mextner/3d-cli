# `3d projects` — manage the project registry

Registers project directories so `3d web` and cross-project tooling can find more than the
nearest `3d.yaml` from the current working directory.

## Usage

```
3d projects <subcommand>
```

| Subcommand | What |
|---|---|
| `list` | Show registered projects: name, added date, and path. |
| `add <path>` | Register a project directory. |
| `remove <path>` | Unregister a project directory. |

```bash
3d projects add ./my-bracket
3d projects list
3d projects remove ~/old-print
```

The registry lives under the shared config directory, `~/.config/3d-cli/`, or
`$XDG_CONFIG_HOME/3d-cli/`.
