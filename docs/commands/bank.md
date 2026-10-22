# `3d bank` — thumbnails, embedding index and retrieval over a reference bank

Three actions over a bank of reference objects: `thumbs` renders lie-flat thumbnails from meshes, `index` embeds them
(CLIP ViT-B/32 + DINOv2-small + a silhouette descriptor) into a file, `query` returns the nearest entries for an object in a photo.
Implemented by `threed_vision.bank` in `packages/threed-vision`; `3d bank` is a thin launcher (`lib/cli/vision_env.py`) that runs
`python -m threed_vision bank ...` in the vision venv. `3d recognise --bank` uses the same index
([recognise.md](recognise.md)).

## TEXTURED THUMBNAILS ARE REQUIRED for image retrieval

CLIP and DINOv2 compare *appearance*. A thumbnail rendered from an untextured mesh is a flat shaded grey-blue shape: in the vision
spike (34 STL tools, 33 hand-labelled crops of a real photo) grey renders gave top-3 of only 0.30 (CLIP) to 0.45 (silhouette), and only
0.39 for DINOv2 — a coin flip, not a recogniser. Any bank that wants image retrieval must therefore carry **colour**:

- meshes with vertex / face colours or an image texture (`.ply`, `.obj` + texture, `.glb`/`.gltf`, `.3mf`), rendered as they are, or
- real photographs of the reference objects, supplied as a hand-made `bank.json` (see below).

`bank thumbs` records `textured` per entry and for the whole bank. When any entry fell back to shaded grey, `bank.json` gets
`"textured": false` and a `"warning"` string, the command prints `WARNING: ...` on stderr, and `index.json` repeats the warning.
`query --metric auto` then uses the **silhouette** descriptor (shape only) for a bank that is mostly grey (< 50 % textured) and the
appearance embeddings (`clip+dino`) otherwise. Silhouette retrieval is the honest fallback, not a substitute: it returns the generic
"rectangular box" for most boxes.

**hsw-editor's current bank holds HOLDERS (hooks, bins, inserts, panels), not tools.** Its thumbnails show the holder, so they cannot be
retrieved by the look of a screwdriver lying on a carpet. Until a *tool* reference library with textured thumbnails exists, recognition
in hsw-editor is text-prompt based (`3d recognise --categories <tool classes>`); the bank only maps a recognised class to the holders
that fit it. A textured tool bank (colour meshes or photos with `extents_mm`) is tracked in [#485](https://github.com/alex-mextner/3d-cli/issues/485).

## Usage

```
3d bank thumbs <mesh-dir|bank.json> -o DIR [--view longest-axis|top|iso] [--px 192] [--categories map.json] [--unit-scale F] [--json]
3d bank index  <bank-dir|bank.json> [-o DIR] [--device auto|cuda|cpu] [--json]
3d bank query  <image> --index DIR [--mask M.png | --instances seg/instances.json --id N] [--topk 5]
               [--metric auto|clip|dino|sil|clip+dino|all] [--category a,b] [--device auto|cuda|cpu] [--json]
```

```bash
3d bank thumbs tools/ -o bank/ --px 192                    # NAME.png, NAME_sil.png, bank.json
3d bank index bank/                                        # bank/index.npz + bank/index.json (needs clip-b32, dinov2-s)
3d bank query tools.jpg --index bank/ --instances seg/instances.json --id 70 --topk 5 --json
3d bank query crop.png --index bank/ --metric sil --category screwdriver,pliers --device cpu
3d bank thumbs meshes/ -o bank/ --view iso --px 256 --categories names.json   # iso view, 256 px, {"mesh name": "category"} map
3d bank thumbs glb-tools/ -o bank/ --unit-scale 1000                           # glTF meshes are in metres
3d bank thumbs photos/bank.json -o bank/                                       # a hand-made bank of real photos
```

### `thumbs`

Renders every mesh (`.stl .obj .ply .glb .gltf .3mf .off`, searched recursively) **lying flat**: the camera looks along the *shortest*
extent with the *longest* extent vertical in the image, the way a tool lies on a table (`--view top` looks down -Z, `--view iso` is a
fixed three-quarter view). Painter's-algorithm rasteriser, light shading by |normal . view|, face colours taken from the mesh
(image textures are sampled at the face-centroid UV); the view is always a rotation, never a mirror. `--unit-scale` converts mesh
units to millimetres (glTF is metres: `1000`).

`bank.json`:

```json
{"version":1,"view":"longest-axis","px":192,"textured":false,"n_textured":0,
 "warning":"34 of 34 entries are untextured shaded-grey renders: CLIP/DINOv2 image retrieval needs colour ...",
 "entries":[{"id":"screwdriver_1","name":"screwdriver_1","category":"screwdriver","thumb":"screwdriver_1.png",
             "sil":"screwdriver_1_sil.png","extents_mm":[212.4,24.0,24.0],"mm_per_px":1.2292,"textured":false}]}
```

`extents_mm` is `[L, W, T]` sorted descending, `mm_per_px` the thumbnail scale (so a recognised bank entry gives the fitter a physical
size). `category` comes from `categories.json` next to the meshes (or `--categories`): `{"mesh name": "category"}`; without one, name rules
map the default 22-class vocabulary of [recognise.md](recognise.md) (`screwdriver_3` -> `screwdriver`, `insulation_tape_1` -> `roll of
tape`, anything unknown -> `other`). Ids are file stems, made unique with `_2`, `_3` ... for repeated stems.

A **hand-made bank** (real photos) is a `bank.json` whose entries have `id`, `thumb` (+ optional `sil`, `category`, `name`, `extents_mm`,
`mm_per_px`); `bank thumbs bank.json -o DIR` copies the images next to a normalised `bank.json` (entries are assumed textured).
Photos should show the object lying flat, longest axis vertical, on a plain background; a `sil` mask lets the index crop tightly.

### `index`

Embeds each thumbnail as a tightly cropped object on the neutral CLIP background (CLIP and DINOv2-small, both L2-normalised) and stores
a 96x96 principal-axis silhouette descriptor. Output `index.npz` (`clip` (N,512) float32, `dino` (N,768) float32, `sil` bit-packed) and
`index.json` (`meta` + entries). The index directory is self-contained (thumbnails are copied next to it when `-o` is another directory);
**loading it needs numpy and json only: no network, no models** — `query --metric sil` runs with no model at all. Models are never
downloaded implicitly (exit 127 names `3d vision-models pull clip-b32 dinov2-s`).

### `query`

Returns the top-k entries with scores, best first. With a mask (`--mask`, or `--instances` + `--id` of a `3d segment` run) the object is cut
out, rotated so its longest axis is vertical *like the thumbnails*, and matched both ways up (the axis sign is ambiguous): the score of an entry
is the better of the two views. Without a mask the whole image is the object (no rotation, no silhouette metric).

| `--metric` | score |
|---|---|
| `clip` / `dino` | cosine of the CLIP / DINOv2 embeddings (best of the two orientations) |
| `sil` | IoU of the principal-axis silhouette descriptors, best of the 4 flips (rotation, scale and position invariant; needs a mask) |
| `clip+dino` | mean of the two cosines |
| `all` | mean of clip, dino and sil |
| `auto` (default) | `clip+dino` for a bank that is >= 50 % textured (or without a mask), else `sil` |

```json
{"matches":[{"bank_id":"screwdriver_3","name":"screwdriver_3","category":"screwdriver","score":0.8123,"metric":"clip+dino",
             "extents_mm":[212.4,24.0,24.0],"thumb":"bank/screwdriver_3.png"}],"metric":"clip+dino","timing_ms":41.2,"masked":true}
```

Exit codes: 0 ok, 2 bad input (missing mesh dir / index / image, mask of another size, unknown view), 127 missing model or `trimesh`
(`uv pip install 'packages/threed-vision[bank]'`).

## Python API (what a service calls)

```python
from threed_vision import bank, io
from threed_vision.clip import ClipEncoder, DinoEncoder

bank.build_thumbnails("tools/", "bank/", view="longest-axis", px=192)   # -> Path("bank/bank.json"); light, trimesh only
clip, dino = ClipEncoder("auto"), DinoEncoder("auto")                    # shared, resident encoders (optional; default: created and freed)
idx_dir = bank.build_index("bank/bank.json", "bank/", clip=clip, dino=dino)   # -> Path("bank/"), holds index.npz + index.json
index = bank.load_index(idx_dir)                                         # numpy + json only; keep it in memory in a service

rgb = io.load_rgb("tools.jpg")
hits = bank.query(index, rgb, topk=5, mask=instance["mask"], clip=clip, dino=dino)   # mask: bool HxW of the object inside `rgb`
# [{"bank_id","name","category","score","metric","extents_mm","thumb"}, ...] best first
hits = bank.query(idx_dir, crop_rgb, topk=5, metric="clip", categories=["screwdriver"])   # a bare crop, restricted to one class
```

`query` accepts an index directory or a loaded `BankIndex`; `metric` is keyword-only. Pass `align=False` to skip the upright rotation of the object.
`bank.silhouette_descriptor(mask)` / `bank.silhouette_variants(desc)` expose the shape descriptor.

## Measured

The 34 untextured STL tools of the spike (`refit/tools`) and the 33 labelled crops of the carpet photo: accuracy table in
[recognise.md](recognise.md#measured) (grey bank, top-1 0.24-0.36, top-3 0.36-0.42 for the three metrics). Through `bin/3d`: `bank thumbs` renders the 34
STLs in 1.6 s wall (192 px, CPU), `bank index` embeds them in 3.2 s (4.4 s wall incl. start-up, RTX 4060), `index.npz` is 214 KB, `bank query` takes
40-70 ms with the silhouette metric. Retrieval accuracy for a **textured** bank is not measured yet: no textured tool library exists (tracked in
[#485](https://github.com/alex-mextner/3d-cli/issues/485)), so the numbers are the grey-bank worst case, not what a textured bank will give.
