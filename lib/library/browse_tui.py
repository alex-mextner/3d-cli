"""library/browse_tui.py — the Textual front end of the model browser (`3d models browse`, `search --tui`; #578).

WHAT: a thin shell around `browse_state.BrowserState`. It maps key events to `state.press(key)`, carries out the
returned effects (actions on worker threads through `browse_run.perform`, ranking streams through `browse_run.Stream`),
posts the streamed `3d.models.rank/1` events back onto the UI thread, and paints `browse_state.render_screen`. There
is no business logic here: a key that does something calls `library.actions` exactly as `3d models act` does.

TERMINAL HAND-OFFS: the agent key attaches the agent to this terminal and the picture key prints a kitty / iTerm2 /
sixel / half-block image; both need the real terminal, so the app suspends itself (`App.suspend`), runs them, and
repaints. A slicer or browser is a GUI program and is started detached.

OPTIONAL DEPENDENCY: `textual` (extra `tui`: `uv sync --extra tui`). `browsecli.require_textual()` raises a
structured MissingDependency (exit 127) with the install hint BEFORE this module is imported; it imports textual at
the top. Tests drive the same app headlessly (`App.run_test`).
"""

from __future__ import annotations

import contextlib
import sys
import threading
from collections.abc import Callable, Iterator
from typing import Any

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.widgets import Static

from library import actions, browse_run, browse_state
from library.browse_run import ActOutcome, ActParams, EventSource, Invoke
from library.browse_state import Act, BrowserState, Quit, Refine, Rerank

STYLES = {
    "title": "bold white on blue",
    "status": "bold",
    "cursor": "reverse",
    "dim": "dim",
    "bold": "bold",
    "fits": "green",
    "maybe": "yellow",
    "no": "red",
    "sel": "bold cyan",
    "": "",
}
NAMED_KEYS = frozenset(
    {
        "up",
        "down",
        "pageup",
        "pagedown",
        "home",
        "end",
        "enter",
        "escape",
        "backspace",
        "ctrl+c",
        "space",
    }
)


def key_name(event: events.Key) -> str:
    """The name `BrowserState.press` understands: named keys as Textual spells them, else the typed character."""
    if event.key in NAMED_KEYS:
        return event.key
    if event.character and event.is_printable:
        return event.character
    return event.key


class BrowserApp(App[int]):
    """The model browser. `start` yields the first event stream (a new search or a saved one)."""

    CSS = "Screen { layout: vertical; } #screen { width: 100%; height: 100%; }"

    def __init__(
        self,
        state: BrowserState,
        source: EventSource,
        params: ActParams,
        *,
        start: Callable[[], Iterator[dict[str, Any]]] | None = None,
        invoke: Invoke = actions.invoke,
        suspended: Callable[[], contextlib.AbstractContextManager[object]]
        | None = None,
    ) -> None:
        super().__init__()
        self.state = state
        self.source = source
        self.params = params
        self._start = start
        self._invoke = invoke
        self._suspended = suspended or self.suspend
        self._threads: list[threading.Thread] = []
        self._streams: list[browse_run.Stream] = []
        self._lock = threading.Lock()
        self.outcomes: list[
            ActOutcome
        ] = []  # every finished action, in order (tests and `?` read them)

    # ------------------------------------------------------------------ app
    def compose(self) -> ComposeResult:
        yield Static("", id="screen", markup=False)

    def on_mount(self) -> None:
        self.repaint()
        if self._start is not None:
            self._stream(self._start())

    def on_resize(self, event: events.Resize) -> None:
        self.repaint()

    def on_unmount(self) -> None:
        for stream in self._streams:
            stream.close()

    def repaint(self) -> None:
        size = self.size
        width, height = max(20, size.width), max(6, size.height)
        text = Text(no_wrap=True, overflow="crop")
        lines = browse_state.render_screen(self.state, width, height)
        for i, (line, style) in enumerate(lines):
            text.append(line[:width], style=STYLES.get(style, ""))
            if i < len(lines) - 1:
                text.append("\n")
        with contextlib.suppress(
            Exception
        ):  # the widget is gone while the app shuts down
            self.query_one("#screen", Static).update(text)

    # ------------------------------------------------------------------ keys
    def on_key(self, event: events.Key) -> None:
        event.stop()
        self.press(key_name(event))

    def press(self, key: str) -> None:
        for effect in self.state.press(key):
            self.carry_out(effect)
        self.repaint()

    def carry_out(self, effect: browse_state.Effect) -> None:
        if isinstance(effect, Quit):
            self.exit(0)
        elif isinstance(effect, Act):
            self._act(effect)
        elif isinstance(effect, (Rerank, Refine)):
            events_ = browse_run.effect_events(effect, self.state, self.source)
            if events_ is None:
                self.state.say("no saved search to re-rank yet")
            else:
                self._stream(events_)

    # ------------------------------------------------------------------ streams
    def _stream(self, events_: Iterator[dict[str, Any]]) -> None:
        def post(event: dict[str, Any]) -> None:
            self.call_from_thread(self._event, event)

        def finished(error: BaseException | None) -> None:
            with contextlib.suppress(Exception):  # the app may be gone
                self.call_from_thread(self._stream_end, error)

        stream = browse_run.Stream(events_, post, finished).start()
        self._streams.append(stream)
        self._threads.append(stream.thread)

    def _event(self, event: dict[str, Any]) -> None:
        self.state.ingest(event)
        self.repaint()

    def _stream_end(self, error: BaseException | None) -> None:
        if error is not None:
            self.state.say(f"stream failed: {error}")
            self.state.busy = ""
            self.state.phase = "done"
        self.repaint()

    # ------------------------------------------------------------------ actions
    def _act(self, effect: Act) -> None:
        rows = browse_state.rows_for(self.state, effect.keys)
        if effect.action == "image":
            self._draw_image(rows)
            return
        params = self.params
        self.state.busy = effect.action
        if (
            effect.action == "agent"
            and not params.dry_run
            and params.agent_mode in ("attach", "auto")
        ):
            outcome = self._perform(
                effect.action, rows, params
            )  # attaches to this terminal: no worker thread
            self._finish(outcome)
            return
        thread = threading.Thread(
            target=self._act_worker, args=(effect.action, rows, params), daemon=True
        )
        self._threads.append(thread)
        thread.start()

    def _perform(
        self, action: str, rows: list[browse_state.Row], params: ActParams
    ) -> ActOutcome:
        known = dict(self.state.downloads)
        run = lambda: browse_run.perform(  # noqa: E731
            action,
            rows,
            session=self.state.session,
            query=self.state.query,
            params=params,
            known=known,
            invoke=self._invoke,
        )
        if (
            action == "agent"
            and params.agent_mode in ("attach", "auto")
            and not params.dry_run
        ):
            with self._suspended():
                sys.stdout.write(
                    f"\n3d models: starting the agent ({browse_state.cli_line('agent', rows, self.state.session)})\n"
                )
                return run()
        return run()

    def _act_worker(
        self, action: str, rows: list[browse_state.Row], params: ActParams
    ) -> None:
        try:
            outcome = self._perform(action, rows, params)
        except Exception as exc:  # noqa: BLE001 — a failing action is a status line, not a crashed UI
            outcome = ActOutcome(action, [r.key for r in rows], plan=[f"failed: {exc}"])
        with contextlib.suppress(Exception):
            self.call_from_thread(self._finish, outcome)

    def _finish(self, outcome: ActOutcome) -> None:
        self.outcomes.append(outcome)
        self.state.busy = ""
        self.state.record_downloads(outcome.folders)
        if outcome.action == "preview" and outcome.results and outcome.results[0]["ok"]:
            data = outcome.results[0]["data"]
            cur = self.state.current()
            if (
                cur is not None
                and data.get("site") == cur.site
                and str(data.get("id")) == cur.model_id
            ):
                self.state.set_preview(data)
        else:
            line = f"{outcome.summary()}  $ {outcome.cli}" if outcome.cli else outcome.summary()
            self.state.say(line)
        self.repaint()

    def _draw_image(self, rows: list[browse_state.Row]) -> None:
        """Print the preview picture on the real terminal (kitty / iTerm2 / sixel / half blocks) and wait for Enter."""
        image = (self.state.preview or {}).get("image") or {}
        sequence = image.get("sequence")
        if not sequence:
            self.state.say(
                "no picture to draw (this terminal has no image support, or the result has none)"
            )
            return
        title = rows[0].title if rows else ""
        with self._suspended():
            sys.stdout.write(
                f"\n{title}\n{sequence}\n[{image.get('protocol')}] press Enter to return to the browser "
            )
            sys.stdout.flush()
            with contextlib.suppress(EOFError, OSError):
                input()

    # ------------------------------------------------------------------ tests
    async def settled(self, timeout: float = 5.0) -> None:
        """Wait until every worker thread has finished and its results are on screen (headless tests)."""
        import asyncio
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._threads = [t for t in self._threads if t.is_alive()]
            await asyncio.sleep(0.02)
            if not self._threads:
                await asyncio.sleep(0.05)
                return


def run_browser(
    state: BrowserState,
    source: EventSource,
    params: ActParams,
    *,
    start: Callable[[], Iterator[dict[str, Any]]] | None,
    invoke: Invoke = actions.invoke,
) -> int:
    """Run the browser until the user quits; returns the process exit code."""
    app = BrowserApp(state, source, params, start=start, invoke=invoke)
    app.run()
    return int(app.return_code or 0)
