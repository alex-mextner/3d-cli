# `3d profiles` — slicer presets in the repo, synced both ways

The user presets of OrcaSlicer, Snapmaker Orca and Bambu Studio (filament, process and machine
presets the user saved — not the vendor system presets) live in this repo as
`profiles/<slicer>/<kind>/<name>.json`, next to a `.info` when the preset has a parent system
preset. `3d profiles` keeps them in sync with the slicers. Calibrations write calibrated values
into the repo copy (`apply_values`, below) and a sync pushes them into the slicer.
Issue: [#180](https://github.com/alex-mextner/3d-cli/issues/180), PrintOps decision D10.

## Usage

```
3d profiles status [--slicer S] [--json]
3d profiles diff [NAME] [--slicer S]
3d profiles sync [--slicer S] [--pull | --push] [--dry-run] [--yes] [--force]
```

- `status` — per preset, what a sync would do; ends with `clean` when both sides match.
  Writes nothing.
- `diff [NAME]` — unified diff repo -> slicer of every preset that differs, or only of `NAME`
  (the file name without `.json`, or its `<kind>/<file>` path).
- `sync` — apply pulls and pushes. Without `--yes` it asks first on a terminal and refuses
  without one.
- `--slicer S` — `orca`, `snapmaker-orca`, `bambu` or `all` (default). A slicer that has never
  run on this machine is reported as not found and skipped; naming it explicitly to `sync` is an
  error (exit 2).
- `--pull` — apply only slicer -> repo changes; `--push` only repo -> slicer changes.
- `--dry-run` — print the plan, write nothing.
- `--yes` — apply without asking.
- `--force` — write into a slicer's presets even while it runs.
- `--json` — `status` as JSON: `{"clean": bool, "slicers": [{"slicer", "found", "user_dir",
  "repo_dir", "items": [{"rel", "name", "action", "reason"}]}]}`.

Exit codes: 0 ok; 1 conflicts were left, the slicer is running, or the prompt was declined;
2 bad arguments (or `sync` needs `--yes` without a terminal).

Examples:

```
3d profiles status
3d profiles status --slicer snapmaker-orca --json
3d profiles diff "0.32mm Standard @Snapmaker U1 (0.4 nozzle)"
3d profiles sync --dry-run
3d profiles sync --pull --yes
3d profiles sync --slicer bambu --push --yes --force
```

## How the sync decides

`~/.config/3d-cli/profiles-sync.json` records, per repo dir and slicer dir, a digest of every
preset as it was at the last sync. Against that base:

| repo | slicer | result |
|---|---|---|
| unchanged | changed / new / deleted | **pull** into the repo |
| changed / new / deleted | unchanged | **push** into the slicer |
| changed | changed differently | **conflict** |
| equal on both sides | | in sync |

A preset present on both sides with different content and no base (the first sync) is a
conflict too. Conflicts are printed as a unified diff and never resolved by the tool: make both
sides equal — edit the repo file, or change the preset in the slicer — and sync again.

## Where the presets are

On Linux the slicer data dir is `$XDG_CONFIG_HOME/<App>` (default `~/.config/<App>`), else the
Flatpak `~/.var/app/<app-id>/config/<App>` — the first one the slicer has run in (it has a `user/`
dir); on macOS it is `~/Library/Application Support/<App>`:

| `--slicer` | `<App>` | Flatpak app ids |
|---|---|---|
| `orca` | `OrcaSlicer` | `com.orcaslicer.OrcaSlicer`, `io.github.softfever.OrcaSlicer` |
| `snapmaker-orca` | `Snapmaker_Orca` | `io.github.Snapmaker.Snapmaker_Orca` |
| `bambu` | `BambuStudio` | `com.bambulab.BambuStudio` |

User presets are in `<data>/user/<folder>/{filament,process,machine}/` and their `base/`
subdirs, where `<folder>` is `app.preset_folder` from `<data>/<App>.conf` (a cloud account id
when logged in) or `default`. `$THREED_PROFILES_DIR` replaces the repo `profiles/` dir.

## What is kept out of the repo

- Printer connection keys — `print_host`, `print_host_webui`, `printhost_apikey`,
  `printhost_cafile`, `printhost_user`, `printhost_password`, `printhost_port` (the set Orca
  itself does not carry over from a project). The repo copy omits them; a push keeps the
  slicer's values. A repo file that contains one is refused.
- `.info` sync state — `sync_info`, `user_id`, `setting_id`, `updated_time` are cloud state of
  this account; the repo `.info` holds only `base_id`. A push writes the full `.info` the way a
  save in the slicer does: it keeps the account fields, stamps `updated_time`, and marks an
  already cloud-synced preset `sync_info = update`. A repo deletion of a cloud-synced preset
  leaves its `.info` marked `sync_info = delete`, as the slicer does.

Everything else is copied byte for byte. When a file has to be rewritten (to put connection
keys back, or by `apply_values`) it keeps the slicer's indent (4 spaces in Snapmaker Orca 2.x
and Bambu Studio, a tab in current upstream OrcaSlicer) and sorted key order.

## Safety

- Every push first copies the slicer files it replaces or removes to
  `~/.cache/3d-cli/profiles-backup/<UTC time>/<slicer>/` (`$XDG_CACHE_HOME` honoured) and
  prints that path.
- A sync that would push into a slicer refuses while that slicer runs (it rewrites presets from
  memory when the user saves), checked with `ps` before anything is written; `--force` skips the
  check. Pulls only read the slicer dir.

## Library: `apply_values`

```python
from profiles.values import apply_values

diff = apply_values("snapmaker-orca", "filament", "My PLA",
                    {"filament_flow_ratio": 0.97, "pressure_advance": 0.028})
```

Sets option values in the repo preset `profiles/<slicer>/<kind>/[base/]<name>.json` and returns
the unified diff (`""` when nothing changed); `dry_run=True` returns the diff without writing.
Values are stored as the slicer stores them: strings, and a scalar given for an option the
preset holds as a list (per extruder) is written to every element; a new per-extruder option
must be passed as a list. The preset identity (`name`, `from`, `inherits`, `version`) and the
connection keys are refused. Run `3d profiles sync --push` afterwards to put the change into the
slicer.
