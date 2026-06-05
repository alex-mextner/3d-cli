# `3d materials` — inspect [FDM](GLOSSARY.md#fdm) material properties

Shows the material names and properties that projects reference in `3d.yaml`. The registry
is layered so users and projects can override built-in defaults field-by-field.

## Usage

```
3d materials <subcommand>
```

| Subcommand | What |
|---|---|
| `list` | Table of material names, density, max service temperature, and finish. |
| `show <name>` | Full property view for one material, including mechanical values and layer-adhesion factor. |

```bash
3d materials list
3d materials show PLA
3d materials show PETG
```

## Registry Layers

Later layers override earlier ones:

1. Built-in defaults.
2. `~/.config/3d-cli/materials.yaml`.
3. `./materials.yaml` next to the active project `3d.yaml`.
