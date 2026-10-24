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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from errors import InvalidArgument
from marketplaces import thingiverse
from publishing.licenses import PLATFORM_LICENSES
from publishing.remix import LicenseLookup, RemixCheck, check_remix
from publishing.spec import FileSet, PublishSpec, load_spec, select_files

PLATFORMS = ("makerworld", "printables", "thingiverse")
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
class Evaluation:
    project: Path
    spec: PublishSpec
    fileset: FileSet
    remix: RemixCheck | None
    plans: tuple[PlatformPlan, ...]
    problems: tuple[str, ...]  # refusals that apply to every platform

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


PLANNERS: dict[str, Callable[[PublishSpec, FileSet], PlatformPlan]] = {
    "thingiverse": _plan_thingiverse,
    "makerworld": lambda spec, files: _plan_browser("makerworld", spec, files),
    "printables": lambda spec, files: _plan_browser("printables", spec, files),
}


def platform_list(raw: str) -> list[str]:
    choices = (*PLATFORMS, "all")
    if raw not in choices:
        raise InvalidArgument("--platform", raw, list(choices), command="models")
    return list(PLATFORMS) if raw == "all" else [raw]


def evaluate(
    project: Path, platforms: Sequence[str], lookup: LicenseLookup
) -> Evaluation:
    """Load the spec, select the files, apply every refusal rule and build one plan per platform."""
    spec = load_spec(project)
    files = select_files(project)
    problems: list[str] = []
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
    plans = tuple(PLANNERS[p](spec, files) for p in platforms)
    return Evaluation(project, spec, files, remix, plans, tuple(problems))


def _rel(path: Path, project: Path) -> str:
    try:
        return str(path.relative_to(project))
    except ValueError:
        return str(path)


def render_text(ev: Evaluation, *, dry_run: bool) -> str:
    spec, out = ev.spec, []
    out.append(
        f"3d models publish — {'DRY RUN, nothing is sent' if dry_run else 'PUBLISH'}"
    )
    out.append(f"project   {ev.project}")
    out.append(f"title     {spec.title}")
    out.append(f"licence   {spec.license.label} ({spec.license.id})")
    if ev.remix is not None:
        out.append(f"remix of  {ev.remix.source_url}")
        out.extend(f"          - {line}" for line in ev.remix.evidence)
        if ev.remix.permission_ref:
            out.append(f"          - permission_ref: {ev.remix.permission_ref}")
        out.extend(
            f"          - lifted by permission_ref: {line}" for line in ev.remix.waived
        )
        out.append(
            "          => " + ("REFUSED, see below" if ev.remix.problems else "allowed")
        )
    out.append(
        f"\nfiles ({len(ev.fileset.files)}) — the same set goes to every platform"
    )
    out.extend(
        f"  {f.role:<5} {_rel(f.path, ev.project)}  ({f.size} B)"
        for f in ev.fileset.files
    )
    out.append(f"images ({len(spec.images)})")
    out.extend(f"  {_rel(i, ev.project)}" for i in spec.images)
    for plan in ev.plans:
        state = "REFUSED" if plan.problems or ev.problems else "ready"
        out.append(f"\n== {plan.platform} [{state}] — {plan.route}")
        out.append("  fields")
        for key, value in plan.fields.items():
            shown = ", ".join(value) if isinstance(value, list) else str(value)
            flat = " ".join(shown.split())
            out.append(f"    {key}: {flat if len(flat) <= 100 else flat[:99] + '…'}")
        out.append("  steps")
        out.extend(f"    {n}. {s}" for n, s in enumerate(plan.steps, start=1))
    problems = ev.all_problems()
    if problems:
        out.append(f"\nREFUSED — {len(problems)} problem(s), nothing would be sent:")
        out.extend(f"  - {p}" for p in problems)
    elif dry_run:
        out.append("\nOK — rerun with --yes to publish.")
    return "\n".join(out)


def to_json(ev: Evaluation, *, dry_run: bool) -> dict[str, Any]:
    return {
        "dry_run": dry_run,
        "project": str(ev.project),
        "title": ev.spec.title,
        "license": ev.spec.license.id,
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
        "platforms": {
            p.platform: {
                "route": p.route,
                "fields": p.fields,
                "steps": list(p.steps),
                "problems": list(p.problems),
            }
            for p in ev.plans
        },
        "problems": list(ev.all_problems()),
        "refused": bool(ev.all_problems()),
    }
