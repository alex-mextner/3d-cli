"""library/browse_state.py — the model browser as a pure state machine (#578, S7).

WHAT: everything the interactive model browser DECIDES, with no terminal, thread, network or database in it:
  - `BrowserState.ingest(event)` folds the `3d.models.rank/1` events of `library.rankflow` (docs/contracts/
    search-events.md) into rows, verdicts, questions and follow-ups; it is idempotent, so a re-ranking that
    re-emits every result updates rows in place.
  - `BrowserState.press(key)` turns one key (`"down"`, `"space"`, `"d"`, `"enter"`, ...) into a new state and a list of
    EFFECTS (`Act`, `Rerank`, `Refine`, `Quit`) that a front end carries out through `library.actions`.
  - `render_screen(state, width, height)` draws the whole screen as `(text, style)` lines. The Textual front
    end (`browse_tui.py`), the SVG snapshots and the tests all show exactly this.
  - `cli_line(action, rows, session)` is the equivalent `3d models act ...` line shown for each action.

WHY: the issue's rule "no business logic in the UI": the Textual app only maps key events to `press`, runs effects
on worker threads and paints `render_screen`; the plain prompt (`browse_plain.py`) and `3d models act` drive the
same state and the same effects. Tests drive keys through this module with a fake terminal and faked actions.

KEYS (list view): up/down (k/j), pageup/pagedown, home/end (g/G), space select + move, A select all/none,
enter/p preview, b browser, d download, s slicer, a agent, f follow-ups and questions, r refine, / filter,
o toggle order, ? help, q quit. d/s/a ask for confirmation (y/enter, n/escape) and show the CLI line.

STDLIB-ONLY at module top (checked by tests/test_imports.py).
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from library import ranking, textview

ACTION_KEYS = {
    "p": "preview",
    "b": "open",
    "d": "download",
    "s": "slicer",
    "a": "agent",
}
CONFIRM = frozenset(
    {"download", "slicer", "agent"}
)  # actions that start something: ask first
ACT_NAMES = ("preview", "open", "download", "slicer", "agent", "queue")
SITE_TAG = {
    "printables": "prin",
    "makerworld": "maker",
    "thingiverse": "thing",
    "cults": "cults",
}
VERDICT_TAG = {"fits": "fits", "maybe": "maybe", "no": "no", None: "-"}
HELP = (
    ("up / down  k / j", "move"),
    ("pgup / pgdn  home / end", "page, first / last (g / G)"),
    ("space", "select or unselect, then move down"),
    ("A", "select all visible / none"),
    ("enter  p", "preview: details, description, files, analytics"),
    ("i  (in preview)", "draw the picture (kitty, iTerm2, sixel or blocks)"),
    ("b", "open in the web browser"),
    ("d", "download (all files + model.json), asks first"),
    ("s", "open in the slicer (downloads first), asks first"),
    ("a", "launch an agent with the selected models, asks first"),
    ("f", "follow-up queries and the planner's questions"),
    ("r", "refine: search again with a new query"),
    ("/", "filter the list (text in title, site, verdict, licence, tags)"),
    ("o", "order: by verdict and score / as found"),
    ("?", "this help"),
    ("q", "quit"),
)
HINT = "? help  q quit  space select  p preview  b browser  d download  s slicer  a agent  f follow-ups  r refine  / filter"


# --------------------------------------------------------------------------- effects
@dataclass(frozen=True)
class Quit:
    pass


@dataclass(frozen=True)
class Act:
    """Run action `action` (one of ACT_NAMES) on the rows `keys` ("site:id")."""

    action: str
    keys: tuple[str, ...]


@dataclass(frozen=True)
class Rerank:
    """Judge the saved search again with `answers` (question key -> value) and/or run follow-ups `follow`."""

    answers: tuple[tuple[str, str], ...] = ()
    follow: tuple[int, ...] = ()


@dataclass(frozen=True)
class Refine:
    """Search again with `query` as a child of the current search and merge the hits into the list."""

    query: str


Effect = Quit | Act | Rerank | Refine


# --------------------------------------------------------------------------- data
@dataclass
class Row:
    key: str
    site: str
    model_id: str
    position: int
    card: dict[str, Any]
    session: str | None
    found_by: str
    kind: str
    verdict: str | None = None
    score: float | None = None
    rationale: str = ""

    @property
    def title(self) -> str:
        return textview.flat(str(self.card.get("title") or self.model_id))

    @property
    def url(self) -> str:
        return str(self.card.get("url") or "")


def row_key(site: str, model_id: str) -> str:
    return f"{site}:{model_id}"


@dataclass
class Pending:
    action: str
    keys: tuple[str, ...]


@dataclass
class BrowserState:
    query: str = ""
    session: str | None = None
    rows: dict[str, Row] = field(default_factory=dict)
    order: str = "rank"  # rank | found
    filter: str = ""
    cursor: str | None = None
    selected: set[str] = field(default_factory=set)
    mode: str = (
        "list"  # list preview help followups choose answer filter refine confirm
    )
    phase: str = "idle"  # idle searching judging planning done
    status: str = ""
    notes: list[str] = field(default_factory=list)
    questions: dict[str, dict[str, Any]] = field(default_factory=dict)
    followups: dict[int, dict[str, Any]] = field(default_factory=dict)
    queries: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    llm: str = ""
    buffer: str = ""
    filter_before: str = ""
    panel_cursor: int = 0
    choice_cursor: int = 0
    choice_marks: set[str] = field(default_factory=set)
    pending: Pending | None = None
    preview: dict[str, Any] | None = None
    downloads: dict[str, str] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)
    busy: str = ""  # what a worker is doing right now ("download", "re-ranking", ...): shown in the status line

    # ------------------------------------------------------------------ the list
    def _ordered(self) -> list[Row]:
        rows = list(self.rows.values())
        if self.order == "rank":
            rows.sort(key=lambda r: ranking.sort_key(r.verdict, r.score, r.position))
        else:
            rows.sort(key=lambda r: r.position)
        return rows

    def visible(self) -> list[Row]:
        rows = self._ordered()
        needle = self.filter.strip().lower()
        return [r for r in rows if needle in _haystack(r)] if needle else rows

    def current(self) -> Row | None:
        rows = self.visible()
        if not rows:
            return None
        for r in rows:
            if r.key == self.cursor:
                return r
        return rows[0]

    def index(self) -> int:
        rows = self.visible()
        cur = self.current()
        return rows.index(cur) if cur is not None and rows else 0

    def targets(self) -> list[Row]:
        """The rows an action key applies to: the selection (in list order), else the row under the cursor."""
        if self.selected:
            chosen = [r for r in self._ordered() if r.key in self.selected]
            if chosen:
                return chosen
        cur = self.current()
        return [cur] if cur is not None else []

    def numbered(self) -> list[Row]:
        """Rows in the order `3d models act` numbers them: the final ranking (fits, maybe, unjudged, no)."""
        return sorted(
            self.rows.values(),
            key=lambda r: ranking.sort_key(r.verdict, r.score, r.position),
        )

    # ------------------------------------------------------------------ events
    def ingest(self, event: Mapping[str, Any]) -> None:
        kind = event.get("event")
        if kind == "start":
            llm = event.get("llm") or {}
            self.llm = (
                f"LLM {llm.get('backend')}"
                if llm.get("enabled")
                else "no LLM (deterministic)"
            )
            self.query = self.query or str(event.get("query") or "")
            self.phase = "searching" if event.get("kind") == "search" else "judging"
            self.busy = "searching" if self.phase == "searching" else "loading"
        elif kind == "search":
            self.session = self.session or event.get("search")
            for err in event.get("errors") or []:
                self._note(f"{err.get('site')}: {err.get('message')}")
        elif kind == "result":
            self._result(event)
        elif kind == "phase":
            self.phase = "judging"
            self.busy = f"judging {event.get('results', 0)} results"
        elif kind == "verdict":
            row = self.rows.get(row_key(str(event["site"]), str(event["id"])))
            if row is not None:
                row.verdict = event.get("verdict")
                row.score = event.get("score")
                row.rationale = textview.flat(str(event.get("rationale") or ""))
        elif kind == "plan":
            if event.get("status") == "running":
                self.phase = "planning"
                self.busy = "planning queries"
            elif event.get("status") == "failed":
                self._note(f"planner failed: {event.get('reason')}")
            else:
                for problem in event.get("problems") or []:
                    self._note(f"plan: {problem}")
        elif kind == "query":
            self._query(event)
        elif kind == "question":
            self.questions[str(event["key"])] = {
                "id": event.get("id"),
                "key": event["key"],
                "type": event.get("type", "text"),
                "prompt": event.get("prompt", ""),
                "choices": list(event.get("choices") or []),
                "answer": event.get("answer"),
            }
        elif kind == "warning":
            self._note(str(event.get("message") or event.get("code")))
        elif kind == "done":
            self.phase = "done"
            self.busy = ""
            self.session = self.session or event.get("search")
            self.counts = dict(event.get("counts") or {})
            for q in event.get("questions") or []:
                cur = self.questions.get(str(q["key"]))
                if cur is not None:
                    cur["answer"] = q.get("answer")
            c = self.counts
            self.status = (
                f"done: {c.get('results', 0)} results, {c.get('fits', 0)} fit, "
                f"{c.get('maybe', 0)} maybe, {c.get('no', 0)} no, {c.get('unjudged', 0)} unjudged"
            )

    def _note(self, text: str) -> None:
        if text and text not in self.notes:
            self.notes.append(text)

    def _result(self, event: Mapping[str, Any]) -> None:
        key = row_key(str(event["site"]), str(event["id"]))
        card = dict(event.get("card") or {})
        existing = self.rows.get(key)
        if existing is not None:
            existing.card = card or existing.card
            return
        self.rows[key] = Row(
            key=key,
            site=str(event["site"]),
            model_id=str(event["id"]),
            position=int(event.get("position") or len(self.rows) + 1),
            card=card,
            session=event.get("search"),
            found_by=str(event.get("query") or ""),
            kind=str(event.get("kind") or "user"),
        )
        if card.get("verdict"):  # a stored result already carries its verdict
            row = self.rows[key]
            row.verdict = card.get("verdict")
            row.score = card.get("score")
            row.rationale = textview.flat(str(card.get("rationale") or ""))
        # the cursor stays unpinned until the user moves it, so it follows the top of the list as verdicts re-sort it

    def _query(self, event: Mapping[str, Any]) -> None:
        if event.get("status") == "suggested":
            n = int(event["n"])
            self.followups[n] = {
                "n": n,
                "query": event.get("query", ""),
                "rationale": event.get("rationale", ""),
                "status": "suggested",
                "search": event.get("search"),
            }
            return
        entry = {
            "query": event.get("query", ""),
            "kind": event.get("kind", ""),
            "search": event.get("search"),
            "new": event.get("new"),
            "rationale": event.get("rationale", ""),
        }
        if not any(
            q["search"] == entry["search"] and q["query"] == entry["query"]
            for q in self.queries
        ):
            self.queries.append(entry)
        if event.get("kind") == "followup":
            for fu in self.followups.values():
                if fu["query"] == entry["query"]:
                    fu["status"] = "run"
                    fu["search"] = entry["search"]

    # ------------------------------------------------------------------ results of effects
    def record_downloads(self, folders: Mapping[str, str]) -> None:
        self.downloads.update(folders)

    def set_preview(self, data: dict[str, Any] | None) -> None:
        self.preview = data

    def say(self, text: str) -> None:
        self.status = text
        self.log.append(text)
        del self.log[:-50]

    # ------------------------------------------------------------------ keys
    def press(self, key: str) -> list[Effect]:
        handler = {
            "list": self._list_key,
            "preview": self._preview_key,
            "help": self._help_key,
            "followups": self._followups_key,
            "choose": self._choose_key,
            "answer": self._text_key,
            "filter": self._text_key,
            "refine": self._text_key,
            "confirm": self._confirm_key,
        }[self.mode]
        return handler(key)

    def move(self, delta: int) -> None:
        rows = self.visible()
        if not rows:
            return
        self.cursor = rows[max(0, min(len(rows) - 1, self.index() + delta))].key

    def toggle(self, row: Row) -> None:
        if row.key in self.selected:
            self.selected.discard(row.key)
        else:
            self.selected.add(row.key)

    def _list_key(self, key: str) -> list[Effect]:
        rows = self.visible()
        if key in ("up", "k"):
            self.move(-1)
        elif key in ("down", "j"):
            self.move(1)
        elif key == "pageup":
            self.move(-10)
        elif key == "pagedown":
            self.move(10)
        elif key in ("home", "g"):
            self.move(-len(rows))
        elif key in ("end", "G"):
            self.move(len(rows))
        elif key == "space":
            cur = self.current()
            if cur is not None:
                self.toggle(cur)
                self.move(1)
        elif key == "A":
            keys = {r.key for r in rows}
            if keys and keys <= self.selected:
                self.selected -= keys
            else:
                self.selected |= keys
        elif key in ("p", "enter"):
            return self.act("preview")
        elif key in ACTION_KEYS:
            return self.act(ACTION_KEYS[key])
        elif key == "f":
            self.mode, self.panel_cursor = "followups", 0
        elif key == "r":
            self.mode, self.buffer = "refine", ""
        elif key == "/":
            self.mode, self.buffer, self.filter_before = (
                "filter",
                self.filter,
                self.filter,
            )
        elif key == "o":
            self.order = "found" if self.order == "rank" else "rank"
            self.say(
                "order: "
                + ("as found" if self.order == "found" else "verdict, then score")
            )
        elif key == "?":
            self.mode = "help"
        elif key == "escape" and self.filter:
            self.filter = ""
        elif key in ("q", "ctrl+c"):
            return [Quit()]
        return []

    def act(self, action: str) -> list[Effect]:
        targets = self.targets()
        if not targets:
            self.say("nothing to act on: no results yet")
            return []
        keys = tuple(r.key for r in targets)
        if action in CONFIRM:
            self.pending = Pending(action, keys)
            self.mode = "confirm"
            return []
        if action == "preview":
            self.mode = "preview"
            self.preview = None
            cur = self.current()
            return [Act("preview", (cur.key if cur else keys[0],))]
        return [Act(action, keys)]

    def _preview_key(self, key: str) -> list[Effect]:
        if key in ("escape", "q", "p", "enter", "backspace"):
            self.mode = "list"
            return []
        if key in ("up", "k", "down", "j"):
            self.move(-1 if key in ("up", "k") else 1)
            cur = self.current()
            self.preview = None
            return [Act("preview", (cur.key,))] if cur else []
        if key == "i":
            cur = self.current()
            return [Act("image", (cur.key,))] if cur else []
        if key in ACTION_KEYS and key != "p":
            self.mode = "list"
            return self._list_key(key)
        return []

    def _help_key(self, key: str) -> list[Effect]:
        self.mode = "list"
        return []

    def _confirm_key(self, key: str) -> list[Effect]:
        pending, self.pending, self.mode = self.pending, None, "list"
        if pending is not None and key in ("y", "enter"):
            return [Act(pending.action, pending.keys)]
        self.say("cancelled")
        return []

    def panel_items(self) -> list[tuple[str, Mapping[str, Any]]]:
        return [("question", q) for q in self.questions.values()] + [
            ("followup", f) for _, f in sorted(self.followups.items())
        ]

    def _followups_key(self, key: str) -> list[Effect]:
        items = self.panel_items()
        if key in ("escape", "q", "f"):
            self.mode = "list"
        elif key in ("up", "k"):
            self.panel_cursor = max(0, self.panel_cursor - 1)
        elif key in ("down", "j"):
            self.panel_cursor = min(max(0, len(items) - 1), self.panel_cursor + 1)
        elif key == "enter" and items:
            kind, item = items[min(self.panel_cursor, len(items) - 1)]
            if kind == "followup":
                self.mode = "list"
                self.say(f"follow-up {item['n']}: {item['query']!r}")
                return [Rerank(follow=(int(item["n"]),))]
            if item["type"] == "text":
                self.mode, self.buffer = "answer", ""
            else:
                self.mode, self.choice_cursor, self.choice_marks = "choose", 0, set()
        return []

    def _open_question(self) -> Mapping[str, Any] | None:
        items = self.panel_items()
        if not items:
            return None
        kind, item = items[min(self.panel_cursor, len(items) - 1)]
        return item if kind == "question" else None

    def _choose_key(self, key: str) -> list[Effect]:
        q = self._open_question()
        if q is None or key == "escape":
            self.mode = "followups"
            return []
        choices = list(q["choices"])
        if key in ("up", "k"):
            self.choice_cursor = max(0, self.choice_cursor - 1)
        elif key in ("down", "j"):
            self.choice_cursor = min(len(choices) - 1, self.choice_cursor + 1)
        elif key == "space" and q["type"] == "multi" and choices:
            self.choice_marks ^= {choices[self.choice_cursor]}
        elif key == "enter" and choices:
            if q["type"] == "multi":
                picked = [c for c in choices if c in self.choice_marks] or [
                    choices[self.choice_cursor]
                ]
                value = ",".join(picked)
            else:
                value = choices[self.choice_cursor]
            return self._answer(str(q["key"]), value)
        return []

    def _answer(self, qkey: str, value: str) -> list[Effect]:
        self.mode = "list"
        self.say(f"answer {qkey}={value}: judging again")
        return [Rerank(answers=((qkey, value),))]

    def _text_key(self, key: str) -> list[Effect]:
        mode = self.mode
        if key == "escape":
            if mode == "filter":
                self.filter = self.filter_before
            self.mode = "followups" if mode == "answer" else "list"
            return []
        if key == "enter":
            text = " ".join(self.buffer.split())
            if mode == "filter":
                self.filter, self.mode = text, "list"
            elif not text:
                self.mode = "followups" if mode == "answer" else "list"
            elif mode == "refine":
                self.mode = "list"
                self.say(f"refine: searching {text!r}")
                return [Refine(text)]
            else:
                q = self._open_question()
                if q is not None:
                    return self._answer(str(q["key"]), text)
                self.mode = "list"
            return []
        if key == "backspace":
            self.buffer = self.buffer[:-1]
        elif key == "space":
            self.buffer += " "
        elif len(key) == 1 and key.isprintable():
            self.buffer += key
        if mode == "filter":
            self.filter = self.buffer
        return []


def _haystack(row: Row) -> str:
    card = row.card
    return " ".join(
        [
            row.title,
            row.site,
            row.verdict or "",
            str(card.get("license") or ""),
            str(card.get("price") or ""),
            " ".join(card.get("tags") or []),
            row.rationale,
        ]
    ).lower()


# --------------------------------------------------------------------------- the equivalent command line
def cli_line(
    action: str, rows: Sequence[Row], session: str | None, *, extra: Sequence[str] = ()
) -> str:
    """`3d models act SESSION --do ACTION [--yes] --select KEY,KEY`: what the key does, as a script would. The
    selection comes last so a clipped line still shows the action."""
    parts = ["3d", "models", "act", session or "SEARCH_ID", "--do", action]
    if action in CONFIRM or action in ("queue", "open"):
        parts.append("--yes")
    parts += list(extra)
    if rows:
        parts += ["--select", ",".join(r.key for r in rows)]
    return " ".join(shlex.quote(p) for p in parts)


# --------------------------------------------------------------------------- the screen
Line = tuple[str, str]


def _fit(text: str, width: int) -> str:
    return textview.clip(text, width).ljust(width)[:width]


def _size(card: Mapping[str, Any]) -> str:
    box = card.get("size_mm")
    return "x".join(f"{v:g}" for v in box) if box else "size ?"


def _cad(card: Mapping[str, Any]) -> str:
    if not (card.get("analytics") or {}).get("files_known", False):
        return "CAD?"
    return "CAD" if card.get("cad") else "-"


def _terms(card: Mapping[str, Any]) -> str:
    price = card.get("price")
    lic = card.get("license") or "no licence"
    return f"{lic} {price}".strip() if price else str(lic)


def row_text(row: Row) -> str:
    """One result as a single unpadded line (the plain prompt and `act --do list`)."""
    score = f" {row.score:.2f}" if row.score is not None else ""
    return (
        f"{VERDICT_TAG[row.verdict]}{score}  {row.title}  [{row.site}]  {_size(row.card)} mm  "
        f"{_cad(row.card)}  {_terms(row.card)}"
    )


def format_row(row: Row, n: int, selected: bool, width: int) -> str:
    mark = "[x]" if selected else "[ ]"
    score = f"{row.score:.2f}" if row.score is not None else "    "
    head = f"{mark} {n:>3} {VERDICT_TAG[row.verdict]:<5} {score} {SITE_TAG.get(row.site, row.site)[:6]:<6} "
    tail = f" {_size(row.card):>11} {_cad(row.card):<4} {_terms(row.card)}"
    tail_w = 24 if width < 100 else 34
    tail = _fit(tail, tail_w)
    title_w = max(8, width - len(head) - len(tail))
    return _fit(head + _fit(row.title, title_w) + tail, width)


def wrap(text: str, width: int, indent: str = "") -> list[str]:
    import textwrap

    return textwrap.wrap(
        text, max(10, width), initial_indent=indent, subsequent_indent=indent
    ) or [""]


def detail_lines(state: BrowserState, row: Row, width: int) -> list[Line]:
    card = row.card
    out: list[Line] = [(textview.clip(row.title, width), "bold")]
    verdict = (
        f"{row.verdict} {row.score:.2f}: {row.rationale}"
        if row.verdict and row.score is not None
        else "not judged yet"
        if state.phase in ("searching", "judging")
        else "unjudged"
    )
    out += [(line, row.verdict or "dim") for line in wrap(verdict, width)[:3]]
    facts = (
        f"{row.site} #{row.model_id}  by {card.get('author') or '?'}  {_terms(card)}  {_size(card)} mm  "
        f"CAD {'yes (' + ', '.join(card.get('cad_formats') or []) + ')' if card.get('cad') else 'no'}"
    )
    out.append((textview.clip(facts, width), ""))
    if card.get("summary"):
        out += [
            (line, "dim")
            for line in wrap(textview.flat(str(card["summary"])), width)[:2]
        ]
    out.append((textview.clip(row.url, width), "dim"))
    return out


def preview_lines(state: BrowserState, row: Row, width: int) -> list[Line]:
    """The preview panel: details, description, tags, files and analytics (from the preview action, or the card)."""
    data = state.preview or {}
    card = row.card
    out: list[Line] = [(row.title, "bold")]
    if row.verdict:
        score = f" {row.score:.2f}" if row.score is not None else ""
        out += [
            (line, row.verdict)
            for line in wrap(f"{row.verdict}{score}: {row.rationale}", width)
        ]
    out.append(
        (
            f"{row.site} #{row.model_id}  by {card.get('author') or '?'}  licence {card.get('license') or '?'}  "
            f"price {card.get('price') or 'free'}",
            "",
        )
    )
    out.append(
        (
            f"{_size(card)} mm  CAD {'yes' if card.get('cad') else 'no'}  query: {row.found_by}",
            "",
        )
    )
    out.append((row.url, "dim"))
    if card.get("summary"):
        out += [("", ""), ("description", "bold")]
        out += [(line, "") for line in wrap(textview.flat(str(card["summary"])), width)]
    if card.get("tags"):
        out.append(("tags: " + ", ".join(card["tags"]), "dim"))
    files = card.get("files") or []
    out += [("", ""), (f"files ({len(files)})", "bold")]
    for f in files[:12]:
        size = (
            textview.human_bytes(int(f["size"])) if f.get("size") is not None else "?"
        )
        out.append((f"  {f.get('name')}  {size}  {f.get('class') or ''}", ""))
    if len(files) > 12:
        out.append((f"  ... {len(files) - 12} more", "dim"))
    facts = card.get("analytics") or {}
    if facts:
        out += [("", ""), ("analytics", "bold")]
        if facts.get("files_known", False):
            formats = ", ".join(
                f"{n} {ext}" for ext, n in (facts.get("formats") or {}).items()
            )
            out.append((f"  {facts.get('file_count')} files: {formats}", ""))
            flags = [
                f
                for f, on in (
                    ("multi-part", facts.get("is_multi_part")),
                    ("assembly", facts.get("is_assembly")),
                )
                if on
            ]
            if flags:
                out.append(("  " + ", ".join(flags), ""))
        ref = facts.get("reference") or {}
        out.append(
            (f"  as a reference model: {'yes' if ref.get('suitable') else 'no'}", "")
        )
        deep = facts.get("deep") or {}
        if deep.get("status") == "ok":
            out.append(
                (
                    f"  measured: {_size({'size_mm': deep.get('bbox_mm')})} mm, watertight {deep.get('watertight')}",
                    "",
                )
            )
    image = (data.get("image") or {}) if data else {}
    if not card.get("image"):
        out += [("", ""), ("picture: none listed", "dim")]
    elif image.get("sequence"):
        out += [
            ("", ""),
            (f"picture: press i to draw it ({image.get('protocol')})", ""),
        ]
    elif data:
        out += [
            ("", ""),
            (
                "picture: this terminal cannot show images (try THREED_IMAGE_PROTOCOL=kitty|iterm2|sixel)",
                "dim",
            ),
        ]
    else:
        out += [("", ""), ("picture: loading...", "dim")]
    out += [
        ("", ""),
        (
            "esc back   b browser   d download   s slicer   a agent   up/down other result",
            "dim",
        ),
        ("$ " + cli_line("preview", [row], state.session), "dim"),
    ]
    return out


def followups_lines(state: BrowserState, width: int) -> list[Line]:
    out: list[Line] = [
        ("Follow-ups and questions", "bold"),
        ("enter: answer a question / run a follow-up   esc: back", "dim"),
        ("", ""),
    ]
    items = state.panel_items()
    if not items:
        out.append(
            (
                "nothing planned yet: the planner proposes queries and questions once the model has judged",
                "dim",
            )
        )
    for i, (kind, item) in enumerate(items):
        style = "cursor" if i == state.panel_cursor else ""
        if kind == "question":
            answer = item.get("answer")
            shown = (
                f"  = {', '.join(answer) if isinstance(answer, list) else answer}"
                if answer is not None
                else ""
            )
            choices = f"  [{' | '.join(item['choices'])}]" if item["choices"] else ""
            out.append(
                (
                    _fit(
                        f"? {item['key']} ({item['type']}): {item['prompt']}{choices}{shown}",
                        width,
                    ),
                    style,
                )
            )
        else:
            why = f"  - {item['rationale']}" if item.get("rationale") else ""
            out.append(
                (
                    _fit(
                        f"> follow-up {item['n']} [{item['status']}]: {item['query']!r}{why}",
                        width,
                    ),
                    style,
                )
            )
    if state.queries:
        out += [("", ""), ("queries run", "bold")]
        for q in state.queries:
            out.append(
                (
                    textview.clip(
                        f"  [{q['kind']}] {q['query']!r}  +{q['new'] if q['new'] is not None else '?'} new",
                        width,
                    ),
                    "dim",
                )
            )
    return out


def choose_lines(state: BrowserState, width: int) -> list[Line]:
    q = state._open_question()
    if q is None:
        return []
    multi = q["type"] == "multi"
    out: list[Line] = [
        (textview.clip(str(q["prompt"]), width), "bold"),
        (("space toggles, " if multi else "") + "enter answers, esc cancels", "dim"),
        ("", ""),
    ]
    for i, choice in enumerate(q["choices"]):
        mark = ("[x] " if choice in state.choice_marks else "[ ] ") if multi else ""
        out.append((f"  {mark}{choice}", "cursor" if i == state.choice_cursor else ""))
    return out


def help_lines() -> list[Line]:
    out: list[Line] = [("Keys", "bold"), ("", "")]
    out += [(f"  {k:<26}{v}", "") for k, v in HELP]
    out += [
        ("", ""),
        (
            "Every key has a twin: 3d models act SEARCH_ID --select ... --do ... (docs/commands/tui.md)",
            "dim",
        ),
        ("any key closes", "dim"),
    ]
    return out


def column_header(width: int) -> str:
    """The caption line over the list, aligned with `format_row`."""
    tail_w = 24 if width < 100 else 34
    head = f"{'':3} {'#':>3} {'ver.':<5} {'scor':<4} {'site':<6} "
    title_w = max(8, width - len(head) - tail_w)
    tail = _fit(f" {'size mm':>11} {'cad':<4} licence / price", tail_w)
    return _fit(head + _fit("title", title_w) + tail, width)


def header_lines(state: BrowserState, width: int) -> list[Line]:
    c = {"fits": 0, "maybe": 0, "no": 0}
    for r in state.rows.values():
        if r.verdict in c:
            c[r.verdict] += 1
    phase = state.busy or state.phase
    right = f"{phase}  {state.llm} "
    left = _fit(
        f" 3d models  {state.query!r}  {len(state.rows)} results  {c['fits']} fit {c['maybe']} maybe {c['no']} no",
        max(10, width - len(right) - 1),
    )
    return [
        (_fit(left + " " + right.rjust(width - len(left) - 1), width), "title"),
        (column_header(width), "dim"),
    ]


def footer_lines(state: BrowserState, width: int) -> list[Line]:
    if state.mode in ("filter", "refine", "answer"):
        label = {"filter": "/ ", "refine": "refine query: ", "answer": "answer: "}[
            state.mode
        ]
        status = f" {label}{state.buffer}█"
        hint = "enter accepts, esc cancels"
    elif state.mode == "confirm" and state.pending is not None:
        n = len(state.pending.keys)
        status = f" {state.pending.action} {n} model{'s' if n != 1 else ''}?  y / enter = yes, n / esc = no"
        hint = ""
    else:
        notes = f"  ! {state.notes[-1]}" if state.notes else ""
        sel = f"{len(state.selected)} selected  " if state.selected else ""
        status = f" {sel}{state.busy + '...  ' if state.busy and not state.status else ''}{state.status}{notes}"
        hint = HINT
    return [(_fit(status, width), "status"), (_fit(" " + hint, width), "dim")]


def render_screen(state: BrowserState, width: int, height: int) -> list[Line]:
    """The screen as exactly `height` lines of `(text, style)`; styles: title status cursor dim bold fits maybe no sel."""
    head = header_lines(state, width)
    foot = footer_lines(state, width)
    body_h = max(1, height - len(head) - len(foot))
    body: list[Line]
    if state.mode == "help":
        body = help_lines()
    elif state.mode == "preview" and state.current() is not None:
        cur = state.current()
        assert cur is not None
        body = preview_lines(state, cur, width)
    elif state.mode in ("followups", "answer"):
        body = followups_lines(state, width)
    elif state.mode == "choose":
        body = choose_lines(state, width)
    else:
        detail_h = min(8, max(4, body_h // 3))
        list_h = max(1, body_h - detail_h - 1)
        body = _list_lines(state, width, list_h)
        body.append(("─" * width, "dim"))
        cur = state.current()
        if state.mode == "confirm" and state.pending is not None:
            rows = [state.rows[k] for k in state.pending.keys if k in state.rows]
            body += [
                (_fit(line, width), "bold")
                for line in _confirm_lines(state, rows, width)
            ]
        elif cur is not None:
            body += [
                (_fit(text, width), style)
                for text, style in detail_lines(state, cur, width)
            ]
        elif state.rows == {} and state.phase in ("idle", "searching"):
            body.append((" waiting for the first results...", "dim"))
        else:
            body.append(
                (" no result matches the filter", "dim")
                if state.filter
                else (" no results", "dim")
            )
            body += [(f" ! {note}", "maybe") for note in state.notes]
    body = [
        (_fit(t, width) if s != "" or len(t) > width else t.ljust(width), s)
        for t, s in body
    ]
    body = body[:body_h] + [("", "")] * max(0, body_h - len(body))
    return head + body + foot


def _confirm_lines(state: BrowserState, rows: Sequence[Row], width: int) -> list[str]:
    assert state.pending is not None
    action = state.pending.action
    out = [f" {action}: " + "; ".join(r.title for r in rows)[:width]]
    out += wrap("equivalent: " + cli_line(action, rows, state.session), width - 2, " ")
    return out


def _list_lines(state: BrowserState, width: int, height: int) -> list[Line]:
    rows = state.visible()
    if not rows:
        return []
    idx = state.index()
    top = max(0, min(idx - height // 2, len(rows) - height))
    out: list[Line] = []
    numbers = {r.key: i for i, r in enumerate(state.numbered(), start=1)}
    cursor = state.current()
    for r in rows[top : top + height]:
        style = (
            "cursor"
            if cursor is not None and r.key == cursor.key
            else ("sel" if r.key in state.selected else (r.verdict or ""))
        )
        out.append(
            (format_row(r, numbers[r.key], r.key in state.selected, width), style)
        )
    return out


def rows_for(state: BrowserState, keys: Iterable[str]) -> list[Row]:
    return [state.rows[k] for k in keys if k in state.rows]
