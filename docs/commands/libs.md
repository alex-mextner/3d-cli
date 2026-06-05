# `3d libs` — [OpenSCAD](GLOSSARY.md#openscad) library info

Prints the path to the auto-installed OpenSCAD libraries ([`BOSL2`](GLOSSARY.md#bosl2), [`NopSCADlib`](GLOSSARY.md#nopscadlib)) and lists what is currently installed. Libraries are cloned into `libs/` automatically on the first `3d` invocation; `OPENSCADPATH` is auto-exported so `include <BOSL2/std.scad>` just works.

**Why it exists.** You should not need to manually clone BOSL2 or set `OPENSCADPATH`. This command lets you verify what is installed and export the path in your own shell if needed.

## Usage

```
3d libs <subcommand>
```

| Subcommand | What |
|---|---|
| `path` | Print the `OPENSCADPATH` line to export |
| `list` | Show installed libraries |

```bash
3d libs list
export $(3d libs path)
```

## Notes

- `libs install` was removed — libraries auto-install on first run.
- To force a re-install: `rm ~/.config/3d-cli/.bootstrapped && 3d help`
