# `3d fit-check` — does the socket still fit an insert that is already printed?

Parts of one assembly are often printed days apart, on different printers, while the model keeps being rebuilt. A rebuild can silently move or resize the socket — a pocket, a bow-tie slot — of an insert that already exists in plastic; the mismatch shows only at assembly, after the large mating part has been printed. `fit-check` locks the insert's **printed size** and the designed **gap per side**, both in the assembly frame, and fails before the mating part is printed.

## Usage

```
3d fit-check --insert A.stl[,B.stl] --socket S1.stl[,S2.stl] --gap MM [options]
3d fit-check --spec fits.yaml [--json]
```

```bash
3d fit-check --insert out/assembly/PFL__DOOR.stl --socket 'out/assembly/BASE2__*.stl' --gap 0.2 --printed-size 46.275,75.446,15.311
3d fit-check --insert KEY1.stl --socket BASE1.stl,BASE2.stl --gap 0.15 --printed-size 28,14,4.3 --any-turn --name KEY1
3d fit-check --insert plug.stl --socket plate.stl --gap 0.2 --gap-tol 0.05 --size-tol 0.05 --step 0.1 --min-depth 3.5
3d fit-check --insert magnet.stl --socket 'HALF_*.stl' --gap 0.1 --open none
3d fit-check --insert foot.stl --socket base.stl --gap 0.2 --open below
3d fit-check --spec fits.yaml
3d fit-check --spec fits.yaml --json | jq '.fits[] | {insert, status, gap_mm, socket_depth_mm}'
```

Insert and socket meshes (STL/3MF/OBJ, closed manifolds) must already be placed in the assembly frame, with the insert pushed home in its socket and inserted along Z. Comma-join (or glob — quote it) the parts of one side: they are unioned. `--flag=value` works too (`--insert=A.stl`).

## What is checked

| Check | FAIL when |
|---|---|
| size | the insert's bounding box differs from `--printed-size` by more than `--size-tol` on any axis (any axis order with `--any-turn`) |
| overlap | at any layer the insert section and the socket section overlap by more than 1e-4 mm² (the z of every such layer is reported), or — between the layers — the solids intersect by more than 1e-3 mm³ |
| gap | at a layer with socket material within 2·gap+1 mm of the insert, the clearance differs from `--gap` by more than `--gap-tol` |
| socket | no socket section lies within 2·gap+1 mm of the insert at any layer |
| depth | the socket is shallower than `--min-depth` (only when given) |
| open | moved out along its insertion direction (`--open above`, the default, or `below`), the insert meets socket material outside itself: a roof over the socket, or a shoulder above a wider foot (an undercut). The path is the insert swept in `--step` slabs (each slab carries the outline of the insert part already past it, shrunk 0.02 mm so contact faces do not count) and then its full outline past the socket's end. `--open none` skips it for an insert captured between parts. |

The layers start 0.05 mm above the insert's bottom and go up every `--step` mm to the insert's top. At each one the insert and the socket are sliced (manifold3d, rings combined even-odd), the socket is cut to the part within 2·gap+1 mm of the insert, and the **clearance** is the shapely distance from the insert section to that part — the gap on the tightest side. Every layer with socket material that close is checked, so a second pocket higher up, a lip or a shelf obeys the same gap; layers with nothing that close (an opening in the wall, above the socket) are skipped. A clearance is only sampled at the layers, but the insert and socket solids are also intersected as a whole, so a shelf thinner than `--step` cannot hide an interference. The **socket depth** runs from the insert's bottom to the top of the highest socket layer, found to ~0.001 mm by bisection; a socket whose upper part was flared beyond 2·gap+1 mm reads as shallower — guard that with `--min-depth`. Tolerances are tested on the unrounded measurements; the report shows them rounded. Without `--printed-size` the size is reported but not checked.

## Options

- `--insert A[,B]` — insert mesh(es), unioned; globs allowed; repeat the flag to add more (`--insert A.stl --insert B.stl`)
- `--socket S1[,S2]` — socket part(s), unioned; globs allowed; repeatable
- `--gap MM` — designed clearance per side
- `--printed-size X,Y,Z` — size of the printed insert in mm (`20x10x5` works too)
- `--any-turn` — accept the printed size in any axis order (the insert sits turned in the assembly), e.g. `--printed-size 28,14,4.3 --any-turn`
- `--name NAME` — insert name in the report, e.g. `--name KEY1` (default: the insert file names)
- `--size-tol MM` — printed-size tolerance, e.g. `--size-tol 0.05` (default 0.02)
- `--gap-tol MM` — clearance tolerance, e.g. `--gap-tol 0.05` (default 0.03)
- `--step MM` — layer step up the socket, e.g. `--step 0.1` (default 0.25)
- `--min-depth MM` — FAIL when the socket is shallower, e.g. `--min-depth 3.5` (default: not checked)
- `--open DIR` — the direction the socket must be open to: `above` (default), `below`, or `none` for a captured insert (a magnet closed in by a mating half), e.g. `--open none`
- `--spec FILE` — many fits from a YAML/JSON file, e.g. `--spec fits.yaml`; takes no pair flags
- `--json` — JSON report on stdout: `status` and `fits[]` with `insert`, `socket`, `insert_files`, `socket_files`, `status`, `insert_size_mm`, `printed_size_mm`, `any_turn`, `size_tol_mm`, `gap_set_mm`, `gap_tol_mm`, `step_mm`, `min_depth_mm`, `open`, `open_blocked_mm3` (socket volume in the insert's way out; null when open or not checked), `gap_mm` (min), `gap_max_mm`, `socket_depth_mm`, `overlap_z_mm`, `overlap_volume_mm3` (insert ∩ socket solids), `layers` (`z`, `gap` per socket layer) and `errors`

## Fits spec

```yaml
gap: 0.15                 # optional defaults for every fit: gap, any_turn, size_tol, gap_tol, step, min_depth, open
any_turn: true
fits:
  - name: PFL
    insert: out/assembly/PFL__DOOR.stl            # a path, a comma-joined list or a list; globs ok
    socket: [out/assembly/BASE2__*.stl]
    gap: 0.2
    printed_size: [46.275, 75.446, 15.311]
    any_turn: false
    min_depth: 3
  - name: KEY1
    insert: out/assembly/KEY1__SOIL.stl
    socket: [out/assembly/BASE1__*.stl, out/assembly/BASE2__*.stl]
    printed_size: [28, 14, 4.3]
```

Paths are relative to the spec file. Unknown keys are an error (a typo must not silently drop a check), and so are a non-number dimension (`gap: true`) and `any_turn` on a fit without `printed_size`.

## Exit codes

- `0` — `PASS`
- `1` — `FAIL` (a fit's size, overlap, gap, socket, depth or open check failed)
- `2` — usage / spec / load error (missing file, glob matching nothing, bad number, mesh that is not a closed manifold)

## Related

- [`mesh`](mesh.md) — watertight/manifold checks of each part on its own
- [`fit-niche`](fit-niche.md) — generate an insert for a described cavity
