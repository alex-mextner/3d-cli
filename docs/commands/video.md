# `3d video` - turntables and progress clips

`3d video` turns existing CLI render artifacts into shareable motion. It has two modes:
`turntable` renders a deterministic orbit through `3d render --cam`, and `progress`
encodes an existing directory of PNG frames into a video.

With `--dry-run`, both modes print the plan without calling OpenSCAD or `ffmpeg`.

## Usage

```bash
3d video <turntable|progress> ...
```

Turntable mode:

```bash
3d video turntable <file.scad> [options]
```

Progress mode:

```bash
3d video progress <frames-dir> [options]
```

Options:

- `-o, --out PATH` sets the output video path. Default: `<input>.mp4`.
- `--workdir DIR` sets the turntable frame directory. Default: `<output>_frames`.
- `--frames N` sets the turntable frame count. Default: `36`.
- `--fps N` sets frames per second. Default: `24` for turntable, `12` for progress.
- `--size WxH` sets turntable render size. Default: `800x600`.
- `--radius N` sets turntable orbit radius. Default: auto from model bounds.
- `--elevation DEG` sets camera elevation from `-89` to `89`. Default: `25`.
- `--start-angle DEG` sets the first orbit azimuth. Default: `0`.
- `--degrees DEG` sets the orbit span. Default: `360`.
- `--pattern GLOB` selects progress PNG frames. Default: `*.png`.
- `-D k=v` passes OpenSCAD defines to every turntable render. Repeatable.
- `--dry-run` prints the plan without rendering or encoding.

## Examples

Plan a 48-frame orbit without heavy dependencies:

```bash
3d video turntable bracket.scad --dry-run --frames 48 --size 1024x768
```

Render and encode a shareable spin:

```bash
3d video turntable bracket.scad -o bracket-spin.mp4 --frames 48 --fps 24
```

Reuse existing render frames as a progress clip:

```bash
3d video progress previews/ -o progress.mp4 --fps 12 --pattern '*.png'
```

Save a dry-run plan and inspect it with normal shell tools:

```bash
3d video progress previews/ --dry-run --pattern '*.png' > video-plan.txt
cat video-plan.txt | grep '^frames:'
```

## Dependencies

`turntable --dry-run` only needs the `.scad` input. A real turntable render requires
OpenSCAD through the existing `3d render` path and `ffmpeg` for encoding. `progress`
requires existing PNG frames and only calls `ffmpeg` when `--dry-run` is not set.
