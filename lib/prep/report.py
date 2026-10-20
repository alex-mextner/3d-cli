"""report.py — the proof report of a `3d prep` run: one markdown file plus a `proof/` directory of PNGs.

WHAT: `ProofReport` collects one section per pipeline step (verdict, one-line summary, key numbers,
  images, notes) and `write()` renders a single markdown document: header (model, digest, printer, ...),
  a verdict table, the per-step sections with relative image links, and a closing "Honest limits" list.
WHY: the print preparation proof used to be hand-assembled per model; the orchestrator now feeds step
  results in and gets the same document every time, readable on GitHub or in any markdown viewer.
INVARIANTS:
  * Pure string building: no clock, no environment. Timestamps appear only if the caller passes them in
    `meta`; the same inputs give byte-identical output.
  * Verdicts are the words `PASS`/`WARN`/`FAIL`/`SKIP`, never symbols. A FAIL section is flagged with a
    bold `FAIL` call-out under its heading and the overall verdict (worst of all steps) leads the report.
  * Images are copied (or, for arrays, written) under `<out_dir>/proof/` with a section-number prefix, and
    linked relative to the markdown file, so the output directory is self-contained and movable.
  * Table cells are escaped (`|`, newlines) so a summary can never break the table.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from errors import InputNotFound, InvalidArgument
from prep.contracts import Verdict, worst
from prep.raster import write_png

REPORT_NAME = "proof-report.md"
PROOF_DIR = "proof"
_VERDICTS: dict[str, Verdict] = {
    "PASS": "PASS",
    "WARN": "WARN",
    "FAIL": "FAIL",
    "SKIP": "SKIP",
}

ImageSource = Path | NDArray[np.uint8]


@dataclass
class _Section:
    title: str
    verdict: Verdict
    summary: str
    rows: list[tuple[str, str]]
    images: list[ImageSource]
    notes: list[str]
    seconds: float | None


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "step"


@dataclass
class ProofReport:
    """Builder: `add_section(...)` per step in pipeline order, then `write(out_dir, ...)`."""

    _sections: list[_Section] = field(default_factory=list)
    _limits: list[str] = field(default_factory=list)

    def add_section(
        self,
        title: str,
        verdict: str,
        summary: str,
        rows: Sequence[tuple[str, str]] | None = None,
        images: Sequence[ImageSource] | None = None,
        notes: Sequence[str] | None = None,
        *,
        seconds: float | None = None,
    ) -> None:
        """Append a step. `images` are PNG paths (copied into proof/) or RGB arrays (written there)."""
        if verdict not in _VERDICTS:
            raise InvalidArgument("verdict", verdict, list(_VERDICTS), command="prep")
        self._sections.append(
            _Section(
                title,
                _VERDICTS[verdict],
                summary,
                list(rows or []),
                list(images or []),
                list(notes or []),
                seconds,
            )
        )

    def add_limit(self, text: str) -> None:
        """Append one line to the closing "Honest limits" list."""
        self._limits.append(text)

    @property
    def overall(self) -> Verdict:
        """Worst verdict over all sections (SKIP when there are none)."""
        return worst([s.verdict for s in self._sections])

    def write(
        self,
        out_dir: str | Path,
        *,
        title: str,
        subtitle: str = "",
        meta: Mapping[str, str] | None = None,
    ) -> Path:
        """Write the markdown and the images; returns the markdown path."""
        out = Path(out_dir)
        proof = out / PROOF_DIR
        proof.mkdir(parents=True, exist_ok=True)
        links = [
            self._materialise(i, s, proof) for i, s in enumerate(self._sections, 1)
        ]

        lines: list[str] = [f"# {title}", ""]
        if subtitle:
            lines += [subtitle, ""]
        lines += [f"**Overall: {self.overall}**", ""]
        for key, value in (meta or {}).items():
            lines.append(f"- **{_cell(key)}**: {_cell(value)}")
        if meta:
            lines.append("")

        lines += [
            "| # | Step | Verdict | Summary | Seconds |",
            "|---|------|---------|---------|---------|",
        ]
        for i, s in enumerate(self._sections, 1):
            sec = "-" if s.seconds is None else f"{s.seconds:.2f}"
            lines.append(
                f"| {i} | {_cell(s.title)} | {s.verdict} | {_cell(s.summary)} | {sec} |"
            )
        lines.append("")

        for i, (s, imgs) in enumerate(zip(self._sections, links, strict=True), 1):
            lines += [f"## {i}. {s.title} - {s.verdict}", ""]
            if s.verdict == "FAIL":
                lines += [f"> **FAIL**: {s.summary}", ""]
            else:
                lines += [s.summary, ""]
            if s.rows:
                lines += ["| Key | Value |", "|-----|-------|"]
                lines += [f"| {_cell(k)} | {_cell(v)} |" for k, v in s.rows]
                lines.append("")
            for rel in imgs:
                lines += [f"![{Path(rel).stem}]({rel})", ""]
            for note in s.notes:
                lines.append(f"- {note}")
            if s.notes:
                lines.append("")

        lines += ["## Honest limits", ""]
        lines += [f"- {x}" for x in self._limits] or ["- None recorded."]
        lines.append("")

        path = out / REPORT_NAME
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    @staticmethod
    def _materialise(index: int, section: _Section, proof: Path) -> list[str]:
        """Put the section's images under proof/ and return their links relative to the markdown file."""
        rels: list[str] = []
        for n, src in enumerate(section.images, 1):
            if isinstance(src, Path):
                if not src.is_file():
                    raise InputNotFound(str(src), command="prep")
                dest = proof / f"{index:02d}-{src.name}"
                if src.resolve() != dest.resolve():
                    shutil.copyfile(src, dest)
            else:
                dest = proof / f"{index:02d}-{_slug(section.title)}-{n}.png"
                write_png(dest, src)
            rels.append(f"{PROOF_DIR}/{dest.name}")
        return rels
