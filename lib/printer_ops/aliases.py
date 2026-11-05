"""aliases.py — the `3d printer -n NAME <alias>` registry: per adapter, per model, per printer.

WHAT: loads lib/data/printer-aliases.yaml (the built-in table), merges the layers adapter ->
  matching models -> the printer's own aliases (`printer-aliases.yaml`, user then project), and
  binds the words a user typed to the alias's `args`, producing the G-code lines to send.

WHY: "home", "load filament", "park" are the same intent on every printer but a different
  command on each firmware (Klipper `G28`, Snapmaker U1 `AUTO_FEEDING EXTRUDER=n LOAD=1`,
  Creality K1 `LOAD_MATERIAL`). The mapping is data, overridable per printer, and an alias a
  model does not have is reported as such — never guessed. The table schema is in the data
  file's header. Issue https://github.com/alex-mextner/3d-cli/issues/718.

PER-PRINTER FILES (later wins per alias name, whole entry replaced):
  ~/.config/3d-cli/printer-aliases.yaml, then ./printer-aliases.yaml:
    u1:                               # the printer's fleet / printer-devices.yaml name
      home: {summary: ..., kind: motion, gcode: ["G28 X Y"]}
      park: null                      # removed for this printer
      cooldown: {unsupported: "why"}  # removed, reason shown

INVARIANTS: stdlib + lazy pyyaml. Every value reaching a G-code line went through a typed
  check here (axes letters, a fleet head label, a bounded number, a listed choice) or the
  table itself; there is no free-text argument.
"""

from __future__ import annotations

import functools
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from errors import InvalidArgument, MissingDependency, ThreeDError, UsageError
from printer_ops import macros

DATA_FILE: Final = (
    Path(__file__).resolve().parent.parent / "data" / "printer-aliases.yaml"
)
OVERRIDES_FILENAME: Final = "printer-aliases.yaml"
TYPES: Final = ("axes", "head", "int", "float", "choice")
_ENTRY_KEYS: Final = frozenset(
    {"summary", "kind", "yes", "timeout", "args", "gcode", "unsupported"}
)
_ARG_KEYS: Final = frozenset({"type", "optional", "default", "min", "max", "choices"})
_ALIAS_NAME: Final = re.compile(r"[a-z][a-z0-9-]*")
_PLACEHOLDER: Final = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_HEAD_LABEL: Final = re.compile(r"T[0-9]+", re.IGNORECASE)


class AliasConfigError(ThreeDError):
    """The alias table (built-in or a printer's own file) breaks its schema. Exit 2."""

    exit_code = 2


@dataclass(frozen=True)
class HeadInfo:
    index: int
    extruder: str  # Klipper object name: extruder, extruder1, ...
    usable: bool
    reason: str | None


@dataclass(frozen=True)
class ArgSpec:
    name: str
    type: str
    optional: bool = False
    default: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    choices: dict[str, dict[str, str]] = field(default_factory=dict, hash=False)


@dataclass(frozen=True)
class AliasSpec:
    name: str
    summary: str
    kind: str
    gcode: tuple[str, ...]
    source: str  # which layer defined it, for messages
    args: tuple[ArgSpec, ...] = ()
    yes: bool = False
    timeout: float = 120.0
    unsupported: str | None = None  # set: the alias does NOT exist here; this is why


def _yaml() -> Any:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - pyyaml is a core dependency
        raise MissingDependency(
            "pyyaml",
            install="uv sync",
            degrades="printer aliases cannot be read",
            command="printer",
        ) from exc
    return yaml


def _bad(where: str, why: str) -> AliasConfigError:
    return AliasConfigError(
        f"{where}: {why}",
        command="printer",
        remediation=[f"Schema: the header of {DATA_FILE}"],
    )


def _number(value: Any, where: str, key: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise _bad(where, f"`{key}` must be a number, got {value!r}")
    return float(value)


def _text(value: Any) -> str:
    """A scalar of the YAML as the text a G-code line gets (210.0 -> 210)."""
    return f"{value:g}" if isinstance(value, float) else str(value)


# ---- parsing one entry ----------------------------------------------------------------------


def _choices(spec: dict[str, Any], where: str) -> dict[str, dict[str, str]]:
    if spec["type"] != "choice":
        if "choices" in spec:
            raise _bad(where, "`choices` is for type choice only")
        return {}
    raw = spec.get("choices")
    if not isinstance(raw, dict) or not raw:
        raise _bad(where, "a choice needs a non-empty `choices` mapping")
    out: dict[str, dict[str, str]] = {}
    for label, fills in raw.items():
        if not isinstance(fills, dict):
            raise _bad(where, f"choice {label!r} must map placeholder -> value")
        out[str(label)] = {str(k): _text(v) for k, v in fills.items()}
    return out


def _arg(name: str, spec: Any, where: str) -> ArgSpec:
    where = f"{where} arg {name}"
    if (
        not isinstance(spec, dict)
        or set(spec) - _ARG_KEYS
        or spec.get("type") not in TYPES
    ):
        raise _bad(
            where,
            f"must be a mapping with type in {', '.join(TYPES)} and keys {', '.join(sorted(_ARG_KEYS))}",
        )
    lo = _number(spec["min"], where, "min") if "min" in spec else None
    hi = _number(spec["max"], where, "max") if "max" in spec else None
    default = _text(spec["default"]) if "default" in spec else None
    optional = bool(spec.get("optional", False)) or default is not None
    return ArgSpec(name, spec["type"], optional, default, lo, hi, _choices(spec, where))


def _placeholders(arg: ArgSpec) -> set[str]:
    if arg.type == "head":
        return {arg.name, f"{arg.name}_extruder", f"{arg.name}_suffix"}
    if arg.type == "choice":
        return {arg.name, *(k for fills in arg.choices.values() for k in fills)}
    return {arg.name}


def _removed(name: str, spec: Any, source: str, where: str) -> AliasSpec | None:
    """`None` / `{unsupported: why}`: the alias is removed from this layer on; None when `spec` is a real entry."""
    if spec is None:
        return AliasSpec(
            name, "", macros.INERT, (), source, unsupported="disabled for this printer"
        )
    if not isinstance(spec, dict) or set(spec) - _ENTRY_KEYS:
        raise _bad(
            where, f"must be a mapping with keys {', '.join(sorted(_ENTRY_KEYS))}"
        )
    if "unsupported" not in spec:
        return None
    reason = spec["unsupported"]
    if len(spec) != 1 or not isinstance(reason, str) or not reason.strip():
        raise _bad(where, "`unsupported` takes a reason and no other key")
    return AliasSpec(name, "", macros.INERT, (), source, unsupported=reason.strip())


def _gcode_lines(
    spec: dict[str, Any], args: tuple[ArgSpec, ...], where: str
) -> tuple[str, ...]:
    gcode = spec.get("gcode")
    if (
        not isinstance(gcode, list)
        or not gcode
        or not all(isinstance(g, str) and g.strip() for g in gcode)
    ):
        raise _bad(where, "`gcode` must be a non-empty list of lines")
    known = {p for a in args for p in _placeholders(a)}
    for line in gcode:
        macros.refuse_denied(line, f"{where} gcode")
        unknown = set(_PLACEHOLDER.findall(line)) - known
        if unknown:
            raise _bad(
                where,
                f"gcode line {line!r} uses {', '.join(sorted(unknown))}, which no arg provides",
            )
        if re.search(r"[;\n\r]", line):
            raise _bad(where, f"gcode line {line!r} must be one command without `;`")
    return tuple(g.strip() for g in gcode)


def parse_entry(name: str, spec: Any, source: str) -> AliasSpec:
    """One alias entry (`None` or `{unsupported: why}` = removed) -> AliasSpec; every schema problem is reported."""
    where = f"{source} alias {name}"
    if not _ALIAS_NAME.fullmatch(name):
        raise _bad(where, "an alias name is lower-case letters, digits and -")
    removed = _removed(name, spec, source, where)
    if removed is not None:
        return removed
    if spec.get("kind") not in macros.KINDS:
        raise _bad(where, f"`kind` must be one of {', '.join(macros.KINDS)}")
    summary = spec.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise _bad(where, "`summary` is required (it is the --help text)")
    raw_args = spec.get("args") or {}
    if not isinstance(raw_args, dict):
        raise _bad(where, "`args` must be a mapping")
    args = tuple(_arg(str(k), v, where) for k, v in raw_args.items())
    timeout = _number(spec["timeout"], where, "timeout") if "timeout" in spec else 120.0
    if not 0 < timeout <= 3600:
        raise _bad(where, "`timeout` is seconds, 1-3600")
    return AliasSpec(
        name,
        summary.strip(),
        str(spec["kind"]),
        _gcode_lines(spec, args, where),
        source,
        args,
        bool(spec.get("yes", False)),
        timeout,
    )


def parse_entries(value: Any, source: str) -> dict[str, AliasSpec]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _bad(
            source,
            "must be a mapping of alias name -> entry (schema: lib/data/printer-aliases.yaml)",
        )
    return {str(k): parse_entry(str(k), v, source) for k, v in value.items()}


# ---- the layers -----------------------------------------------------------------------------


def _model_layer(item: Any) -> tuple[str, dict[str, AliasSpec]]:
    if (
        not isinstance(item, dict)
        or set(item) != {"contains", "aliases"}
        or not isinstance(item["contains"], str)
    ):
        raise _bad(str(DATA_FILE), "each model is {contains: TEXT, aliases: {...}}")
    return item["contains"].lower(), parse_entries(
        item["aliases"], f"model {item['contains']}"
    )


@functools.cache
def _table() -> tuple[
    dict[str, dict[str, AliasSpec]], tuple[tuple[str, dict[str, AliasSpec]], ...]
]:
    data = _yaml().safe_load(DATA_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) - {"adapters", "models"}:
        raise _bad(str(DATA_FILE), "top level must have `adapters` and `models` only")
    adapters = {
        str(a): parse_entries(entries, f"adapter {a}")
        for a, entries in (data.get("adapters") or {}).items()
    }
    return adapters, tuple(_model_layer(item) for item in data.get("models") or [])


def builtin_names() -> set[str]:
    """Every alias name any built-in layer defines (they become `3d printer` subcommands)."""
    return {n for _, entries in builtin_layers() for n in entries}


def builtin_layers() -> list[tuple[str, dict[str, AliasSpec]]]:
    """(layer label, entries) of the whole built-in table, adapters first."""
    adapters, models = _table()
    return [
        *((f"adapter {a}", e) for a, e in adapters.items()),
        *((f"model {c}", e) for c, e in models),
    ]


def overrides_for(name: str, cwd: Path | None = None) -> dict[str, AliasSpec]:
    """The aliases printer `name` has of its own: user file, then project file (later wins per alias)."""
    from cli.paths import config_dir

    merged: dict[str, AliasSpec] = {}
    for path in (
        config_dir() / OVERRIDES_FILENAME,
        (cwd or Path.cwd()) / OVERRIDES_FILENAME,
    ):
        if not path.is_file():
            continue
        try:
            data = _yaml().safe_load(path.read_text(encoding="utf-8"))
        except _yaml().YAMLError as exc:
            raise AliasConfigError(
                f"{path}: invalid YAML: {exc}", command="printer"
            ) from exc
        if data is not None and not isinstance(data, dict):
            raise _bad(str(path), "top level must map a printer name -> its aliases")
        merged.update(parse_entries((data or {}).get(name), f"{path} {name}"))
    return merged


def resolve(
    adapter: str, model: str | None, overrides: Mapping[str, AliasSpec] | None = None
) -> dict[str, AliasSpec]:
    """Aliases of one printer, unsupported ones included (their `unsupported` says why)."""
    adapters, models = _table()
    merged: dict[str, AliasSpec] = dict(adapters.get(adapter, {}))
    text = (model or "").lower()
    for contains, entries in models:
        if contains in text:
            merged.update(entries)
    merged.update(overrides or {})
    return merged


def supported(table: Mapping[str, AliasSpec]) -> dict[str, AliasSpec]:
    return {n: s for n, s in sorted(table.items()) if s.unsupported is None}


# ---- binding the words of a call ------------------------------------------------------------


@dataclass(frozen=True)
class Bound:
    lines: tuple[str, ...]


def _scan(words: list[str], names: list[str]) -> tuple[dict[str, str], list[str]]:
    """Words -> (named values, positional words): `--name V`, `--name=V`, `NAME=V`; the rest is positional."""
    named: dict[str, str] = {}
    positional: list[str] = []
    it = iter(words)
    for w in it:
        key, value = _named(w, names, it)
        if key is None:
            positional.append(w)
            continue
        if key not in names:
            raise UsageError(
                f"unknown argument {key!r}",
                command="printer",
                remediation=[f"Accepted: {', '.join(names) or 'none'}"],
            )
        if key in named:
            raise UsageError(f"argument {key!r} given twice", command="printer")
        named[key] = value
    return named, positional


def _named(word: str, names: list[str], rest: Any) -> tuple[str | None, str]:
    """(argument name, value) when `word` names an argument (consuming the next word for `--name V`)."""
    if word.startswith("--"):
        key, sep, value = word[2:].partition("=")
        if not sep:
            value = next(rest, "")
            if not value:
                raise UsageError(f"{word} needs a value", command="printer")
        return key.lower(), value
    key, sep, value = word.partition("=")
    return (key.lower(), value) if sep and key.lower() in names else (None, "")


def _split(words: list[str], spec: AliasSpec) -> dict[str, str]:
    """Words -> {arg name: raw text}: named ones first, positional ones fill the rest in `args` order."""
    names = [a.name for a in spec.args]
    out, positional = _scan(words, names)
    free = [n for n in names if n not in out]
    if len(positional) > len(free):
        raise UsageError(
            f"`{spec.name}` takes {len(names)} argument(s) ({', '.join(names) or 'none'}), "
            f"got extra {positional[len(free) :]}",
            command="printer",
        )
    out.update(zip(free, positional, strict=False))
    return out


def _number_arg(arg: ArgSpec, text: str) -> str:
    try:
        v = float(text) if arg.type == "float" else float(int(text))
    except ValueError:
        raise InvalidArgument(
            arg.name,
            text,
            [f"a {'number' if arg.type == 'float' else 'whole number'}"],
            command="printer",
        ) from None
    below = arg.minimum is not None and v < arg.minimum
    above = arg.maximum is not None and v > arg.maximum
    if not math.isfinite(v) or below or above:
        lo = "-inf" if arg.minimum is None else f"{arg.minimum:g}"
        hi = "inf" if arg.maximum is None else f"{arg.maximum:g}"
        raise InvalidArgument(arg.name, text, [f"{lo} .. {hi}"], command="printer")
    return f"{v:g}"


def _head_values(
    arg: ArgSpec, text: str, heads: Mapping[str, HeadInfo], alias: str
) -> dict[str, str]:
    label = text.upper()
    if not _HEAD_LABEL.fullmatch(text):
        raise InvalidArgument(
            arg.name,
            text,
            ["a head label: " + ", ".join(sorted(heads))],
            command="printer",
            extra="Heads are named by their label (T1..), not by a 0- or 1-based number.",
        )
    info = heads.get(label)
    if info is None:
        raise InvalidArgument(
            arg.name,
            text,
            sorted(heads),
            command="printer",
            extra=f"`{alias}` knows only the heads listed for this printer in fleet/printers.yaml.",
        )
    if not info.usable:
        from printer_ops.base import PrinterRefused

        raise PrinterRefused(
            f"head {label} is marked unusable: {info.reason or 'no reason recorded'}",
            command="printer",
        )
    suffix = str(info.index) if info.index else ""
    return {
        arg.name: str(info.index),
        f"{arg.name}_extruder": info.extruder,
        f"{arg.name}_suffix": suffix,
    }


def _axes(text: str) -> str:
    letters = list(text.upper())
    if (
        not letters
        or any(c not in "XYZ" for c in letters)
        or len(set(letters)) != len(letters)
    ):
        raise InvalidArgument(
            "axes",
            text,
            ["X, Y, Z in any order, each once, e.g. XY"],
            command="printer",
        )
    return " ".join(letters)


def _typed(
    arg: ArgSpec, text: str, heads: Mapping[str, HeadInfo], alias: str
) -> dict[str, str]:
    """The placeholder values one argument contributes, every one checked against its type."""
    if arg.type == "axes":
        return {arg.name: _axes(text) if text else ""}
    if arg.type == "head":
        return _head_values(arg, text, heads, alias)
    if arg.type == "choice":
        match = {k.upper(): k for k in arg.choices}.get(text.upper())
        if match is None:
            raise InvalidArgument(
                arg.name, text, sorted(arg.choices), command="printer"
            )
        return {arg.name: match}
    return {arg.name: _number_arg(arg, text)}


def _collect(
    spec: AliasSpec, words: list[str], heads: Mapping[str, HeadInfo]
) -> dict[str, str]:
    """Placeholder -> checked value for everything the call provided; a choice's fills only where unset."""
    raw = _split(words, spec)
    values: dict[str, str] = {}
    fills: dict[str, str] = {}
    for arg in spec.args:
        text = raw.get(arg.name, arg.default)
        if text is None:
            if not arg.optional:
                raise UsageError(
                    f"`{spec.name}` needs {arg.name}",
                    command="printer",
                    remediation=[f"3d printer {spec.name} --help"],
                )
            continue
        values.update(_typed(arg, text, heads, spec.name))
        if arg.type == "choice":
            fills.update(arg.choices[values[arg.name]])
    by_name = {a.name: a for a in spec.args}
    for key, fill in fills.items():
        if key not in values:
            values[key] = _number_arg(by_name[key], fill) if key in by_name else fill
    return values


def bind(spec: AliasSpec, words: list[str], heads: Mapping[str, HeadInfo]) -> Bound:
    """Typed values from `words` -> the alias's G-code lines. A line whose placeholder has no value is left out;
    no line left is a usage error (e.g. `preheat` without a material, --nozzle or --bed)."""
    values = _collect(spec, words, heads)
    lines = [
        _PLACEHOLDER.sub(lambda m: values[m.group(1)], line).strip()
        for line in spec.gcode
        if set(_PLACEHOLDER.findall(line)) <= set(values)
    ]
    if not lines:
        raise UsageError(
            f"`{spec.name}` needs at least one of: {', '.join(a.name for a in spec.args)}",
            command="printer",
            remediation=[f"3d printer {spec.name} --help"],
        )
    return Bound(tuple(lines))
