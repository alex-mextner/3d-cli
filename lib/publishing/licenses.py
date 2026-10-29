"""Licence parsing for `3d models publish`: free text ("Standard Digital File License",
"CC BY-NC-SA 4.0", Thingiverse's "Creative Commons - Attribution - Share Alike", the codes
`cc-sa`/`pd0`) to a canonical `License`, plus each platform's name for the canonical ones.

The remix rules in `remix.py` read three facts off a licence: does it forbid derivatives,
does it require share-alike, is it non-commercial. A text that matches nothing parses to
None — callers treat that as "unknown", never as "allowed".
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class License:
    id: str  # canonical id, the value publish.yaml `license:` takes
    label: str
    forbids_derivatives: bool = False
    share_alike: bool = False
    non_commercial: bool = False


_CC = (
    License("cc0", "CC0 / Public Domain"),
    License("cc-by", "CC BY"),
    License("cc-by-sa", "CC BY-SA", share_alike=True),
    License("cc-by-nd", "CC BY-ND", forbids_derivatives=True),
    License("cc-by-nc", "CC BY-NC", non_commercial=True),
    License("cc-by-nc-sa", "CC BY-NC-SA", share_alike=True, non_commercial=True),
    License(
        "cc-by-nc-nd", "CC BY-NC-ND", forbids_derivatives=True, non_commercial=True
    ),
)
_OTHER = (
    License("gpl", "GNU GPL"),
    License("lgpl", "GNU LGPL"),
    License("bsd", "BSD"),
    License(
        "sdfl",
        "Standard Digital File License",
        forbids_derivatives=True,
    ),
    License("arr", "All rights reserved", forbids_derivatives=True),
)
LICENSES: dict[str, License] = {lic.id: lic for lic in (*_CC, *_OTHER)}

# platform -> canonical id -> the platform's own name for it. A canonical licence missing from a
# platform's table is not offered there and the platform is refused (never silently swapped).
# Thingiverse: the `license` codes of POST /things. MakerWorld: the labels of its licence
# filter (read 2026-10-24). Printables: UNVERIFIED against the live upload form (the site is
# behind a Cloudflare challenge), the standard Creative Commons / GNU / BSD set.
PLATFORM_LICENSES: dict[str, dict[str, str]] = {
    "thingiverse": {
        "cc0": "pd0",
        "cc-by": "cc",
        "cc-by-sa": "cc-sa",
        "cc-by-nd": "cc-nd",
        "cc-by-nc": "cc-nc",
        "cc-by-nc-sa": "cc-nc-sa",
        "cc-by-nc-nd": "cc-nc-nd",
        "gpl": "gpl",
        "lgpl": "lgpl",
        "bsd": "bsd",
    },
    "makerworld": {
        "cc0": "Public Domain",
        "cc-by": "BY",
        "cc-by-sa": "BY-SA",
        "cc-by-nd": "BY-ND",
        "cc-by-nc": "BY-NC",
        "cc-by-nc-sa": "BY-NC-SA",
        "cc-by-nc-nd": "BY-NC-ND",
        "sdfl": "Standard Digital File License",
    },
    "printables": {
        "cc0": "CC0",
        "cc-by": "CC BY",
        "cc-by-sa": "CC BY-SA",
        "cc-by-nd": "CC BY-ND",
        "cc-by-nc": "CC BY-NC",
        "cc-by-nc-sa": "CC BY-NC-SA",
        "cc-by-nc-nd": "CC BY-NC-ND",
        "gpl": "GPL",
        "lgpl": "LGPL",
        "bsd": "BSD",
    },
}

_WORD = re.compile(r"[a-z0-9]+")
_CC_VOCABULARY = frozenset(
    {
        "cc",
        "by",
        "nc",
        "sa",
        "nd",
        "creative",
        "commons",
        "attribution",
        "non",
        "commercial",
        "noncommercial",
        "share",
        "alike",
        "sharealike",
        "no",
        "derivatives",
        "derivative",
        "derivs",
        "noderivatives",
        "noderivs",
        "license",
        "licence",
        "international",
        "unported",
        "generic",
    }
)


def parse_license(text: str) -> License | None:
    """The canonical licence a free text names, or None when it names none we know."""
    tokens = _WORD.findall(text.lower())
    if not tokens:
        return None
    joined = " ".join(tokens)
    word = set(tokens)
    if "sdfl" in word or "standard digital file" in joined:
        return LICENSES["sdfl"]
    if "all rights reserved" in joined or "proprietary" in word:
        return LICENSES["arr"]
    if "arr" in word:
        return LICENSES["arr"]
    if "not" not in word and (
        word & {"cc0", "pd0"}
        or "public domain" in joined
        or "cc zero" in joined
        or "creative commons zero" in joined
    ):
        return LICENSES["cc0"]
    for plain in ("lgpl", "gpl", "bsd"):
        if plain in word and not word & {"cc", "by", "nc", "sa", "nd"}:
            return LICENSES[plain]
    return _cc_license(tokens, word, joined)


def _cc_license(tokens: list[str], word: set[str], joined: str) -> License | None:
    """The Creative Commons licence the tokens name, or None when they are not CC at all."""
    if not (word <= _CC_VOCABULARY or all(t.isdigit() for t in word - _CC_VOCABULARY)):
        return None  # any word that is not part of a CC licence name: unknown terms, never assumed free
    is_cc = (
        "cc" in word
        or ("creative commons" in joined and "attribution" in word)  # "Creative Commons" alone names no licence
        or tokens == ["by"]  # MakerWorld's bare label for CC BY
        or ("by" in word and bool(word & {"nc", "sa", "nd"}))
    )
    if not is_cc:
        return None
    nc = bool(word & {"nc", "noncommercial"}) or "non commercial" in joined
    sa = bool(word & {"sa", "sharealike"}) or "share alike" in joined
    nd = bool(word & {"nd", "noderivatives", "noderivs"}) or bool(
        re.search(r"\bno (derivatives|derivative|derivs)\b", joined)
    )
    if sa and nd:
        return None  # contradictory: not a licence that exists
    return LICENSES[
        "cc-by" + ("-nc" if nc else "") + ("-sa" if sa else "") + ("-nd" if nd else "")
    ]
