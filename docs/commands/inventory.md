# `3d inventory` - local materials and parts inventory

Keeps a small JSON inventory under the 3d config directory so agents and scripts can
check available spools, stock parts, and locations before planning a print.

## Usage

```bash
3d inventory <subcommand>
```

Subcommands are `list`, `add`, and `show`.

## Add Items

```bash
3d inventory add material PLA --qty 1 --unit spool --location "bin 2"
3d inventory add part "M3 nut" --qty 25 --material steel --notes "drawer A"
```

| Option | Applies to | What |
|---|---|---|
| `--qty N` | material, part | Required positive quantity |
| `--unit U` | material, part | Unit; parts default to `pcs` |
| `--location TEXT` | material, part | Storage location |
| `--material TEXT` | part | Part material or material family |
| `--notes TEXT` | material, part | Human-readable note |

## List And Show

```bash
3d inventory list
3d inventory list materials
3d inventory list parts
3d inventory show part "M3 nut"
```

`list` prints a table grouped by materials and parts. `show` prints one record with all
stored fields.
