"""The publish OFFER: after a print finishes or a downloaded model is modified, prepare a publication plan and
ask — or hand a plan to an agent — instead of leaving the design unpublished (issue #579, umbrella #570).

WHAT: `run_offer` builds the dry-run plan(s) for a design — the remix/original listing with CAD files, renders
and photos on every platform, and a make post when the print was of somebody else's model — and then, by mode:

  off   nothing at all: no output, no library read, exit 0 (`--publish off`, or `offer: off` in the config).
  ask   interactive terminal: prints the summary and asks "Publish ... to all platforms? [Y/n]" (the default is
        yes only for an open licence). Not a terminal (agent, pipe, `--json`): prints the plan and does not
        publish; the answer is `3d models publish ... --yes`.
  auto  publishes without asking, but ONLY together with `--yes` on the same command line. A config default of
        `auto` never publishes by itself: without `--yes` it behaves like `ask` on a non-terminal.

NEVER publishes without a confirmation: an interactive yes, or `--yes`. The final Publish click of the
MakerWorld/Printables browser routes stays with the owner (see publishing/execute.py).

MODE DEFAULT: `--publish`, else `$THREED_PUBLISH_OFFER`, else `offer` in `<config dir>/publish.json`, else `ask`.

LICENCE DEFAULT (stated, not hidden): a design without `license` in publish.yaml is offered under CC BY (open,
available on every platform); a remix inherits what its source requires (share-alike, non-commercial, GPL).
`defaults_used` in the plan lists every field the offer assumed.

HOOKS: `agent_lines(printer)` is what `3d printer subscribe --on bed-free` appends to its message (the agent
runs the offer with `--json`); `after_modify(path)` is what a modify step (`3d mesh deform`) calls.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from errors import InputNotFound, InvalidArgument, UsageError
from publishing import flow

SCHEMA = "3d.publish.offer/1"
MODES = ("auto", "ask", "off")
TRIGGERS = ("print", "modify")
ENV_MODE = "THREED_PUBLISH_OFFER"
CONFIG_NAME = "publish.json"


def config_path() -> Path:
    from cli import paths

    return paths.config_dir() / CONFIG_NAME


def default_mode() -> str:
    """`$THREED_PUBLISH_OFFER`, else the config file's `offer`, else `ask`."""
    raw = os.environ.get(ENV_MODE)
    source = f"${ENV_MODE}"
    if raw is None:
        try:
            data = json.loads(config_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        raw = data.get("offer") if isinstance(data, dict) else None
        source = str(config_path())
    if raw is None:
        return "ask"
    if raw not in MODES:
        raise InvalidArgument(
            f"offer ({source})", str(raw), list(MODES), command="models"
        )
    return str(raw)


# --------------------------------------------------------------------------- finding what to offer
def _latest_make(conn: sqlite3.Connection, printer: str) -> sqlite3.Row | None:
    return conn.execute(  # type: ignore[no-any-return]
        "SELECT * FROM make WHERE printer = ? AND status IN ('printing', 'done') ORDER BY id DESC LIMIT 1",
        (printer,),
    ).fetchone()


def candidate_for_printer(
    printer: str, db_path: str | None = None
) -> dict[str, str] | None:
    """The make a bed-free event of `printer` is about, with its project: None when the library does not
    know a printing/done make there, or the design has no project directory."""
    with flow.library(db_path, create=False) as conn:
        if conn is None:
            return None
        make = _latest_make(conn, printer)
        if make is None:
            return None
        design = conn.execute(
            "SELECT * FROM design WHERE id = ?", (make["design_id"],)
        ).fetchone()
        if design is None or not design["project_dir"]:
            return None
        return {
            "make": str(make["uid"]),
            "design": str(design["name"]),
            "project": str(design["project_dir"]),
        }


def _design_source(
    conn: sqlite3.Connection, design: sqlite3.Row
) -> tuple[str | None, bool]:
    """(url of the source the design came from, True when it is a published REMIX of it).

    `remix_of` = a modified, published remix; `derived_from` = printed/studied but not modified; a make's own
    `source_id` counts as derived."""
    for rel, remix in (("remix_of", True), ("derived_from", False)):
        row = conn.execute(
            "SELECT s.url FROM relation r JOIN model_source s ON s.uid = r.to_id "
            "WHERE r.rel = ? AND r.from_kind = 'design' AND r.from_id = ? ORDER BY r.id DESC LIMIT 1",
            (rel, design["uid"]),
        ).fetchone()
        if row is not None:
            return str(row["url"]), remix
    row = conn.execute(
        "SELECT s.url FROM make m JOIN model_source s ON s.id = m.source_id WHERE m.design_id = ? "
        "ORDER BY m.id DESC LIMIT 1",
        (design["id"],),
    ).fetchone()
    return (str(row["url"]), False) if row is not None else (None, False)


def _already_published(conn: sqlite3.Connection, design: sqlite3.Row) -> list[str]:
    return [
        str(r["platform"])
        for r in conn.execute(
            "SELECT DISTINCT platform FROM publication WHERE design_id = ? AND state IN ('draft', 'published') "
            "AND platform NOT LIKE ?",
            (design["id"], f"%{flow.MAKE_SUFFIX}"),
        )
    ]


def _from_library(
    project: Path | None,
    design: str | None,
    make: str | None,
    printer: str | None,
    db_path: str | None,
) -> tuple[str | None, str | None, Path | None, str | None, bool, list[str]]:
    """What the library knows about the offer: (design name, make uid, project dir, source url, is-a-remix,
    platforms already published). The arguments stand when the library is absent or knows nothing."""
    name, make_uid, src_url, remixed = design, make, None, False
    published: list[str] = []
    with flow.library(db_path, create=False) as conn:
        if conn is None:
            return name, make_uid, project, src_url, remixed, published
        from library import records

        make_row = None
        if make:
            make_row = records.resolve(conn, "make", make, command="models")
        elif printer:
            make_row = _latest_make(conn, printer)
        design_row = None
        if make_row is not None:
            design_row = conn.execute(
                "SELECT * FROM design WHERE id = ?", (make_row["design_id"],)
            ).fetchone()
            make_uid = str(make_row["uid"])
        elif design:
            design_row = conn.execute(
                "SELECT * FROM design WHERE name = ?", (design,)
            ).fetchone()
        elif project is not None:
            design_row = conn.execute(
                "SELECT * FROM design WHERE project_dir = ? OR name = ? ORDER BY project_dir IS NULL LIMIT 1",
                (str(project), flow.design_name(project, None)),
            ).fetchone()
        if design_row is None:
            return name, make_uid, project, src_url, remixed, published
        if make_uid is None:
            latest = conn.execute(
                "SELECT uid FROM make WHERE design_id = ? AND status IN ('printing', 'done') "
                "ORDER BY id DESC LIMIT 1",
                (design_row["id"],),
            ).fetchone()
            make_uid = str(latest["uid"]) if latest is not None else None
        name = str(design_row["name"])
        if project is None and design_row["project_dir"]:
            project = Path(str(design_row["project_dir"]))
        src_url, remixed = _design_source(conn, design_row)
        published = _already_published(conn, design_row)
    return name, make_uid, project, src_url, remixed, published


def _source_license(src_url: str | None, db_path: str | None) -> tuple[str, str]:
    """(licence id, why) the offer defaults to for a source url (None: an original)."""
    source = None
    if src_url:
        from publishing.source import SourceRef, load_source

        with flow.library(db_path, create=False) as conn:
            source = (
                load_source(src_url, conn)
                if conn is not None
                else SourceRef(src_url, "", "", "", "", "", "url")
            )
    return flow.default_license(source)


def _publish_kind(
    trigger: str, src_url: str | None, spec_remix: bool, remixed: bool
) -> str | None:
    """`remix` / `original` for the design itself, None when this trigger offers no design post."""
    if trigger == "modify":
        return "remix" if (src_url or spec_remix) else "original"
    if remixed or spec_remix or src_url is None:
        return "remix" if (remixed or spec_remix) else "original"
    return None


# --------------------------------------------------------------------------- the offer
def build(
    project: Path | None,
    *,
    trigger: str,
    design: str | None = None,
    make: str | None = None,
    printer: str | None = None,
    platform: str = "all",
    db_path: str | None = None,
    allow: tuple[str, ...] = (),
    reason: str = "",
    drop: bool = False,
) -> dict[str, Any]:
    """The plan(s) to offer, as `{design, project, license, plans: [Prepared...]}`. Sends and writes nothing.

    Raises UsageError when neither a project directory nor a library design/make says what to offer."""
    if trigger not in TRIGGERS:
        raise InvalidArgument("--trigger", trigger, list(TRIGGERS), command="models")
    name, make_uid, project, src_url, remixed, published = _from_library(
        project, design, make, printer, db_path
    )
    if project is None:
        raise UsageError(
            "nothing to offer: no PROJECT_DIR given and the library knows no design/make with a project directory",
            command="models",
            remediation=[
                "3d models offer PROJECT_DIR --trigger print|modify",
                "Record the design first: `3d make add --design NAME --project-dir DIR`.",
            ],
        )
    if not project.is_dir():
        raise InputNotFound(str(project), command="models")
    name = flow.design_name(project, name)
    spec_remix = _publish_yaml_remix(project)
    lic, lic_why = _source_license(src_url, db_path)
    title = name.replace("_", " ").replace("-", " ").strip().capitalize()
    defaults = {
        "title": title,
        "description": f"{title}: an OpenSCAD design with CAD sources, renders and photos of the print.",
        "license": lic,
    }
    plans: list[flow.Prepared] = []
    publish_kind = _publish_kind(trigger, src_url, bool(spec_remix), remixed)
    if publish_kind is not None:
        plans.append(
            flow.prepare(
                project,
                platform=platform,
                kind=publish_kind,
                of=src_url if publish_kind == "remix" else None,
                design=name,
                make=make_uid,
                db_path=db_path,
                allow=allow,
                reason=reason,
                drop=drop,
                defaults=defaults,
            )
        )
    if trigger == "print" and make_uid and (src_url or published):
        plans.append(
            flow.prepare(
                project,
                platform=platform,
                kind="make",
                of=src_url,
                design=name,
                make=make_uid,
                db_path=db_path,
                defaults=defaults,
            )
        )
    return {
        "design": name,
        "project": project,
        "make": make_uid,
        "license": {"id": lic, "why": lic_why, "open": flow.is_open(lic)},
        "source": src_url,
        "already_published": published,
        "plans": plans,
    }


def _publish_yaml_remix(project: Path) -> str:
    path = project / "publish.yaml"
    if not path.is_file():
        return ""
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return ""
    return str(data.get("remix_of") or "") if isinstance(data, dict) else ""


def offer_doc(
    built: dict[str, Any],
    *,
    mode: str,
    trigger: str,
    status: str,
    outcomes: list[Any] | None = None,
) -> dict[str, Any]:
    """The `3d.publish.offer/1` document."""
    from publishing import plan

    plans = built["plans"]
    return {
        "schema": SCHEMA,
        "mode": mode,
        "trigger": trigger,
        "status": status,  # offered | needs_confirmation | declined | published | failed | nothing_to_offer | off
        "design": built["design"],
        "make": built["make"],
        "project": str(built["project"]),
        "license": built["license"],
        "source": built["source"],
        "already_published": built["already_published"],
        "plans": [plan.to_json(p.ev, dry_run=outcomes is None) for p in plans],
        "command": _command(built, trigger),
        "outcomes": None if outcomes is None else [o.to_json() for o in outcomes],
    }


def _command(built: dict[str, Any], trigger: str) -> list[list[str]]:
    """The exact command that publishes what was offered (the agent runs it after the user said yes).

    It re-runs the offer with `--yes`, not `3d models publish`: only the offer fills the title / description /
    licence that publish.yaml leaves out, so a replayed `models publish` would be refused for those projects."""
    plans = built["plans"]
    if not plans:
        return []
    cmd = [
        "3d",
        "models",
        "offer",
        str(built["project"]),
        "--trigger",
        trigger,
        "--platform",
        plans[0].platform,
    ]
    if built["make"]:
        cmd += ["--make", str(built["make"])]
    return [[*cmd, "--yes"]]


def summary_text(built: dict[str, Any]) -> str:
    lic = built["license"]
    lines = [
        f"Publish offer for design '{built['design']}' ({built['project']})",
        f"  licence: {lic['id']} — {lic['why']}",
    ]
    if built["already_published"]:
        lines.append(f"  already published: {', '.join(built['already_published'])}")
    for p in built["plans"]:
        ev = p.ev
        platforms = ", ".join(pl.platform for pl in ev.plans) or "no platform"
        lines.append(
            f"  {p.kind}: {platforms}; {len(ev.fileset.files)} file(s), {len(ev.spec.images)} image(s), "
            f"{len(ev.photos)} library photo(s)"
        )
        lines.extend(f"    ! {x}" for x in ev.all_problems())
        if ev.gate is not None:
            lines.append(f"    ! {ev.gate.message}")
        lines.extend(f"    warning: {w}" for w in ev.warnings)
    return "\n".join(lines)


def run_offer(
    project: Path | None,
    *,
    trigger: str,
    mode: str | None,
    yes: bool,
    as_json: bool,
    design: str | None = None,
    make: str | None = None,
    printer: str | None = None,
    platform: str = "all",
    db_path: str | None = None,
    allow: tuple[str, ...] = (),
    reason: str = "",
    drop: bool = False,
    interactive: Callable[[], bool] | None = None,
    ask: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
    send: Callable[[flow.Prepared], list[flow.Outcome]] = flow.send,
) -> int:
    """Run the offer; returns the process exit code (0 offered/declined/published, 1 a send failed)."""
    mode = mode or default_mode()
    if mode not in MODES:
        raise InvalidArgument("--publish", mode, list(MODES), command="models")
    if mode == "off":
        if as_json:
            out(
                json.dumps(
                    {
                        "schema": SCHEMA,
                        "mode": "off",
                        "status": "off",
                        "trigger": trigger,
                    },
                    indent=2,
                )
            )
        return 0
    built = build(
        project,
        trigger=trigger,
        design=design,
        make=make,
        printer=printer,
        platform=platform,
        db_path=db_path,
        allow=allow,
        reason=reason,
        drop=drop,
    )
    plans: list[flow.Prepared] = built["plans"]
    tty = (interactive or (lambda: sys.stdin.isatty() and sys.stdout.isatty()))()
    if not plans:
        _emit(
            offer_doc(built, mode=mode, trigger=trigger, status="nothing_to_offer"),
            summary_text(built) + "\n  nothing to offer for this trigger",
            as_json,
            out,
        )
        return 0
    confirmed, status = _confirmation(built, mode, yes, as_json, tty, ask, out)
    if not confirmed:
        doc = offer_doc(built, mode=mode, trigger=trigger, status=status)
        _report_unconfirmed(built, doc, status, as_json, tty, out)
        return 0
    return _publish(built, mode, trigger, as_json, send, out)


def _publish(
    built: dict[str, Any],
    mode: str,
    trigger: str,
    as_json: bool,
    send: Callable[[flow.Prepared], list[flow.Outcome]],
    out: Callable[[str], None],
) -> int:
    """Send every plan (none when any is blocked) and report; the process exit code."""
    plans: list[flow.Prepared] = built["plans"]
    blocked = [p for p in plans if p.ev.gate is not None or p.ev.all_problems()]
    if blocked:  # show why, and send NOTHING of any plan (a half-published offer is worse than none)
        _emit(
            offer_doc(built, mode=mode, trigger=trigger, status="refused"),
            summary_text(built),
            as_json,
            out,
        )
        flow.refuse_if_blocked(blocked[0])
    outcomes = [o for p in plans for o in send(p)]
    failed = not all(o.ok for o in outcomes)
    doc = offer_doc(
        built,
        mode=mode,
        trigger=trigger,
        status="failed" if failed else "published",
        outcomes=outcomes,
    )
    summary = (
        summary_text(built)
        + "\n"
        + "\n".join(f"  {o.platform}: {o.message}" for o in outcomes)
    )
    _emit(doc, summary, as_json, out)
    return 1 if failed else 0


def _report_unconfirmed(
    built: dict[str, Any],
    doc: dict[str, Any],
    status: str,
    as_json: bool,
    tty: bool,
    out: Callable[[str], None],
) -> None:
    """Nothing was published: show the plan and the command that would (a declined answer on a terminal is silent)."""
    if as_json:
        out(json.dumps(doc, indent=2, ensure_ascii=False))
    elif status != "declined" or not tty:
        out(summary_text(built))
        out(
            "  not published: confirm with the command below"
            if status == "offered"
            else "  not published: auto needs --yes"
        )
        out("\n".join("  " + " ".join(c) for c in doc["command"]))


def _ask_user(
    built: dict[str, Any], ask: Callable[[str], str], out: Callable[[str], None]
) -> bool:
    """Show the summary and ask; the default answer is yes only for an open licence with nothing refused."""
    plans = built["plans"]
    clean = [p for p in plans if p.ev.gate is None and not p.ev.all_problems()]
    out(summary_text(built))
    default_yes = built["license"]["open"] and len(clean) == len(plans)
    answer = (
        ask(
            f"Publish to all platforms with CAD files, renders and photos? [{'Y/n' if default_yes else 'y/N'}] "
        )
        .strip()
        .lower()
    )
    return answer in ("y", "yes") or (answer == "" and default_yes)


def _emit(
    doc: dict[str, Any], text: str, as_json: bool, out: Callable[[str], None]
) -> None:
    """The offer document as JSON, or the human text."""
    out(json.dumps(doc, indent=2, ensure_ascii=False) if as_json else text)


def _confirmation(
    built: dict[str, Any],
    mode: str,
    yes: bool,
    as_json: bool,
    tty: bool,
    ask: Callable[[str], str],
    out: Callable[[str], None],
) -> tuple[bool, str]:
    """(confirmed, status): asks on a terminal in `ask` mode; `--yes` confirms; `auto` without it waits."""
    if not as_json and tty and not yes and mode == "ask":
        confirmed = _ask_user(built, ask, out)
        return confirmed, "offered" if confirmed else "declined"
    if not yes and mode == "auto":
        return False, "needs_confirmation"
    return yes, "offered"


# --------------------------------------------------------------------------- hooks
def agent_lines(printer: str, db_path: str | None = None) -> list[str]:
    """Lines for the bed-free message an omp session receives: the publish offer, when there is one to make.
    [] when the mode is off or the library knows no print on `printer`; the agent only ASKS the user."""
    try:
        if default_mode() == "off":
            return []
        found = candidate_for_printer(printer, db_path)
    except Exception:  # an offer hint must never break the bed-free event itself
        return []
    if found is None:
        return []
    return [
        f"4. Publish offer: the library has a make of '{found['design']}' on {printer}. Run "
        f"`3d models offer --make {found['make']} --trigger print --json` (dry run, publishes nothing), "
        "show the user the plan (platforms, CAD files, renders, photos, licence) and ASK before publishing; "
        f"only after a yes run the `command` of that document (`--yes`). `--publish off` silences this.",
        f"   Table photo for the listing: `3d photo capture --make {found['make']} --lift auto` "
        "(dry run; add --yes to move the bed).",
    ]


def after_modify(path: Path, *, out: Callable[[str], None] | None = None) -> None:
    """Called by a modify step with the file it wrote: when it sits in a project directory, offer to publish.
    Interactive terminal: the same prompt as `3d models offer --trigger modify`; otherwise one hint line on
    stderr. Silent when the mode is off. Never raises: a modify step succeeded whatever the offer does."""
    try:
        mode = default_mode()
        if mode == "off":
            return
        project = project_of(path)
        if project is None:
            return
        if sys.stdin.isatty() and sys.stdout.isatty() and mode == "ask":
            run_offer(project, trigger="modify", mode=mode, yes=False, as_json=False)
        else:
            print(
                f"publish offer: `3d models offer {project} --trigger modify --json` prepares a remix listing "
                "(dry run); --publish off silences this",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 — see docstring
        print(f"publish offer skipped: {exc}", file=sys.stderr)


def project_of(path: Path) -> Path | None:
    for parent in (path.resolve().parent, *path.resolve().parents):
        if (
            (parent / "publish.yaml").is_file()
            or (parent / "3d.yaml").is_file()
            or (parent / "model.json").is_file()
        ):
            return parent
    return None
