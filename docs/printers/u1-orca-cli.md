# Snapmaker U1 — slicing headless with the Snapmaker Orca CLI

The single place for what is known about driving the Snapmaker Orca / OrcaSlicer CLI without the GUI.
The code lives in `lib/slicing/orca_cli.py` (`find_orca`, `slicing_copy`, `slice_project`, `KNOWN_QUIRKS`);
this page is written from `KNOWN_QUIRKS` and `tests/test_orca_cli.py` fails when a quirk id is missing here.
Verified against CLI build `01.10.01.50` (Snapmaker Orca 2.4.0 line).

## Find the CLI

`orca_cli.find_orca()` takes the first existing executable of:

1. `$ORCA`, 2. `$SLICER` (an env var naming a missing file is skipped, not an error),
3. `~/Applications/snapmaker_orca/AppRun` (the extracted Linux AppImage),
4. `/Applications/Snapmaker Orca.app/Contents/MacOS/Snapmaker Orca`,
5. `~/.local/bin/snapmaker-orca`, 6. `snapmaker-orca` / `orca-slicer` on `PATH`.

`find_orca(probe_version=True)` also reads the build number from `--help` (about 7 s: the AppImage starts its whole
runtime, so it is off by default). The vendor presets (`resources/profiles/<Vendor>/{machine,process,filament}`) are
found next to the CLI, in `$ORCA_RES`, or at the AppImage / macOS app location (`orca_cli.find_resources`).

## Slice headless

```
ORCA --debug 2 --datadir WORK/dd --arrange 0 --slice 0 --outputdir WORK --export-3mf sliced.3mf WORK/in.3mf
```

| flag | why |
| --- | --- |
| `--datadir WORK/dd` | private, writable data dir; the run never touches `~/.config/Snapmaker_Orca` |
| `--arrange 0` | keep the project's layout (see `arrange-moves-objects`, `exclude-triangles`) |
| `--slice 0` | slice plate 0 = all plates of the project |
| `--outputdir WORK` | `plate_1.gcode`, `result.json`, `sliced.3mf`, `slice.log` land here |
| `--export-3mf sliced.3mf` | the sliced project; its `Metadata/slice_info.config` carries the warnings |

`in.3mf` is NOT the shipped project but `slicing_copy(project, WORK/in.3mf)`: the project without its `<plate>`
blocks and `Metadata/plate_N.json` (`plate-block-segfault`). Everything else is byte-identical; `set_cfg={...}`
overrides keys of `Metadata/project_settings.config`, `drop_layer_profile=True` removes the layer-height profile.
Ship the original 3MF to the GUI, slice the copy.

`slice_project(project, workdir, heavy=True, timeout_s=3600)` returns a `SliceOutcome` (verdict PASS / WARN / FAIL):
the G-code, the sliced 3MF, the log, the slicer warnings (log + `result.json` + `slice_info`, host noise removed) and,
on failure, a named class with the remediation text:

| failure | meaning | quirks |
| --- | --- | --- |
| `segfault` | exit -11 / 139 | `plate-block-segfault`, `layer-range-segfault` |
| `exclude_triangles` | no output, log has `calc_exclude_triangles` | `exclude-triangles`, `arrange-moves-objects` |
| `return_code` | non-zero exit or non-zero `result.json` `return_code` | `arrange-moves-objects` |
| `no_gcode` | exit 0 but no `plate_N.gcode` | |
| `timeout` | exceeded `timeout_s` (GNU `timeout`; without it the Python deadline, lock wait included) | |
| `lock_busy` | the heavy lock stayed busy for its bounded wait | |

## How heavy runs are wrapped

A slice of a real model needs minutes and gigabytes, so `slice_project(heavy=True)` runs the CLI behind
`cli.heavy.heavy_prefix()`: `flock /tmp/3d-cli-heavy.lock systemd-run --user --scope -p MemoryMax=6G -p MemorySwapMax=0
-p CPUQuota=600% nice -n 10 env THREED_HEAVY_HELD=1`, one heavy job at a time machine-wide. Inside a process tree that
already holds the lock `heavy_prefix()` returns `[]`, so nothing nests: a nested `flock` on the same file once
deadlocked a 25-minute run. Never type your own `flock` around code that calls `slice_project`; pass `heavy=False`
when you already wrapped it by hand.

## Quirks

Each row: what you see, why, what to do, where it was observed.

| id | symptom | cause | fix | evidence |
| --- | --- | --- | --- | --- |
| `plate-block-segfault` | CLI segfaults (rc -11 / 139) before any G-code | a `<plate>` block in `Metadata/model_settings.config` crashes `PartPlate::generate_plate_name_texture()` in 01.10.01.50 | slice `slicing_copy(...)`, keep the original for the GUI | babushkin-dom `gen/verify_slices.py` `cli_copy()`; Chekhov `u1/verify_gcode.py` header |
| `layer-range-segfault` | CLI segfaults on a project with `Metadata/layer_config_ranges.xml` | layer-range metadata crashes the CLI | use `Metadata/layer_heights_profile.txt` (one line `object_id=N\|z;h;z;h`) | Chekhov `u1/RECIPE.md` ("Что НЕ работает") |
| `exclude-triangles` | no output; log ends `calc_exclude_triangles:Unable to create exclude triangles` | seen with `--datadir` but without `--arrange 0` on a project whose machine preset the CLI could not find; with both flags the line is only a warning (U1 has no exclude area) | always `--datadir WORK/dd --arrange 0 --slice 0`; rebuild the project with the vendor-preset `project_settings.config` | `~/.cache/prep-speedup/tx/U1MixPipeline.txt` (smoke slice 17:22); Chekhov `u1/RECIPE.md` (accepted warning) |
| `prime-tower-single-object` | slicing refuses prime tower + variable layer heights with several objects | `Print.cpp:1745` requires equal layer-height profiles for all objects; the profile is per object | ONE object made of colour parts (volumes) | snap-orca `libslic3r/Print.cpp:1695-1790`; Chekhov `u1/RECIPE.md`, `u1/layer_profile.py` |
| `mixed-row-convention` | a Full Spectrum pattern prints on other heads than a gate reads | tokens 1 / 2 mean the row's component_a / component_b, 3..9 a slot (`MixedFilament.cpp`) | write rows with `component_a=1, component_b=2` so a token is the slot number | Chekhov `u1/build_3mf.py` `mix_row()`; `u1/colours.py` |
| `tool-index-offset` | G-code `T1` where the recipe says slot 2 | `Tn` is the 0-based filament index: slot n+1 = tool Tn (slot 1 = T0) | tool = slot - 1 whenever G-code and recipes meet | Chekhov `u1/RECIPE.md` ("Подводные камни"); `u1/verify_gcode.py` header |
| `layer-profile-top-mismatch` | a layer-height profile is accepted but ignored | `generate_object_layers` drops a profile whose last z differs from the object height by more than 1e-3 mm | end the profile at the object height after placement (`build_project` refuses a mismatch) | Chekhov `u1/RECIPE.md`; `u1/build_3mf.py` |
| `local-z-dithering` | a custom profile has no effect | Local-Z dithering replaces the object's layer-height profile | `dithering_local_z_mode = 0` | Chekhov `u1/RECIPE.md`; `u1/build_3mf.py` `project_settings()` |
| `arrange-moves-objects` | objects overlap, slice fails with -101 | without `--arrange 0` the CLI arranges the plate itself | pass `--arrange 0` | babushkin-dom `gen/verify_slices.py` (P1S roofs, error -101) |
| `inherits-one-level` | settings of grandparent presets are lost | `--load-settings` resolves one `inherits` level | flatten the chain (`SystemPresets.flat`) | babushkin-dom `gen/build_orca_3mf.py` |
| `application-tag-newer` | the CLI refuses a project we wrote: `result.json` `return_code` -24, "Unsupported 3MF version" | an `Application` tag newer than the CLI's build number is rejected (even `Snapmaker_Orca-2.4.0`, the tag the CLI wrote itself; reproduced by `test_real_cli_refuses_its_own_unstamped_project`) | stamp the build number (`01.10.01.50`) | babushkin-dom `gen/build_orca_3mf.py` `orca_version()` |
| `no-thumbnails` | sliced G-code has no preview image | `export_gcode` gets a null thumbnail callback | `3d gcode-thumbnails add FILE` | babushkin-dom `gen/verify_slices.py` |
| `host-noise-warnings` | a clean slice lists warnings (version banner, OpenGL / GLFW / Wayland) | headless thumbnail renderer and banner logged as warnings / errors | ignore; `SliceOutcome.warnings` omits them (`slice_warnings.HOST_NOISE`) | `lib/slicing/slice_warnings.py` |

## Related

* `3d profiles doctor` — checks the user presets these projects inherit from (parents, layer-height limits, `.info`).
* `lib/prep/project3mf.py` — builds the project 3MF these notes are about; its manifest lists what the G-code verifier expects.
