# `3d collision` — collision / penetration engine

Runs a generic collision / penetration check over a project configuration. Supports static analysis, per-frame animation checking, and visual overlap highlighting.

**Why it exists.** Mechanical assemblies can have parts that collide or penetrate. Detecting this early prevents broken prints and wasted design iterations.

## Usage

```
3d collision <config.json> [--frame] [--viz]
```

The `config.json` supplies: `pair_scad` (placement), `parts`, `phases`, `intended_contacts`, and thresholds (`eps_mm3`, `touch_tol_mm`, `contact_max_mm3`). All paths in the config are resolved **relative to the config file’s directory**.

### Modes

| Mode | Flag | What |
|---|---|---|
| Static | *(default)* | Every pair at every phase (`collision_check.py`) |
| Per-frame | `--frame` | Gate over the config’s timeline (`frame_check.py`) |
| Visualise | `--viz` | Render each phase with overlaps highlighted red (`collision_viz.py`) |

## Environment

- `PHASES_SEL="0,1"` (static) overrides the config’s phase list.
- `FRAMES=N` (frame mode) overrides the frame count.

```bash
3d collision project/verify/collision.json
3d collision project/verify/collision.json --frame
```

## Dependencies

Needs the Python mesh stack ([`trimesh`](GLOSSARY.md#trimesh), [`manifold3d`](GLOSSARY.md#manifold3d), `rtree`, `numpy`, `scipy`). `--viz` also needs `pyvista`.
