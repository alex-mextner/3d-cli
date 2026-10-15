# `3d gcode-bounds` — does every move stay on the machine?

Checks a sliced G-code before it goes anywhere: every move, including the bulge of every
arc, must stay inside the printer's X/Y travel, and the plate must stay inside the printable
area. It never contacts a printer, so agents and CI can run it on any file.
Issue: [#101](https://github.com/alex-mextner/3d-cli/issues/101).

Why: a Creality K1 SE plate stopped with "Y out of range" and the bed dropped mid-job. The
cause was a spiral z-hop arc near the bed edge: `G3 Z1.36 I-.359 J-1.79` starting at
(62.83, 2.774) is a full circle that reaches Y −0.84, below the K1 SE's Y minimum of −0.5.
Both end points of the arc were on the bed, so an end-point check and the slicer preview
showed nothing wrong.

## Usage

```
3d gcode-bounds FILE.gcode [--printer NAME | --axis-limits X0:X1,Y0:Y1] [--json]
```

| Check | What | FAIL means |
|---|---|---|
| `axis travel` | Every G0/G1/G2/G3 in the file, start and end G-code included. | A point is outside the printer's X/Y limits. SKIP when the limits are unknown. |
| `print area` | Moves from the first layer change to the end of the last object or slicer feature (a prime tower printed after the last object counts). Vendor `;TYPE:Custom` blocks (parking, purging, tool changes) are skipped. | A point is outside the G-code's `; printable_area`, or an extruding line or arc passes through `; bed_exclude_area` (even with both end points outside it; travel over it is allowed). WARN `UNVERIFIED` when the file has no layer markers or no printable area. |
| `unverified` | Only shown when needed. | WARN: moves only partly checked — an arc given by `R` or in the XZ/YZ plane (`G18`/`G19`), or a relative move from an unknown position. |
| `macros` | Only shown when the G-code calls macros — any command other than the moves above and stock commands that cannot move X/Y (`SET_VELOCITY_LIMIT`, `M104`, `EXCLUDE_OBJECT_*`, …). | FAIL: a macro not listed in the printer's `trusted_macros` (`fleet/printers.yaml` or `printer-devices.yaml`, via `--printer NAME`). Its body may move the head anywhere, and after it the position counts as unknown. PASS lists the trusted ones. |

How moves are followed: absolute (`G90`) and relative (`G91`) X/Y, `G92` coordinate offsets,
and `G2`/`G3` arcs about `I`/`J` with their real extreme points; an arc whose start equals
its end is a full circle. Extrusion follows `M82`/`M83` and `G92 E`. After `G28` (or `G28 X`,
`G28 Y`) the homed axis is unknown until it is set again; `G28 Z` keeps X and Y. A known axis
is checked even while the other one is unknown, so `G1 X300` right after homing still fails.
Positions are G-code coordinates: tool offsets applied inside the firmware are not visible.

Where the axis limits come from:

- `--axis-limits -5:229,-0.5:227` — explicit X and Y ranges;
- `--printer k1se` (or `--axis-limits k1se`) — the printer's `axis_limits` in
  [`fleet/printers.yaml`](../../fleet/printers.yaml) or `printer-devices.yaml` (see
  [`3d printer`](printer.md#fleet-registry)); a printer without them gives SKIP.

Offline fleet printers (`access: offline` — the neighbour's P1S, Boris's A1 mini) are never
contacted, but their `axis_limits` and `trusted_macros` still serve this check:
`3d gcode-bounds plate.gcode --printer p1s` passes the stock Bambu firmware commands
(`G29`, `M620`–`M625`, `M1002`, `T255`, …) and the out-of-bed purge/wipe moves (P1S
X 0..256, Y −3..265) instead of FAILing on them
([#142](https://github.com/alex-mextner/3d-cli/issues/142)).

Options:

- `--printer NAME` — `3d gcode-bounds print/k1se/base.gcode --printer k1se`
- `--axis-limits SPEC` — `3d gcode-bounds print/k1se/base.gcode --axis-limits -5:229,-0.5:227`
- `--json` — `3d gcode-bounds print/u1_trees.gcode --json | jq '.checks[] | select(.status=="FAIL") | .detail'`

Exit: 0 PASS, WARN or SKIP; 1 FAIL; 2 usage error, unknown printer or missing file.

`3d print-preflight` runs the same checks, and `3d printer start` refuses on their FAIL.

## Examples

```bash
3d gcode-bounds print/k1se/base.gcode --printer k1se
3d gcode-bounds print/k1se/base.gcode --axis-limits -5:229,-0.5:227
3d gcode-bounds print/u1_trees.gcode
3d gcode-bounds print/u1_trees.gcode --json | jq '.checks[] | select(.status=="FAIL") | .detail'
```

Output for the K1 SE plate that faulted (`3d gcode-bounds k1_base_1_v2.gcode --printer k1se`):

```
BOUNDS k1_base_1_v2.gcode
  FAIL  axis travel X -5..229, Y -0.5..227 (k1se in ~/.config/3d-cli/printer-devices.yaml): 1 point(s) outside: line 2412 (62.47, -0.84)
  FAIL  print area  plate moves x 0.42..143.14, y -0.84..93.68; printable_area 0..220 x 0..220: 2 point(s) outside: line 1645 (61.76, -0.33), line 2412 (62.47, -0.84)
STATUS: FAIL
```
