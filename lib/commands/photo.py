"""3d photo — photos of makes and designs in the local library: add a file, capture the table photo, list.

`add` and `renders` only register files; `capture` photographs the finished print with the printer's camera at a
lifted bed (the guarded Z move of `3d printer bed`, restored afterwards). Every path goes through the `photo`
action of `library/actions.py`, the same door as the TUI and the publish flow (umbrella #570, S8 #579).
`capture` is a DRY RUN unless `--yes`: without it the printer is only read and the lift is reported.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from cli.registry import Command
from errors import InputNotFound, InvalidArgument, UsageError

USAGE = """3d photo <add|capture|renders|list> [options]
  Photos of makes and designs in the local library; they go into publications. `capture` moves the printer's
  bed only with --yes, and always puts it back.

Subcommands:
  add FILE (--make ID | --design NAME) [--kind table|bed|render] [--z-mm Z] [--db PATH] [--json]
             register an existing photo of a make or a design.
             Example: 3d photo add shots/mole_table.jpg --make 0199a1c2-7d3e --kind table --z-mm 70
  capture --make ID [--printer P] [--lift auto|MM] [--camera URL] [--part-height MM] [--out FILE] [--yes] [--json]
             photograph the print on the table: raise the bed to a comfortable viewing height (`auto`: the Z
             that shows the plate best, from the printer's camera calibration; or a toolhead Z in mm), take a
             fresh camera frame, restore the bed, store `photo(kind=table, z_mm)`. DRY RUN without --yes: reads
             the printer, reports the planned lift, moves nothing. Refused (exit 1) while the printer is
             printing/paused/busy, when Z is not homed, when the height of the parts on the bed is unknown
             (--part-height) or the lift is outside floor..z_max-5.
             Example: 3d photo capture --make 0199a1c2-7d3e --lift 70 --part-height 40 --yes
  renders PROJECT_DIR --design NAME [--yes] [--json]
             register the project's previews/*.png|jpg (the existing render tools write them) as
             photo(kind=render) of the design. Dry run lists them; --yes registers.
             Example: 3d photo renders projects/mole --design mole --yes
  list [--make ID | --design NAME] [--db PATH] [--json]
             the photos of a make, of a design (with its makes), or all.
             Example: 3d photo list --design mole --json

Options:
  --make ID          the make (full uid or a unique prefix of >= 8 characters). Example: 3d photo list --make 0199a1c2-7d3e
  --design NAME      the design (name or uid). Example: 3d photo add mole.png --design mole --kind render
  --kind K           table (default) | bed | render. Example: 3d photo add bed.jpg --make 0199a1c2-7d3e --kind bed
  --z-mm Z           the bed Z (mm) the photo was taken at. Example: 3d photo add t.jpg --make 0199a1c2-7d3e --z-mm 70
  --printer P        capture: the printer (default: the make's). Example: 3d photo capture --make 0199a1c2-7d3e --printer k1se
  --lift L           capture: auto (default) or the toolhead Z in mm. Example: 3d photo capture --make 0199a1c2-7d3e --lift 70
  --camera URL       capture: snapshot URL/path instead of the printer's configured camera.
                     Example: 3d photo capture --make 0199a1c2-7d3e --camera /server/files/camera/monitor.jpg
  --part-height MM   capture: height of the tallest thing on the bed (the floor of the lift).
                     Example: 3d photo capture --make 0199a1c2-7d3e --part-height 40
  --out FILE         capture: where to save the frame (default: <data dir>/library/photos/<make>/...).
                     Example: 3d photo capture --make 0199a1c2-7d3e --out /tmp/table.jpg --yes
  --yes              capture: really move the bed and take the photo; renders: register. Example: 3d photo renders projects/mole --design mole --yes
  --db PATH          library database file. Example: 3d photo list --db /tmp/library.db
  --json             machine-readable output: the `3d.action/1` result of the photo action (list: `3d.photo/1`).
                     Example: 3d photo capture --make 0199a1c2-7d3e --json
  -h, --help         print this help

Exit codes: 0 ok; 1 a guard refused the lift (printer busy, not homed, outside the Z range), the bed could not be
restored, or the camera gave no frame; 2 bad usage or an unknown make/design; 127 missing dependency.
"""

SCHEMA = "3d.photo/1"
_VALUE = {
    "--make",
    "--design",
    "--kind",
    "--z-mm",
    "--printer",
    "--lift",
    "--camera",
    "--part-height",
    "--out",
    "--db",
}
_FLAGS = {"--yes", "--json"}


def _parse(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    positional: list[str] = []
    opts: dict[str, str] = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        name, eq, inline = a.partition("=")
        if name in _VALUE:
            if eq:
                opts[name] = inline
            elif i + 1 < len(argv):
                i += 1
                opts[name] = argv[i]
            else:
                raise UsageError(
                    f"{name} needs a value",
                    command="photo",
                    remediation=["See `3d photo --help`."],
                )
        elif a in _FLAGS:
            opts[a] = "1"
        elif a.startswith("--"):
            raise UsageError(
                f"unknown option {a}",
                command="photo",
                remediation=["See `3d photo --help`."],
            )
        else:
            positional.append(a)
        i += 1
    return positional, opts


def _number(opts: dict[str, str], flag: str) -> float | None:
    if flag not in opts:
        return None
    try:
        value = float(opts[flag])
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        raise InvalidArgument(flag, opts[flag], ["a finite number"], command="photo")
    return value


def _show(result: dict[str, Any], opts: dict[str, str], text: str) -> int:
    """Print an action result; an `ok: false` one is an error exit with its messages (JSON: also printed)."""
    if "--json" in opts:
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    if result["ok"]:
        if "--json" not in opts:
            print(text)
        return 0
    if "--json" not in opts:
        for e in result["errors"]:
            print(f"error: {e['message']}")
            for step in e["remediation"]:
                print(f"  -> {step}")
    return 1


def _add(positional: list[str], opts: dict[str, str]) -> int:
    from library import actions

    if len(positional) != 1:
        raise UsageError(
            "photo add needs exactly one FILE",
            command="photo",
            remediation=["3d photo add table.jpg --make ID"],
        )
    path = Path(positional[0]).expanduser()
    if not path.is_file():
        raise InputNotFound(str(path), command="photo")
    result = actions.photo(
        str(path),
        make=opts.get("--make"),
        design=opts.get("--design"),
        kind=opts.get("--kind", "table"),
        z_mm=_number(opts, "--z-mm"),
        db_path=opts.get("--db"),
    )
    data = result["data"]
    return _show(
        dict(result),
        opts,
        f"photo {data.get('photo')} ({data.get('kind')}): {data.get('path')}",
    )


def _capture(positional: list[str], opts: dict[str, str]) -> int:
    from library import actions

    if positional:
        raise UsageError(
            f"unexpected argument {positional[0]!r}",
            command="photo",
            remediation=["3d photo capture --make ID"],
        )
    if "--make" not in opts:
        raise UsageError(
            "photo capture needs --make ID",
            command="photo",
            remediation=["3d photo capture --make 0199a1c2-7d3e --lift auto"],
        )
    from bed_photo import parse_lift

    dry_run = "--yes" not in opts
    result = actions.photo(
        make=opts["--make"],
        capture=True,
        kind=opts.get("--kind", "table"),
        printer=opts.get("--printer"),
        lift=parse_lift(opts.get("--lift", "auto")),
        camera=opts.get("--camera"),
        part_height_mm=_number(opts, "--part-height"),
        out=opts.get("--out"),
        dry_run=dry_run,
        db_path=opts.get("--db"),
    )
    data = result["data"]
    plan = data.get("plan") or {}
    if dry_run:
        text = (
            f"DRY RUN: {data['printer']} would lift the bed from Z {plan['from_z']} to Z {plan['target_z']} "
            f"({plan['reason']}); allowed Z {plan['floor']}..{plan['ceiling']}. Nothing moved; add --yes to take the photo."
            if plan.get("move")
            else f"DRY RUN: the bed of {data['printer']} already stands at Z {plan.get('target_z')}; add --yes to take the photo."
        )
    else:
        text = (
            f"photo {data['photo']} ({data['kind']}, bed at Z {data['z_mm']}): {data['path']}"
            + ("; bed put back" if data["moved"] and data["restored"] else "")
        )
    code = _show(dict(result), opts, text)
    if result["ok"] and "--json" not in opts:
        for w in result["warnings"]:
            print(f"warning: {w['message']}")
    return code


def _require_renders_args(positional: list[str], opts: dict[str, str]) -> None:
    if len(positional) != 1:
        raise UsageError(
            "photo renders needs PROJECT_DIR",
            command="photo",
            remediation=["3d photo renders projects/mole --design mole"],
        )
    if "--design" not in opts:
        raise UsageError(
            "photo renders needs --design NAME",
            command="photo",
            remediation=["3d photo renders projects/mole --design mole"],
        )


def _render_source_key(design: str, image: Path) -> str:
    import hashlib

    return (
        "render:"
        + hashlib.sha256(f"{design}:{image.resolve()}".encode()).hexdigest()[:32]
    )


def _print_renders(design: str, rows: list[dict[str, Any]], dry_run: bool) -> None:
    head = (
        "DRY RUN, nothing registered (add --yes)"
        if dry_run
        else f"{sum(r['registered'] for r in rows)} new of {len(rows)}"
    )
    print(f"renders of {design}: {head}")
    print("\n".join(f"  {r['path']}" for r in rows))


def _renders(positional: list[str], opts: dict[str, str]) -> int:
    from library import actions
    from publishing.spec import default_images

    _require_renders_args(positional, opts)
    project = Path(positional[0]).expanduser()
    previews = project / "previews"
    if not previews.is_dir():
        raise InputNotFound(str(previews), command="photo")
    images = default_images(project)
    dry_run = "--yes" not in opts
    rows: list[dict[str, Any]] = []
    for image in images:
        row: dict[str, Any] = {"path": str(image), "registered": False, "photo": None}
        if not dry_run:
            res = actions.photo(
                str(image),
                design=opts["--design"],
                kind="render",
                db_path=opts.get("--db"),
                source_key=_render_source_key(opts["--design"], image),
            )
            if not res["ok"]:
                return _show(dict(res), opts, "")
            row.update(
                registered=bool(res["data"]["created"]), photo=res["data"]["photo"]
            )
        rows.append(row)
    doc = {
        "schema": SCHEMA,
        "dry_run": dry_run,
        "design": opts["--design"],
        "renders": rows,
    }
    if "--json" in opts:
        print(json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        _print_renders(opts["--design"], rows, dry_run)
    return 0


def _list(positional: list[str], opts: dict[str, str]) -> int:
    from library import db as librarydb
    from library import records, views

    if positional:
        raise UsageError(
            f"unexpected argument {positional[0]!r}",
            command="photo",
            remediation=["3d photo list [--make ID | --design NAME]"],
        )
    if "--make" in opts and "--design" in opts:
        raise UsageError(
            "--make and --design contradict each other",
            command="photo",
            remediation=["Pass one of them."],
        )
    conn = librarydb.open_library(opts.get("--db"))
    try:
        if "--make" in opts:
            rows = records.photos_of(
                conn,
                make_id=int(
                    records.resolve(conn, "make", opts["--make"], command="photo")["id"]
                ),
            )
        elif "--design" in opts:
            rows = records.photos_of(
                conn,
                design_id=int(
                    records.resolve(conn, "design", opts["--design"], command="photo")[
                        "id"
                    ]
                ),
            )
        else:
            rows = records.photos_of(conn)
    finally:
        conn.close()
    if "--json" in opts:
        print(
            json.dumps(
                {"schema": SCHEMA, "photos": rows},
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
        )
    else:
        print(
            views.table(
                rows,
                [
                    ("uid", "photo"),
                    ("kind", "kind"),
                    ("z_mm", "z mm"),
                    ("path", "file"),
                ],
            )
        )
    return 0


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    subs = {"add": _add, "capture": _capture, "renders": _renders, "list": _list}
    if argv[0] not in subs:
        raise InvalidArgument("subcommand", argv[0], list(subs), command="photo")
    if any(a in ("-h", "--help") for a in argv[1:]):
        print(USAGE)
        return 0
    positional, opts = _parse(argv[1:])
    return subs[argv[0]](positional, opts)


COMMAND = Command(
    name="photo",
    group="LIBRARIES",
    summary="photos of makes/designs: add a file, capture the table photo with a lifted bed, register renders",
    usage=USAGE,
    run=run,
)
