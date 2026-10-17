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
3d camera ls [--json] [--api URL] [--token T]
3d camera show NAME [--json] [--api URL] [--token T]
3d camera rm NAME [--api URL] [--token T]
```

| Subcommand | What |
|---|---|
| `controls` | List every control (v4l2-ctl name, alias, type, range, step, default, current value, inactive flag, menu items) and the frame sizes per format. `--set` writes controls in the given order and prints what the camera reads back. |
| `still` | Lock, stream, drop `--skip` frames, keep `--frames` frames, average them, and write `FILE.png` plus `FILE.json` (`topcam-still/1`: size, frames, temporal noise, requested lock, control read-back, source). |
| `serve` | Run the camera-node HTTP service on this machine: the same code the Pi runs. Use it to test the node API against the C920 on the PC. |
| `ls` / `show` / `rm` | The camera nodes registered with 3d-api: list (online + age), one record, forget one. See [the registry](#camera-node-registry-ls-show-rm). |

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
3d camera ls
3d camera ls --json --api http://192.168.0.39:8765 --token po_…
3d camera show topcam --json
3d camera rm old-topcam
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

## Camera-node registry (`ls`, `show`, `rm`)

A camera node (topcam on the Raspberry Pi Zero W, or `3d camera serve` on a PC) registers
itself with 3d-api: every `api.interval_s` seconds (default 60) it sends
`PUT /v1/camera-nodes/{name}` with its URL, preview stream, V4L2 card/device, formats, LED and
version. The record is how `--camera NAME` finds the node after DHCP gives it a new address
(no record, or 3d-api not configured → `http://NAME.local:8080`).

A node is **online** when its last check-in is at most **180 s** old (three default
intervals); `age_s` is the time since that check-in.

### `ls`

One line per node: `NAME  STATE  URL  CARD  VERSION`, STATE = `online 12s` / `offline 3h`.

- `--json` — print the API body `{"items": [record, …]}` instead.
  `3d camera ls --json | jq -r '.items[] | select(.online) | .url'`
- `--api URL` — 3d-api base URL (default `$PRINTOPS_API`, else `api:` in
  `~/.config/3d-cli/printops.yaml`).
  `3d camera ls --api http://192.168.0.39:8765`
- `--token T` — 3d-api token with scope `read` (default `$PRINTOPS_TOKEN`, else `token:` in the
  same file).
  `3d camera ls --api http://192.168.0.39:8765 --token po_…`

### `show`

The node's whole record: url, preview stream, hostname, card, device, formats, LED, version,
first/last check-in, the address the last check-in came from and the token name that sent it.
404 → `no camera node 'NAME'` (exit 1).

- `--json` — the record as JSON.
  `3d camera show topcam --json | jq .formats.YUYV`
- `--api URL` / `--token T` — as for `ls` (scope `read`).
  `3d camera show topcam --api http://192.168.0.39:8765 --token po_…`

### `rm`

Forget a node's record (e.g. a renamed or retired node). A node that is still running
re-registers on its next check-in.

- `--api URL` / `--token T` — as for `ls`; needs scope `write`.
  `3d camera rm old-topcam --token po_…`

### The node's token (scope `camera`)

The token on the node's SD card gets only the `camera` scope: 3d-api accepts it for
`PUT /v1/camera-nodes/{name}` and nothing else (every other route answers 403), so a lost card
cannot read or change spools, printers or calibrations. **Name the token like the node**: a
`camera`-only token may register only the node of its own name (403 for any other), so a lost
card cannot point another node's record at a different address either. Node names for such a
token are therefore lower-case (token names are `[a-z0-9][a-z0-9_.-]*`). Mint it on the Odroid
(printed once):

```
3d printops token create topcam --scopes camera --ssh ultra@192.168.0.39
```

and put it in the node's `api.token_file` (mode 0600; the SD image build of
[#343](https://github.com/alex-mextner/3d-cli/issues/343) writes it there for you).

Rotate: `3d printops token revoke topcam --ssh ultra@192.168.0.39`, create again, rebuild or
replace the token file on the node.

API details (body schema, validation): services/3d-api/README.md, "API (camera nodes)".
