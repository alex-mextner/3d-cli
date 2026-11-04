"""alias_cli.py — `3d printer -n NAME aliases` and the alias subcommands (home, level, load, preheat, park, ...).

WHAT: the CLI side of printer_ops/aliases.py: lists what each alias sends on a printer (or per adapter/model),
  and runs one alias through the same gates as `macro run` (printer_ops/macro_cli.py `execute`: typed values,
  `access: full`, a G-code channel, defined on the printer, `--yes`, an idle printer; `--dry-run`, `--offline`).

WHY: an alias a printer model lacks must say so and list what it has — never guess a macro name — and every alias
  must show the exact G-code it sends. Issue https://github.com/alex-mextner/3d-cli/issues/718.

INVARIANTS: stdlib-only at import time; imported lazily by `commands/printer.py`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from errors import UsageError
from printer_ops import aliases, macros
from printer_ops.macro_cli import (
    FLAGS,
    VALUE_OPTS,
    adapter_of,
    device_of,
    execute,
    heads_of,
    require_full_access,
    split_opts,
    timeout_of,
)

if TYPE_CHECKING:
    from printer_ops.devices import Device

ALIASES_USAGE = """3d printer [-n NAME] aliases [--json]
  With -n NAME: the aliases that printer has (adapter + model + its own printer-aliases.yaml entries), what each
  sends, and which are not supported on it and why. Without -n: the whole built-in table
  (lib/data/printer-aliases.yaml)."""


def _wants_help(argv: list[str]) -> bool:
    return any(a in ("-h", "--help") for a in argv)


def _alias_line(spec: aliases.AliasSpec) -> str:
    args = " ".join(f"[{a.name}]" if a.optional else f"<{a.name}>" for a in spec.args)
    return f"{spec.name} {args}".rstrip()


def _arg_line(a: aliases.ArgSpec) -> str:
    choices = f" {', '.join(a.choices)}" if a.choices else ""
    limits = (
        f" {a.minimum:g}..{a.maximum:g}"
        if a.minimum is not None and a.maximum is not None
        else ""
    )
    default = f" (default {a.default})" if a.default else ""
    return f"      {a.name}: {a.type}{choices}{limits}{default}"


def _detail(spec: aliases.AliasSpec) -> str:
    if spec.unsupported is not None:
        return f"  {spec.name}: NOT supported - {spec.unsupported}"
    confirm = ", needs --yes" if spec.yes or spec.kind == macros.DANGEROUS else ""
    return "\n".join(
        [
            f"  {_alias_line(spec)}",
            f"      {spec.summary}",
            f"      sends: {'; '.join(spec.gcode)}   [{spec.kind}{confirm}]  (from {spec.source})",
            *(_arg_line(a) for a in spec.args),
        ]
    )


def _as_json(spec: aliases.AliasSpec) -> dict[str, Any]:
    return {
        "summary": spec.summary,
        "kind": spec.kind,
        "gcode": list(spec.gcode),
        "source": spec.source,
        "unsupported": spec.unsupported,
    }


def _list_builtin(as_json: bool) -> int:
    layers = aliases.builtin_layers()
    if as_json:
        print(
            json.dumps(
                {label: {n: _as_json(s) for n, s in e.items()} for label, e in layers},
                indent=2,
            )
        )
        return 0
    for label, entries in layers:
        print(f"{label}:")
        for spec in entries.values():
            print(_detail(spec))
    return 0


def _list_printer(name: str, as_json: bool) -> int:
    device = device_of(name, "aliases")
    table = aliases.resolve(
        device.adapter, device.model, aliases.overrides_for(device.name)
    )
    if as_json:
        print(json.dumps({n: _as_json(s) for n, s in sorted(table.items())}, indent=2))
        return 0
    print(
        f"{device.name} ({device.model or device.adapter}, adapter {device.adapter}):"
    )
    ordered = sorted(
        table.values(), key=lambda s: s.unsupported is not None
    )  # the usable ones first
    for spec in ordered:
        print(_detail(spec))
    return 0


def run_aliases(name: str | None, argv: list[str]) -> int:
    """`aliases [--json]`: the aliases of printer NAME (resolved), or the whole built-in table."""
    if _wants_help(argv):
        print(ALIASES_USAGE)
        return 0
    words, opts = split_opts(argv, frozenset({"--json"}))
    if words:
        raise UsageError(
            f"`aliases` takes no arguments, got {words}", command="printer"
        )
    return (
        _list_builtin("--json" in opts)
        if name is None
        else _list_printer(name, "--json" in opts)
    )


def _print_builtin_help(sub: str) -> int:
    for label, entries in aliases.builtin_layers():
        if sub in entries:
            print(f"{label}:")
            print(_detail(entries[sub]))
    return 0


def _no_printer(sub: str, argv: list[str]) -> int | None:
    """An alias word without -n NAME: its built-in help, or a usage error; None when `sub` is no alias at all."""
    if sub not in aliases.builtin_names():
        return None
    if _wants_help(argv):
        return _print_builtin_help(sub)
    raise UsageError(
        f"`{sub}` is an alias and needs the printer: 3d printer -n NAME {sub} ...",
        command="printer",
        remediation=["3d printer aliases   # what each alias sends, per model"],
    )


def _unknown_alias(
    name: str,
    sub: str,
    model: str | None,
    adapter: str,
    table: dict[str, aliases.AliasSpec],
) -> UsageError:
    return UsageError(
        f"alias `{sub}` is not defined for {name} ({model or 'no model'}, adapter {adapter})",
        command="printer",
        remediation=[
            f"Defined for it: {', '.join(aliases.supported(table)) or 'none'}",
            f"Run a macro it defines: `3d printer -n {name} macro list`",
            f"Or add `{name}: {{{sub}: {{summary: ..., kind: motion, gcode: [...]}}}}` to "
            f"./{aliases.OVERRIDES_FILENAME} (or the user config dir's)",
        ],
    )


def _lookup(device: Device, sub: str, argv: list[str]) -> aliases.AliasSpec | None:
    """The alias to run; None when `--help` was printed instead. Raises for what the printer cannot do."""
    helping = _wants_help(argv)
    if not helping:
        require_full_access(device, f"alias {sub}")
        adapter_of(
            device
        )  # an adapter without a G-code channel says so before any "alias not defined"
    table = aliases.resolve(
        device.adapter, device.model, aliases.overrides_for(device.name)
    )
    spec = table.get(sub)
    if spec is None:
        raise _unknown_alias(device.name, sub, device.model, device.adapter, table)
    if spec.unsupported is not None:
        raise UsageError(
            f"alias `{sub}` is not supported on {device.name} ({device.model or device.adapter}): "
            f"{spec.unsupported}",
            command="printer",
            remediation=[
                f"Defined for it: {', '.join(aliases.supported(table)) or 'none'}"
            ],
        )
    if helping:
        print(
            f"3d printer -n {device.name} {_alias_line(spec)} [--yes] [--dry-run] [--offline] [--timeout S]"
        )
        print(_detail(spec))
        return None
    return spec


def run_alias(name: str | None, sub: str, argv: list[str]) -> int | None:
    """Subcommand `sub` as an alias of printer `name`; None when `sub` is no alias at all (the caller reports it)."""
    if name is None:
        return _no_printer(sub, argv)
    device = device_of(name, sub)
    spec = _lookup(device, sub, argv)
    if spec is None:
        return 0
    words, opts = split_opts(argv, FLAGS, VALUE_OPTS)
    bound = aliases.bind(spec, words, heads_of(device))
    return execute(
        device,
        list(bound.lines),
        what=f"alias {sub}",
        again=" ".join([sub, *words]),
        kind=spec.kind,
        curated=True,
        entry_yes=spec.yes,
        timeout=timeout_of(opts, spec.timeout),
        opts=opts,
    )
