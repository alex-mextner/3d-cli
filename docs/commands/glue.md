# `3d glue` — glue-stick reminder per printer

A glue-stick layer on a PEI plate holds PLA for a few prints, then it is worn, wiped thin or dusty. `3d glue`
remembers when a printer's bed was last glued and says when a fresh layer is due
([#762](https://github.com/alex-mextner/3d-cli/issues/762): a K1 SE part came loose twice and nobody knew how
old the glue was).

```
3d glue status [--printer NAME] [--json]
3d glue applied [--printer NAME] [--at TIME]
3d glue limits [--printer NAME] [--prints N] [--days D] [--reset] [--off | --on] [--json]
```

## When glue is due

A fresh layer is **due** when the time since the last application reaches the day limit **or** the prints since
it reach the print limit, whichever comes first (both inclusive), and while no application is on record at all
(`never`). The reminder is on for every printer 3d knows (`3d printer list`); only `limits --off` turns it off.

Defaults: **5 prints or 7 days**. A glue-stick layer on (textured) PEI is good for a handful of PLA parts: each
part pulls some of it off and the wipe between prints thins it; after about a week it has dried and gathered
dust whatever the count. Change them per printer:

```
3d glue limits --printer k1se --prints 4 --days 5
3d glue limits --printer k1se --json
3d glue limits --printer k1se --reset      # the defaults again, reminder on
3d glue limits --printer u1 --off          # this printer prints without glue: no reminders
3d glue limits --printer u1 --on
```

One record per printer: the glue on the bed it has now. 3d does not know which of several plates is mounted;
after swapping to a plate glued earlier, record that glue with `3d glue applied --at`.

## How prints are counted

From the printer's own job history, read-only: Moonraker `GET /server/history/list?since=<glue time>`. A print
started from Orca, Fluidd/Mainsail or the touchscreen counts as well as one from `3d printer start`. Counted:
every job started at or after the application that put plastic on the plate — completed, cancelled, errored,
`klippy_shutdown`/interrupted jobs with print time or filament used, and the job printing now — once per
Moonraker `job_id` (a record read twice is one print; a reprint of the same file has a new id and is another
print). A job that never extruded (interrupted before the first line) does not count.

When the printer does not answer, or has no Moonraker (Creality stock firmware), 3d's own start log
(`~/.local/state/3d-cli/prints/NAME.jsonl`, one line per start `3d printer start` got acknowledged) is the
count. It holds the starts 3d sent (a start cancelled while heating is in it) and misses prints started
elsewhere, so it is shown as an estimate: `~3`. A history reply without a job list, a read slower than 15 s
in total, a history longer than 10 000 jobs, or a job since the glue with no start time or with missing,
partial or invalid print metrics (including the zeros Moonraker leaves on a job it marks interrupted after a
restart) also falls back to it; the estimate is then never below the prints the history did confirm.

## Recording glue

```
3d glue applied --printer k1se
3d glue applied --printer k1se --at 2026-11-18T00:35
```

`--at` is an ISO date or date-time, local time without an offset (`2026-11-17T23:35Z` with one); not in the
future. The application is the one baseline of both limits: recording it restarts the days and the print count
together, in one atomic write.

## Status

```
$ 3d glue status --printer k1se
k1se
  last glue       2026-11-18 00:35 CET (0h 40m ago)
  time since      0h 40m of 7d
  prints since    0 of 5 (moonraker http://192.168.0.27:7125/server/history)
  limits          5 prints or 7d, whichever comes first (default)
  status          ok: due after 5 more prints or on 2026-11-25 00:35 CET, whichever comes first
```

`3d glue status` without `--printer` shows every printer. `--json` gives a list of
`{printer, due, fired, applied_at, elapsed_days, prints, prints_exact, prints_source, prints_note, max_prints,
max_days, limits_source, confirm_command}`; `fired` holds `never`, `days` and/or `prints`.

```
3d glue status --printer k1se --json | jq '.[0] | {due, fired, prints, elapsed_days}'
```

## Before every print

`3d printer upload` and `3d printer start` check the glue of the printer before they send anything (the print
count comes from that printer's history). Not due: one line, `glue k1se: ok, 2 of 5 prints, ...`. Due: the
warning names the limit and the values,

```
GLUE DUE on k1se: 5 prints since the glue (limit 5)
  last glue 2026-11-18 00:35 CET; a fresh glue-stick layer is due before this print
Glue applied? [y/N]
```

On a terminal, `y` records the glue now and the command goes on; `N` (or Enter) goes on with the warning. Without
a terminal (agents, scripts) the warning ends with the command that records it:
`  Glue applied? Record it with: 3d glue applied --printer k1se`. The check never refuses a print, and a broken
glue record only prints `glue check skipped: ...`.

The job watch (`3d printer watch NAME --job`, `start --watch`) adds the same warning, in Russian, to its
Telegram message about a lost print, with the `3d glue applied` command (plain `tg` has no reply buttons).

## Files

- `~/.local/state/3d-cli/glue/NAME.json` — per printer: `{"applied_at": ISO, "recorded_at": ISO}`.
- `~/.config/3d-cli/glue.json` — per printer limits and off: `{"k1se": {"max_prints": 4, "max_days": 5},
  "u1": {"enabled": false}}`.
- `~/.local/state/3d-cli/prints/NAME.jsonl` — 3d's own start log (the fallback count).
