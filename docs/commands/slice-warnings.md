# `3d slice-warnings` — the slicer's warnings, bound to the G-code they belong to

The slicer is the only tool that sees floating regions, empty layers, path conflicts or a bed
temperature above the filament's range — and it says so in a log that scrolls past. This
command records every warning and error of one slice in `NAME.slice-warnings.json` next to
`NAME.gcode`, together with the G-code's SHA-256. [`3d print-preflight`](print-preflight.md)
(its `slicer warn` gate) and [`3d printer start`](printer.md) FAIL until each warning of that
exact G-code is resolved (fixed and re-sliced) or accepted with a reason.

## Usage

```
3d slice-warnings record FILE.gcode [--log LOG]... [--result result.json]... [--sliced-3mf FILE.3mf]...
                         [--accept-warning ID REASON]...
3d slice-warnings accept FILE.gcode ID REASON
3d slice-warnings check FILE.gcode [--json]
```

[`3d slice`](slice.md) records the sidecar itself. A project script that runs the slicer CLI
calls `record` after the **last** edit of the G-code (copying it into place, adding thumbnails):

```bash
"$ORCA" --debug 2 --arrange 0 --slice 0 --outputdir "$TMP" --export-3mf sliced.3mf plate.3mf > "$TMP/slice.log" 2>&1
cp "$TMP/plate_1.gcode" out/plate.gcode
3d gcode-thumbnails add out/plate.gcode
3d slice-warnings record out/plate.gcode --log "$TMP/slice.log" --result "$TMP/result.json" --sliced-3mf "$TMP/sliced.3mf"
3d slice-warnings accept out/plate.gcode wc6181fe0 'bed 60 C is the K1 profile default for this PLA'
3d slice-warnings check out/plate.gcode
```

Python scripts pass the captured output the same way:

```python
r = subprocess.run([ORCA, "--debug", "2", "--slice", "0", "--outputdir", tmp, "--export-3mf", "sliced.3mf", src],
                   capture_output=True, text=True)
Path(tmp, "slice.log").write_text(r.stdout + r.stderr)
# ... copy plate_1.gcode to its final NAME.gcode, add thumbnails ...
subprocess.run(["3d", "slice-warnings", "record", final_gcode, "--log", f"{tmp}/slice.log",
                "--result", f"{tmp}/result.json", "--sliced-3mf", f"{tmp}/sliced.3mf"], check=True)
```

## Sources

| Option | Reads | Becomes |
|---|---|---|
| `--log LOG` (`-` = stdin) | the CLI's stdout + stderr: Orca-family boost-log entries `[warning] …` / `[error] …`; a message continues on the following lines without a log prefix | one warning per message, level `warning` or `error` |
| `--result FILE` | the CLI's `result.json` in its `--outputdir` | `return_code` ≠ 0 → an error with `error_string`; each `sliced_plates[].warning_message` → a warning |
| `--sliced-3mf FILE` | `Metadata/slice_info.config` of a sliced project (`.gcode.3mf`, or `--export-3mf` output) | each `<warning msg= level= error_code=>`; level 2–3 is an error |

Each warning gets an id `w` + 8 hex digits from its level and message (the `plate N: found …
slicing warnings:` prefix and the logger's `function:line,` are not part of it), so the same
message keeps its id across re-slices and slicer builds; the same message from several sources
is one warning. Acceptances in the previous sidecar carry over to the same id.

**Host noise.** A headless CLI also logs lines about the machine, not the plate: the version
banner (`cli mode, Current Snapmaker_Orca Version …`) and the thumbnail renderer failing to
open a display (GLFW / Wayland / GLEW / OpenGL). They are stored under `ignored` with the rule
that matched and counted in the gate's line, never dropped silently. Everything else — a
missing system preset, `no filament colors found in projects`, `Unable to create exclude
triangles`, `bed_temperature_too_high_than_filament` — is a warning to resolve or accept.

## The sidecar

```json
{
 "schema": 1,
 "gcode": "lawn.gcode",
 "gcode_sha256": "0aa1f1bd…",
 "slicer": "Snapmaker Orca 2.4.0 on 2026-10-14 at 13:30:56",
 "sources": [{"kind": "log", "path": "slice.log"}, {"kind": "result.json", "path": "result.json"}],
 "warnings": [
  {"id": "wc6181fe0", "level": "warning", "message": "bed_temperature_too_high_than_filament",
   "sources": ["slice_info"], "error_code": "1000C001",
   "accepted": {"reason": "bed 60 C is the K1 profile default for this PLA", "at": "2026-10-14T11:40:02+00:00"}}
 ],
 "ignored": [{"message": "init opengl failed! skip thumbnail generating", "rule": "headless display: OpenGL/GLEW/GLFW"}],
 "post_processed": []
}
```

[`3d gcode-thumbnails add`](gcode-thumbnails.md) changes the hash but not the toolpath: it
rewrites a sidecar that matched the file before, and notes the step under `post_processed`.
Any other edit makes the sidecar stale.

## Gate results (`check`, preflight `slicer warn`)

| Result | When |
|---|---|
| FAIL | no sidecar; the sidecar's `gcode_sha256` differs from the G-code (re-sliced or edited since); a warning without a reason — each listed as `OPEN id level: message` |
| PASS | no warnings, or every one accepted; each accepted one is listed with its reason |

## Options

- `--log LOG` — slicer CLI output to read (repeatable): `3d slice-warnings record out/plate.gcode --log out/slice.log`
- `--result FILE` — the CLI's result.json: `3d slice-warnings record out/plate.gcode --log out/slice.log --result out/result.json`
- `--sliced-3mf FILE` — a sliced project's slice_info: `3d slice-warnings record out/plate.gcode --sliced-3mf out/sliced.3mf`
- `--accept-warning ID REASON` — accept one warning of this slice while recording (repeatable; an id the slice did not report is noted, not an error): `3d slice-warnings record out/plate.gcode --log out/slice.log --accept-warning wc264eaa3 'colours set per head on the printer'`
- `--json` (check) — `{file, status, detail, lines}`: `3d slice-warnings check out/plate.gcode --json`

`record` needs at least one source. A reason must say something (three characters or more).

Exit: `record`/`accept` 0 on success, 2 on a usage error, missing file, unknown id or stale
sidecar; `check` 0 PASS, 1 FAIL.
