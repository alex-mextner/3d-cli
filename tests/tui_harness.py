"""Headless driving of the Textual model browser for tests (#578): `drive` runs a scenario on `App.run_test`,
`Harness` waits for the worker threads and reads the screen back as text (`svg_text` of the exported SVG)."""

from __future__ import annotations

import asyncio
import contextlib
import html
import re
from collections.abc import Awaitable, Callable
from typing import Any

from browse_fixtures import QUERY

from library import browse_tui
from library.browse_run import ActParams, PipelineSource
from library.browse_state import BrowserState


def svg_text(svg: str) -> str:
    """The text a person reads in an exported screenshot: tags dropped, entities and nbsp decoded, lines kept."""
    rows: dict[str, list[tuple[float, str]]] = {}
    for x, y, text in re.findall(
        r'<text[^>]*?x="([\d.]+)" y="([\d.]+)"[^>]*>(.*?)</text>', svg
    ):
        rows.setdefault(y, []).append(
            (float(x), html.unescape(re.sub("<[^>]+>", "", text)).replace("\xa0", " "))
        )
    return "\n".join(
        "".join(t for _, t in sorted(parts)).rstrip()
        for _, parts in sorted(rows.items(), key=lambda kv: float(kv[0]))
    )


class Harness:
    def __init__(self, app: browse_tui.BrowserApp, pilot: Any) -> None:
        self.app, self.pilot, self.state = app, pilot, app.state

    async def until(self, condition: Callable[[], bool], timeout: float = 10.0) -> None:
        end = asyncio.get_running_loop().time() + timeout
        while not condition():
            assert asyncio.get_running_loop().time() < end, (
                f"timed out; status={self.state.status!r} phase={self.state.phase}"
            )
            await asyncio.sleep(0.02)
        await self.pilot.pause()

    async def done(self) -> None:
        await self.until(lambda: self.state.phase == "done" and not self.state.busy)

    async def press(self, *keys: str) -> None:
        await self.pilot.press(*keys)
        await self.app.settled()
        await self.pilot.pause()

    def screen(self) -> str:
        return svg_text(self.app.export_screenshot())


def drive(
    scenario: Callable[[Harness], Awaitable[None]],
    *,
    state: BrowserState | None = None,
    source: PipelineSource,
    start: Callable[[], Any] | None,
    invoke: Any,
    size: tuple[int, int] = (120, 40),
    params: ActParams | None = None,
    suspended: Any = None,
) -> browse_tui.BrowserApp:
    app = browse_tui.BrowserApp(
        state or BrowserState(query=QUERY),
        source,
        params or ActParams(agent_mode="attach"),
        start=start,
        invoke=invoke,
        suspended=suspended or contextlib.nullcontext,
    )

    async def run() -> None:
        async with app.run_test(size=size) as pilot:
            await scenario(Harness(app, pilot))

    asyncio.run(run())
    return app
