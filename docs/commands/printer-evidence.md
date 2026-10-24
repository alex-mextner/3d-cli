# `3d printer evidence` — save what a print left behind, after EVERY print

Issue: [#551](https://github.com/alex-mextner/3d-cli/issues/551). Part of [`3d printer`](printer.md).

After a print ends — done, cancelled or failed — the only long-lived record on a Moonraker printer is
its **history job**. The console (`/server/gcode_store`) is a RAM ring buffer of a few hundred lines
that the 1 Hz temperature reports push every event out of within hours, and `klippy.log` rotates.
Chekhov 2026-10-23: the black head T2 failed its pickup (`err-sta ['1-(FFF)']`) 9.7 h in, the print
sat paused for 6 h and was cancelled at layer 455 of 644; the only trace of the cause was one line
of klippy.log, copied by hand before it rotated away. `evidence` does that copy in one command.

## Usage

```text
3d printer evidence NAME [--job ID | --last] [--out DIR] [--gcode FILE.gcode] [--note TEXT]
                         [--klippy-log FILE] [--job-file FILE] [--context N] [--max-lines N] [--secs S]
```

Read-only on the printer: only `GET` requests are ever sent (`tests/e2e/test_printer_evidence_workflows.py` fails on any
other method), so it is safe while the
printer prints. Write the result into the project, next to the print, so it survives `rm -rf ~/.cache`:

```bash
3d printer evidence u1 --out projects/chekhov/print/runs
```

- `NAME` — a Moonraker printer from `3d printer list`. A Creality (stock firmware) printer has neither
  history jobs nor klippy.log and is refused (exit 2). With both `--job-file` and `--klippy-log` the printer
  is not contacted or looked up: `NAME` is only the label in the report.
- `--job ID` — the Moonraker history `job_id` (e.g. `000070`; `3d printer status NAME`, Fluidd's
  history list). `3d printer evidence u1 --job 000070 --out runs`. A job the printer's history does not
  have (Moonraker answers 404 naming the job) is a usage error (exit 2) and **nothing is written**; a 404 that does not
  mention the job — no `[history]` component, a proxy — is a failed history read (exit 1); `--job` and `--job-file` exclude each other.
- `--last` — the default: the newest job that is **not** `in_progress` (the next print may already be
  running). `3d printer evidence u1 --last --out runs`. `--job` and `--last` exclude each other.
- `--out DIR` — where `<date>-<outcome>.md` and `<date>-<outcome>.json` are written (created):
  `3d printer evidence u1 --out projects/chekhov/print/runs`. `<date>` is the job's **start** day in
  local time, `<outcome>` is `done` (Moonraker `completed`), `cancelled`, `in-progress`, or `failed`
  (`error`, `klippy_shutdown`, `klippy_disconnect`, `server_exit`, `interrupted`). Saving the same job
  again overwrites its own files; a different job of the same day and outcome gets `-<job id>` appended —
  nothing is ever replaced (`-<job id>-2`, `-3`, ... when that is taken too; the id is sanitised, a record
  is not trusted to name a path). A job saved while it was still running and again after it ended leaves
  both files; the second run points at the first. A run that has no job record (the printer did not answer) is saved as
  `<capture day>-unknown-<HHMMSS>`; delete it once the job is saved. Without `--out` the report goes to
  stdout and nothing is written.
- `--gcode FILE` — the sliced G-code the job printed: `3d printer evidence u1 --out runs --gcode
  print/chekhov.gcode`. Every `file_position` the log mentions is mapped to its **layer**, that layer's
  **z**, and the **top z of the last complete layer** below it (where `3d gcode-resume` has to cut).
  The file is checked against the job: the content hash `3d printer upload` puts at the end of the stored
  name (`<stem>-<sha256[:16]>.gcode`) must match its sha256, else the job's recorded size; a mismatch is
  labelled **MISMATCH**, listed under *MISSING / WRONG* and exits 1 — the mapping then describes
  another file; so does a `file_position` beyond the end of the G-code. Orca / PrusaSlicer G-code only (`;LAYER_CHANGE` + `;Z:`); layer numbers count the
  `;LAYER_CHANGE` markers of the file (the slicer's own "total layers" line may differ by one).
- `--note TEXT` — what the operator saw, in the `.md`: `--note "T2 head failed pickup, nut replaced"`.
- `--klippy-log FILE` — a saved copy of klippy.log instead of fetching it (copied before it rotated, or
  the printer is off): `--klippy-log klippy.log.copy`. klippy.log stamps only the UTC time of day, so the
  dates are rebuilt backwards from when its newest line was written: the HTTP `Last-Modified` of the
  download (the `Date` of the reply when there is none: an idle printer writes nothing, so the day of the
  request can be days later), the **mtime of the copy** otherwise — copy with `cp -p` / `rsync -t`, or the dates are the copy's (a log
  that then does not overlap the job at all is reported under *MISSING / WRONG*).
- `--job-file FILE` — a saved job record (the JSON of `/server/history/job`, or its `job` object)
  instead of fetching it: `--job-file job.json`. With both `--job-file` and `--klippy-log` the printer is
  not contacted at all.
- `--context N` — non-`Stats` log entries kept before and after each event (default 6):
  `--context 12`.
- `--max-lines N` — at most N log entries in the excerpt (default 400); what is cut is counted in the
  report, the first and last events are kept: `--max-lines 800`.
- `--secs S` — timeout of each HTTP request (default 10; the klippy.log download gets 6×):
  `--secs 30`.

## What is saved

`DIR/<date>-<outcome>.md` (the report, committed to the project) and `DIR/<date>-<outcome>.json` (raw:
the job record, `print_stats` at capture, every shown log entry with its UTC time and event kinds, the
layer map). The report:

1. the job (file, start/end in UTC and local time, durations, filament, Moonraker status → outcome);
2. **first fault** in the log and, with `--gcode`, **where it stopped**: *file_position 64070964 = layer
   455 of 644, z 65.798, layer starts at byte 64061656, 9308 B into it; last complete layer 454, top z
   65.698*;
3. the operator's note and what was collected from where (`GET` only);
4. **MISSING / WRONG** when a part could not be collected;
5. the raw history record;
6. the klippy.log excerpt in UTC, event lines marked `!`.

The log of the Snapmaker U1 stamps every line `HH:MM:SS.mmm:`; a **stock Klipper** log has no stamps at all
(not one line stamped): it is then searched whole, without a time window, and the excerpt
shows `(no time)` — positions are still found (`Exiting SD card print (position N)`).

Events (a regex over the log message, `printer_ops/evidence.py` `EVENT_PATTERNS`): `pickup failed`,
`detached`, `Raising exception` / `exception cleared` (a clear is an event, never the "first fault"), `Pausing...` / `request pause`, `Resuming...`,
`request cancel` / `Exited to idle state`, `Exiting SD card print` / `exit_print:` (their `file_position`),
`Starting SD card print` (where a resume re-entered the file) / `Finished SD card print`, MCU / Klipper
shutdown and tracebacks. Only events inside the job's time window count (2 min before its start to 15 min
after its end); events of other jobs in the same log are counted, not shown, and a `Starting SD card
print` / exit that names another G-code file (a print started minutes after this one) is never mapped. If the job started before
the log begins, the report says its start was already rotated away.

## Exit codes

- `0` — saved.
- `1` — something is missing or wrong (job, log, or a mismatching `--gcode`; or the printer did not
  answer). **What could be collected is still written**, so a printer that was power-cycled leaves
  the log and the note; rerun with the rest (`--job-file`) — the same job overwrites its own files.
- `2` — usage error (unknown flag, missing file, `--job` with `--last` or `--job-file`, a job the history
  does not have, not a Moonraker printer).

## Examples

```bash
3d printer evidence u1 --out projects/chekhov/print/runs
3d printer evidence u1 --job 000070 --out projects/chekhov/print/runs --gcode print/chekhov.gcode \
  --note "head T2 failed pickup"
3d printer evidence u1 --job-file job.json --klippy-log klippy.log.copy --gcode print/chekhov.gcode --out runs
3d printer evidence u1 --last --context 12 --max-lines 800
3d printer evidence u1 --last --secs 30 | less      # report to stdout, nothing written
```
