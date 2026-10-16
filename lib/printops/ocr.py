"""ocr.py — the text of a spool label photo, read with tesseract (no LLM, issue #379).

WHAT: `read_label(path)` OCRs the photo turned by 0, 90, 180 and 270 degrees (ImageMagick:
  EXIF orientation applied, greyscale, at most 2400 px) and keeps the turn whose words tesseract
  is most confident about — phone photos of a spool are as often sideways as upright. Returns
  the text line by line (tesseract's own line grouping) with the turn and the score.

WHY: the deterministic path of epic #356 / #363: a label is read by OCR + rules
  (printops.spool_label) and confirmed by the user; a vision model may only add to it later
  (#364).

INVARIANTS: tesseract and ImageMagick run as subprocesses (stdlib only here); a missing binary
  is a MissingDependency with the install line. Languages: eng, plus rus when installed.
  The four turns run in parallel, bounded by cli.resources.pool_size, one tesseract thread each.
"""

from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from cli.env import require_magick, require_tesseract
from cli.resources import pool_size
from errors import InputNotFound, ThreeDError

TURNS = (0, 90, 180, 270)
MAX_SIDE_PX = 2400
MAX_INPUT_SIDE_PX = 16384  # larger photos are refused (phones shoot ≤ 8000 px)
MIN_WORD_CONF = (
    30.0  # tesseract confidence (0-100) below which a word is dropped as noise
)
SCORE_CONF = 60.0  # words at least this confident count toward a turn's score
TIMEOUT_S = 120


@dataclass(frozen=True)
class OcrText:
    text: str
    rotation: int  # degrees clockwise the photo was turned before OCR
    score: float  # confident letters read: the best turn has the most


def read_label(path: Path, *, command: str = "printops spool scan") -> OcrText:
    """The label text of the photo at `path`, from its best-reading turn."""
    if not path.is_file():
        raise InputNotFound(str(path), command=command)
    tess = require_tesseract(command)
    mgk = require_magick(command)
    langs = _langs(tess)
    with ThreadPoolExecutor(pool_size(len(TURNS))) as ex:
        results = list(
            ex.map(lambda t: _read_turn(tess, mgk, langs, path, t, command), TURNS)
        )
    return max(results, key=lambda r: r.score)


def _langs(tess: str) -> str:
    try:
        out = subprocess.run(
            [tess, "--list-langs"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return "eng"
    have = {ln.strip() for ln in out.splitlines()}
    return "+".join(x for x in ("eng", "rus") if x in have) or "eng"


def _read_turn(
    tess: str, mgk: str, langs: str, path: Path, turn: int, command: str
) -> OcrText:
    png = _run(
        [
            mgk,
            # a decompression bomb must not eat the shared machine: refuse huge images
            *("-limit", "width", str(MAX_INPUT_SIDE_PX)),
            *("-limit", "height", str(MAX_INPUT_SIDE_PX)),
            *("-limit", "memory", "512MiB"),
            # absolute, so a file named "-x.jpg" is never read as an option
            str(path.resolve()),
            "-auto-orient",
            "-rotate",
            str(turn),
            "-colorspace",
            "Gray",
            "-resize",
            f"{MAX_SIDE_PX}x{MAX_SIDE_PX}>",
            "png:-",
        ],
        None,
        f"ImageMagick could not read {path}",
        command,
    )
    tsv = _run(
        [tess, "stdin", "stdout", "-l", langs, "--psm", "3", "tsv"],
        png,
        f"tesseract failed on {path}",
        command,
        env={**os.environ, "OMP_THREAD_LIMIT": "1"},
    )
    text, score = parse_tsv(tsv.decode("utf-8", errors="replace"))
    return OcrText(text, turn, score)


def _run(
    argv: list[str],
    stdin: bytes | None,
    what: str,
    command: str,
    env: dict[str, str] | None = None,
) -> bytes:
    try:
        r = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            timeout=TIMEOUT_S,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ThreeDError(f"{what}: {e}", command=command) from e
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise ThreeDError(
            f"{what}: {err[-1] if err else f'exit {r.returncode}'}", command=command
        )
    return r.stdout


def parse_tsv(tsv: str) -> tuple[str, float]:
    """tesseract TSV → (text with one label line per line, score). Words under MIN_WORD_CONF
    are dropped; the score sums the alphanumeric letters of words from SCORE_CONF up."""
    lines: dict[tuple[str, str, str, str], list[str]] = {}
    score = 0.0
    for row in tsv.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5":
            continue
        word = cols[11].strip()
        try:
            conf = float(cols[10])
        except ValueError:
            continue
        if not word or conf < MIN_WORD_CONF:
            continue
        lines.setdefault((cols[1], cols[2], cols[3], cols[4]), []).append(word)
        alnum = sum(ch.isalnum() for ch in word)
        if conf >= SCORE_CONF and alnum >= 2:
            score += alnum * conf / 100
    return "\n".join(" ".join(ws) for ws in lines.values()), round(score, 1)
