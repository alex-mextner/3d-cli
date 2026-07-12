# ─────────────────────────────────────────────────────────────────────────────
# ai/veto.py — semantic-feature VETO for the image→3D loop.
#
# WHAT / WHY
#   A silhouette can match while the geometry is semantically WRONG: right bounding
#   box, wrong column count. Boundary F1 stays high, the model is still garbage. The
#   veto is the direct antidote (diagnosis §3, CADReview/ReCAD template): a VLM reads a
#   per-CLASS list of CRITICAL features (e.g. a temple's column count) from an image and
#   a stage FAILS if any critical feature is off — even when the global boundary metric
#   looks good.
#
#   Discrete structural counts are exactly what boundary optimizers are worst at (integer
#   counts, not continuous dimensions), so pinning them with perception and letting the
#   boundary fit recover the continuous dimensions is the division of labor this enables.
#
# HOW IT'S REACHED
#   `lib/img3d_loop.py` calls `perceive()` on the reference to read the critical count,
#   builds the blockout with it, and calls `run_veto()` on the final model render as a
#   stage-unlock + refine dispose gate. The backend is any `ai.backends.Backend`
#   (resolve_backend / MockBackend) so it is swappable and the tests stay deterministic.
#
# INVARIANTS
#   - STDLIB-ONLY (parsing + a Backend handle). Safe to import anywhere.
#   - `perceive()` never raises on a garbled model reply: an unparseable feature is
#     reported as `None` and treated as a veto FAILURE (fail-closed), never a silent pass.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping, Sequence

if TYPE_CHECKING:
    from ai.backends import Backend


@dataclass(frozen=True)
class CriticalFeature:
    """One per-class critical feature a VLM must confirm.

    `name` is the JSON key the model is asked to fill; `tolerance` is the allowed
    absolute deviation from the expected value (0 for an exact integer count).
    """

    name: str
    description: str
    tolerance: float = 0.0
    integer: bool = True


# Per-class critical-feature registry. Keyed by template/class name; extend as new
# blockout families are added.
TEMPLE_FEATURES: tuple[CriticalFeature, ...] = (
    CriticalFeature(
        name="column_count",
        description="the number of distinct vertical columns in the colonnade",
        tolerance=0.0,
        integer=True,
    ),
)

FEATURE_SPECS: dict[str, tuple[CriticalFeature, ...]] = {
    "temple": TEMPLE_FEATURES,
}


@dataclass(frozen=True)
class VetoResult:
    passed: bool
    observed: dict[str, float | None]
    expected: dict[str, float]
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "observed": self.observed,
            "expected": self.expected,
            "failures": self.failures,
        }


def build_veto_prompt(features: Sequence[CriticalFeature]) -> str:
    """Ask the VLM to report each critical feature as strict single-line JSON."""
    lines = [
        "You are the semantic feature CRITIC for a 3D reconstruction loop.",
        "Look at the attached image of a building and report these features:",
    ]
    for feat in features:
        lines.append(f"  - {feat.name}: {feat.description}")
    keys = ", ".join(f'"{f.name}": <number>' for f in features)
    lines.append(
        "Respond with ONLY a single-line strict JSON object, no prose, no code fences:"
    )
    lines.append("  {" + keys + "}")
    return "\n".join(lines)


def parse_features(text: str, features: Sequence[CriticalFeature]) -> dict[str, float | None]:
    """Extract each feature value from a model reply (JSON first, then regex fallback).

    A feature that cannot be read is `None` (fail-closed downstream)."""
    observed: dict[str, float | None] = {f.name: None for f in features}
    for raw in reversed(re.findall(r"\{[^{}]*\}", text)):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            for feat in features:
                if feat.name in data and _as_number(data[feat.name]) is not None:
                    observed[feat.name] = _as_number(data[feat.name])
            if all(observed[f.name] is not None for f in features):
                return observed
    # Regex fallback for chatty replies: "column_count = 6", "6 columns".
    for feat in features:
        if observed[feat.name] is not None:
            continue
        observed[feat.name] = _regex_feature(text, feat.name)
    return observed


def perceive(
    backend: "Backend",
    image: str | pathlib.Path,
    features: Sequence[CriticalFeature],
    *,
    timeout: float = 300.0,
) -> dict[str, float | None]:
    """Ask the backend to read the critical features from `image`."""
    prompt = build_veto_prompt(features)
    img_path = pathlib.Path(image)
    images = [img_path] if img_path.exists() else None
    reply = backend.complete("", prompt, images=images, timeout=timeout)
    return parse_features(reply, features)


def evaluate(
    observed: Mapping[str, float | None],
    expected: Mapping[str, float],
    features: Sequence[CriticalFeature],
) -> VetoResult:
    """Compare observed vs expected feature values; fail-closed on any critical miss."""
    failures: list[str] = []
    obs: dict[str, float | None] = {}
    exp: dict[str, float] = {}
    for feat in features:
        if feat.name not in expected:
            # Fail-closed: a critical feature with no configured expectation is a veto
            # failure, never a silent pass.
            obs[feat.name] = observed.get(feat.name)
            failures.append(f"{feat.name}: no expected value configured")
            continue
        want = float(expected[feat.name])
        got = observed.get(feat.name)
        obs[feat.name] = got
        exp[feat.name] = want
        if got is None:
            failures.append(f"{feat.name}: unreadable (expected {_fmt(want, feat)})")
        elif abs(got - want) > feat.tolerance:
            failures.append(
                f"{feat.name}: observed {_fmt(got, feat)} != expected "
                f"{_fmt(want, feat)} (tol {feat.tolerance:g})"
            )
    return VetoResult(passed=not failures, observed=obs, expected=exp, failures=failures)


def run_veto(
    backend: "Backend",
    image: str | pathlib.Path,
    expected: Mapping[str, float],
    features: Sequence[CriticalFeature] = TEMPLE_FEATURES,
    *,
    timeout: float = 300.0,
) -> VetoResult:
    """Perceive features from `image` and veto against `expected` (fail-closed)."""
    observed = perceive(backend, image, features, timeout=timeout)
    return evaluate(observed, expected, features)


def _as_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = re.search(r"-?\d+(?:\.\d+)?", value)
        if m:
            return float(m.group(0))
    return None


def _regex_feature(text: str, name: str) -> float | None:
    key = name.replace("_", "[ _]?")
    m = re.search(rf"{key}\s*[:=]?\s*(-?\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if m:
        return float(m.group(1))
    if name == "column_count":
        m = re.search(r"(\d+)\s*columns?\b", text, re.IGNORECASE)
        if m:
            return float(m.group(1))
    return None


def _fmt(value: float, feat: CriticalFeature) -> str:
    return str(int(round(value))) if feat.integer else f"{value:g}"
