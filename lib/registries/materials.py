"""materials.py — the FDM material registry loader (ROADMAP §2a: headless core).

ACCESSED VIA: `3d materials list/show` (lib/commands/materials.py) and `3d strength`/`3d
pack`/`3d slice`, which look a part's `material:` name up here for density (mass/cost),
modulus/yield (strength), and the cross-layer anisotropy factor. This is a SIMPLE three-layer
loader, not a plugin/registry-manager system; it has no argv/printing — callers raise/format.

INVARIANTS:
  - Three layers, later overrides earlier, merged FIELD-BY-FIELD (not whole-entry): so a user
    who sets only `PLA: {color: ...}` keeps the builtin density/modulus and changes just color.
      (1) built-in    lib/data/materials.yaml  (resolved via __file__, NOT cwd)
      (2) user        ~/.config/3d-cli/materials.yaml   (cli.paths.config_dir())
      (3) project     ./materials.yaml NEXT TO the nearest 3d.yaml (project.find_project())
    Note the asymmetry: the user file lives under the `3d-cli/` config subdir; the project file
    sits directly beside `3d.yaml`.
  - Merge raw dicts across all layers FIRST, then build `Material` from each merged dict. A
    material that exists only in a user/project layer must therefore be COMPLETE — a missing
    required field raises MaterialError naming the field (not a silent default).
  - An unknown material name raises InvalidArgument listing the accepted names (ROADMAP §1.3).
  - yaml is lazy-imported inside functions (mirrors project._require_yaml) so importing this
    module stays stdlib-light and the offline `3d help` guarantee holds.
"""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass
from typing import Any

from cli.paths import config_dir
from errors import InvalidArgument, MissingDependency, ThreeDError
from project import find_project

# The built-in defaults, shipped under lib/data/ while this module lives in lib/registries/.
_BUILTIN_PATH = pathlib.Path(__file__).resolve().parents[1] / "data" / "materials.yaml"

# The on-disk filename for the user and project override layers.
OVERRIDE_FILENAME = "materials.yaml"

# Accepted surface-finish values (shared with the dataclass validation below).
FINISHES = ("matte", "gloss", "metal")

# Required keys every fully-resolved material must carry (in display order).
_REQUIRED_FIELDS = (
    "density",
    "e_modulus_mpa",
    "tensile_mpa",
    "yield_mpa",
    "max_temp_c",
    "color",
    "finish",
    "layer_adhesion",
)

# Optional mechanical fields `3d strength` reads (structural solver). A user/project material that
# omits them falls back to these generic FDM values, and `Material.defaulted` records which
# ones, so a report can list them as assumptions instead of silently inventing a datasheet.
_OPTIONAL_DEFAULTS: dict[str, float] = {
    "poisson": 0.35,           # in-plane Poisson ratio
    "z_modulus_factor": 0.85,  # E across layers / E in-plane
}


class MaterialError(ThreeDError):
    """A materials.yaml layer is malformed, or a defined material is incomplete/invalid. Exit 2."""

    exit_code = 2


@dataclass(slots=True, frozen=True)
class Material:
    """One resolved FDM material (all override layers already merged).

    `layer_adhesion` is the FDM cross-layer anisotropy factor in (0, 1]: the fraction of the
    in-plane strength retained ACROSS layer lines (the weak Z direction). 1.0 = isotropic;
    PLA ~0.45, PETG ~0.7 (approximate, brand/temperature dependent). `3d strength` multiplies an
    in-plane strength by this for the across-layer allowable.

    The optional structural fields (`poisson`, `z_modulus_factor`, `strain_limit_pct`) feed the
    transversely isotropic stiffness and the snap-fit strain check; `defaulted` lists the ones the
    card did not set."""

    name: str
    density: float          # g/cm^3
    e_modulus_mpa: float    # tensile (Young's) modulus, MPa, in-plane
    tensile_mpa: float      # ultimate tensile strength, MPa, in-plane
    yield_mpa: float        # tensile yield strength, MPa, in-plane
    max_temp_c: float       # practical continuous service temperature, deg C
    color: str              # default display hex, e.g. "#1f9c4b"
    finish: str             # matte | gloss | metal
    layer_adhesion: float   # cross-layer anisotropy factor in (0, 1]
    shrink_xy_pct: float | None = None  # linear XY shrink, % of the printed size; None = not recorded
    shrink_z_pct: float | None = None   # linear Z shrink, %; None = not recorded (no Z compensation)
    poisson: float = 0.35           # in-plane Poisson ratio
    z_modulus_factor: float = 0.85  # E across the layers / E in-plane, in (0, 1]
    strain_limit_pct: float = 0.0   # permissible single-assembly strain, in-plane, percent (0 = yield strain)
    defaulted: tuple[str, ...] = ()  # optional fields that came from generic defaults, not a card


def _optional_percent(name: str, field: str, value: Any) -> float | None:
    """An optional shrink percentage: absent/None = not recorded, otherwise a number in [0, 20)."""
    if value is None:
        return None
    number = _coerce_float(name, field, value)
    if not 0 <= number < 20:
        raise MaterialError(
            f"material {name!r}: `{field}` must be a percentage in [0, 20), got {value!r}",
            command="materials",
            remediation=[f"Set `{field}:` to the linear shrink in percent (PLA ~0.3), or remove it."],
        )
    return number


def _require_yaml() -> Any:
    try:
        import yaml  # lazy: pyyaml is a real dependency of the material layers, not import-time
    except ImportError as exc:  # pragma: no cover - exercised via load error path
        raise MissingDependency(
            "pyyaml",
            install="uv sync  (pyyaml is a core dependency)  # or: pip install pyyaml",
            degrades="the material registry (materials.yaml cannot be parsed)",
        ) from exc
    return yaml


def _load_layer(path: pathlib.Path) -> dict[str, dict[str, Any]]:
    """Parse one materials.yaml layer into name -> field-dict. Missing file -> empty layer."""
    if not path.is_file():
        return {}
    yaml = _require_yaml()
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:  # type: ignore[attr-defined]
        raise MaterialError(
            f"could not parse {path}: {exc}",
            command="materials",
            remediation=[f"Fix the YAML in {path} (check indentation and `key: value` pairs)."],
        ) from exc
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise MaterialError(
            f"{path} must be a YAML mapping of name -> properties, got {type(doc).__name__}",
            command="materials",
            remediation=["Each entry is `<NAME>:` with density/e_modulus_mpa/... fields under it."],
        )
    out: dict[str, dict[str, Any]] = {}
    for name, spec in doc.items():
        if not isinstance(spec, dict):
            raise MaterialError(
                f"material {name!r} in {path} must be a mapping, got {type(spec).__name__}",
                command="materials",
                remediation=[f"Write `{name}:` then indented `density: ...` etc. under it."],
            )
        out[str(name)] = dict(spec)
    return out


def _layer_paths(start: str | os.PathLike[str] | None) -> list[pathlib.Path]:
    """The three layer files in precedence order (builtin first, project last)."""
    paths = [_BUILTIN_PATH, config_dir() / OVERRIDE_FILENAME]
    project_file = find_project(start)
    if project_file is not None:
        paths.append(project_file.parent / OVERRIDE_FILENAME)
    return paths


def _coerce_float(name: str, field: str, value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise MaterialError(
            f"material {name!r}: `{field}` must be a number, got {value!r}",
            command="materials",
            remediation=[f"Set a numeric `{field}:` for {name} in your materials.yaml."],
        ) from None


def _build_material(name: str, spec: dict[str, Any]) -> Material:
    """Validate a fully-merged field-dict into a Material (raises MaterialError on a gap)."""
    missing = [f for f in _REQUIRED_FIELDS if spec.get(f) is None]
    if missing:
        raise MaterialError(
            f"material {name!r} is missing required field(s): {', '.join(missing)}",
            command="materials",
            remediation=[
                f"Add {', '.join(missing)} under {name} in your materials.yaml. "
                "A material defined only in a user/project file must be complete "
                "(fields are merged per-field, but a wholly new material has no builtin to fall back on).",
            ],
        )
    finish = str(spec["finish"])
    if finish not in FINISHES:
        raise MaterialError(
            f"material {name!r}: `finish` is {finish!r}; accepted: {', '.join(FINISHES)}",
            command="materials",
            remediation=[f"Set `finish:` to one of {', '.join(FINISHES)} for {name}."],
        )
    yield_mpa = _coerce_float(name, "yield_mpa", spec["yield_mpa"])
    e_modulus_mpa = _coerce_float(name, "e_modulus_mpa", spec["e_modulus_mpa"])
    layer_adhesion = _coerce_float(name, "layer_adhesion", spec["layer_adhesion"])
    if not 0.0 < layer_adhesion <= 1.0:
        raise MaterialError(
            f"material {name!r}: `layer_adhesion` must be in (0, 1], got {layer_adhesion:g}",
            command="materials",
            remediation=[f"Set `layer_adhesion:` between 0 (exclusive) and 1 for {name}."],
        )
    optional: dict[str, float] = {}
    defaulted: list[str] = []
    for field_name, fallback in _OPTIONAL_DEFAULTS.items():
        if spec.get(field_name) is None:
            optional[field_name] = fallback
            defaulted.append(field_name)
        else:
            optional[field_name] = _coerce_float(name, field_name, spec[field_name])
    if not 0.0 < optional["poisson"] < 0.5:
        raise MaterialError(
            f"material {name!r}: `poisson` must be in (0, 0.5), got {optional['poisson']:g}",
            command="materials",
            remediation=[f"Set `poisson:` between 0 and 0.5 (exclusive) for {name}."],
        )
    if not 0.0 < optional["z_modulus_factor"] <= 1.0:
        raise MaterialError(
            f"material {name!r}: `z_modulus_factor` must be in (0, 1], got {optional['z_modulus_factor']:g}",
            command="materials",
            remediation=[f"Set `z_modulus_factor:` between 0 (exclusive) and 1 for {name}."],
        )
    if spec.get("strain_limit_pct") is None:
        strain_limit_pct = 100.0 * yield_mpa / e_modulus_mpa
        defaulted.append("strain_limit_pct")
    else:
        strain_limit_pct = _coerce_float(name, "strain_limit_pct", spec["strain_limit_pct"])
        if strain_limit_pct <= 0.0:
            raise MaterialError(
                f"material {name!r}: `strain_limit_pct` must be positive, got {strain_limit_pct:g}",
                command="materials",
                remediation=[f"Set a positive `strain_limit_pct:` (percent) for {name}."],
            )
    return Material(
        name=name,
        density=_coerce_float(name, "density", spec["density"]),
        e_modulus_mpa=e_modulus_mpa,
        tensile_mpa=_coerce_float(name, "tensile_mpa", spec["tensile_mpa"]),
        yield_mpa=yield_mpa,
        max_temp_c=_coerce_float(name, "max_temp_c", spec["max_temp_c"]),
        color=str(spec["color"]),
        finish=finish,
        layer_adhesion=layer_adhesion,
        poisson=optional["poisson"],
        z_modulus_factor=optional["z_modulus_factor"],
        strain_limit_pct=strain_limit_pct,
        defaulted=tuple(defaulted),
        shrink_xy_pct=_optional_percent(name, "shrink_xy_pct", spec.get("shrink_xy_pct")),
        shrink_z_pct=_optional_percent(name, "shrink_z_pct", spec.get("shrink_z_pct")),
    )


def load_materials(*, start: str | os.PathLike[str] | None = None) -> dict[str, Material]:
    """Load the merged material registry (builtin < user < project), name -> Material.

    `start` seeds the project-layer search (default cwd); pass it so callers/tests can resolve a
    project's `./materials.yaml` without chdir. Fields merge per-material across layers; each
    merged entry is then validated into a `Material` (raising MaterialError on an incomplete or
    invalid one)."""
    merged: dict[str, dict[str, Any]] = {}
    for path in _layer_paths(start):
        for name, spec in _load_layer(path).items():
            merged[name] = {**merged.get(name, {}), **spec}
    return {name: _build_material(name, spec) for name, spec in merged.items()}


def get_material(name: str, *, start: str | os.PathLike[str] | None = None) -> Material:
    """Resolve one material by name, or raise InvalidArgument listing the accepted names.

    `start` seeds the project-layer search (default cwd), like load_materials()."""
    mats = load_materials(start=start)
    mat = mats.get(name)
    if mat is None:
        raise InvalidArgument(
            "name",
            name,
            sorted(mats),
            command="materials",
            extra="Run `3d materials list` to see available materials, "
            "or define it in ./materials.yaml or ~/.config/3d-cli/materials.yaml.",
        )
    return mat
