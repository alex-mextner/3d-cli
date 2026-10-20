"""contracts.py — what every `3d prep` step result looks like.

WHAT: the verdict vocabulary and the `StepResult` protocol shared by all prep steps, plus the
  print-frame convention they all assume.
WHY: the pipeline caches each step as JSON and the proof pack renders it; both must work from
  `to_dict()` alone, so a step can change internals without touching its callers.
INVARIANTS:
  * Print frame: Z up, the model rests on the bed at Z = 0, bounding box centred on X = 0, Y = 0, units mm
    (`frame.to_print_frame`). Every geometric step takes and returns meshes in this frame.
  * `to_dict()` returns plain JSON types only (str/int/float/bool/None/list/dict); numpy scalars are converted.
  * A step never raises for a model that is merely bad (that is a FAIL verdict with a reason); it raises
    `errors.InvalidArgument` / `InputNotFound` for unusable input.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    import trimesh

Verdict = Literal["PASS", "WARN", "FAIL", "SKIP"]

#: worst-first order, used to fold several verdicts into one
VERDICT_ORDER: tuple[Verdict, ...] = ("FAIL", "WARN", "PASS", "SKIP")


@runtime_checkable
class StepResult(Protocol):
    """Result object of one prep step."""

    verdict: Verdict

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable record of the step (cached in the work dir, read by the proof pack)."""
        ...

    def summary(self) -> str:
        """One line for the console and the report table, e.g. `412k faces -> watertight, 0 defects`."""
        ...


def worst(verdicts: list[Verdict]) -> Verdict:
    """Fold verdicts: any FAIL -> FAIL, else any WARN -> WARN, else PASS (SKIP only when nothing ran)."""
    for v in VERDICT_ORDER:
        if v in verdicts and (v != "SKIP" or all(x == "SKIP" for x in verdicts)):
            return v
    return "SKIP"


@dataclass(frozen=True)
class Filament:
    """One loaded spool: `slot` is 1-based (G-code tool Tn = slot n + 1), `key` a one-letter recipe alias (K, W, R, G ...)."""

    slot: int
    key: str
    name: str
    hex: str


@dataclass(frozen=True)
class Assign:
    """What a colour part prints with: exactly one of a pure `slot`, a pure filament `key`, or a layer-cycle `mix`.

    `mix` is one character per layer of the cycle, each a filament key letter or a slot digit (`"GWG"`, `"1213"`).
    """

    slot: int | None = None
    key: str | None = None
    mix: str | None = None

    def __post_init__(self) -> None:
        if sum(v is not None for v in (self.slot, self.key, self.mix)) != 1:
            raise ValueError("Assign needs exactly one of slot, key, mix")


@dataclass
class Part:
    """A disjoint colour volume of the model, ready to become one part of a project 3MF object."""

    name: str
    mesh: trimesh.Trimesh
    assign: Assign
