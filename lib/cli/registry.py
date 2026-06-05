"""registry.py — the command registry (plugin architecture) for the `3d` CLI.

THE COMMAND-AUTHORING CONTRACT (read this before adding a command):

  A command is a single module `lib/commands/<name>.py` that defines a module-level
  `COMMAND` object:

      from __future__ import annotations
      from cli.registry import Command

      def run(argv: list[str]) -> int:
          ...                       # do the work; return a process exit code
          return 0

      COMMAND = Command(
          name="mything",
          group="GEOMETRY & EXPORT",      # which --help section it appears under
          summary="one-line help shown in `3d help`",
          usage="mything <file> [options]",
          run=run,
      )

  Adding a command requires ZERO edits to bin/3d or any shared file — discovery globs
  `lib/commands/*.py`, imports each, and reads `COMMAND`. That is the whole extension
  point for the swarm.

  HARD RULE — keep command modules stdlib-only and import-light. Discovery imports
  EVERY command module on EVERY `3d` invocation, so a heavy top-level `import trimesh`
  in one module would slow/break all commands AND defeat the offline `3d help`/`3d
  render` guarantee. Reach heavy deps (trimesh/manifold3d/cv2) and external binaries
  (openscad/magick/slicer) via subprocess (`cli.pyrun`) or a LAZY import inside `run()`,
  never at module top level. `tests/test_imports.py` enforces this.

  Aliases: declare `aliases=[...]` on a Command (e.g. `acceptance` -> `check`), OR make
  a tiny dedicated module whose `run()` prepends args and calls the target's run. Both
  patterns are fine; pick whichever reads cleaner for the case.
"""
from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import Callable

# A command's entry point: argv (everything after the subcommand name) -> exit code.
RunFn = Callable[[list[str]], int]

# Stable ordering of the --help groups. Commands in unknown groups sort last.
GROUP_ORDER = [
    "RENDER & VIEW",
    "GEOMETRY & EXPORT",
    "QA & GATES",
    "REFERENCE-MATCH PIPELINE",
    "SLICING",
    "LIBRARIES",
    "ENVIRONMENT",
    "META",
]


@dataclass(frozen=True)
class Command:
    """A self-registering CLI command (see the module docstring for the contract)."""

    name: str
    summary: str
    run: RunFn
    group: str = "META"
    usage: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)


class Registry:
    """Holds all discovered commands + their aliases; resolves a name to a Command."""

    def __init__(self) -> None:
        self._by_name: dict[str, Command] = {}
        self._aliases: dict[str, str] = {}  # alias -> canonical name

    def add(self, cmd: Command) -> None:
        # A name must not collide with an existing command name OR an existing alias
        # (otherwise _by_name would silently shadow the alias, making routing
        # order-dependent on discovery order).
        if cmd.name in self._by_name:
            raise ValueError(f"duplicate command name: {cmd.name}")
        if cmd.name in self._aliases:
            raise ValueError(f"command name reuses an existing alias: {cmd.name}")
        self._by_name[cmd.name] = cmd
        for a in cmd.aliases:
            if a in self._aliases or a in self._by_name:
                raise ValueError(f"duplicate command/alias: {a}")
            self._aliases[a] = cmd.name

    def resolve(self, name: str) -> Command | None:
        """Resolve a name or alias to its Command (None if unknown)."""
        if name in self._by_name:
            return self._by_name[name]
        canon = self._aliases.get(name)
        if canon is not None:
            return self._by_name.get(canon)
        return None

    def names(self) -> list[str]:
        """All canonical names + aliases (for did-you-mean suggestions)."""
        return sorted([*self._by_name.keys(), *self._aliases.keys()])

    def commands(self) -> list[Command]:
        """All Commands, sorted by group order then name (for `3d help`)."""
        def key(c: Command) -> tuple[int, str]:
            try:
                gi = GROUP_ORDER.index(c.group)
            except ValueError:
                gi = len(GROUP_ORDER)
            return (gi, c.name)

        return sorted(self._by_name.values(), key=key)

    def alias_map(self) -> dict[str, str]:
        return dict(self._aliases)


def discover(package: str = "commands") -> Registry:
    """Import every `lib/commands/*.py` and build the registry from their COMMAND objects.

    `package` is imported by name, so `lib/` must already be on sys.path (the dispatcher
    arranges that). A module without a `COMMAND` attribute is skipped silently — that lets
    a module be a shared helper rather than a command if it ever needs to be.
    """
    reg = Registry()
    pkg = importlib.import_module(package)
    # __path__ exists on packages; commands/ is a package (has __init__.py).
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        if mod_info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{package}.{mod_info.name}")
        cmd = getattr(mod, "COMMAND", None)
        if isinstance(cmd, Command):
            reg.add(cmd)
    return reg
