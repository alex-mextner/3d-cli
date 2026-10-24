"""Per-platform publication plans for `3d models publish`: which files, which fields, which
exact steps — and which problems stop it. Building a plan sends nothing and needs no
credentials; the dry run is just the plan printed, and a real publish executes the same
plan, so what the dry run shows is what is sent.

Routes (researched 2026-10-24):
* Thingiverse — the official REST API (POST /things, /files/0/uploadFile,
  /files/0/FinalizeFiles, /things/{id}/publish); needs an OAuth access token with write scope.
* MakerWorld, Printables — no public upload API exists. The route is the website itself,
  driven with agent-browser; login goes through `keychain fill` and any 2FA/captcha is handed
  to the owner. The scripted steps stop before the final Publish click.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from errors import InvalidArgument, ThreeDError, UsageError
from marketplaces import cults, thingiverse
from publishing import guard
from publishing.licenses import PLATFORM_LICENSES
from publishing.remix import LicenseLookup, RemixCheck, check_remix
from publishing.source import SourceRef
from publishing.spec import FileSet, PublishSpec, load_spec, select_files

SCHEMA = "3d.publish/1"
PLATFORMS = ("makerworld", "printables", "thingiverse")
KINDS = ("auto", "original", "remix", "make")
SUMMARY_LIMIT = 160
MAKERWORLD_URL = "https://makerworld.com/en"
PRINTABLES_URL = "https://www.printables.com/"
AGENT_BROWSER = "agent-browser"

BrowserStep = tuple[
    str, ...
]  # agent-browser argv after the program name, e.g. ("open", URL)


@dataclass(frozen=True)
class PlatformPlan:
    platform: str
    route: str
    fields: dict[str, Any]
    files: tuple[Path, ...]
    images: tuple[Path, ...]
    steps: tuple[str, ...]  # display lines of the exact steps, in order
    browser_steps: tuple[
        BrowserStep, ...
    ]  # the same steps as agent-browser argv ((): API route)
    problems: tuple[str, ...]


@dataclass(frozen=True)
class Photo:
    """A library photo that goes into the listing (`photo.kind`: table | bed | render)."""

    path: Path
    kind: str
    z_mm: float | None = None

    def to_json(self, project: Path) -> dict[str, Any]:
        return {"path": _rel(self.path, project), "kind": self.kind, "z_mm": self.z_mm}


@dataclass(frozen=True)
class Options:
    """What `3d models publish`/`offer` add to the project's publish.yaml."""

    kind: str = "auto"  # auto | original | remix | make
    of: SourceRef | None = None  # --of: the source of a remix / the model a make is of
    photos: tuple[Photo, ...] = ()
    targets: tuple[
        tuple[str, str], ...
    ] = ()  # make: (platform, model page URL) pairs known to the library
    printer: str = ""  # make: what the print was made on
    filament: str = ""
    note: str = ""  # make: the text of the post (default: the description)
    platform_all: bool = (
        False  # `--platform all`: platforms the project can reach, not every name
    )
    allow_nonprintable: tuple[str, ...] = ()
    reason: str = ""
    drop_nonprintable: bool = False
    defaults: Mapping[str, str] | None = (
        None  # offer only: stand-ins for publish.yaml's title/description/license
    )


@dataclass(frozen=True)
class Evaluation:
    project: Path
    spec: PublishSpec
    fileset: FileSet
    remix: RemixCheck | None
    plans: tuple[PlatformPlan, ...]
    problems: tuple[str, ...]  # refusals that apply to every platform
    kind: str = "original"
    source: SourceRef | None = None
    photos: tuple[Photo, ...] = ()
    parts: guard.PartsReport | None = None
    warnings: tuple[str, ...] = ()
    gate: ThreeDError | None = (
        None  # NonPrintableParts: raised by the caller AFTER the plan is printed
    )

    def all_problems(self) -> tuple[str, ...]:
        found = list(self.problems)
        found.extend(
            f"{plan.platform}: {p}" for plan in self.plans for p in plan.problems
        )
        return tuple(found)


def description_for(spec: PublishSpec) -> str:
    text = spec.description
    if spec.remix_of:
        text += f"\n\nRemix of {spec.remix_of}"
        if spec.permission_ref:
            text += f" (published with the original author's permission: {spec.permission_ref})"
    return text


def summary_for(spec: PublishSpec) -> str:
    if spec.summary:
        return spec.summary
    flat = " ".join(spec.description.split())
    return (
        flat if len(flat) <= SUMMARY_LIMIT else flat[: SUMMARY_LIMIT - 1].rstrip() + "…"
    )


def thingiverse_ancestor(url: str) -> int | None:
    """The Thingiverse thing id when `url` is a Thingiverse model (Thingiverse links remixes by id)."""
    ref = thingiverse.match(url)
    return int(ref) if ref else None


def _plan_thingiverse(spec: PublishSpec, files: FileSet) -> PlatformPlan:
    problems: list[str] = []
    code = PLATFORM_LICENSES["thingiverse"].get(spec.license.id)
    if code is None:
        problems.append(f"Thingiverse does not offer the licence {spec.license.label}")
    if not spec.category:
        problems.append(
            "Thingiverse requires a category: set `category` in publish.yaml (a full name such as 'Household')"
        )
    body: dict[str, Any] = {
        "name": spec.title,
        "license": code or spec.license.id,
        "category": spec.category,
        "description": description_for(spec),
        "tags": list(spec.tags),
        "is_wip": False,
    }
    ancestor = thingiverse_ancestor(spec.remix_of) if spec.remix_of else None
    if ancestor is not None:
        body.update(is_remix=True, ancestors=[ancestor])
    paths = tuple(f.path for f in files.files)
    api = thingiverse.api_url()
    steps = [
        f"POST {api}/things  (JSON body = fields; creates an unpublished draft)",
        *(
            f"POST {api}/files/0/uploadFile  (multipart: {p.name})"
            for p in (*paths, *spec.images)
        ),
        f"POST {api}/files/0/FinalizeFiles  "
        '({"pending_uploads": [ids, rank 10, 20, ...], "target_id": <thing id>, "target_type": "thing"})',
        f"POST {api}/things/<thing id>/publish",
    ]
    return PlatformPlan(
        "thingiverse",
        "official REST API, OAuth bearer token",
        body,
        paths,
        spec.images,
        tuple(steps),
        (),
        tuple(problems),
    )


def _browser_steps(
    platform: str, spec: PublishSpec, files: FileSet, fields: dict[str, Any]
) -> tuple[BrowserStep, ...]:
    start, upload_label = (
        (MAKERWORLD_URL, "3D Original Model")
        if platform == "makerworld"
        else (PRINTABLES_URL, "Upload a model")
    )
    uploads = [*(str(f.path) for f in files.files), *(str(i) for i in spec.images)]
    steps: list[BrowserStep] = [
        ("open", start),
        (
            "find",
            "role",
            "link",
            "click",
            "--name",
            "Upload" if platform == "makerworld" else "Create",
        ),
        ("find", "text", upload_label, "click"),
        ("upload", "input[type=file]", *uploads),
    ]
    for label, key in (("Title", "title"), ("Description", "description")):
        steps.append(("find", "label", label, "fill", str(fields[key])))
    if platform == "printables":
        steps.append(("find", "label", "Summary", "fill", str(fields["summary"])))
    steps.append(("find", "label", "Tags", "fill", ", ".join(fields["tags"])))
    steps.append(("find", "label", "License", "click"))
    steps.append(("find", "text", str(fields["license"]), "click"))
    if fields["category"]:
        steps.append(("find", "label", "Category", "fill", str(fields["category"])))
    steps.append(("screenshot",))
    return tuple(steps)


def _plan_browser(platform: str, spec: PublishSpec, files: FileSet) -> PlatformPlan:
    problems: list[str] = []
    name = PLATFORM_LICENSES[platform].get(spec.license.id)
    if name is None:
        problems.append(f"{platform} does not offer the licence {spec.license.label}")
    if not spec.images:
        problems.append(
            "a cover image is required: put renders in previews/ or list them under `images` in publish.yaml"
        )
    fields: dict[str, Any] = {
        "title": spec.title,
        "description": description_for(spec),
        "tags": list(spec.tags),
        "license": name or spec.license.id,
        "category": spec.category,
    }
    if platform == "printables":
        fields["summary"] = summary_for(spec)
    if spec.remix_of:
        fields["remix_of"] = spec.remix_of
    steps = _browser_steps(platform, spec, files, fields)
    display = (
        *(f"{AGENT_BROWSER} {shlex.join(_shorten(s))}" for s in steps),
        "STOP before the final Publish button: review the draft in the open window and press it yourself",
    )
    route = "agent-browser (no public upload API); login via keychain fill, 2FA/captcha by the owner"
    return PlatformPlan(
        platform,
        route,
        fields,
        tuple(f.path for f in files.files),
        spec.images,
        display,
        steps,
        tuple(problems),
    )


def _shorten(step: BrowserStep, limit: int = 60) -> BrowserStep:
    if (
        step[0] == "upload"
    ):  # one line: the selector and the file names, not their full paths
        return (step[0], step[1], *(Path(a).name for a in step[2:]))
    return tuple(a if len(a) <= limit else a[: limit - 1] + "…" for a in step)


MAKE_LABELS = {
    # The button/link that starts a "make" post on each site. UNVERIFIED against the live pages (#565): the
    # run stops at the first step the page does not answer and says which.
    "printables": "Add make",
    "makerworld": "Share your print",
    "thingiverse": "I Made One",
    "cults": "Add a make",
}
CULTS_MAX_URLS = 10  # fileUrls / imageUrls per createCreation call (Cults API)
CULTS_CURRENCY = "EUR"


def _plan_cults(spec: PublishSpec, files: FileSet) -> PlatformPlan:
    """Cults3D: GraphQL `createCreation`. Cults PULLS files and images from public https URLs (the API takes
    no upload), so every file and image needs a `hosted:` URL in publish.yaml; category and licence ids are
    resolved by two read-only queries just before the mutation."""
    problems: list[str] = []
    hosted = dict(spec.hosted)
    paths = tuple(f.path for f in files.files)
    names = [*(p.name for p in paths), *(i.name for i in spec.images)]
    missing = [n for n in names if n not in hosted]
    if missing:
        problems.append(
            "Cults pulls files and images from public https URLs; add `hosted:` entries to publish.yaml for "
            + ", ".join(missing)
        )
    if not spec.category:
        problems.append(
            "Cults requires a category: set `category` in publish.yaml (its English name, e.g. 'Home')"
        )
    if len(files.files) > CULTS_MAX_URLS or len(spec.images) > CULTS_MAX_URLS:
        problems.append(
            f"Cults takes at most {CULTS_MAX_URLS} file URLs and {CULTS_MAX_URLS} image URLs per creation: "
            "bundle the CAD files into a zip"
        )
    if not spec.images:
        problems.append(
            "a cover image is required: put renders in previews/ or list them under `images` in publish.yaml"
        )
    fields: dict[str, Any] = {
        "name": spec.title,
        "description": description_for(spec),
        "tags": list(spec.tags),
        "license": spec.license.label,
        "category": spec.category,
        "locale": "EN",
        "price": f"0 {CULTS_CURRENCY} (free download)",
        "file_urls": [hosted[p.name] for p in paths if p.name in hosted],
        "image_urls": [hosted[i.name] for i in spec.images if i.name in hosted],
    }
    api = cults.api_url()
    steps = (
        f"POST {api}  query categories {{ id name(locale: EN) }}  (find '{spec.category}')",
        f"POST {api}  query licenses {{ code name }}  (find the one named like {spec.license.label})",
        f"POST {api}  mutation createCreation(name, description, categoryId, licenseCode, tagNames, "
        "fileUrls, imageUrls, downloadPrice: 0, locale: EN)  (HTTP Basic: Cults username + API key)",
    )
    return PlatformPlan(
        "cults",
        "GraphQL API, HTTP Basic (username + API key); files/images pulled from your public https URLs",
        fields,
        paths,
        spec.images,
        steps,
        (),
        tuple(problems),
    )


def _plan_make(
    platform: str,
    spec: PublishSpec,
    opts: Options,
    model_url: str,
    photos: tuple[Path, ...],
) -> PlatformPlan:
    """A make post: open the model page, press the site's "I made one", attach the photos, fill the note,
    stop before the final button. The same agent-browser route for every site (no site has a documented API
    for makes), so it is as unverified as the other browser routes."""
    problems: list[str] = []
    if not photos:
        problems.append(
            "a make post needs at least one photo of the print: `3d photo capture --make ID --yes` or "
            "`3d photo add FILE --make ID --kind table`"
        )
    detail = ", ".join(
        x
        for x in (
            f"printer {opts.printer}" if opts.printer else "",
            f"filament {opts.filament}" if opts.filament else "",
        )
        if x
    )
    note = (opts.note or spec.description) + (
        f"\n\nPrinted with {detail}." if detail else ""
    )
    fields: dict[str, Any] = {
        "model_url": model_url,
        "note": note,
        "printer": opts.printer or "",
        "filament": opts.filament or "",
        "photos": [p.name for p in photos],
    }
    steps: tuple[BrowserStep, ...] = (
        ("open", model_url),
        ("find", "text", MAKE_LABELS[platform], "click"),
        ("upload", "input[type=file]", *(str(p) for p in photos)),
        ("find", "label", "Description", "fill", note),
        ("screenshot",),
    )
    display = (
        *(f"{AGENT_BROWSER} {shlex.join(_shorten(s))}" for s in steps),
        "STOP before the final Post button: review the draft in the open window and press it yourself",
    )
    return PlatformPlan(
        platform,
        "agent-browser (no public API for makes); login via keychain fill, 2FA/captcha by the owner",
        fields,
        (),
        photos,
        display,
        steps,
        tuple(problems),
    )


PLANNERS: dict[str, Callable[[PublishSpec, FileSet], PlatformPlan]] = {
    "thingiverse": _plan_thingiverse,
    "makerworld": lambda spec, files: _plan_browser("makerworld", spec, files),
    "printables": lambda spec, files: _plan_browser("printables", spec, files),
    "cults": _plan_cults,
}


def platform_list(raw: str) -> list[str]:
    """`all` is the three platforms every project can reach (Cults joins it when publish.yaml declares
    `hosted:` URLs, see `evaluate`); otherwise one name or a comma-separated list."""
    choices = (*PLATFORMS, "cults", "all")
    if raw == "all":
        return list(PLATFORMS)
    names = [n.strip() for n in raw.split(",")]
    for name in names:
        if name not in choices[:-1]:
            raise InvalidArgument("--platform", name, list(choices), command="models")
    return list(dict.fromkeys(names))


def _photo_images(
    spec_images: tuple[Path, ...], photos: Sequence[Photo]
) -> tuple[tuple[Path, ...], list[str]]:
    """The spec's images plus the library photos that exist on disk; the missing ones are warnings."""
    images = list(spec_images)
    warnings: list[str] = []
    for photo in photos:
        if not photo.path.is_file():
            warnings.append(
                f"photo {photo.path} ({photo.kind}) is in the library but the file is gone: skipped"
            )
        elif photo.path not in images:
            images.append(photo.path)
    return tuple(images), warnings


def evaluate(
    project: Path,
    platforms: Sequence[str],
    lookup: LicenseLookup,
    options: Options | None = None,
) -> Evaluation:
    """Load the spec, select the files, apply every refusal rule and build one plan per platform."""
    opts = options or Options()
    if opts.kind not in KINDS:
        raise InvalidArgument("--kind", opts.kind, list(KINDS), command="models")
    spec = load_spec(project, opts.defaults)
    kind = opts.kind
    if kind == "auto":
        kind = "remix" if (opts.of or spec.remix_of) else "original"
    if kind == "original" and opts.of is not None:
        raise UsageError(
            "--of names a source, so the post is a remix or a make, not an original",
            command="models",
            remediation=[
                "Use --kind remix --of SOURCE, --kind make --of SOURCE, or drop --of."
            ],
        )
    if kind == "remix" and not (opts.of or spec.remix_of):
        raise UsageError(
            "--kind remix needs the source: --of URL_OR_MODEL_JSON, or remix_of in publish.yaml",
            command="models",
            remediation=[
                "3d models publish DIR --kind remix --of https://www.printables.com/model/311253 --platform all"
            ],
        )
    if kind == "make":
        return _evaluate_make(project, platforms, spec, opts)
    if opts.of is not None:
        spec = replace(spec, remix_of=opts.of.url)
    problems: list[str] = []
    files = select_files(project)
    report, files = guard.evaluate_parts(
        project,
        spec,
        files,
        allow=opts.allow_nonprintable,
        reason=opts.reason,
        drop=opts.drop_nonprintable,
    )
    gate = report.gate_error()
    images, warnings = _photo_images(spec.images, opts.photos)
    attribution = guard.attribution(report.references)
    spec = replace(
        spec,
        summary=summary_for(spec),
        images=images,
        description=spec.description + (f"\n\n{attribution}" if attribution else ""),
    )
    for role, hint in (
        ("3mf", "slice the model and save the project 3mf as print/<name>.3mf"),
        ("scad", "the OpenSCAD source must be published with the model"),
    ):
        if role in files.missing():
            problems.append(
                f"no .{role} file in {project} (looked in the project root and print/): {hint}"
            )
    problems.extend(
        f"two files are named {name}: rename one so the upload names stay unique"
        for name in files.clashes
    )
    remix = check_remix(spec, lookup)
    if remix is not None:
        problems.extend(remix.problems)
    problems.extend(report.problems())
    chosen = list(platforms)
    if opts.platform_all and spec.hosted and "cults" not in chosen:
        chosen.append("cults")
    plans = tuple(PLANNERS[p](spec, files) for p in chosen)
    return Evaluation(
        project,
        spec,
        files,
        remix,
        plans,
        tuple(problems),
        kind=kind,
        source=opts.of,
        photos=opts.photos,
        parts=report,
        warnings=(*warnings, *report.warnings()),
        gate=gate,
    )


def _evaluate_make(
    project: Path, platforms: Sequence[str], spec: PublishSpec, opts: Options
) -> Evaluation:
    """A make post: one plan per platform that has a model page to attach the make to. Licence rules of a
    remix do not apply (printing a model is what every licence allows); the photos are the whole payload."""
    targets = dict(opts.targets)
    if opts.of is not None and opts.of.site and opts.of.site not in targets:
        targets[opts.of.site] = opts.of.url
    problems: list[str] = []
    chosen = (
        [p for p in platforms if p in targets] if opts.platform_all else list(platforms)
    )
    if not chosen:
        problems.append(
            "no model page to attach a make to: pass --of URL_OR_MODEL_JSON (a marketplace model) or publish "
            "the design first so the library knows its page"
        )
    problems.extend(
        f"{p}: no model page on {p} to attach the make to (pass --of with a {p} URL, or publish there first)"
        for p in chosen
        if p not in targets
    )
    real = tuple(p for p in opts.photos if p.kind != "render")
    photos, warnings = _photo_images((), real)
    plans = tuple(
        _plan_make(p, spec, opts, targets[p], photos) for p in chosen if p in targets
    )
    return Evaluation(
        project,
        spec,
        FileSet((), ()),
        None,
        plans,
        tuple(problems),
        kind="make",
        source=opts.of,
        photos=opts.photos,
        warnings=tuple(warnings),
    )


def _rel(path: Path, project: Path) -> str:
    try:
        return str(path.relative_to(project))
    except ValueError:
        return str(path)


def _parts_lines(report: guard.PartsReport) -> list[str]:
    out: list[str] = []
    if report.matched or report.references:
        out.append(
            f"\nparts ({len(report.parts)}) — only `printed` parts are published as printable files"
        )
        for p in report.parts:
            mark = "printable" if p.printable else "NOT printable"
            src = (
                f" <- {p.reference_source.url} ({p.reference_source.license})"
                if p.reference_source
                else ""
            )
            out.append(f"  {p.name:<16} {p.role:<9} {mark}{src}")
        out.extend(
            f"  reference {c.source_url}: "
            + ("REFUSED, see below" if c.problems else "licence ok")
            for c in report.checks
        )
    return out


def render_text(ev: Evaluation, *, dry_run: bool) -> str:
    spec, out = ev.spec, []
    out.append(
        f"3d models publish — {'DRY RUN, nothing is sent' if dry_run else 'PUBLISH'}"
    )
    out.append(f"project   {ev.project}")
    out.append(f"kind      {ev.kind}")
    out.append(f"title     {spec.title}")
    out.append(f"licence   {spec.license.label} ({spec.license.id})")
    if spec.defaults_used:
        out.append(
            f"          (assumed: {', '.join(spec.defaults_used)} — not set in publish.yaml)"
        )
    if ev.kind == "make" and ev.source is not None:
        out.append(f"make of   {ev.source.url}")
    if ev.remix is not None:
        out.append(f"remix of  {ev.remix.source_url}")
        out.extend(f"          - {line}" for line in ev.remix.evidence)
        if ev.source is not None and ev.source.origin != "url":
            out.append(
                f"          - the source licence above was read from the {ev.source.origin}, not asked of the site"
            )
        if ev.remix.permission_ref:
            out.append(f"          - permission_ref: {ev.remix.permission_ref}")
        out.extend(
            f"          - lifted by permission_ref: {line}" for line in ev.remix.waived
        )
        out.append(
            "          => " + ("REFUSED, see below" if ev.remix.problems else "allowed")
        )
    if ev.kind != "make":
        out.append(
            f"\nfiles ({len(ev.fileset.files)}) — the same set goes to every platform"
        )
        out.extend(
            f"  {f.role:<5} {_rel(f.path, ev.project)}  ({f.size} B)"
            for f in ev.fileset.files
        )
        out.append(f"images ({len(spec.images)})")
        out.extend(f"  {_rel(i, ev.project)}" for i in spec.images)
    if ev.photos:
        out.append(f"photos from the library ({len(ev.photos)})")
        out.extend(
            f"  {p.kind:<6} {_rel(p.path, ev.project)}"
            + (f"  (bed at Z {p.z_mm:g})" if p.z_mm is not None else "")
            for p in ev.photos
        )
    if ev.parts is not None:
        out.extend(_parts_lines(ev.parts))
    out.extend(f"warning: {w}" for w in ev.warnings)
    for plan in ev.plans:
        state = "REFUSED" if plan.problems or ev.problems else "ready"
        out.append(f"\n== {plan.platform} [{state}] — {plan.route}")
        out.append("  fields")
        for key, value in plan.fields.items():
            shown = (
                ", ".join(map(str, value)) if isinstance(value, list) else str(value)
            )
            flat = " ".join(shown.split())
            out.append(f"    {key}: {flat if len(flat) <= 100 else flat[:99] + '…'}")
        out.append("  steps")
        out.extend(f"    {n}. {s}" for n, s in enumerate(plan.steps, start=1))
    problems = ev.all_problems()
    if ev.gate is not None:
        out.append(f"\nREFUSED — {ev.gate.message}")
        out.extend(f"  - {r}" for r in ev.gate.remediation)
    if problems:
        out.append(f"\nREFUSED — {len(problems)} problem(s), nothing would be sent:")
        out.extend(f"  - {p}" for p in problems)
    elif dry_run and ev.gate is None:
        out.append("\nOK — rerun with --yes to publish.")
    return "\n".join(out)


def to_json(ev: Evaluation, *, dry_run: bool) -> dict[str, Any]:
    problems = list(ev.all_problems())
    return {
        "schema": SCHEMA,
        "dry_run": dry_run,
        "kind": ev.kind,
        "project": str(ev.project),
        "title": ev.spec.title,
        "license": ev.spec.license.id,
        "defaults_used": list(ev.spec.defaults_used),
        "source": None if ev.source is None else ev.source.to_json(),
        "remix": None
        if ev.remix is None
        else {
            "source": ev.remix.source_url,
            "evidence": list(ev.remix.evidence),
            "permission_ref": ev.remix.permission_ref,
            "source_licenses": [lic.id for lic in ev.remix.source_licenses],
        },
        "files": [
            {"role": f.role, "path": _rel(f.path, ev.project), "size": f.size}
            for f in ev.fileset.files
        ],
        "images": [_rel(i, ev.project) for i in ev.spec.images],
        "photos": [p.to_json(ev.project) for p in ev.photos],
        "parts": None if ev.parts is None else ev.parts.to_json(),
        "warnings": list(ev.warnings),
        "platforms": {
            p.platform: {
                "route": p.route,
                "fields": p.fields,
                "steps": list(p.steps),
                "problems": list(p.problems),
                "files": [_rel(f, ev.project) for f in p.files],
                "images": [_rel(i, ev.project) for i in p.images],
            }
            for p in ev.plans
        },
        "problems": problems,
        "gate": None if ev.gate is None else ev.gate.message,
        "refused": bool(problems) or ev.gate is not None,
    }
