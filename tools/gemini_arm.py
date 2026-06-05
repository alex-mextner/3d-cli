"""gemini_arm.py — agentic OpenSCAD modeling loop driven by Gemini over REST.

Purpose: one "arm" of the benchmark. Ask Gemini to build a Pantheon .scad, render it,
feed the renders (and, in 3dcli mode, the `3d` CLI fit-camera overlays + IoU numbers)
back, and iterate until the model says DONE or --max-rounds is hit.

Accessed-via: CLI only:
  python tools/gemini_arm.py --workdir DIR --mode {mcp|3dcli} --refs a.jpg,b.jpg \
      --model gemini-3.1-pro-preview --max-rounds 10

Invariants:
  * stdlib only; the model call goes through tools/gemini_client (also stdlib). openscad
    and bin/3d are reached via subprocess (NOT by importing command modules).
  * The base prompt text is byte-for-byte the task-specified ModelRift prompt; 3dcli mode
    appends exactly one extra line about the `3d` feedback being provided each round.
  * Each round writes <workdir>/pantheon.scad and renders it. Stops on DONE in the
    model text or when max-rounds is reached. Always writes <workdir>/run.json with
    {rounds, prompt_tokens_total, output_tokens_total, wall_seconds, scad_path}.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gemini_client  # noqa: E402  (local module, sys.path adjusted above)

OPENSCAD = os.environ.get("OPENSCAD_BIN", "/opt/homebrew/bin/openscad")
THREED = os.environ.get("THREED_BIN", "/Users/ultra/xp/3d-cli/bin/3d")

# EXACT ModelRift prompt — do not paraphrase.
BASE_PROMPT = (
    "see two ref images and build .scad file with openscad implementation of pantheon. "
    "use openscad CLI (available) to preview your work (by rendering openscad model to "
    ".png) and iterate until you are happy with the result."
)
# Single extra line appended only in 3dcli mode.
THREEDCLI_EXTRA = (
    "\nEach round you are also given the `3d` CLI feedback: fit-camera overlay images "
    "(your render in cyan over the reference in red) and the silhouette IoU numbers per "
    "reference, plus the `3d check` verdict. Use them to correct camera-visible shape "
    "and proportion errors."
)

# Two angles to render in every mode (eye_x,eye_y,eye_z,center_x,center_y,center_z).
_CAMERAS = {
    "r_front.png": "0,-300,90,0,0,40",
    "r_iso.png": "220,-220,160,0,0,40",
}


def _extract_scad(text: str) -> str:
    """Pull the ```scad ... ``` block out of the model text; fall back to whole text."""
    m = re.search(r"```(?:scad|openscad)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip() + "\n"
    return text.strip() + "\n"


def _run(cmd: list[str], timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
    """Run a subprocess, capturing output; never raise on nonzero (best-effort feedback)."""
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[gemini_arm] command failed: {' '.join(cmd)} :: {exc}", file=sys.stderr)
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr=str(exc))


def _render_openscad(scad: str, workdir: str) -> list[str]:
    """Render the .scad from the fixed camera angles into workdir; return PNG paths made."""
    made: list[str] = []
    for fname, cam in _CAMERAS.items():
        out = os.path.join(workdir, fname)
        cp = _run(
            [
                OPENSCAD,
                "-o",
                out,
                "--autocenter",
                "--viewall",
                "--imgsize=900,900",
                f"--camera={cam}",
                scad,
            ]
        )
        if os.path.isfile(out):
            made.append(out)
        else:
            print(f"[gemini_arm] openscad produced no {fname}:\n{cp.stderr[:400]}", file=sys.stderr)
    return made


def _threed_feedback(scad: str, refs: list[str], workdir: str) -> tuple[list[str], list[str]]:
    """Run `3d render --multi`, `3d fit-camera` per ref, `3d check`; return (image_paths, text_lines)."""
    images: list[str] = []
    lines: list[str] = []

    # multi-view render (front/back/left/right/top/iso) into workdir/views
    views_dir = os.path.join(workdir, "views")
    _run([THREED, "render", scad, "--multi", views_dir, "--render"])
    for v in ("front.png", "iso.png"):
        p = os.path.join(views_dir, v)
        if os.path.isfile(p):
            images.append(p)

    # fit-camera per reference -> overlay PNG + IoU
    for i, ref in enumerate(refs):
        out_json = os.path.join(workdir, f"fit_{i}.json")
        cp = _run([THREED, "fit-camera", scad, ref, "--out", out_json])
        iou = None
        overlay = None
        if os.path.isfile(out_json):
            try:
                with open(out_json, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                iou = data.get("iou")
                overlay = data.get("overlay")
            except (OSError, json.JSONDecodeError):
                pass
        # derive overlay if json didn't carry it (fit-camera writes <out_base>_overlay.png)
        if not overlay:
            cand = os.path.splitext(out_json)[0] + "_overlay.png"
            overlay = cand if os.path.isfile(cand) else None
        if overlay and os.path.isfile(overlay):
            images.append(overlay)
        lines.append(
            f"  ref[{i}] {os.path.basename(ref)}: IoU="
            + (f"{iou:.3f}" if isinstance(iou, (int, float)) else "n/a")
            + (" (overlay attached)" if overlay and os.path.isfile(overlay) else "")
        )
        if cp.returncode != 0 and iou is None:
            lines.append(f"    (fit-camera error: {cp.stderr.strip()[:200]})")

    # check verdict
    cp = _run([THREED, "check", scad])
    verdict = "PASS" if cp.returncode == 0 else "FAIL"
    tail = (cp.stdout or cp.stderr).strip().splitlines()
    lines.append(f"  3d check: {verdict}")
    if tail:
        lines.append("    " + " | ".join(tail[-3:]))

    return images, lines


def run_arm(
    workdir: str,
    mode: str,
    refs: list[str],
    model: str,
    max_rounds: int,
) -> dict[str, Any]:
    """Run the agentic loop; return (and write to run.json) the run summary."""
    os.makedirs(workdir, exist_ok=True)
    scad_path = os.path.join(workdir, "pantheon.scad")

    prompt = BASE_PROMPT + (THREEDCLI_EXTRA if mode == "3dcli" else "")
    ref_parts = [gemini_client.image_part(r) for r in refs]

    # 3dcli mode = the productive environment: feed the project's modeling lessons +
    # openscad skill as a system instruction (REST has no skill auto-discovery). mcp mode
    # stays the bare ModelRift baseline (no skills) for a fair A/B.
    system_text: str | None = None
    if mode == "3dcli":
        chunks: list[str] = []
        for rel in ("CLAUDE.md", os.path.join(".claude", "skills", "openscad", "SKILL.md")):
            p = os.path.join(workdir, rel)
            try:
                chunks.append(open(os.path.realpath(p), encoding="utf-8").read())
            except OSError:
                pass
        if chunks:
            system_text = "\n\n---\n\n".join(chunks)

    t0 = time.time()
    prompt_tokens_total = 0
    output_tokens_total = 0
    rounds = 0
    done = False

    for rnd in range(1, max_rounds + 1):
        rounds = rnd
        parts: list[dict[str, Any]] = [gemini_client.text_part(prompt)]
        parts.append(gemini_client.text_part("Reference image 1:"))
        parts.append(ref_parts[0])
        if len(ref_parts) > 1:
            parts.append(gemini_client.text_part("Reference image 2:"))
            parts.append(ref_parts[1])

        if rnd > 1:
            # feed back the latest renders (+ 3dcli overlays/numbers)
            parts.append(
                gemini_client.text_part(
                    f"Round {rnd}. Here is the current render of your latest .scad. "
                    "Improve it. Reply DONE (anywhere in your text) when satisfied; "
                    "otherwise return the full updated ```scad ... ``` block."
                )
            )
            for png in _latest_render_pngs(workdir):
                parts.append(gemini_client.image_part(png))
            if mode == "3dcli":
                imgs, lines = _threed_feedback(scad_path, refs, workdir)
                if lines:
                    parts.append(
                        gemini_client.text_part("3d CLI feedback:\n" + "\n".join(lines))
                    )
                for img in imgs:
                    parts.append(gemini_client.image_part(img))

        print(f"[gemini_arm] round {rnd}/{max_rounds} -> {model}", file=sys.stderr)
        res = gemini_client.generate(model, parts, system=system_text)
        prompt_tokens_total += res["prompt_tokens"]
        output_tokens_total += res["output_tokens"]
        text = res["text"]

        scad = _extract_scad(text)
        with open(scad_path, "w", encoding="utf-8") as fh:
            fh.write(scad)

        # render (mcp + 3dcli both do the openscad angles)
        _render_openscad(scad_path, workdir)

        if "DONE" in text:
            done = True
            print(f"[gemini_arm] model reported DONE at round {rnd}", file=sys.stderr)
            break

    summary = {
        "rounds": rounds,
        "done": done,
        "mode": mode,
        "model": model,
        "refs": refs,
        "prompt_tokens_total": prompt_tokens_total,
        "output_tokens_total": output_tokens_total,
        "wall_seconds": round(time.time() - t0, 2),
        "scad_path": scad_path,
    }
    with open(os.path.join(workdir, "run.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
    return summary


def _latest_render_pngs(workdir: str) -> list[str]:
    """The render PNGs from the previous round (the fixed-camera openscad outputs)."""
    out = []
    for fname in _CAMERAS:
        p = os.path.join(workdir, fname)
        if os.path.isfile(p):
            out.append(p)
    if not out:  # fallback: any r_*.png
        out = sorted(glob.glob(os.path.join(workdir, "r_*.png")))
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="gemini_arm",
        description=(
            "Agentic Gemini-over-REST loop that builds a Pantheon .scad and iterates "
            "on it. WHY: one benchmark arm comparing modeling with vs. without the `3d` "
            "CLI feedback. EXAMPLE: python tools/gemini_arm.py --workdir runs/a1 "
            "--mode 3dcli --refs refs/p1.jpg,refs/p2.jpg --max-rounds 8"
        ),
    )
    ap.add_argument("--workdir", required=True, help="output dir for .scad, renders, run.json")
    ap.add_argument(
        "--mode",
        choices=["mcp", "3dcli"],
        required=True,
        help="mcp = openscad renders only; 3dcli = also feed back `3d` fit-camera overlays + IoU",
    )
    ap.add_argument(
        "--refs",
        required=True,
        help="comma-separated reference image paths (2 expected), e.g. a.jpg,b.jpg",
    )
    ap.add_argument(
        "--model",
        default="gemini-3.1-pro-preview",
        help="Gemini model id (default gemini-3.1-pro-preview)",
    )
    ap.add_argument(
        "--max-rounds",
        type=int,
        default=10,
        help="max iterate rounds before stopping (default 10); also stops on DONE in text",
    )
    args = ap.parse_args(argv)

    refs = [r.strip() for r in args.refs.split(",") if r.strip()]
    if not refs:
        print("error: --refs must list at least one image path", file=sys.stderr)
        return 2
    for r in refs:
        if not os.path.isfile(r):
            print(f"error: reference not found: {r}", file=sys.stderr)
            return 2

    summary = run_arm(args.workdir, args.mode, refs, args.model, args.max_rounds)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
