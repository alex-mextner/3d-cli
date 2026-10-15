"""policy.py — loader and schema check of `fleet/policy.yaml`, the PrintOps policy file.

ACCESSED VIA: 3d-api (drying sweep, later the gates and the calibration engine) and the tests
that keep the committed file valid.

LAYOUT: one top-level section per concern — `drying:` (#162), later `gates:` (#164),
`calibrations:` (#167). Each section has ONE parser, registered in `SECTION_PARSERS` as
"module:function"; the parser takes the section mapping and returns its typed form or raises
PolicyError. An unknown top-level key is an error (a typo such as `dryng:` must not silently
fall back to defaults).

FILE LOCATION: `$PRINTOPS_POLICY`, else `<repo>/fleet/policy.yaml` (the 3d-api image copies
fleet/ next to lib/, so the same relative path works in the container).
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

# section name -> "module:function" (parser of that section's mapping). Slices append here.
SECTION_PARSERS: dict[str, str] = {
    "drying": "printops.drying_policy:parse",
    "calibrations": "printops.calibration_policy:parse",
}


class PolicyError(ValueError):
    """The policy file is missing, not YAML, or a section breaks its schema."""


def policy_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    raw = env.get("PRINTOPS_POLICY")
    return Path(raw).expanduser() if raw else REPO_ROOT / "fleet" / "policy.yaml"


def load_raw(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """The file as {section: mapping}; checks the top level only."""
    path = path or policy_path()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise PolicyError(
            f"policy file {path} not found (set PRINTOPS_POLICY or add fleet/policy.yaml)"
        ) from e
    except yaml.YAMLError as e:
        raise PolicyError(f"policy file {path} is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise PolicyError(
            f"policy file {path} must be a mapping of sections ({', '.join(SECTION_PARSERS)})"
        )
    unknown = sorted(str(k) for k in data if k not in SECTION_PARSERS)
    if unknown:
        raise PolicyError(
            f"{path}: unknown section(s) {', '.join(unknown)}; known: {', '.join(SECTION_PARSERS)}"
        )
    for name, body in data.items():
        if not isinstance(body, dict):
            raise PolicyError(f"{path}: section `{name}` must be a mapping")
    return data


def _parser(name: str) -> Callable[[Mapping[str, Any]], Any]:
    module, _, func = SECTION_PARSERS[name].partition(":")
    parser: Callable[[Mapping[str, Any]], Any] = getattr(
        importlib.import_module(module), func
    )
    return parser


def section(name: str, path: Path | None = None) -> Any:
    """The typed form of one section (PolicyError if absent or invalid)."""
    if name not in SECTION_PARSERS:
        raise KeyError(f"unknown policy section {name!r}")
    raw = load_raw(path)
    if name not in raw:
        raise PolicyError(f"{path or policy_path()}: section `{name}` is missing")
    try:
        return _parser(name)(raw[name])
    except PolicyError as e:
        raise PolicyError(f"{path or policy_path()}: {name}: {e}") from e


def check(path: Path | None = None) -> list[str]:
    """Every schema problem in the file (empty = valid); each present section is parsed."""
    try:
        raw = load_raw(path)
    except PolicyError as e:
        return [str(e)]
    problems: list[str] = []
    for name, body in raw.items():
        try:
            _parser(name)(body)
        except PolicyError as e:  # noqa: PERF203 — report every broken section, not just the first
            problems.append(f"{name}: {e}")
    return problems
