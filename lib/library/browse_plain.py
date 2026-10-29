"""library/browse_plain.py — the model browser as a plain numbered prompt (no curses, no Textual).

WHAT: the fallback of `3d models browse` when there is no full-screen terminal (stdout not a TTY, `THREED_NO_TUI=1`,
`--plain`): it prints the ranked results as a numbered list and reads commands from stdin, one per line. It drives
the same `BrowserState` and the same effects as the Textual app, so every key of the browser has a typed twin here:

    3,5-7         select those rows            p [N]   preview        b [N...]  open in browser
    d [N...]      download (asks y/N)          s       open in slicer a         launch an agent
    f             follow-ups and questions     answer KEY VALUE...     follow N
    refine QUERY  search again                 filter TEXT             list      help      q

Without numbers an action key acts on the selection, else on row 1. A pipe works as input, so a script can drive
it, but `3d models act` is the precise non-interactive interface.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Iterator
from typing import Any, TextIO

from library import actions, browse_run, browse_state, textview
from library.browse_run import ActParams, EventSource, Invoke
from library.browse_state import Act, BrowserState, Quit, Refine, Rerank, Row

PROMPT = "browse> "


def list_lines(state: BrowserState, width: int) -> list[str]:
    """The ranked list as text, numbered like `3d models act --select`."""
    rows = state.numbered()
    if state.filter:
        keep = {r.key for r in state.visible()}
        rows = [r for r in rows if r.key in keep]
    out = [
        f"search {state.session or '(unsaved)'}  {state.query!r}  {len(state.rows)} results  {state.llm}"
    ]
    for n, row in enumerate(state.numbered(), start=1):
        if row not in rows:
            continue
        mark = "*" if row.key in state.selected else " "
        out.append(
            textview.clip(
                f"{mark}{n:>3}. {browse_state.format_row(row, n, False, 200)[10:].strip()}",
                width,
            )
        )
        if row.rationale:
            out.append(
                f"       {textview.clip(row.verdict + ': ' + row.rationale if row.verdict else row.rationale, width - 7)}"
            )
    out.extend(f"  note: {textview.clip(note, width - 8)}" for note in state.notes[-3:])
    return out


def question_lines(state: BrowserState, width: int) -> list[str]:
    out: list[str] = []
    for q in state.questions.values():
        choices = f"  [{' | '.join(q['choices'])}]" if q["choices"] else ""
        answer = f"  = {q['answer']}" if q["answer"] is not None else ""
        out.append(f"question {q['key']} ({q['type']}): {q['prompt']}{choices}{answer}")
    for n, f in sorted(state.followups.items()):
        out.append(
            f"follow-up {n} [{f['status']}]: {f['query']!r}"
            + (f"  - {f['rationale']}" if f.get("rationale") else "")
        )
    return [textview.clip(line, width) for line in out] or ["nothing planned yet"]


def _numbers(state: BrowserState, words: list[str]) -> list[Row] | None:
    """`3 5-7` -> rows; None when a word is not a number or a range."""
    numbered = state.numbered()
    picked: list[Row] = []
    for word in words:
        for token in word.split(","):
            if not token:
                continue
            lo_s, _, hi_s = token.partition("-")
            if not (lo_s.isdecimal() and (not hi_s or hi_s.isdecimal())):
                return None
            lo, hi = int(lo_s), int(hi_s or lo_s)
            if not 1 <= lo <= hi <= len(numbered):
                return None
            picked += numbered[lo - 1 : hi]
    return picked


def run_plain(
    state: BrowserState,
    source: EventSource,
    params: ActParams,
    *,
    start: Callable[[], Iterator[dict[str, Any]]],
    invoke: Invoke = actions.invoke,
    stdin: TextIO,
    stdout: TextIO,
    width: int = 100,
) -> int:
    def say(text: str = "") -> None:
        stdout.write(text + "\n")
        stdout.flush()

    def drain(events: Iterator[dict[str, Any]]) -> None:
        try:
            for event in events:
                state.ingest(event)
        except Exception as exc:  # noqa: BLE001 — a failing stream is a message; the list stays usable
            say(f"error: {exc}")
        state.busy = ""

    drain(start())
    for line in list_lines(state, width):
        say(line)
    say("type ? for the commands, q to quit")

    def carry(effects: list[browse_state.Effect]) -> bool:
        for effect in effects:
            if isinstance(effect, Quit):
                return False
            if isinstance(effect, Act):
                rows = browse_state.rows_for(state, effect.keys)
                outcome = browse_run.perform(
                    effect.action,
                    rows,
                    session=state.session,
                    query=state.query,
                    params=params,
                    known=state.downloads,
                    invoke=invoke,
                )
                state.record_downloads(outcome.folders)
                say(outcome.summary())
                if (
                    effect.action == "preview"
                    and outcome.results
                    and outcome.results[0]["ok"]
                ):
                    state.set_preview(outcome.results[0]["data"])
                    if rows:
                        for text, _ in browse_state.preview_lines(
                            state, rows[0], width
                        ):
                            say("  " + text)
            elif isinstance(effect, (Rerank, Refine)):
                events = browse_run.effect_events(effect, state, source)
                if events is None:
                    say("no saved search to re-rank yet")
                else:
                    drain(events)
                    for line in list_lines(state, width):
                        say(line)
        return True

    while True:
        stdout.write(PROMPT)
        stdout.flush()
        raw = stdin.readline()
        if not raw:
            return 0
        try:
            words = shlex.split(raw)
        except ValueError as exc:
            say(f"cannot read that: {exc}")
            continue
        if not words:
            continue
        cmd, rest = words[0].lower(), words[1:]
        picked = _numbers(state, words)
        if picked is not None:
            for row in picked:
                state.toggle(row)
            say(
                "selected: "
                + (
                    ", ".join(
                        r.key for r in state.rows.values() if r.key in state.selected
                    )
                    or "none"
                )
            )
        elif cmd in ("q", "quit", "exit"):
            return 0
        elif cmd in ("l", "list"):
            for line in list_lines(state, width):
                say(line)
        elif cmd in ("?", "help", "h"):
            for key, text in browse_state.HELP:
                say(f"  {key:<26}{text}")
            say(
                "  3,5-7 select rows; p/b/d/s/a [N...] act on rows; f follow-ups; answer KEY VALUE; follow N; refine QUERY; filter TEXT"
            )
        elif cmd in ("p", "b", "d", "s", "a"):
            rows = _numbers(state, rest) if rest else None
            if rest and rows is None:
                say("give row numbers like 1 3 5-7")
                continue
            targets = (
                rows
                or [r for r in state.numbered() if r.key in state.selected]
                or state.numbered()[:1]
            )
            if not targets:
                say("nothing to act on: no results")
                continue
            action = browse_state.ACTION_KEYS[cmd]
            keys = tuple(r.key for r in targets)
            if action in browse_state.CONFIRM:
                say(
                    f"{action} {len(targets)} model(s): "
                    + "; ".join(r.title for r in targets)
                )
                say(
                    "equivalent: "
                    + browse_state.cli_line(action, targets, state.session)
                )
                stdout.write("go ahead? [y/N] ")
                stdout.flush()
                if stdin.readline().strip().lower() not in ("y", "yes"):
                    say("cancelled")
                    continue
            if not carry([Act(action, keys[:1] if action == "preview" else keys)]):
                return 0
        elif cmd == "f":
            for line in question_lines(state, width):
                say(line)
        elif cmd == "answer" and len(rest) >= 2:
            if not carry([Rerank(answers=((rest[0], " ".join(rest[1:])),))]):
                return 0
        elif cmd == "follow" and rest and rest[0].isdecimal():
            if not carry([Rerank(follow=(int(rest[0]),))]):
                return 0
        elif cmd == "refine" and rest:
            if not carry([Refine(" ".join(rest))]):
                return 0
        elif cmd == "filter":
            state.filter = " ".join(rest)
            for line in list_lines(state, width):
                say(line)
        else:
            say(f"unknown command {cmd!r}: ? lists them")
