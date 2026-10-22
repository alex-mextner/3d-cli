"""cache.py — incremental step cache of the prep work dir.

WHAT: every step stores `<workdir>/steps/<step>.json` = {key, seconds, result, artifacts}; a later run with the
  same key and all artifacts still on disk reuses it instead of recomputing.
WHY: in the Chekhov session `fill_cavity` ran 7x, `repair` 5x, `build_3mf`/`verify_gcode` 13-14x — each from the
  start, on 411k faces, because a downstream tweak (colours, notch shape) re-ran the whole chain. Keyed per step, a
  colour change redoes colours -> slice only, and the 30-60 s mesh stages stay cached.
INVARIANTS: the key of a step hashes its own parameters AND the key of the step it consumes, so a change
  propagates downstream and nothing else; stdlib only; JSON written atomically (tmp + rename).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: bump when a step's algorithm changes in a way old cached results must not survive
CODE_VERSION = "1"


def make_key(name: str, params: dict[str, Any], upstream: str = "") -> str:
    """Stable hex key of a step: its name, code version, parameters and the key of its input."""
    payload = json.dumps(
        [CODE_VERSION, name, params, upstream], sort_keys=True, default=str
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class Entry:
    """A cached step record."""

    key: str
    seconds: float
    result: dict[str, Any]
    artifacts: list[str]


class StepCache:
    """`<workdir>/steps/*.json` records with artifact paths stored relative to the workdir."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.dir = workdir / "steps"

    def path(self, step: str) -> Path:
        return self.dir / f"{step}.json"

    def load(self, step: str, key: str) -> Entry | None:
        """The cached record when its key matches and every artifact still exists, else None."""
        p = self.path(step)
        if not p.is_file():
            return None
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if raw.get("key") != key:
            return None
        artifacts = [str(a) for a in raw.get("artifacts", [])]
        if any(not (self.workdir / a).exists() for a in artifacts):
            return None
        return Entry(
            key, float(raw.get("seconds", 0.0)), raw.get("result", {}), artifacts
        )

    def store(
        self,
        step: str,
        key: str,
        seconds: float,
        result: dict[str, Any],
        artifacts: list[Path],
    ) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        rel = [
            str(a.resolve().relative_to(self.workdir.resolve())) for a in artifacts
        ]
        record = {
            "key": key,
            "seconds": round(seconds, 3),
            "result": result,
            "artifacts": rel,
        }
        tmp = self.path(step).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, self.path(step))

    def drop(self, step: str) -> None:
        self.path(step).unlink(missing_ok=True)

    def drop_from(self, steps: tuple[str, ...], first: str) -> None:
        """Forget `first` and every step after it in `steps` (the `--from` flag)."""
        start = steps.index(first)
        for step in steps[start:]:
            self.drop(step)
