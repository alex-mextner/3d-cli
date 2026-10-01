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
| `list` | Known libraries, install state, the folder OpenSCAD would load each from, and any [leftover staging folders](#concurrent-runs-and-interrupted-installs) |
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
| `--force` | Reinstall, or replace an existing folder of the same name. The new copy is fetched first: if that fails, the existing install stays as it was |

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
  files that disappeared upstream and keeping files you added.
- `update` and `install --force` fetch the new copy before touching the old one: it is
  built with its manifest in a hidden `.<name>.3d-staging-*` folder next to the library,
  then renamed into place. If the clone fails (offline, dead URL, a `--ref` that does not
  exist) the command exits non-zero and the installed files and manifest stay exactly as
  they were. Once the new copy is in place, a folder 3d did not install (replaced with
  `--force`) is deleted; a 3d install loses only its recorded files, and files you added
  move into the new copy. Should one clash with a file the new version ships, the previous
  copy (holding yours) stays in the staging folder and its path is printed.
- Git libraries are shallow-cloned into a temporary folder under
  `~/.local/share/3d-cli/openscad-libs/` (honors `$XDG_DATA_HOME`); their files, minus
  hidden folders such as `.git`/`.github`, are copied into the staging folder and the
  clone is removed.
- Library names must be a single folder name (letters, digits, `.`, `_`, `+`, `-`); `/`
  and `..` are rejected, so nothing outside the library folder is ever written or deleted.
  `uninstall` also ignores manifest entries that point outside the library folder.
- `update` and `install --force` replace the library folder, so the new one is given the
  group and mode of the folder it replaces (for example group-write on a shared folder),
  and on macOS its ACL entries. Each is best effort and independent: one that 3d cannot
  copy is skipped, never an error. Times and extended attributes are not copied.
- If an `OPENSCADPATH` folder already has a library of the same name, OpenSCAD loads that
  copy first; `install` warns about it. The repo `libs/` that `3d` itself puts on
  `OPENSCADPATH` for its own renders is not part of this check or of `path`/`where`.

## Concurrent runs and interrupted installs

**One change to a library at a time.** `install`, `update` and `uninstall` hold a
per-library lock: an `flock` on `<3d data dir>/openscad-libs/locks/<name>.lock` (under
`~/.local/share/3d-cli/`, honoring `$XDG_DATA_HOME`; it covers every `--dir` for that
name, for runs by the same user: another user working on a shared `--dir` has their own
lock file). A run that finds the library busy prints
``waiting for another `3d openscad libs` run on <name> to finish ...`` on stderr and
carries on when the other run ends, so the outcome is always one of the two sequential
orders: an `uninstall` that arrives during an `update` runs after it, and the library ends
up uninstalled. The operating system drops the lock when a process ends (a crash, Ctrl-C
or SIGKILL included), so a dead run never blocks the next one and there is no lock file to
clean up.

**Leftovers of a run that was killed.** A run that dies mid-install leaves its hidden
`.<name>.3d-staging-*` folder in the library folder and, for git libraries, a clone in
`<3d data dir>/openscad-libs/<name>-*`. The next `install`, `update` or `uninstall` of that
library takes the lock (waiting for a live run to end first), then removes them and prints
`removed <path>: left by an interrupted install of <name>`; a folder that cannot be removed
is reported as `could not remove <path> (<reason>); delete it yourself`. Only folders with
exactly that naming are touched, and a staging folder is deleted only when it holds nothing
but the half-built copy: every file in it is one that copy's own manifest lists (the manifest
is written first, so a run killed mid-copy leaves one; a folder with files but no manifest,
such as one left by 3d 0.3.0 before it wrote the manifest first, is kept). One that holds
more (the previous copy of the library, because the run died between setting the old copy
aside and moving the new one in or because files of yours clashed with the new version, or
any file 3d did not create, even one you added inside the half-built copy) is never deleted: it is
printed as `kept <path>: it holds more than a half-built copy of <name> ...` and stays until
you look through it and delete it yourself. `3d openscad libs list` reports the folders left in
the user library folder without changing anything.
