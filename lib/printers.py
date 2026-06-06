"""printers.py — the printer registry (a layered name->Printer loader, ROADMAP §2a).

ACCESSED VIA: `3d printers list` / `3d printers show <name>` (commands/printers.py) for
human inspection, and by slice/check/pack which take a project's `printer:` NAME (from
3d.yaml, lib/project.py) and call get_printer() to resolve it into a build volume,
firmware and default material. This is headless core (§20): no argv, no printing — it
returns dataclasses and raises structured errors; callers format.

DESIGN — a SIMPLE three-layer loader, NOT a plugin system (same stance as materials):
  lowest  built-in  lib/data/printers.yaml   (shipped with the tool)
          user      ~/.config/3d-cli/printers.yaml  (cli.paths.config_dir())
  highest project   ./printers.yaml next to the nearest 3d.yaml (project.find_project())
A later layer with the SAME printer name fully REPLACES the earlier entry (last writer
wins per name — no deep field merge), so a user/project can correct a built-in's bed
size or swap firmware without editing the shipped file.

INVARIANTS:
  - The built-in data path is derived from __file__ (lib/data/printers.yaml), never cwd,
    so the registry resolves no matter where `3d` is invoked.
  - `bed` is [x, y, z] build volume in mm; `nozzle_mm` defaults to 0.4 when omitted.
  - get_printer() on an unknown name raises InvalidArgument listing every accepted name
    (ROADMAP §1.3), so the fix is copy-pasteable.
  - yaml is imported LAZILY (module stays stdlib-only at import time, per the registry
    contract); a missing/empty/unreadable user or project file is skipped, not fatal —
    only the built-in layer is required.
"""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field
from typing import Any

from errors import InvalidArgument, MissingDependency, ThreeDError

# Firmware flavors a downstream slicer/g-code step can target.
ACCEPTED_FIRMWARE = ("klipper", "bambu", "marlin", "prusa")
DEFAULT_NOZZLE_MM = 0.4

# lib/data/printers.yaml, resolved from this module's location (never cwd).
_BUILTIN_DATA = pathlib.Path(__file__).resolve().parent / "data" / "printers.yaml"
# The user override lives in the tool's config dir; the project one sits next to 3d.yaml.
_USER_FILENAME = "printers.yaml"
_PROJECT_FILENAME = "printers.yaml"


class PrinterError(ThreeDError):
    """A printers.yaml is malformed (not a mapping, bad entry shape). Exit 2."""

    exit_code = 2


@dataclass(slots=True)
class Printer:
    """One resolved printer: a build volume + firmware + default-loaded material."""

    name: str
    bed: list[float]                 # [x, y, z] build volume in mm
    nozzle_mm: float = DEFAULT_NOZZLE_MM
    firmware: str | None = None      # klipper|bambu|marlin|prusa (None = unspecified)
    material: str | None = None      # default filament NAME into materials.yaml
    raw: dict[str, Any] = field(default_factory=dict)  # untouched source entry


def _require_yaml() -> Any:
    try:
        import yaml  # lazy: keep this module stdlib-only at import time (registry contract)
    except ImportError as exc:  # pragma: no cover - exercised only without pyyaml
        raise MissingDependency(
            "pyyaml",
            install="uv sync  (pyyaml is a core dependency)  # or: pip install pyyaml",
            degrades="the printer registry (printers.yaml cannot be parsed)",
        ) from exc
    return yaml


def _user_path() -> pathlib.Path:
    from cli.paths import config_dir  # lazy: avoid pulling cli.* at module import

    return config_dir() / _USER_FILENAME


def _project_path(start: str | os.PathLike[str] | None = None) -> pathlib.Path | None:
    from project import find_project  # lazy: avoid import-time coupling

    found = find_project(start)
    if found is None:
        return None
    return found.parent / _PROJECT_FILENAME


def _load_layer(path: pathlib.Path, *, command: str | None) -> dict[str, Any]:
    """Parse one printers.yaml into a name->entry dict. Missing file -> {} (layers are
    optional). A present-but-malformed file is a real error the user should see."""
    if not path.is_file():
        return {}
    yaml = _require_yaml()
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:  # type: ignore[attr-defined]
        raise PrinterError(
            f"could not parse {path}: {exc}",
            command=command,
            remediation=[f"Fix the YAML syntax in {path} (each entry is `Name:` with a `bed:` list)."],
        ) from exc
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise PrinterError(
            f"{path} must be a YAML mapping of name -> printer, got {type(doc).__name__}",
            command=command,
            remediation=['Write entries like:  "Prusa MK4":\n    bed: [250, 210, 220]'],
        )
    return doc


def _build_printer(name: str, spec: Any, *, command: str | None) -> Printer:
    if not isinstance(spec, dict):
        raise PrinterError(
            f"printer {name!r} must be a mapping, got {type(spec).__name__}",
            command=command,
            remediation=[f'Write:  "{name}":\n    bed: [220, 220, 250]'],
        )
    bed = spec.get("bed")
    if not isinstance(bed, (list, tuple)) or len(bed) != 3:
        raise PrinterError(
            f"printer {name!r}: `bed` must be a [x, y, z] list of 3 numbers",
            command=command,
            remediation=[f'Set the build volume, e.g.  bed: [256, 256, 256]  under "{name}".'],
        )
    try:
        bed_list = [float(v) for v in bed]
    except (TypeError, ValueError):
        raise PrinterError(
            f"printer {name!r}: `bed` values must be numbers, got {bed!r}", command=command
        ) from None

    nozzle = spec.get("nozzle_mm", DEFAULT_NOZZLE_MM)
    try:
        nozzle_mm = float(nozzle)
    except (TypeError, ValueError):
        raise PrinterError(
            f"printer {name!r}: `nozzle_mm` must be a number, got {nozzle!r}", command=command
        ) from None

    firmware = spec.get("firmware")
    if firmware is not None:
        firmware = str(firmware)
        if firmware not in ACCEPTED_FIRMWARE:
            raise InvalidArgument(
                "firmware",
                firmware,
                ACCEPTED_FIRMWARE,
                command=command,
                extra=f"Fix the `firmware:` of printer {name!r}.",
            )

    material = spec.get("material")
    return Printer(
        name=name,
        bed=bed_list,
        nozzle_mm=nozzle_mm,
        firmware=firmware,
        material=str(material) if material is not None else None,
        raw=dict(spec),
    )


def load_printers(
    *, command: str | None = None, start: str | os.PathLike[str] | None = None
) -> dict[str, Printer]:
    """Merge built-in + user + project printers.yaml into a name->Printer dict.

    Layers are applied lowest-to-highest (built-in, then user, then project); a later
    layer's entry for a name fully replaces the earlier one. The built-in layer is
    required; user/project layers are optional and silently absent when their file
    does not exist."""
    merged: dict[str, Any] = {}
    if not _BUILTIN_DATA.is_file():  # pragma: no cover - ships with the package
        raise PrinterError(
            f"built-in printer registry not found at {_BUILTIN_DATA}",
            command=command,
            remediation=["Reinstall the `3d` tool — lib/data/printers.yaml is missing."],
        )
    merged.update(_load_layer(_BUILTIN_DATA, command=command))
    merged.update(_load_layer(_user_path(), command=command))
    project_path = _project_path(start)
    if project_path is not None:
        merged.update(_load_layer(project_path, command=command))

    return {
        name: _build_printer(str(name), spec, command=command)
        for name, spec in merged.items()
    }


def get_printer(
    name: str, *, command: str | None = None, start: str | os.PathLike[str] | None = None
) -> Printer:
    """Resolve a printer NAME to its Printer. Unknown name -> InvalidArgument listing
    every accepted name (so a user/project override or a typo fix is obvious)."""
    printers = load_printers(command=command, start=start)
    printer = printers.get(name)
    if printer is None:
        raise InvalidArgument(
            "printer",
            name,
            sorted(printers.keys()),
            command=command,
            extra="Add the printer to ./printers.yaml or ~/.config/3d-cli/printers.yaml, "
            "or use one of the accepted names above.",
        )
    return printer
