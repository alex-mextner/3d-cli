# `3d kinematics` - project joint spec validation

Validates the `kinematics.joints` section in a `3d.yaml` project and prints a stable JSON
summary. This is a dry, deterministic planning surface for scripts and agents before
motion simulation exists.

## Usage

```bash
3d kinematics <3d.yaml|project-dir>
```

The command accepts either the project file or the directory that contains it.

## Joint Schema

```yaml
kinematics:
  joints:
    shoulder:
      type: revolute
      parent: base
      child: arm
      axis: [0, 0, 1]
      origin: [0, 0, 0]
      limits: [-90, 90]
```

Supported joint types:

| Type | Required fields | Limits units |
|---|---|---|
| `revolute` | `parent`, `child`, `axis`, `limits` | degrees |
| `prismatic` | `parent`, `child`, `axis`, `limits` | project units |
| `fixed` | `parent`, `child` | `none` |

`parent` and `child` must reference parts declared under `parts:`. Moving joint axes are
normalized in the JSON summary, and all output keys are sorted for repeatable shell use.

## Examples

```bash
3d kinematics 3d.yaml
3d kinematics ./robot-arm > robot-kinematics.json
```
