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

## Fit-table fields

Two optional fields feed `3d fits resolve` ([fits](fits.md)): `shrink_xy_pct` and `shrink_z_pct`, the linear
shrink of the printed part in percent. They are reported as the slicer's shrinkage compensation (`xy_scale` =
`100 / (100 - pct)`) and never change a clearance. The built-in materials ship `shrink_xy_pct` (PLA 0.3, PETG 0.4,
TPU 0.6, ASA 0.7, ABS 0.8; typical values, source in `lib/data/materials.yaml`) and no Z value; a material without
the field is reported as "no material correction", never as zero. `3d materials show` prints them when set.

## Registry Layers

Later layers override earlier ones:

1. Built-in defaults.
2. `~/.config/3d-cli/materials.yaml`.
3. `./materials.yaml` next to the active project `3d.yaml`.
