"""The publish flow shared by `3d models publish`, `3d models offer` and `library.actions.publish`.

`prepare` builds the plan (reads the library for photos, makes and known model pages; sends and writes
nothing). `send` carries a prepared plan out and records it: a `publication` row per platform
(`planned` before the attempt, then `draft` / `published` / `failed`), the `remix_of` relation of a remix, the
registered render photos, and a library event for every explicit non-printable override. Nothing is ever
deleted: a failed attempt stays as a `failed` row.

Dry run is `prepare` alone: it opens the library read-only when the file exists and never creates it.
"""

from __future__ import annotations

import contextlib
import hashlib
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from errors import ThreeDError, UsageError
from publishing import execute, plan
from publishing.licenses import parse_license
from publishing.plan import Evaluation, Options, Photo
from publishing.remix import LicenseLookup
from publishing.source import SourceRef, load_source, lookup_for

DEFAULT_LICENSE = "cc-by"
OPEN_LICENSES = (
    "cc0",
    "cc-by",
    "cc-by-sa",
    "cc-by-nc",
    "cc-by-nc-sa",
    "gpl",
    "lgpl",
    "bsd",
)
MAKE_SUFFIX = ":make"  # publication.platform of a make post: "printables:make"


@dataclass
class Prepared:
    ev: Evaluation
    project: Path
    design_name: str
    platform: str
    kind: str
    db_path: str | None
    design_uid: str | None = None  # None: the design is not in the library yet
    make_uid: str | None = None
    source: SourceRef | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Outcome:
    platform: str
    ok: bool
    state: str  # published | draft | failed
    url: str | None
    message: str
    publication: str | None = (
        None  # uid of the library row ("" when no library was written)
    )

    def to_json(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "ok": self.ok,
            "state": self.state,
            "url": self.url,
            "message": self.message,
            "publication": self.publication,
        }


@contextlib.contextmanager
def library(
    db_path: str | None, *, create: bool
) -> Iterator[sqlite3.Connection | None]:
    """The library connection; with `create=False` None when the file does not exist (a dry run never makes it)."""
    from library import db as librarydb

    path = Path(db_path).expanduser() if db_path else librarydb.library_db_path()
    if not create and not path.exists():
        yield None
        return
    conn = librarydb.open_library(path)
    try:
        yield conn
    finally:
        conn.close()


def design_name(project: Path, explicit: str | None) -> str:
    """`--design`, else `name:` of the project's 3d.yaml, else the directory name."""
    if explicit:
        return explicit
    path = project / "3d.yaml"
    if path.is_file():
        import yaml

        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError, UnicodeDecodeError):
            data = None
        if (
            isinstance(data, dict)
            and isinstance(data.get("name"), str)
            and data["name"].strip()
        ):
            return str(data["name"]).strip()
    return project.resolve().name


def default_license(source: SourceRef | None) -> tuple[str, str]:
    """(licence id, why) of the open licence a new design is offered under: CC BY, raised to what the source
    licence requires of a remix (share-alike, non-commercial, GPL)."""
    if source is None or not source.license:
        return DEFAULT_LICENSE, "default: CC BY (open, offered by every platform)"
    lic = parse_license(source.license)
    if lic is None or lic.forbids_derivatives:
        return (
            DEFAULT_LICENSE,
            f"default: CC BY (the source licence '{source.license}' needs a permission, see the plan)",
        )
    if lic.id in ("gpl", "lgpl"):
        return lic.id, f"the source is {lic.label}"
    wanted = (
        "cc-by"
        + ("-nc" if lic.non_commercial else "")
        + ("-sa" if lic.share_alike else "")
    )
    if wanted == DEFAULT_LICENSE:
        return DEFAULT_LICENSE, "default: CC BY (the source allows it)"
    return wanted, f"inherited from the source: {lic.label}"


def is_open(license_id: str) -> bool:
    return license_id in OPEN_LICENSES


def _source_from_row(row: sqlite3.Row) -> SourceRef:
    return SourceRef(
        str(row["url"]),
        str(row["site"]),
        str(row["model_id"]),
        str(row["license"] or ""),
        str(row["author"] or ""),
        "",
        "library",
    )


def library_photos(
    conn: sqlite3.Connection | None, design_id: int | None, make_id: int | None
) -> tuple[Photo, ...]:
    if conn is None or (design_id is None and make_id is None):
        return ()
    from library import records

    return tuple(
        Photo(Path(p["path"]), p["kind"], p["z_mm"])
        for p in records.photos_of(
            conn, design_id=None if make_id else design_id, make_id=make_id
        )
    )


def prepare(
    project: Path,
    *,
    platform: str,
    kind: str = "auto",
    of: str | None = None,
    design: str | None = None,
    make: str | None = None,
    db_path: str | None = None,
    note: str = "",
    allow: Sequence[str] = (),
    reason: str = "",
    drop: bool = False,
    defaults: Mapping[str, str] | None = None,
    lookup: LicenseLookup | None = None,
) -> Prepared:
    """Build the evaluation. Reads the library when it exists (photos, the make, known model pages)."""
    from library import records

    name = design_name(project, design)
    platforms = plan.platform_list(platform)
    with library(db_path, create=False) as conn:
        design_row = (
            conn.execute("SELECT * FROM design WHERE name = ?", (name,)).fetchone()
            if conn is not None
            else None
        )
        make_row = None
        if make is not None:
            if conn is None:
                raise UsageError(
                    f"--make {make}: there is no library database to find it in",
                    command="models",
                    remediation=[
                        "Record the make first: `3d make add --design NAME ...` (or pass --db PATH)."
                    ],
                )
            make_row = records.resolve(conn, "make", make, command="models")
            design_row = conn.execute(
                "SELECT * FROM design WHERE id = ?", (make_row["design_id"],)
            ).fetchone()
            name = (
                str(design_row["name"])
                if design_row is not None and design is None
                else name
            )
        source: SourceRef | None = None
        if of is not None:
            source = load_source(of, conn)
        elif (
            make_row is not None
            and make_row["source_id"] is not None
            and conn is not None
        ):
            source = _source_from_row(
                conn.execute(
                    "SELECT * FROM model_source WHERE id = ?", (make_row["source_id"],)
                ).fetchone()
            )
        photos = library_photos(
            conn,
            int(design_row["id"]) if design_row is not None else None,
            int(make_row["id"]) if make_row is not None else None,
        )
        targets: dict[str, str] = {}
        printer = filament = ""
        if conn is not None and design_row is not None:
            for r in conn.execute(
                "SELECT platform, url FROM publication WHERE design_id = ? AND url IS NOT NULL "
                "AND state IN ('draft', 'published') AND platform NOT LIKE ? ORDER BY id",
                (design_row["id"], f"%{MAKE_SUFFIX}"),
            ):
                targets[str(r["platform"])] = str(r["url"])
        if make_row is not None:
            printer, filament = (
                str(make_row["printer"] or ""),
                str(make_row["filament"] or ""),
            )
        eff_defaults = dict(defaults or {})
        if kind == "make":
            eff_defaults.setdefault("title", name)
            eff_defaults.setdefault("description", f"My print of {name}.")
            eff_defaults.setdefault("license", DEFAULT_LICENSE)
        opts = Options(
            kind=kind,
            of=source if of is not None or kind == "make" else None,
            photos=photos,
            targets=tuple(targets.items()),
            printer=printer,
            filament=filament,
            note=note,
            platform_all=platform == "all",
            allow_nonprintable=tuple(allow),
            reason=reason,
            drop_nonprintable=drop,
            defaults=eff_defaults or None,
        )
        ev = plan.evaluate(project, platforms, lookup or lookup_for(source), opts)
        return Prepared(
            ev,
            project,
            name,
            platform,
            ev.kind,
            db_path,
            design_uid=str(design_row["uid"]) if design_row is not None else None,
            make_uid=str(make_row["uid"]) if make_row is not None else None,
            source=source,
        )


def refuse_if_blocked(prep: Prepared) -> None:
    """Raise the refusal of a plan that must not be sent: a non-printable part (exit 1) first, then the
    problems (exit 2). Callers print the plan before calling this."""
    if prep.ev.gate is not None:
        raise prep.ev.gate
    problems = prep.ev.all_problems()
    if problems:
        raise UsageError(
            f"publish refused: {len(problems)} problem(s); nothing was sent",
            command="models",
            remediation=["The REFUSED list above has each problem and its fix."],
        )


def site_key(url: str) -> tuple[str, str]:
    from publishing.source import split_url

    site, model_id = split_url(url)
    return (site, model_id) if site else (urlsplit(url).hostname or "web", url)


def record_renders(db_path: str | None, design_uid: str, ev: Evaluation) -> int:
    """Register the project's listing images (previews/) as `photo(kind=render)` of the design through the
    `photo` action (the same door as the CLI and the TUI); idempotent. -> how many rows are new."""
    from library import actions

    n = 0
    for image in ev.spec.images:
        if any(p.path == image for p in ev.photos):
            continue
        key = (
            "render:"
            + hashlib.sha256(f"{design_uid}:{image}".encode()).hexdigest()[:32]
        )
        res = actions.photo(
            str(image),
            design=design_uid,
            kind="render",
            db_path=db_path,
            source_key=key,
        )
        n += int(bool(res["ok"] and res["data"].get("created")))
    return n


def record_remix(
    conn: sqlite3.Connection, design_uid: str, source: SourceRef | None
) -> str | None:
    """design --remix_of--> model_source; returns the source uid."""
    from library import records

    if source is None:
        return None
    site, model_id = (
        (source.site, source.model_id) if source.site else site_key(source.url)
    )
    row, _ = records.ensure_source(
        conn,
        site=site,
        model_id=model_id or source.url,
        url=source.url,
        license=source.license or None,
        author=source.author or None,
    )
    records.relate(
        conn, "remix_of", "design", design_uid, "model_source", str(row["uid"])
    )
    return str(row["uid"])


def send(
    prep: Prepared,
    *,
    runner: execute.Runner = execute.run_agent_browser,
    fetch: execute.Fetch | None = None,
    thingiverse_api: str | None = None,
    cults_api: str | None = None,
    token_for: Callable[[], str] | None = None,
    cults_credentials: Callable[[], tuple[str, str]] | None = None,
) -> list[Outcome]:
    """Publish for real and record it. Credentials are resolved first, so a missing token cannot leave one
    platform published and the rest not. The caller has already refused blocked plans."""
    from library import records
    from marketplaces import credentials
    from printops import events

    ev = prep.ev
    token = ""
    if any(p.platform == "thingiverse" for p in ev.plans) and prep.kind != "make":
        token = (token_for or credentials.thingiverse_upload_token)()
    cults_auth = ("", "")
    if any(p.platform == "cults" for p in ev.plans) and prep.kind != "make":
        cults_auth = (cults_credentials or credentials.cults_basic_auth)()
    outcomes: list[Outcome] = []
    with library(prep.db_path, create=True) as conn:
        assert conn is not None
        design = records.ensure_design(
            conn, prep.design_name, str(prep.project.resolve())
        )
        design_uid = str(design["uid"])
        if prep.make_uid is not None:
            records.relate(conn, "make_of", "make", prep.make_uid, "design", design_uid)
        if ev.parts is not None:
            for part in ev.parts.overridden:
                with _tx(conn):
                    events.emit(
                        conn,
                        "library.publish.override",
                        f"design:{design_uid}",
                        {
                            "part": part.name,
                            "role": part.role,
                            "reason": ev.parts.reason,
                            "kind": prep.kind,
                        },
                        "cli:models",
                    )
        if prep.kind != "make":
            record_renders(prep.db_path, design_uid, ev)
        if prep.kind == "remix" or (ev.remix is not None):
            source = prep.source or (
                SourceRef(ev.remix.source_url, "", "", "", "", "", "url")
                if ev.remix is not None
                else None
            )
            record_remix(conn, design_uid, source)
        for item in ev.plans:
            suffix = MAKE_SUFFIX if prep.kind == "make" else ""
            pub = records.add_publication(
                conn, prep.design_name, item.platform + suffix, state="planned"
            )
            outcome = _send_one(
                item, ev, token, cults_auth, runner, fetch, thingiverse_api, cults_api
            )
            row = records.set_publication(
                conn, str(pub["uid"]), state=outcome.state, url=outcome.url
            )
            outcomes.append(
                Outcome(
                    outcome.platform,
                    outcome.ok,
                    outcome.state,
                    outcome.url,
                    outcome.message,
                    str(row["uid"]),
                )
            )
    return outcomes


@contextlib.contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[None]:
    from printops import db

    with db.transaction(conn):
        yield


def _send_one(
    item: plan.PlatformPlan,
    ev: Evaluation,
    token: str,
    cults_auth: tuple[str, str],
    runner: execute.Runner,
    fetch: execute.Fetch | None,
    thingiverse_api: str | None,
    cults_api: str | None,
) -> Outcome:
    try:
        if item.platform == "thingiverse" and ev.kind != "make":
            kwargs: dict[str, Any] = {"token": token, "api": thingiverse_api}
            if fetch is not None:
                kwargs["fetch"] = fetch
            url = execute.publish_thingiverse(item, **kwargs)
            return Outcome(item.platform, True, "published", url, f"published {url}")
        if item.platform == "cults" and ev.kind != "make":
            url = execute.publish_cults(
                item, ev.spec.license.id, credentials=cults_auth, api=cults_api
            )
            return Outcome(item.platform, True, "published", url, f"published {url}")
        result = execute.publish_browser(item, runner)
        return Outcome(
            item.platform,
            result.ok,
            "draft" if result.ok else "failed",
            None,
            result.message,
        )
    except ThreeDError as exc:
        return Outcome(item.platform, False, "failed", None, exc.render())
