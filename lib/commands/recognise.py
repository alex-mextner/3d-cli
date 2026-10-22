"""3d recognise — class proposals for the instances of a segmented photo (CLIP text zero-shot + bank retrieval).

WHAT: reads the `3d segment` instances.json of a photo and proposes what each instance is: CLIP text prompts per category,
  calibrated by subtracting the per-class mean similarity over the photo's instances (raw CLIP is biased towards a few classes),
  optionally refined by retrieval over a bank index (`3d bank index`) that is searched ONLY inside the proposed classes.

WHY: the UI shows the proposal and lets the user confirm it; downstream stages (scale, fit) pick their template from the class.
  The algorithm lives in packages/threed-vision (`threed_vision.recognise.Recogniser`); this command is a thin launcher.

Examples:
  3d recognise tools.jpg --instances seg/instances.json --json
  3d recognise tools.jpg --instances seg/instances.json --categories "screwdriver,pliers,hammer" --topk 3
  3d recognise tools.jpg --instances seg/instances.json --bank bank/ --ids 70,87
"""

from __future__ import annotations

from cli.registry import Command
from cli.vision_env import run_stage

USAGE = """3d recognise <image> --instances DIR/instances.json [options]
  Class proposals per instance (CLIP B/32 text prompts, class-mean calibrated) + optional bank matches.
  Needs the vision venv and the clip-b32 model (`3d vision-models pull clip-b32`; `--bank` also dinov2-s).

Options:
  --instances FILE          instances.json from `3d segment` (required; masks are read next to it)
  --categories FILE|a,b,c   vocabulary: a comma list, or a file (one per line, `label: synonym, synonym`; a JSON list of strings or
                            {"id","prompts"|"name"+"synonyms"} objects). Default: 22 workshop tools (screwdriver, pliers, hammer, ...)
  --bank DIR                bank index from `3d bank index`; adds `bank_topk`, searched inside the best --rerank-classes text classes
  --topk N                  text classes per instance (default 5)
  --bank-topk N             bank matches per instance (default = --topk)
  --rerank-classes N        restrict the bank search to the best N text classes (default 3)
  --metric auto|clip|dino|sil|clip+dino|all   bank metric (auto: appearance when the bank is textured, silhouette otherwise)
  --calibrate | --no-calibrate   subtract the per-class mean similarity over the photo's larger instances (default on; needs >= 8;
                            --calibrate-prior is an alias)
  --pool-size N             calibrate over the N largest instances (default 60, the measured protocol; 0 = all instances)
  --pool-min-area-frac F    ... that cover at least this image fraction (default 0.00083 = 2500 px of 2000x1500)
  --ids 3,7                 report only these instance ids (calibration still uses all)
  --device auto|cuda|cpu
  --json                    print exactly one JSON object

Output: {"instances":[{"id","text_topk":[{"label","score","raw"}],"bank_topk":[{"bank_id","score","metric",...}],"calibrated"}],
         "timing_ms_per_crop", "device", "n_crops", "categories", "calibrated"}
Exit: 0 ok, 2 bad input, 127 missing model/dependency (names the `3d vision-models pull` command).

Examples:
  3d recognise tools.jpg --instances seg/instances.json --json
  3d recognise tools.jpg --instances seg/instances.json --categories "screwdriver,pliers,hammer" --topk 3
  3d recognise tools.jpg --instances seg/instances.json --categories tool-classes.json --bank bank/ --ids 70,87
  3d recognise tools.jpg --instances seg/instances.json --bank bank/ --metric sil --rerank-classes 5 --bank-topk 3   # shape retrieval, wider class set
  3d recognise tools.jpg --instances seg/instances.json --no-calibrate --device cpu                                    # raw CLIP scores, CPU
  3d recognise tools.jpg --instances seg/instances.json --pool-size 0                                                  # calibrate over every instance
  3d recognise tools.jpg --instances seg/instances.json --pool-size 40 --pool-min-area-frac 0.002                      # smaller, larger-object pool"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    return run_stage("recognise", argv, heavy=True)


COMMAND = Command(
    name="recognise",
    group="REFERENCE-MATCH PIPELINE",
    summary="class proposals for segmented instances (CLIP text + bank retrieval; threed-vision)",
    usage=USAGE,
    run=run,
)
