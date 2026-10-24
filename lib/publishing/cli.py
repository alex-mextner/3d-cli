"""The argv side of `3d models publish` and `3d models offer` (the `publish` / `offer` subcommands of
commands/models.py, kept here so that command module stays about marketplace search and download).

`publish` is a dry run unless `--yes`; a refusal still prints the plan first. `offer` is `publishing.offer`.
Both take `parse(argv, value_flags=..., bool_flags=...) -> (positional, opts)` from the caller so the flag
grammar (`--flag value`, `--flag=value`) is the one `3d models` already has.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

from errors import InputNotFound, InvalidArgument, UsageError

Parse = Callable[..., tuple[list[str], dict[str, str]]]

PUBLISH_VALUES = {
    "--platform",
    "--kind",
    "--of",
    "--design",
    "--make",
    "--db",
    "--note",
    "--allow-nonprintable",
    "--reason",
}
PUBLISH_FLAGS = {"--dry-run", "--yes", "--json", "--drop-nonprintable"}
OFFER_VALUES = {
    "--trigger",
    "--publish",
    "--design",
    "--make",
    "--printer",
    "--platform",
    "--db",
    "--allow-nonprintable",
    "--reason",
}
OFFER_FLAGS = {"--yes", "--json", "--drop-nonprintable"}


def _project(positional: list[str], what: str = "PROJECT_DIR") -> Path:
    if len(positional) != 1:
        raise UsageError(
            f"expected exactly one {what}, got {len(positional)}",
            command="models",
            remediation=[f"Pass exactly one {what}."],
        )
    project = Path(positional[0]).expanduser()
    if not project.is_dir():
        raise InputNotFound(str(project), command="models")
    return project


def _allow(opts: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        p.strip() for p in opts.get("--allow-nonprintable", "").split(",") if p.strip()
    )


def publish(argv: list[str], parse: Parse) -> int:
    from publishing import flow, plan

    positional, opts = parse(argv, value_flags=PUBLISH_VALUES, bool_flags=PUBLISH_FLAGS)
    project = _project(positional)
    if "--platform" not in opts:
        raise InvalidArgument(
            "--platform",
            "(missing)",
            [*plan.PLATFORMS, "cults", "all"],
            command="models",
        )
    if "--yes" in opts and "--dry-run" in opts:
        raise UsageError(
            "--yes and --dry-run contradict each other",
            command="models",
            remediation=["Drop --dry-run to publish, or drop --yes to see the plan."],
        )
    kind = opts.get("--kind", "auto")
    if kind not in plan.KINDS:
        raise InvalidArgument("--kind", kind, list(plan.KINDS), command="models")
    if kind == "make" and "--make" not in opts:
        raise UsageError(
            "--kind make needs --make ID: the make whose photos are posted",
            command="models",
            remediation=[
                "3d models publish DIR --kind make --make MAKE_UID --of URL --platform printables"
            ],
        )
    dry_run = "--yes" not in opts
    prep = flow.prepare(
        project,
        platform=opts["--platform"],
        kind=kind,
        of=opts.get("--of"),
        design=opts.get("--design"),
        make=opts.get("--make"),
        db_path=opts.get("--db"),
        note=opts.get("--note", ""),
        allow=_allow(opts),
        reason=opts.get("--reason", ""),
        drop="--drop-nonprintable" in opts,
    )
    as_json = "--json" in opts
    blocked = prep.ev.gate is not None or bool(prep.ev.all_problems())
    if not as_json:
        print(plan.render_text(prep.ev, dry_run=dry_run))
    elif blocked or dry_run:
        print(
            json.dumps(
                plan.to_json(prep.ev, dry_run=dry_run), indent=2, ensure_ascii=False
            )
        )
    flow.refuse_if_blocked(prep)
    if dry_run:
        return 0
    outcomes = flow.send(prep)
    if as_json:
        doc = plan.to_json(prep.ev, dry_run=False)
        doc["outcomes"] = [o.to_json() for o in outcomes]
        print(json.dumps(doc, indent=2, ensure_ascii=False))
    else:
        for o in outcomes:
            print(f"{o.platform}: {o.message}", file=sys.stdout if o.ok else sys.stderr)
    return 0 if all(o.ok for o in outcomes) else 1


def offer(argv: list[str], parse: Parse) -> int:
    from publishing import offer as offering

    positional, opts = parse(argv, value_flags=OFFER_VALUES, bool_flags=OFFER_FLAGS)
    project = _project(positional) if positional else None
    mode = opts.get("--publish")
    if mode is not None and mode not in offering.MODES:
        raise InvalidArgument("--publish", mode, list(offering.MODES), command="models")
    trigger = opts.get("--trigger", "print")
    if project is None and not any(
        k in opts for k in ("--design", "--make", "--printer")
    ):
        raise UsageError(
            "offer needs PROJECT_DIR, or --design / --make / --printer to find it in the library",
            command="models",
            remediation=[
                "3d models offer projects/mole --trigger print",
                "3d models offer --make MAKE_UID --json",
            ],
        )
    return offering.run_offer(
        project,
        trigger=trigger,
        mode=mode,
        yes="--yes" in opts,
        as_json="--json" in opts,
        design=opts.get("--design"),
        make=opts.get("--make"),
        printer=opts.get("--printer"),
        platform=opts.get("--platform", "all"),
        db_path=opts.get("--db"),
        allow=_allow(opts),
        reason=opts.get("--reason", ""),
        drop="--drop-nonprintable" in opts,
    )
