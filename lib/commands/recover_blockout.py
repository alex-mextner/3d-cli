"""3d recover-blockout — closed parametric-recovery loop for image→3D (blockout family).

WHAT: turns a reference silhouette into tuned parameters of a parametric BLOCKOUT
  template (a temple/colonnade: column count + continuous dims), as a staged, honestly
  gated loop — perceive (VLM veto reads the column count) → generate → locked-framing
  view-bank contour fit → monotonic boundary refine (veto as a dispose-gate) → proof
  status. It emits a 6-artifact proof panel and a durable result label.

WHY: the image→3D pipeline was stuck because nothing GENERATED geometry — fit-camera
  only fits a camera to a fixed model and match only nudges an existing assembly. This
  command supplies the missing generator and closes the loop against boundary metrics
  plus a semantic-feature veto, so a silhouette that matches while the geometry is
  semantically wrong (wrong column count) is rejected.

HONESTY: `--synthetic` is the acceptance milestone — hidden params + a hidden camera are
  drawn from the same family, rendered to a reference, and recovered WITHOUT ever passing
  the hidden values to the fitter. Real-photo input is diagnostic-only and never claims
  photo→model success.

Examples:
  3d recover-blockout --synthetic --out recover/            # acceptance milestone
  3d recover-blockout --synthetic --size 200x160 --out r/
  3d recover-blockout photo.jpg --template temple --out r/  # real photo (diagnostic only)
"""
from __future__ import annotations

import os

from cli.env import require_openscad
from cli.pyrun import exec_tool
from cli.registry import Command
from errors import InputNotFound, UsageError

USAGE = """3d recover-blockout [<reference-image>] [options]
  Closed parametric-recovery loop for the blockout template family. Perceive the column
  count (VLM veto) -> generate a temple blockout -> locked-framing view-bank contour fit
  -> monotonic boundary refine (veto dispose-gate) -> proof-status gate. Emits a
  6-artifact proof panel + result.json with a durable recovery_status label.

Modes:
  --synthetic          run the synthetic parametric-recovery acceptance milestone
                       (hidden params/camera from the same family; recovered WITHOUT
                       leaking them to the fitter; can report recovery_status=ok)
  <reference-image>    real-photo diagnostic mode; recovery_status is capped at
                       'diagnostic' and never claims photo->model success

Options:
  --template NAME       blockout family (default temple)
  --out DIR             output directory (default ./recover_out)
  --size WxH            render size (default 240x200)
  --backend NAME        veto AI backend: claude|codex|opencode|ollama|mock
                        (default: ai.json backend, else first available)

Artifacts (in --out):
  proof_panel.png       6 cells: reference, mask, recovered render, contour error,
                        boundary metrics, proof status
  recovered_render.png  the recovered model rendered in the reference frame
  reference.png, reference_mask.png, changelog.md, result.json

Examples:
  3d recover-blockout --synthetic --out recover/
  3d recover-blockout --synthetic --size 200x160 --out recover/
  3d recover-blockout photo.jpg --template temple --backend mock --out recover/"""

_VALUE_FLAGS = {"--template", "--out", "--size", "--backend"}


def run(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if not argv:
        print(USAGE)
        return 1
    require_openscad("recover-blockout")

    tool_args: list[str] = []
    reference: str | None = None
    synthetic = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--synthetic":
            synthetic = True
            tool_args.append(a)
            i += 1
        elif a in _VALUE_FLAGS or ("=" in a and a.split("=", 1)[0] in _VALUE_FLAGS):
            if "=" in a:
                tool_args.append(a)
                i += 1
            else:
                if i + 1 >= len(argv):
                    raise UsageError(f"option {a} needs a value", command="recover-blockout")
                tool_args += [a, argv[i + 1]]
                i += 2
        elif a.startswith("-"):
            print(USAGE)
            raise UsageError(f"unknown option '{a}'", command="recover-blockout")
        else:
            if reference is not None:
                raise UsageError("only one reference image is accepted", command="recover-blockout")
            reference = a
            i += 1

    if not synthetic and reference is None:
        print(USAGE)
        raise UsageError(
            "provide a reference image or --synthetic", command="recover-blockout")
    if reference is not None:
        if not os.path.isfile(reference):
            raise InputNotFound(reference, command="recover-blockout")
        tool_args = [reference, *tool_args]

    return exec_tool("numpy,pillow,scipy", "img3d_loop.py", tool_args)


COMMAND = Command(
    name="recover-blockout",
    group="REFERENCE-MATCH PIPELINE",
    summary="closed parametric-recovery loop: silhouette -> tuned blockout params (--synthetic milestone)",
    usage=USAGE,
    run=run,
)
