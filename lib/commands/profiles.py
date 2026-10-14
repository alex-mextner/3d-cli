"""3d profiles — keep the slicers' user presets in the repo and sync them both ways (issue
https://github.com/alex-mextner/3d-cli/issues/180, PrintOps decision D10).

The logic lives in lib/profiles (sync.py: three-way plan, backups, running check); this module
parses flags, prints plans/diffs and asks before writing.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field

from cli.registry import Command
from errors import GateFailure, InvalidArgument, UsageError
from profiles import slicers, sync

USAGE = """3d profiles <status|diff|sync> [options]
  Keep the user presets of OrcaSlicer, Snapmaker Orca and Bambu Studio in the repo as
  profiles/<slicer>/<kind>/<name>.json (+ .info) and sync them both ways. Three-way against the
  last sync: changed only in the slicer -> pull, only in the repo -> push, on both sides ->
  conflict, shown as a diff and never resolved automatically. Printer connection keys
  (print_host, printhost_*) stay in the slicer.

Subcommands:
  status [--slicer S] [--json]    what a sync would do, per preset (writes nothing)
  diff [NAME] [--slicer S]        unified diff repo -> slicer of presets that differ
  sync [--slicer S] [--pull | --push] [--dry-run] [--yes] [--force]
                                  apply pulls and pushes; conflicts are left as they are

Options:
  --slicer S    orca | snapmaker-orca | bambu | all (default: all)
  --pull        apply only slicer -> repo changes
  --push        apply only repo -> slicer changes
  --dry-run     print the plan, write nothing
  --yes         apply without asking (required when stdin is not a terminal)
  --force       write into a slicer's presets even while it runs
  --json        (status) machine-readable output

Before a push, the replaced slicer files are copied to ~/.cache/3d-cli/profiles-backup/<time>/.
The last-sync snapshot is ~/.config/3d-cli/profiles-sync.json. Exit: 0 ok, 1 conflicts left or
slicer running, 2 bad arguments.

Examples:
  3d profiles status
  3d profiles status --slicer snapmaker-orca --json
  3d profiles diff "0.32mm Standard @Snapmaker U1 (0.4 nozzle)"
  3d profiles sync --dry-run
  3d profiles sync --pull --yes
  3d profiles sync --slicer bambu --push --yes --force"""

SLICER_CHOICES = ("orca", "snapmaker-orca", "bambu", "all")
SUBCOMMAND_FLAGS = {
    "status": ("--slicer", "--json"),
    "diff": ("--slicer",),
    "sync": ("--slicer", "--pull", "--push", "--dry-run", "--yes", "--force"),
}


@dataclass
class Options:
    slicer: str = "all"
    name: str | None = None
    pull: bool = False
    push: bool = False
    dry_run: bool = False
    yes: bool = False
    force: bool = False
    json: bool = False


@dataclass
class Row:
    slicer: slicers.Slicer
    pair: sync.Pair | None
    items: list[sync.Item] = field(default_factory=list)

    def header(self) -> str:
        if self.pair is None:
            looked = ", ".join(str(p) for p in slicers.data_dir_candidates(self.slicer))
            return (
                f"{self.slicer.id}: {self.slicer.label} not found (looked in {looked})"
            )
        return f"{self.slicer.id}: {self.pair.user_dir} <-> {self.pair.repo_dir}"


def _parse(argv: list[str], sub: str) -> Options:
    opts = Options()
    allowed = SUBCOMMAND_FLAGS[sub]
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("--") and arg not in allowed:
            raise UsageError(
                f"unknown {sub} option: {arg}",
                command="profiles",
                remediation=["Run `3d profiles --help` for accepted options."],
            )
        if arg == "--slicer":
            if i + 1 >= len(argv):
                raise UsageError("--slicer needs a value", command="profiles")
            if argv[i + 1] not in SLICER_CHOICES:
                raise InvalidArgument(
                    "--slicer", argv[i + 1], SLICER_CHOICES, command="profiles"
                )
            opts.slicer = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--"):
            setattr(opts, arg[2:].replace("-", "_"), True)
        elif sub == "diff" and opts.name is None:
            opts.name = arg
        else:
            raise UsageError(f"unexpected argument: {arg}", command="profiles")
        i += 1
    if opts.pull and opts.push:
        raise UsageError(
            "--pull and --push are exclusive (omit both to sync both ways)",
            command="profiles",
        )
    return opts


def _rows(selected: str, state: sync.State) -> list[Row]:
    rows = []
    for slicer in slicers.SLICERS.values():
        if selected in ("all", slicer.id):
            pair = sync.find_pair(slicer)
            rows.append(
                Row(slicer, pair, sync.status(pair, state) if pair is not None else [])
            )
    return rows


def _indent(text: str) -> str:
    return "".join(f"    {line}" for line in text.splitlines(keepends=True)).rstrip(
        "\n"
    )


def _print_plan(rows: list[Row], *, conflict_diffs: bool) -> tuple[int, int]:
    changes = conflicts = 0
    for row in rows:
        print(row.header())
        for item in row.items:
            if item.action == "same":
                continue
            print(f"  {item.action:<9} {item.rel}  ({item.reason})")
            if item.action != "conflict":
                changes += 1
                continue
            conflicts += 1
            if conflict_diffs and row.pair is not None:
                print(_indent(sync.item_diff(item, row.slicer.id)))
        if row.pair is not None:
            print(f"  {sum(1 for i in row.items if i.action == 'same')} in sync")
    return changes, conflicts


def _run_status(opts: Options) -> int:
    rows = _rows(opts.slicer, sync.State.load())
    if opts.json:
        out = [
            {
                "slicer": row.slicer.id,
                "found": row.pair is not None,
                "user_dir": str(row.pair.user_dir) if row.pair else None,
                "repo_dir": str(row.pair.repo_dir) if row.pair else None,
                "items": [
                    {
                        "rel": i.rel,
                        "name": i.name,
                        "action": i.action,
                        "reason": i.reason,
                    }
                    for i in row.items
                ],
            }
            for row in rows
        ]
        clean = all(i.action == "same" for row in rows for i in row.items)
        print(json.dumps({"clean": clean, "slicers": out}, indent=2))
        return 0
    changes, conflicts = _print_plan(rows, conflict_diffs=False)
    print(
        "clean"
        if not changes and not conflicts
        else f"{changes} to sync, {conflicts} conflict(s)"
    )
    return 0


def _run_diff(opts: Options) -> int:
    matched = 0
    for row in _rows(opts.slicer, sync.State.load()):
        for item in row.items:
            if opts.name is not None and opts.name not in (item.name, item.rel):
                continue
            matched += 1
            if item.action != "same":
                print(f"# {row.slicer.id} {item.rel}: {item.action} ({item.reason})")
                print(sync.item_diff(item, row.slicer.id), end="")
    if opts.name is not None and not matched:
        raise UsageError(
            f"no preset named {opts.name!r}",
            command="profiles",
            remediation=[
                "Run `3d profiles status` for the preset names (file names without .json)."
            ],
        )
    return 0


def _confirm(count: int) -> None:
    if not sys.stdin.isatty():
        raise UsageError(
            "sync would write files; pass --yes to apply without a terminal",
            command="profiles",
            remediation=["Preview first:  3d profiles sync --dry-run"],
        )
    if input(f"Apply {count} change(s)? [y/N] ").strip().lower() not in ("y", "yes"):
        raise GateFailure("sync cancelled; nothing written", command="profiles")


def _run_sync(opts: Options) -> int:
    directions = ("pull",) if opts.pull else ("push",) if opts.push else sync.DIRECTIONS
    state = sync.State.load()
    rows = _rows(opts.slicer, state)
    if opts.slicer != "all" and rows[0].pair is None:
        raise UsageError(
            rows[0].header(),
            command="profiles",
            remediation=["Start the slicer once so it creates its user preset dir."],
        )
    _print_plan(rows, conflict_diffs=True)
    todo = sum(1 for row in rows for i in row.items if i.action in directions)
    conflicts = sum(1 for row in rows for i in row.items if i.action == "conflict")
    if opts.dry_run:
        print(
            f"dry run: {todo} change(s) would be applied, {conflicts} conflict(s); nothing written"
        )
        return 0
    if todo and not opts.yes:
        _confirm(todo)
    if not opts.force and "push" in directions:
        # Check every slicer before writing anything, so a refusal leaves no half-applied sync.
        for row in rows:
            if row.pair is not None and any(i.action == "push" for i in row.items):
                sync.ensure_not_running(row.pair)
    applied = stale = 0
    for row in rows:
        if row.pair is None:
            continue
        result = sync.apply(
            row.pair, row.items, state, directions=directions, force=opts.force
        )
        for item in result.done:
            print(f"{item.action} {row.slicer.id} {item.rel}: done")
        for item in result.stale:
            print(
                f"{item.action} {row.slicer.id} {item.rel}: skipped, changed on disk since the plan"
            )
        applied += len(result.done)
        stale += len(result.stale)
        if result.backup_dir is not None and result.backup_dir.exists():
            print(f"backup: {result.backup_dir}")
    state.save()
    if conflicts or stale:
        err = GateFailure(
            f"{conflicts} conflict(s) left as they are, {stale} change(s) skipped",
            command="profiles",
        )
        err.remediation = [
            "Conflicts: edit the repo file or the preset in the slicer so both sides match "
            "(`3d profiles diff NAME` shows the difference), then sync again.",
            "Skipped: a file changed while sync ran; run the sync again.",
        ]
        raise err
    print(f"synced {applied} change(s)" if applied else "nothing to do")
    return 0


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help", "help") or "-h" in argv or "--help" in argv:
        print(USAGE)
        return 0
    sub = argv[0]
    if sub not in SUBCOMMAND_FLAGS:
        raise InvalidArgument(
            "subcommand", sub, list(SUBCOMMAND_FLAGS), command="profiles"
        )
    opts = _parse(argv[1:], sub)
    if sub == "status":
        return _run_status(opts)
    if sub == "diff":
        return _run_diff(opts)
    return _run_sync(opts)


COMMAND = Command(
    name="profiles",
    group="SLICING",
    summary="keep slicer user presets (Orca, Snapmaker Orca, Bambu Studio) in the repo; status/diff/sync",
    usage="profiles <status|diff|sync> [--slicer S] [--pull|--push] [--dry-run] [--yes]",
    run=run,
)
