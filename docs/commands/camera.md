# `3d camera` — the top camera: controls, locked stills, the camera node

A camera looking straight down at the bed takes the photos the calibration readout measures
([#273](https://github.com/alex-mextner/3d-cli/issues/273)). Two backends drive it the same way:

- **node** — a Raspberry Pi Zero W with the Logitech C920 on its USB port, running the
  topcam service (`lib/topcam/node.py`); addressed as `http://host:port`, `host:port`, or by
  name (`topcam`: the record the node keeps in 3d-api, else `http://topcam.local:8080`).
- **usb** — the camera plugged into this machine (`usb`, or `usb:/dev/videoN`).

Stills are uncompressed YUYV at full sensor resolution (C920: 2304x1536, about 2 frames/s;
its MJPEG modes stop at 1920x1080 and blur edges). Before streaming starts, the camera is
put in manual focus, exposure, gain and white balance, so two stills of the same plate differ
only by sensor noise. Every still starts by resetting all controls to the camera's own defaults
(the `default_value` V4L2 reports: autofocus, auto exposure and auto white balance on), applies the
lock, captures, and resets to the defaults again on any exit — success, error or interrupt — so the
camera is never left locked for other users of it. A still records what the camera reports after the capture (the
C920 rounds some values: `gain=30` reads back as `29`).

Controls are set with plain V4L2 ioctls from Python (`lib/topcam/v4l2.py`). Neither v4l-utils,
ffmpeg nor OpenCV is needed on the PC or on the Pi. Control names are the ones `v4l2-ctl`
prints, so `exposure_time_absolute` here is the same control `v4l2-ctl -c
exposure_time_absolute=300` writes. Short aliases (`focus`, `focus_auto`, `exposure`,
`exposure_auto`, `gain`, `wb`, `wb_auto`, `brightness`, …) resolve by control id, so they also
work on older kernels that name the controls `exposure_absolute`, `focus_auto`, and so on.

## Usage

```bash
3d camera controls [--camera C] [--set NAME=V[,NAME=V…]] [--json]
3d camera still [--camera C] [--res WxH] [--focus auto|N|sweep[:LO-HI[:STEP]]] [--focus-roi R] [--exposure E] [--gain G] [--wb K]
                [--ctrl NAME=V[,NAME=V…]] [--frames N] [--skip K] [--average mean|median|none]
                [--depth 8|16] [--led on|off] [--out FILE.png] [--json]
3d camera serve [--device DEV|auto] [--port 8080] [--name N] [--config FILE] [--no-stream] [--bind ADDR]
```

| Subcommand | What |
|---|---|
| `controls` | List every control (v4l2-ctl name, alias, type, range, step, default, current value, inactive flag, menu items) and the frame sizes per format. `--set` writes controls in the given order and prints what the camera reads back. |
| `still` | Lock, stream, drop `--skip` frames, keep `--frames` frames, average them, and write `FILE.png` plus `FILE.json` (`topcam-still/1`: size, frames, temporal noise, requested lock, control read-back, source). |
| `serve` | Run the camera-node HTTP service on this machine: the same code the Pi runs. Use it to test the node API against the C920 on the PC. |

| Option | What |
|---|---|
| `--camera C` | `usb` (default), `usb:/dev/videoN`, `http://host:port`, `host:port`, or a node name. |
| `--set N=V,…` | `controls`: values to write, e.g. `focus_auto=0,focus=40`. A value outside the control's range is an error, never clamped. |
| `--res WxH` | Still resolution, default `2304x1536`. A size the camera does not offer is an error that names what the driver offered. |
| `--focus F` | `auto` (the camera's continuous autofocus), `N` (manual position, autofocus off; C920 0..250 step 5), or `sweep[:LO-HI[:STEP]]`: software focus — step `focus_absolute` over the range (default 0-250 every 25, then every 5 around the best), score each 1280x720 luma frame by sharpness (Brenner gradient) inside `--focus-roi`, and keep the sharpest position for the whole series. The JSON records the mode and the sweep curve. |
| `--focus-roi R` | Sweep region `X0,Y0,X1,Y1` as frame fractions (default `0.25,0.25,0.75,0.75`): put it on the plate or the markers. |
| `--exposure E` | Manual exposure in 100 µs units; switches auto exposure to manual and turns the dynamic frame rate off (C920: 3..2047). |
| `--gain G` | Gain (C920: 0..255). |
| `--wb K` | White balance in kelvin; turns auto white balance off (C920: 2000..6500). |
| `--ctrl N=V,…` | Any other control by v4l2-ctl name or alias, applied after the lock, e.g. `sharpness=0`. |
| `--frames N` | Frames to keep, 1..32 (default 1; a node allows up to 8 per request, more take several requests; all are held in memory). |
| `--skip K` | Frames dropped after streaming starts (default 3). |
| `--average M` | `mean` (default), `median` (robust to one bad frame), or `none` (last frame). |
| `--depth 8\|16` | PNG bit depth. 16 keeps the sub-LSB detail an average of several frames has. |
| `--led on\|off` | Node only: the LED ring during the still; its previous state is restored afterwards. |
| `--out FILE.png` | Output path (default `./still-<camera>-<time>.png`); the JSON goes next to it. |
| `--json` | Print JSON instead of text. |
| `--device DEV` | `serve`: V4L2 device; `auto` (default) picks the first UVC camera that offers YUYV, by its `/dev/v4l/by-id/` path. |
| `--port P` | `serve`: HTTP port (default 8080). The ustreamer preview listens on `stream.port` (default 8081). |
| `--name N` | `serve`: node name; with `api.url` in the config the node registers under it. |
| `--config FILE` | `serve`: node JSON config (below). |
| `--no-stream` | `serve`: no ustreamer preview. |
| `--bind ADDR` | `serve`: listen address (default `0.0.0.0`). |

```bash
3d camera controls --camera usb
3d camera controls --camera usb --set focus_auto=0,focus=40
3d camera still --camera usb --focus 40 --exposure 300 --gain 0 --wb 4500 --frames 4 --out plate.png
3d camera still --camera usb --focus sweep:0-250:25 --focus-roi 0.2,0.2,0.8,0.8 --exposure 300
3d camera still --camera topcam --res 2304x1296 --exposure 200 --ctrl sharpness=0 --led on
3d camera still --camera http://192.168.0.50:8080 --frames 8 --average median --depth 16 --json
3d camera serve --device /dev/video2 --port 8080 --name pc-c920 --no-stream
```

Exit codes: 0 success; 1 the camera or node refused or is unreachable (the message says
which and why); 2 bad options.

## The camera node

The node is one stdlib-only Python process (`python3 -m topcam.node --config FILE`). It
fits a 512 MB Pi Zero W: a 2304x1536 YUYV frame is 7 MB, and at most `still.max_frames`
frames are held in memory.

### HTTP API

| Request | Answer |
|---|---|
| `GET /health` | `{"ok", "name", "version", "device", "card", "busy", "stream", "stream_port", "led", "uptime_s"}` |
| `GET /controls` | `{"controls": [...], "formats": {"YUYV": [[w, h], ...], ...}}` |
| `POST /controls` | body `{"focus_auto": 0, "focus": 40}` → `{"controls": {read-back}}` |
| `GET /still?res=2304x1536&fmt=yuyv\|grey\|pgm&frames=N&skip=K&focus=F&exposure=E&gain=G&wb=K&focus_roi=X0,Y0,X1,Y1&ctrl=NAME=V&led=on\|off&settle_ms=MS` | N frames back to back: `yuyv` W·H·2 bytes each, `grey` W·H (luma), `pgm` N binary PGM images. Headers `X-Width`, `X-Height`, `X-Format`, `X-Frames`, `X-Dropped`, `X-Seconds`, `X-Controls` (JSON read-back during the lock), `X-Focus` (`{mode, sweep}`). `focus` is `auto`, `N` or `sweep[:LO-HI[:STEP]]`. |
| `GET /stream`, `GET /snapshot` | `307` to ustreamer's MJPEG stream / JPEG snapshot; `404` when the preview is off. |
| `POST /led` | body `{"on": true}` → `{"led": true}` |

Errors are `{"error": "..."}` with 400 (bad request), 404, 409 (a still is running and
`wait=0`), or 503 (the camera refused: unsupported size, value out of range, no frame).

The preview stream and a still cannot use the camera at the same time. A still stops
ustreamer, sets the lock, captures, and starts ustreamer again. The preview passes the
camera's own JPEG frames through (`--format=MJPEG --encoder=HW`), so the Pi does not encode.

```bash
curl http://topcam.local:8080/health
curl -o plate.pgm 'http://topcam.local:8080/still?fmt=pgm&focus=40&exposure=300&wb=4500'
```

### Config (`/etc/topcam/config.json`)

```json
{"name": "topcam", "port": 8080, "device": "auto",
 "still": {"res": "2304x1536", "skip": 3, "max_frames": 8,
           "lock": {"focus": 30, "exposure": 300, "gain": 0, "wb": 4500}},
 "stream": {"enabled": true, "port": 8081, "res": "640x480", "fps": 5, "binary": "ustreamer"},
 "led": {"gpio": 17, "active_high": true, "settle_ms": 300},
 "api": {"url": "http://192.168.0.39:8765", "token_file": "/etc/topcam/token", "interval_s": 60}}
```

- `still.lock` is the default lock; query parameters override it per request.
- `led.gpio` switches an LED ring through a MOSFET with `pinctrl set N op dh|dl`
  (Raspberry Pi OS `raspi-utils`); `settle_ms` waits after switching before the capture.
- With `api.url`, the node sends `PUT /v1/camera-nodes/{name}` every `interval_s` seconds with
  the bearer token from `token_file`. The body is `{url, hostname, stream_url, card, device,
  formats, led, version}`, so `--camera topcam` finds the node wherever DHCP put it.
