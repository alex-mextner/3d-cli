# `3d axis` - validate axes, planes, views, and cameras

Normalizes axis, section plane, named view, and OpenSCAD camera-vector inputs without
rendering.

## Usage

```bash
3d axis <axis|plane|view|camera> <value> [--json]
```

| Kind | Accepted values | What |
|---|---|---|
| `axis` | `X`, `Y`, `Z`, `+X`, `-X`, `+Y`, `-Y`, `+Z`, `-Z`, `left`, `right`, `front`, `back`, `top`, `bottom` | Normalized signed axis and unit vector |
| `plane` | `YZ`, `XZ`, `XY` | Section plane and normal axis/vector |
| `view` | `front`, `back`, `left`, `right`, `top`, `bottom`, `iso`, `3-4`, `front-left`, `front-right`, `rear-left`, `rear-right` | Named render view direction |
| `camera` | `ex,ey,ez,cx,cy,cz` | OpenSCAD eye/center camera vector and direction |

| Option | Default | What |
|---|---|---|
| `--json` | off | Print compact deterministic JSON |

```bash
3d axis axis -Z
3d axis plane YZ --json
3d axis view front-right
3d axis camera 1,-1,1,0,0,0
```
