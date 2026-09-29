# `3d openscad` — install [OpenSCAD](GLOSSARY.md#openscad) libraries for plain OpenSCAD

`3d openscad libs` installs OpenSCAD libraries into OpenSCAD's **user library folder**,
which OpenSCAD searches on every run — in the app and on the command line, from any
working directory, with no `OPENSCADPATH`:

| OS | User library folder |
|---|---|
| macOS | `~/Documents/OpenSCAD/libraries` |
| Linux | `$XDG_DATA_HOME/OpenSCAD/libraries` (default `~/.local/share/OpenSCAD/libraries`) |
| Windows | `%USERPROFILE%\Documents\OpenSCAD\libraries` |

**Why it exists.** [`3d libs`](libs.md) only feeds `3d`'s own renders (the repo `libs/`
on an `OPENSCADPATH` that `3d` exports for its subprocesses). A model opened in the
OpenSCAD app, or rendered with a bare `openscad -o part.3mf part.scad`, needs its
libraries where OpenSCAD itself looks. OpenSCAD has no official package manager
([openscad/openscad#3479](https://github.com/openscad/openscad/issues/3479)); the
community ones (scadm, scadman, scadder) install into a per-project folder from a
manifest, and olman is a Linux snap — none installs into the user library folder or can
ship a library that lives in this repo, so `3d` does it directly (stdlib + `git`).

## Usage

```
3d openscad libs <subcommand>
```

| Subcommand | What |
|---|---|
| `list` | Known libraries, install state, and the folder OpenSCAD would load each from |
| `path` | Print the user library folder and OpenSCAD's search order (`OPENSCADPATH` first) |
| `<name> install [--git URL] [--ref REF] [--dir DIR] [--force]` | Install (idempotent) |
| `<name> uninstall [--dir DIR]` | Remove a library 3d installed (alias: `remove`) |
| `<name> update [--dir DIR]` | Reinstall from the recorded source (fresh git clone / this checkout) |
| `<name> where [--dir DIR]` | Print the folder OpenSCAD loads `<name>` from (alias: `path`); exit 1 if none |

| Option | What |
|---|---|
| `--git URL` | Install a library that is not in the registry from a git repository; `<name>` becomes the folder name used in `use <name/...>` |
| `--ref REF` | Git branch or tag to install instead of the default branch (git libraries only) |
| `--dir DIR` | Library folder to use instead of the user library folder (e.g. a folder on your `OPENSCADPATH`); pass the same `--dir` to `uninstall`/`update`/`where` |
| `--force` | Reinstall, or replace an existing folder of the same name |

```bash
3d openscad libs list
3d openscad libs path
3d openscad libs WriteText install
3d openscad libs WriteText where
3d openscad libs WriteText update
3d openscad libs WriteText uninstall
3d openscad libs BOSL2 install --ref v2.0.763
3d openscad libs BOSL2 install --ref v2.0.763 --force   # switch an existing install to that tag
3d openscad libs MyLib install --git https://github.com/me/MyLib.git
3d openscad libs WriteText install --dir ~/scad-libs --force
3d openscad libs WriteText uninstall --dir ~/scad-libs
```

Library names are case-insensitive (`writetext` installs `WriteText`); the installed
folder always uses the canonical name, because that is what `use <WriteText/...>` needs.

## Known libraries

| Name | Source | What |
|---|---|---|
| `WriteText` | this repo, `openscad-libs/WriteText/` | Text on cylinders/cones via native `text()` (Cyrillic OK); successor of [Write.scad by HarlanDMii](https://www.thingiverse.com/thing:16193), CC BY 3.0 |
| `BOSL2` | https://github.com/BelfrySCAD/BOSL2 | Shapes, attachments, threading, rounding |
| `NopSCADlib` | https://github.com/nophead/NopSCADlib | Vitamins (screws, bearings, electronics) and printed parts |
| `Round-Anything` | https://github.com/Irev-Dev/Round-Anything | Rounded polygons and fillets |
| `threads-scad` | https://github.com/rcolyer/threads-scad | ISO metric threads, screws and nuts |

## How installs are tracked

Each install writes `.3d-openscad-lib.json` into the library folder: source (git URL +
ref, or the repo path it was copied from), commit, time, and the list of files copied.

- `install` never touches a folder of the same name that 3d did not install unless you
  pass `--force`; `list` marks such folders `present (not installed by 3d)`.
- `uninstall` deletes exactly the recorded files; a folder that still holds files you
  added is kept and reported. This also works where macOS privacy protection denies
  listing `~/Documents` to the terminal.
- `update` re-clones git libraries (same URL/ref) or re-copies repo libraries, dropping
  files that disappeared upstream.
- Git libraries are shallow-cloned into a temporary folder under
  `~/.local/share/3d-cli/openscad-libs/` (honors `$XDG_DATA_HOME`); their files, minus
  hidden folders such as `.git`/`.github`, are copied into the library folder and the
  clone is removed.
- Library names must be a single folder name (letters, digits, `.`, `_`, `+`, `-`); `/`
  and `..` are rejected, so nothing outside the library folder is ever written or deleted.
  `uninstall` also ignores manifest entries that point outside the library folder.
- If an `OPENSCADPATH` folder already has a library of the same name, OpenSCAD loads that
  copy first; `install` warns about it. The repo `libs/` that `3d` itself puts on
  `OPENSCADPATH` for its own renders is not part of this check or of `path`/`where`.
