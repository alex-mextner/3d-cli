# `3d pack` - deterministic print-bed layout planning

Plans a rectangular print-bed layout from explicit 2D part footprints. The command is
intentionally deterministic: it uses a simple shelf layout so scripts, agents, and e2e
tests can compare stable coordinates before a full nesting solver exists.

## Usage

```bash
3d pack --bed WxD --part name=WxD[:qty] [options]
```

| Option | Default | What |
|---|---|---|
| `--bed WxD` | required | Print-bed width and depth in millimeters, for example `220x220` |
| `--part name=WxD[:qty]` | required | Part footprint in millimeters; repeat for multiple part types |
| `--gap MM` | `0` | Clearance between placed bounding boxes |
| `--no-rotate` | off | Disable 90-degree rotation when a part only fits rotated |
| `--json` | off | Emit deterministic machine-readable JSON |

## Examples

```bash
3d pack --bed 220x220 --part bracket=60x40 --part clip=30x20:4
3d pack --bed 180x180 --gap 5 --part hinge=75x30:2 --json
3d pack --bed 220x220 --no-rotate --part bracket=60x40
3d pack --bed 120x80 --gap 4 --part bracket=50x30:2 --json | jq '.placements | length'
```

The default table is for humans reviewing a plate plan. Use `--json` when a shell script
or agent needs the bed dimensions, clearance, coordinates, copy indexes, and rotation
status for the next step.

`3d pack` validates that every part can fit on the bed, all dimensions are finite positive
millimeter values, quantities are positive integers, and the requested gap is not negative.
It returns a structured usage error instead of a traceback when a layout cannot be placed.
