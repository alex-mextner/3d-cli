"""3d bank — reference bank of thumbnails + embedding index + retrieval (`thumbs`, `index`, `query`).

WHAT: `thumbs` renders each mesh of a directory lying flat (longest axis vertical) into a thumbnail + silhouette + bank.json with
  extents_mm and mm_per_px; `index` embeds the thumbnails (CLIP + DINOv2) and silhouettes into index.npz + index.json (a file that
  loads without network); `query` returns the nearest bank entries for an object crop.

WHY: recognising a tool by its look needs reference images. TEXTURE MATTERS: CLIP/DINOv2 compare appearance, so a bank of grey
  renders retrieves badly (spike: top-3 0.30-0.45). Meshes with colours/textures (or real photo thumbnails via a hand-made
  bank.json) are what makes image retrieval work; untextured entries are flagged `textured:false` with a warning.
  The algorithm lives in packages/threed-vision (`threed_vision.bank`); this command is a thin launcher.

Examples:
  3d bank thumbs tools/ -o bank/
  3d bank index bank/
  3d bank query crop.png --index bank/ --topk 5 --json
"""

from __future__ import annotations

from cli.registry import Command
from cli.vision_env import run_stage

USAGE = """3d bank thumbs <mesh-dir|bank.json> -o DIR [--view longest-axis|top|iso] [--px 192] [--categories map.json] [--unit-scale F] [--json]
3d bank index <bank-dir|bank.json> [-o DIR] [--device auto|cuda|cpu] [--json]
3d bank query <image> --index DIR [--mask M.png | --instances seg/instances.json --id N] [--topk 5] [--metric ...] [--category a,b] [--device auto|cuda|cpu] [--json]

  A reference bank for image retrieval. Needs the vision venv; `index` / `query` need the clip-b32 and dinov2-s models
  (`3d vision-models pull clip-b32 dinov2-s`); `thumbs` needs only trimesh (`uv pip install 'packages/threed-vision[bank]'`).

TEXTURED THUMBNAILS ARE REQUIRED for image retrieval: CLIP/DINOv2 compare appearance and grey renders of untextured meshes retrieve
  badly (spike: top-3 0.30-0.45 on real photos). A bank needs coloured/textured meshes or real photo thumbnails.

thumbs: renders every mesh (stl/obj/ply/glb/gltf/3mf/off) under the directory lying flat (look along the SHORTEST axis, the
  longest axis vertical, like a tool on a table) and writes NAME.png, NAME_sil.png and bank.json (extents_mm [L,W,T] in mm,
  mm_per_px, category, textured). Vertex/face colours and textures are rendered; a mesh without any is shaded grey and marked
  `textured:false`, with a WARNING (bank.json "warning", printed on stderr): grey thumbnails retrieve badly, bring coloured
  meshes or real photo thumbnails (a hand-made bank.json: entries with id, thumb, optional sil/category/extents_mm).
  --view      longest-axis (default, lie-flat) | top (look down -Z) | iso
  --px        thumbnail size (default 192)
  --categories  JSON {"mesh name": "category"} (default: categories.json next to the meshes, else name rules for the default vocabulary)
  --unit-scale  mesh units -> mm (default 1; glTF is metres, use 1000)
index:  embeds the thumbnails into DIR/index.npz + index.json (default DIR: the bank directory); fully offline afterwards.
query:  nearest entries for an object; with --mask (or --instances + --id) the object is cut out and rotated upright like the
  thumbnails; --metric auto = appearance (clip+dino) for a textured bank, silhouette for a grey one.

Exit: 0 ok, 2 bad input, 127 missing model/dependency.

Examples:
  3d bank thumbs refit/tools -o bank/ --px 192
  3d bank index bank/
  3d bank query tools.jpg --index bank/ --instances seg/instances.json --id 70 --topk 5 --json
  3d bank query crop.png --index bank/ --metric sil --category screwdriver,pliers --device cpu
  3d bank thumbs meshes/ -o bank/ --view iso --px 256 --categories names.json      # iso view, 256 px, {"mesh": "category"} map
  3d bank thumbs glb-tools/ -o bank/ --unit-scale 1000                              # glTF meshes are in metres
  3d bank thumbs photos/bank.json -o bank/                                          # a hand-made bank of real photos"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    return run_stage("bank", argv, heavy=True)


COMMAND = Command(
    name="bank",
    group="REFERENCE-MATCH PIPELINE",
    summary="reference bank: lie-flat thumbnails, CLIP/DINOv2 embedding index, nearest-entry query (threed-vision)",
    usage=USAGE,
    run=run,
)
