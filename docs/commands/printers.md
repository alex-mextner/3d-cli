# `3d printers` — inspect the printer registry

Shows printer names and bed/nozzle metadata used by `3d.yaml`, slicing, and project
checks.

## Usage

```
3d printers <subcommand>
```

| Subcommand | What |
|---|---|
| `list` | List known printer names with build volume, nozzle, and firmware. |
| `show <name>` | Print one printer's spec sheet. |

```bash
3d printers list
3d printers show "Prusa MK4"
```

## Registry Layers

Later layers override earlier ones:

1. Built-in defaults.
2. `~/.config/3d-cli/printers.yaml`.
3. `./printers.yaml` next to the active project `3d.yaml`.
