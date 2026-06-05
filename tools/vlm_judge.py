"""vlm_judge.py — VLM likeness judge for a render vs. a reference photo (stdlib only).

Purpose: score how well a rendered model matches a reference image, on a fixed 0-5
rubric (massing, rotunda+dome, portico+columns, pediment, overall likeness). Used by the
benchmark to turn "looks like the Pantheon?" into a number a script can compare.

Accessed-via: CLI `python tools/vlm_judge.py <render.png> <reference.jpg>`; also importable
(`judge(...)`) by the benchmark harness.

Invariants:
  * stdlib only (delegates the HTTP call to tools/gemini_client which is also stdlib).
  * Sends exactly two images (render + reference) + the rubric text, in that order, so the
    model knows which is which. Mime types follow the actual file extensions.
  * Returns STRICT JSON {score:0-5, per_aspect:{...}, reason:"..."}; fences are stripped
    and responseMimeType=application/json is requested to keep parsing robust.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gemini_client  # noqa: E402  (local module, sys.path adjusted above)

# The five rubric aspects, fixed so scores are comparable across runs/models.
ASPECTS = ["massing", "rotunda_dome", "portico_columns", "pediment", "overall_likeness"]

RUBRIC = """You are a strict architectural-likeness judge.

IMAGE 1 is a RENDER of a 3D model. IMAGE 2 is a REFERENCE photo of the real building
(the Roman Pantheon). Judge how well the RENDER reproduces the REFERENCE.

Score these five aspects, each 0-5 (0 = absent/wrong, 5 = faithful match):
  - massing: overall proportions and silhouette (a low cylindrical drum + a low dome,
    fronted by a rectangular porch block).
  - rotunda_dome: the cylindrical rotunda topped by a hemispherical/segmental dome.
  - portico_columns: the projecting front porch with a row of free-standing columns.
  - pediment: the triangular gable resting on the columns above the entrance.
  - overall_likeness: gestalt — would someone recognize this as the Pantheon?

Then give one integer "score" 0-5 = the overall verdict (use your judgment; it need not
be the mean).

Respond with STRICT JSON ONLY, no prose, no code fences, in exactly this shape:
{"score": <int 0-5>, "per_aspect": {"massing": <int 0-5>, "rotunda_dome": <int 0-5>,
"portico_columns": <int 0-5>, "pediment": <int 0-5>, "overall_likeness": <int 0-5>},
"reason": "<one or two sentences>"}"""


def _strip_fences(text: str) -> str:
    """Remove a ```json ... ``` (or bare ```) wrapper if the model added one."""
    t = text.strip()
    if t.startswith("```"):
        # drop first fence line (``` or ```json) and a trailing fence
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def _coerce(parsed: dict[str, Any]) -> dict[str, Any]:
    """Normalize the model's JSON into the strict {score, per_aspect{...}, reason} shape."""
    per_in = parsed.get("per_aspect") or {}
    per_out: dict[str, int] = {}
    for a in ASPECTS:
        try:
            per_out[a] = int(round(float(per_in.get(a, 0))))
        except (TypeError, ValueError):
            per_out[a] = 0
        per_out[a] = max(0, min(5, per_out[a]))
    try:
        score = int(round(float(parsed.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(5, score))
    return {
        "score": score,
        "per_aspect": per_out,
        "reason": str(parsed.get("reason", "")).strip(),
    }


def judge(
    render_png: str,
    reference_jpg: str,
    model: str = "gemini-2.5-flash",
) -> dict[str, Any]:
    """Score `render_png` against `reference_jpg`; return strict {score, per_aspect, reason}.

    Raises on missing files. On unparseable output, returns a 0 score whose `reason`
    carries the raw text so the caller can see what went wrong.
    """
    for p in (render_png, reference_jpg):
        if not os.path.isfile(p):
            raise FileNotFoundError(p)

    parts = [
        gemini_client.text_part(RUBRIC),
        gemini_client.text_part("IMAGE 1 (RENDER):"),
        gemini_client.image_part(render_png),
        gemini_client.text_part("IMAGE 2 (REFERENCE):"),
        gemini_client.image_part(reference_jpg),
    ]
    res = gemini_client.generate(
        model,
        parts,
        generation_config={"responseMimeType": "application/json", "temperature": 0},
    )
    text = _strip_fences(res["text"])
    try:
        parsed = json.loads(text)
        out = _coerce(parsed)
    except (json.JSONDecodeError, AttributeError):
        out = {
            "score": 0,
            "per_aspect": {a: 0 for a in ASPECTS},
            "reason": f"UNPARSEABLE judge output: {res['text'][:500]!r}",
        }
    out["_tokens"] = {
        "prompt": res["prompt_tokens"],
        "output": res["output_tokens"],
    }
    return out


def _main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] in ("-h", "--help"):
        print("usage: python tools/vlm_judge.py <render.png> <reference.jpg> [model]")
        print(
            "  WHY: turns 'does it look like the Pantheon?' into a 0-5 score on a fixed\n"
            "       rubric (massing, rotunda+dome, portico+columns, pediment, overall),\n"
            "       so the benchmark can rank arm runs objectively.\n"
            "  EXAMPLE: python tools/vlm_judge.py work/r_front.png refs/pantheon.jpg"
        )
        return 0 if (argv and argv[0] in ("-h", "--help")) else 2
    model = argv[2] if len(argv) > 2 else "gemini-2.5-flash"
    result = judge(argv[0], argv[1], model=model)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
