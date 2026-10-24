"""library/textview.py — the terminal view of search results.

WHAT: `render_result()` turns one result record (`library.search.result_record`) into the lines `3d models search`
prints: the title, a statistics line (author, likes, downloads, licence, price, ...), a facts line (CAD yes/no, size
WxDxH, file summary, image marker, reference suitability), a one-line description clipped to the terminal width,
tags and the URL. `--columns` picks which of those fields appear and in which order.

ACCESSED VIA: `commands/models.py` (`search`, `searches show`).

INVARIANTS: pure (no I/O, no terminal queries: the width is passed in); every line is at most `width`
characters except the URL line (a clipped URL is useless); a missing value prints `?`, never a guess.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from errors import InvalidArgument

STAT_COLUMNS = (
    "author",
    "likes",
    "downloads",
    "makes",
    "views",
    "license",
    "price",
    "date",
    "category",
)
FACT_COLUMNS = ("cad", "size", "files", "image", "ref")
LINE_COLUMNS = ("description", "tags", "url")
ALL_COLUMNS = ("site", *STAT_COLUMNS, *FACT_COLUMNS, *LINE_COLUMNS)
DEFAULT_COLUMNS = (
    "author",
    "likes",
    "downloads",
    "license",
    "price",
    "cad",
    "size",
    "files",
    "image",
    "description",
    "url",
)
INDENT = "     "


def parse_columns(raw: str | None) -> tuple[str, ...]:
    """`--columns a,b,c` -> the validated names in the given order; None = DEFAULT_COLUMNS."""
    if raw is None:
        return DEFAULT_COLUMNS
    names = [c.strip().lower() for c in raw.split(",") if c.strip()]
    for name in names:
        if name not in ALL_COLUMNS:
            raise InvalidArgument(
                "--columns", name, list(ALL_COLUMNS), command="models"
            )
    if not names:
        raise InvalidArgument("--columns", raw, list(ALL_COLUMNS), command="models")
    return tuple(dict.fromkeys(names))


def clip(text: str, width: int) -> str:
    """`text` cut to `width` characters with a trailing ellipsis."""
    return text if len(text) <= width else text[: max(1, width - 1)].rstrip() + "…"


def flat(text: str) -> str:
    """Whitespace runs (newlines, tabs, repeated spaces from site text) collapsed to single spaces."""
    return " ".join(text.split())


def human_bytes(n: int) -> str:
    """Decimal units, as file managers and the sites print them: `812 B`, `347 KB`, `12.5 MB`, `1.2 GB`."""
    if n < 1000:
        return f"{n} B"
    if n < 1_000_000:
        return f"{n / 1000:.0f} KB"
    if n < 1_000_000_000:
        return f"{n / 1_000_000:.1f} MB"
    return f"{n / 1_000_000_000:.1f} GB"


def _count(label: str, value: object) -> str:
    return f"{label} {value if value is not None else '?'}"


def _size(rec: Mapping[str, Any]) -> str:
    box = rec.get("size_mm")
    if not box:
        return "size ?"
    return "size " + "x".join(f"{v:g}" for v in box) + " mm"


def _cad(rec: Mapping[str, Any]) -> str:
    facts = rec.get("analytics", {})
    if not facts.get("files_known", False):
        return "CAD: ?"
    return (
        f"CAD: yes ({', '.join(rec['cad_formats'])})" if rec.get("cad") else "CAD: no"
    )


def _files(rec: Mapping[str, Any]) -> str:
    facts = rec.get("analytics", {})
    if not facts.get("files_known", False):
        return "files ?"
    formats = ", ".join(f"{n} {ext}" for ext, n in facts["formats"].items())
    total = facts.get("total_bytes")
    tail = f", {human_bytes(total)}" if total else ""
    flags = [
        name
        for name, on in (
            ("multi-part", facts.get("is_multi_part")),
            ("assembly", facts.get("is_assembly")),
        )
        if on
    ]
    return f"files {facts['file_count']} ({formats}{tail})" + (
        f" [{', '.join(flags)}]" if flags else ""
    )


def _ref(rec: Mapping[str, Any]) -> str:
    ref = rec.get("analytics", {}).get("reference", {})
    if ref.get("suitable"):
        return "ref: yes" + (" (dims known)" if ref.get("dims_known") else "")
    return "ref: no" + (f" ({'; '.join(ref['reasons'])})" if ref.get("reasons") else "")


def _token(name: str, rec: Mapping[str, Any]) -> str:
    if name == "author":
        return f"by {rec.get('author') or '?'}"
    if name == "likes":
        return _count("likes", rec.get("likes"))
    if name == "downloads":
        return _count("downloads", rec.get("downloads"))
    if name == "makes":
        return _count("makes", rec.get("makes"))
    if name == "views":
        return _count("views", rec.get("views"))
    if name == "license":
        return rec.get("license") or "license ?"
    if name == "price":
        return rec.get("price") or "free"
    if name == "date":
        return str(rec.get("date") or "?")[:10]
    if name == "category":
        return rec.get("category") or "category ?"
    if name == "cad":
        return _cad(rec)
    if name == "size":
        return _size(rec)
    if name == "files":
        return _files(rec)
    if name == "image":
        return "[img]" if rec.get("image") else "[no img]"
    if name == "ref":
        return _ref(rec)
    raise KeyError(name)


def render_result(
    rec: Mapping[str, Any], n: int, columns: Sequence[str], width: int
) -> list[str]:
    """The lines of result number `n`: `  n. title`, then one line each for the chosen statistics, facts,
    description, tags and URL (a line with no chosen column is omitted)."""
    body = max(20, width - len(INDENT))
    title = f"  {n}. {flat(str(rec['title']))}" + (
        f"  [{rec['site']}]" if "site" in columns else ""
    )
    lines = [clip(title, width)]
    stats = "  ".join(_token(c, rec) for c in columns if c in STAT_COLUMNS)
    facts = "  ".join(_token(c, rec) for c in columns if c in FACT_COLUMNS)
    if stats:
        lines.append(INDENT + clip(stats, body))
    if facts:
        lines.append(INDENT + clip(facts, body))
    for name in (c for c in columns if c in LINE_COLUMNS):
        if name == "description" and rec.get("summary"):
            lines.append(INDENT + clip(flat(rec["summary"]), body))
        elif name == "tags" and rec.get("tags"):
            lines.append(INDENT + clip("tags " + ", ".join(rec["tags"]), body))
        elif name == "url":
            lines.append(INDENT + str(rec["url"]))
    return lines
