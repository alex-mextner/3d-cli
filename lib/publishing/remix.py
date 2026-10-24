"""May this project be published as a remix of `remix_of`? The rules of `3d models publish`:

* the source's licence comes from the marketplace itself (`lookup`) and from `remix_license`
  in publish.yaml; when both exist the stricter one counts, so a declaration can never loosen
  what the site says;
* a source that forbids derivatives (CC BY-ND/-NC-ND, the Standard Digital File License, all
  rights reserved), or whose licence cannot be determined, refuses the publication unless
  `permission_ref` names the written permission of its author (a URL, a message id, a file);
* share-alike and non-commercial conditions of a Creative Commons source are conditions of the
  grant, not of the author's goodwill: the published licence must carry them, permission or not;
* a GPL/LGPL source needs a GPL (LGPL: or GPL) result.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from errors import ThreeDError
from publishing.licenses import License, parse_license
from publishing.spec import PublishSpec

LicenseLookup = Callable[
    [str], str
]  # source URL -> the licence text the marketplace shows


@dataclass(frozen=True)
class RemixCheck:
    source_url: str
    evidence: tuple[str, ...]  # where each licence text came from
    source_licenses: tuple[License, ...]
    permission_ref: str
    waived: tuple[str, ...]  # barriers the permission_ref lifted
    problems: tuple[str, ...]


def marketplace_license(url: str) -> str:
    """The licence the marketplace lists for the model at `url` (network)."""
    from marketplaces import catalog

    return catalog.show(url).license


def _unknown_problem(spec: PublishSpec, why: str) -> str:
    return (
        f"cannot tell which licence {spec.remix_of} is under ({why}); a remix is only published "
        "with a verified licence or the original author's written permission (permission_ref)"
    )


def check_remix(spec: PublishSpec, lookup: LicenseLookup) -> RemixCheck | None:
    """None when the project is not a remix, else the verdict with every problem found."""
    if not spec.remix_of:
        return None
    evidence: list[str] = []
    texts: list[str] = []
    try:
        looked_up = lookup(spec.remix_of).strip()
    except ThreeDError as exc:
        evidence.append(f"lookup failed: {exc.message}")
    else:
        if looked_up:
            texts.append(looked_up)
            evidence.append(f"marketplace lists '{looked_up}'")
        else:
            evidence.append("the marketplace lists no licence")
    if spec.remix_license:
        texts.append(spec.remix_license)
        evidence.append(f"publish.yaml declares '{spec.remix_license}'")

    parsed = [(text, parse_license(text)) for text in texts]
    known = tuple(lic for _, lic in parsed if lic is not None)
    problems: list[str] = []

    barrier: list[str] = []  # what the permission_ref can waive
    if not texts:
        barrier.append(_unknown_problem(spec, "; ".join(evidence)))
    barrier.extend(
        _unknown_problem(spec, f"'{text}' is not a licence this tool knows")
        for text, lic in parsed
        if lic is None
    )
    barrier.extend(
        f"{spec.remix_of} is under {lic.label}, which forbids derivative works (remixes)"
        for lic in dict.fromkeys(known)
        if lic.forbids_derivatives
    )
    waived = tuple(barrier) if spec.permission_ref else ()
    if barrier and not spec.permission_ref:
        problems.extend(
            f"{b}; set permission_ref in publish.yaml if its author allowed this"
            for b in barrier
        )

    out = spec.license
    for src in dict.fromkeys(known):
        if src.id in ("cc0", "arr", "sdfl"):
            continue  # nothing to inherit; the barrier above already judged these
        if src.non_commercial and not out.non_commercial:
            problems.append(
                f"the source is {src.label} (non-commercial): publish under a non-commercial licence, "
                f"not {out.label}"
            )
        if src.share_alike and not out.share_alike:
            problems.append(
                f"the source is {src.label} (share-alike): publish under a share-alike licence, not {out.label}"
            )
        if src.id == "gpl" and out.id != "gpl":
            problems.append(
                f"the source is GNU GPL: publish under GPL, not {out.label}"
            )
        if src.id == "lgpl" and out.id not in ("lgpl", "gpl"):
            problems.append(
                f"the source is GNU LGPL: publish under LGPL or GPL, not {out.label}"
            )
    return RemixCheck(
        spec.remix_of, tuple(evidence), tuple(dict.fromkeys(known)), spec.permission_ref, waived, tuple(problems)
    )
