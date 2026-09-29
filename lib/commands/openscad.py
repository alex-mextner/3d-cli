"""3d openscad — OpenSCAD integration; `libs` installs libraries for plain OpenSCAD.

WHAT: `3d openscad libs <name> install|uninstall|update|where` manages OpenSCAD libraries
  in OpenSCAD's per-user library folder (macOS ~/Documents/OpenSCAD/libraries), from a
  registry of known libraries (`3d openscad libs list`) or any git URL (`--git`).

WHY: `3d libs` only feeds 3d's own renders (repo libs/ on a 3d-exported OPENSCADPATH).
  A .scad file opened in the OpenSCAD app or rendered with a bare `openscad` from any
  cwd needs its libraries in the user library folder, which OpenSCAD always searches.

Examples:
  3d openscad libs list
  3d openscad libs WriteText install
  3d openscad libs WriteText where
  3d openscad libs WriteText uninstall

INVARIANTS: stdlib-only at import time (registry contract); the manager in
lib/registries/openscad_libs.py is imported lazily inside run().
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cli.registry import Command
from errors import InvalidArgument, ThreeDError, UsageError

USAGE = """3d openscad libs <subcommand>
  Install OpenSCAD libraries into OpenSCAD's user library folder, so `use <Name/...>`
  resolves in the OpenSCAD app and in a bare `openscad` run from any directory.

  list                            known libraries, install state, where OpenSCAD finds them
  path                            print the user library folder and OpenSCAD's search path
  <name> install [--git URL] [--ref REF] [--dir DIR] [--force]
                                  install (idempotent; --force reinstalls or replaces a
                                  folder 3d did not install)
  <name> uninstall [--dir DIR]    remove a 3d-installed library (alias: remove)
  <name> update [--dir DIR]       reinstall from the recorded source (fresh git clone)
  <name> where [--dir DIR]        print the folder OpenSCAD loads <name> from (alias: path)

Options:
  --git URL    install a library that is not in the registry from a git repository;
               <name> becomes the folder name used in `use <name/...>`
  --ref REF    git branch or tag to install instead of the default branch (git only)
  --dir DIR    library folder to use instead of the user library folder (e.g. a folder
               on your OPENSCADPATH); pass the same --dir to uninstall/update/where
  --force      reinstall, or replace an existing folder of the same name

<name> is a single folder name (letters, digits, '.', '_', '+', '-'). Known libraries:
WriteText (text on cylinders/cones with native fonts, Cyrillic included; after Write.scad
by HarlanDMii, https://www.thingiverse.com/thing:16193), BOSL2, NopSCADlib,
Round-Anything and threads-scad.

Examples:
  3d openscad libs list
  3d openscad libs path
  3d openscad libs WriteText install
  3d openscad libs WriteText where
  3d openscad libs WriteText update
  3d openscad libs WriteText uninstall
  3d openscad libs BOSL2 install --ref v2.0.763
  3d openscad libs BOSL2 install --ref v2.0.763 --force
  3d openscad libs MyLib install --git https://github.com/me/MyLib.git
  3d openscad libs WriteText install --dir ~/scad-libs --force
  3d openscad libs WriteText uninstall --dir ~/scad-libs"""

_CTX = "openscad libs"
_ACTIONS = ("install", "uninstall", "remove", "update", "where", "path")


@dataclass(frozen=True)
class _Opts:
    git: str | None = None
    ref: str | None = None
    dir: Path | None = None
    force: bool = False


def _parse_opts(argv: list[str], allowed: tuple[str, ...]) -> _Opts:
    values: dict[str, str] = {}
    force = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg not in allowed:
            raise InvalidArgument("option", arg, list(allowed), command=_CTX)
        if arg == "--force":
            force = True
            i += 1
            continue
        if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
            raise UsageError(f"{arg} needs a value", command=_CTX,
                             remediation=["See: 3d openscad libs --help"])
        values[arg] = argv[i + 1]
        i += 2
    dir_ = values.get("--dir")
    return _Opts(values.get("--git"), values.get("--ref"),
                 Path(dir_).expanduser() if dir_ else None, force)


def _no_extra_args(sub: str, extra: list[str]) -> None:
    if extra:
        raise UsageError(f"'{sub}' takes no arguments (got: {' '.join(extra)})", command=_CTX,
                         remediation=["See: 3d openscad libs --help"])


def _rev(commit: str | None) -> str:
    return f" @ {commit[:12]}" if commit else ""


def _print_list() -> int:
    from registries import openscad_libs as ol  # lazy: keep module import-light (registry contract)

    user = ol.user_library_dir()
    print(f"OpenSCAD user library folder: {user}")
    print()
    width = max(len(s.name) for s in ol.KNOWN_LIBS)
    for spec in ol.KNOWN_LIBS:
        found = ol.locate(spec.name)
        if found is None:
            state = "not installed"
        elif ol.read_manifest(found) is not None:
            state = f"installed    {found}"
        else:
            state = f"present      {found}  (not installed by 3d)"
        print(f"  {spec.name:<{width}}  {state}")
        print(f"  {'':<{width}}  {spec.summary}")
    others = [d for d in ol.unmanaged_dirs(user) or [] if ol.known_lib(d) is None]
    if others:
        print()
        print("Also in the user library folder (not installed by 3d): " + ", ".join(others))
    print()
    print("Install one:  3d openscad libs <name> install")
    return 0


def _print_path() -> int:
    from registries import openscad_libs as ol  # lazy

    print(f"user library folder: {ol.user_library_dir()}")
    print("OpenSCAD search order:")
    for p in ol.search_path():
        print(f"  {p}")
    return 0


def _install(name: str, opts: _Opts) -> int:
    from registries import openscad_libs as ol  # lazy

    spec = ol.resolve_spec(name, git=opts.git, ref=opts.ref)
    res = ol.install(spec, opts.dir, force=opts.force)
    if res.already:
        print(f"{spec.name} is already installed at {res.path} ({res.source})")
        wanted = ol.spec_source(spec)
        if wanted != res.source:
            print(f"  it came from {res.source}, not {wanted}; to switch, rerun with --force")
        else:
            print(f"  refresh it: 3d openscad libs {spec.name} update")
    else:
        print(f"installed {spec.name} -> {res.path} ({res.files} files from {res.source}{_rev(res.commit)})")
    shadow = ol.locate(spec.name)
    if opts.dir is None and shadow is not None and shadow != res.path:
        print(f"warning: OpenSCAD loads {spec.name} from {shadow} first (OPENSCADPATH); "
              "remove that copy or drop it from OPENSCADPATH to use this install")
    print(f"use it:  use <{spec.name}/...>")
    return 0


def _uninstall(name: str, opts: _Opts) -> int:
    from registries import openscad_libs as ol  # lazy

    path, count, leftovers = ol.uninstall(name, opts.dir)
    print(f"uninstalled {path.name}: removed {count} files from {path}")
    for d in leftovers:
        print(f"  kept {d}: it holds files 3d did not install")
    return 0


def _update(name: str, opts: _Opts) -> int:
    from registries import openscad_libs as ol  # lazy

    res = ol.update(name, opts.dir)
    print(f"updated {res.path.name} -> {res.path} ({res.files} files from {res.source}{_rev(res.commit)})")
    return 0


def _where(name: str, opts: _Opts) -> int:
    from registries import openscad_libs as ol  # lazy

    folder = ol.canonical_name(name)
    bases = [opts.dir] if opts.dir else ol.search_path()
    found = next((b / folder for b in bases if (b / folder).is_dir()), None)
    if found is None:
        raise ThreeDError(
            f"OpenSCAD cannot find {folder} (searched: {', '.join(str(b) for b in bases)})",
            command=_CTX,
            remediation=[f"Install it: 3d openscad libs {folder} install"],
        )
    print(found)
    return 0


def _libs(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    first = argv[0]
    if first in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if first == "list":
        _no_extra_args("list", argv[1:])
        return _print_list()
    if first == "path" and (len(argv) == 1 or argv[1].startswith("--")):
        _no_extra_args("path", argv[1:])
        return _print_path()
    if len(argv) < 2:
        raise UsageError(
            f"missing action after '{first}'",
            command=_CTX,
            remediation=[f"e.g. 3d openscad libs {first} install", "See: 3d openscad libs --help"],
        )
    name, action = argv[0], argv[1]
    if name in _ACTIONS and action not in _ACTIONS:  # tolerate `libs install <name>`
        name, action = action, name
    rest = argv[2:]
    if action == "install":
        return _install(name, _parse_opts(rest, ("--git", "--ref", "--dir", "--force")))
    if action in ("uninstall", "remove"):
        return _uninstall(name, _parse_opts(rest, ("--dir",)))
    if action == "update":
        return _update(name, _parse_opts(rest, ("--dir",)))
    if action in ("where", "path"):
        return _where(name, _parse_opts(rest, ("--dir",)))
    raise InvalidArgument("action", action, list(_ACTIONS), command=_CTX)


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv[0] == "libs":
        return _libs(argv[1:])
    raise InvalidArgument("subcommand", argv[0], ["libs"], command="openscad")


COMMAND = Command(
    name="openscad",
    group="LIBRARIES",
    summary="install OpenSCAD libraries (WriteText, BOSL2, …) where plain OpenSCAD finds them",
    usage=USAGE,
    run=run,
)
