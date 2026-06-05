# `3d om` — query [object-model](GLOSSARY.md#object-model) annotations

Parses `.scad` comment annotations and prints the matching object-model nodes as JSON.
OpenSCAD ignores these comments, so annotated files remain normal `.scad` inputs.

## Usage

```
3d om <file.scad> <selector>
```

Supported annotations:

| Annotation | Meaning |
|---|---|
| `// @id <id>` | Unique node id. |
| `// @class <class...>` | One or more classes/tags. |
| `// @anchor <name> pos=[x,y,z] dir=[x,y,z] ...` | Named point/feature metadata. |
| `// @color <name-or-hex>` | Display color metadata. |

Supported selectors:

| Selector | Meaning |
|---|---|
| `#id` | Match one id. |
| `.class` | Match a class/tag. |
| `.class.other` | Match nodes with all listed classes. |

```bash
3d om model.scad '#valve'
3d om model.scad '.structural'
```

Transform operations and descendant selectors are reserved and currently rejected.
