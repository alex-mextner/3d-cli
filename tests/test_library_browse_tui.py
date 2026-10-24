"""The Textual browser driven headlessly (`App.run_test`, the Textual Pilot) over the REAL ranking pipeline running
offline (scripted catalogue + scripted model) and FAKE actions: navigation, multi-select, every hotkey calling the
matching action with the right selection, a question answered inline, a follow-up run, a refinement, a resumed
search and the SVG snapshots at 80x24 and 120x40. No network, no real slicer / agent / browser."""

from __future__ import annotations

import contextlib
import io
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from browse_fixtures import QUERY, FakeActions, backend, make_source, seeded

from errors import MissingDependency

textual = pytest.importorskip("textual")
from tui_harness import Harness, drive, svg_text  # noqa: E402  (imports textual)
from library import browse_tui, browsecli  # noqa: E402

SNAPSHOTS = Path(__file__).resolve().parents[1] / "docs" / "commands" / "snapshots"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("THREED_LIBRARY_DB", str(tmp_path / "library.db"))
    monkeypatch.setenv("THREED_KEYCHAIN", "off")
    monkeypatch.delenv("THREED_AI_CONFIG", raising=False)


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "library.db")


def rows_of(db: str, sql: str, *args: Any) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def live(
    monkeypatch: pytest.MonkeyPatch,
    db: str,
    fake: Any,
    scenario: Callable[[Harness], Awaitable[None]],
    **kw: Any,
) -> browse_tui.BrowserApp:
    source = make_source(monkeypatch, db)
    return drive(
        scenario, source=source, start=lambda: source.search(QUERY), invoke=fake, **kw
    )


# --------------------------------------------------------------------------- streaming into the list
def test_results_stream_in_and_verdicts_update_the_rows_live(
    monkeypatch: pytest.MonkeyPatch, db: str
) -> None:
    shots: dict[str, str] = {}

    async def scenario(h: Harness) -> None:
        await h.done()
        shots["done"] = h.screen()
        shots["rows"] = str(len(h.state.rows))

    app = live(monkeypatch, db, FakeActions(), scenario)
    text = shots["done"]
    assert shots["rows"] == "8"
    for fragment in (
        "Soil probe case 1",
        "Sensor enclosure 1",
        "Keychain 2",
        "fits 0.89",
        "CC-BY",
        "done: 8 results",
    ):
        assert fragment in text, fragment
    assert text.index("Soil probe case 1") < text.index(
        "Keychain 2"
    )  # fits first, "no" last
    assert app.return_code in (0, None)


def test_the_list_is_on_screen_before_the_model_answers(
    monkeypatch: pytest.MonkeyPatch, db: str
) -> None:
    """With a slow model the rows show up first, unjudged, and the verdicts arrive on the live screen afterwards."""
    from browse_fixtures import handler
    from llm_fixtures import ScriptedBackend

    slow = ScriptedBackend(handler, delay_s=0.6)
    source = make_source(monkeypatch, db, scripted=slow)
    seen: dict[str, Any] = {}

    async def scenario(h: Harness) -> None:
        await h.until(lambda: len(h.state.rows) == 6)
        seen["early"] = h.screen()
        seen["early_verdicts"] = [r.verdict for r in h.state.rows.values()]
        await h.done()
        seen["late"] = h.screen()

    drive(
        scenario,
        source=source,
        start=lambda: source.search(QUERY),
        invoke=FakeActions(),
    )
    assert all(v is None for v in seen["early_verdicts"])
    assert (
        "Soil probe case 1" in seen["early"]
        and "judging" in seen["early"]
        and "fits 0.89" not in seen["early"]
    )
    assert "fits 0.89" in seen["late"] and "judging" not in seen["late"]


# --------------------------------------------------------------------------- navigation, selection, hotkeys
def test_arrows_space_and_A_move_and_select(
    monkeypatch: pytest.MonkeyPatch, db: str
) -> None:
    result: dict[str, Any] = {}

    async def scenario(h: Harness) -> None:
        await h.done()
        order = [r.key for r in h.state.visible()]
        await h.press("down", "down", "space", "space")
        result["selected"] = (sorted(h.state.selected), order)
        result["screen"] = h.screen()
        await h.press("A")
        result["all"] = len(h.state.selected)
        await h.press("A", "end", "up")
        result["cursor"] = (h.state.cursor, order)

    live(monkeypatch, db, FakeActions(), scenario)
    selected, order = result["selected"]
    assert (
        selected == sorted(order[2:4])
        and "2 selected" in result["screen"]
        and result["screen"].count("[x]") == 2
    )
    assert result["all"] == 8
    assert result["cursor"][0] == result["cursor"][1][-2]


def test_each_hotkey_calls_the_matching_action_with_the_selection(
    monkeypatch: pytest.MonkeyPatch, db: str, tmp_path: Path
) -> None:
    fake = FakeActions(download_root=tmp_path / "downloads")
    picked: dict[str, Any] = {}

    async def scenario(h: Harness) -> None:
        await h.done()
        await h.press("space", "space")  # rows 1 and 2 of the list
        picked["rows"] = sorted(h.state.selected)
        await h.press("b")  # browser: no confirmation
        await h.press("d")
        picked["confirm"] = h.screen()
        await h.press("y")
        await h.press("s", "y")
        await h.press("a", "y")
        await h.press("p")
        picked["preview_screen"] = h.screen()
        await h.press("escape")

    live(monkeypatch, db, fake, scenario)
    by_name: dict[str, list[dict[str, Any]]] = {}
    for name, kwargs in fake.calls:
        by_name.setdefault(name, []).append(kwargs)
    urls = sorted(
        f"https://{k.split(':')[0]}.example/model/{k.split(':')[1]}"
        for k in picked["rows"]
    )
    assert (
        by_name["open_browser"][0]["urls"] == urls
        and by_name["open_browser"][0]["dry_run"] is False
    )
    assert (
        sorted(k["url"] for k in by_name["download"]) == urls
    )  # d downloaded both; s found them already there
    assert len(by_name["download"]) == 2
    slicer = by_name["open_slicer"][0]
    assert (
        sorted(Path(p).name for p in slicer["path"]) == ["part.stl", "part.stl"]
        and slicer["dry_run"] is False
    )
    agent = by_name["launch_agent"][0]
    assert agent["mode"] == "attach" and agent["dry_run"] is False
    assert (
        agent["context"]["query"] == QUERY
        and sorted(m["url"] for m in agent["context"]["models"]) == urls
    )
    assert all(
        m["dir"] for m in agent["context"]["models"]
    )  # the downloaded paths went into the brief
    assert by_name["preview"][0]["card"]["title"].startswith("Soil probe case")
    assert (
        "download 2 models?" in picked["confirm"]
        and "--do download --yes --select" in picked["confirm"]
    )
    assert "files (1)" in picked["preview_screen"]


def test_without_a_selection_the_hotkeys_act_on_the_row_under_the_cursor(
    monkeypatch: pytest.MonkeyPatch, db: str, tmp_path: Path
) -> None:
    fake = FakeActions(download_root=tmp_path / "dl")

    async def scenario(h: Harness) -> None:
        await h.done()
        await h.press("down", "b", "d", "n", "d", "enter")

    live(monkeypatch, db, fake, scenario)
    assert fake.names() == ["open_browser", "download"]
    assert fake.calls[0][1]["urls"] == [
        "https://printables.example/model/3"
    ]  # the 2nd row in verdict order


def test_cancelling_a_confirmation_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, db: str
) -> None:
    fake = FakeActions()

    async def scenario(h: Harness) -> None:
        await h.done()
        await h.press("d", "n", "s", "escape", "a", "n")

    live(monkeypatch, db, fake, scenario)
    assert fake.calls == []


def test_a_failing_action_is_a_status_line_and_the_ui_keeps_running(
    monkeypatch: pytest.MonkeyPatch, db: str
) -> None:
    from library import actions

    def broken(name: str, **kwargs: Any) -> Any:
        if name == "download":
            return actions.error_result(
                name, [actions.issue("error", "printables: paid model refused")]
            )
        raise RuntimeError("boom")

    seen: dict[str, str] = {}

    async def scenario(h: Harness) -> None:
        await h.done()
        await h.press("d", "y")
        seen["first"] = h.state.status
        await h.press("b")
        seen["second"] = h.state.status
        await h.press("down")
        seen["alive"] = h.state.cursor or ""

    live(monkeypatch, db, broken, scenario)
    assert "download failed: printables: paid model refused" in seen["first"]
    assert "failed: boom" in seen["second"] and seen["alive"]


def test_help_filter_and_quit_work_through_real_key_events(
    monkeypatch: pytest.MonkeyPatch, db: str
) -> None:
    shots: dict[str, str] = {}

    async def scenario(h: Harness) -> None:
        await h.done()
        await h.press("question_mark")
        shots["help"] = h.screen()
        await h.press("x")
        await h.press("slash", "k", "e", "y", "c")
        shots["filter"] = h.screen()
        await h.press("enter")
        shots["rows"] = ",".join(r.title for r in h.state.visible())
        await h.press("q")

    app = live(monkeypatch, db, FakeActions(), scenario)
    assert "Keys" in shots["help"] and "launch an agent" in shots["help"]
    assert "/ keyc" in shots["filter"]
    assert shots["rows"] == "Keychain 1,Keychain 2"
    assert app.return_code == 0


# --------------------------------------------------------------------------- follow-ups, questions, refine
def test_a_clarifying_question_is_answered_inline_and_the_answer_is_saved_and_applied(
    monkeypatch: pytest.MonkeyPatch, db: str
) -> None:
    scripted = backend()
    source = make_source(monkeypatch, db, scripted=scripted)
    shots: dict[str, Any] = {}

    async def scenario(h: Harness) -> None:
        await h.done()
        calls_before = len(scripted.calls)
        await h.press("f")
        shots["panel"] = h.screen()
        await h.press("enter", "down")  # need_cad: pick "no"
        shots["choose"] = h.screen()
        await h.press("enter")
        await h.done()
        shots["calls"] = (calls_before, len(scripted.calls))
        shots["answer"] = dict(h.state.questions["need_cad"])
        await h.press("f")
        shots["after"] = h.screen()

    drive(
        scenario,
        source=source,
        start=lambda: source.search(QUERY),
        invoke=FakeActions(),
    )
    assert (
        "? need_cad (single): Do you need CAD files?" in shots["panel"]
        and "follow-up 1 [suggested]" in shots["panel"]
    )
    assert "Do you need CAD files?" in shots["choose"] and "yes" in shots["choose"]
    assert shots["calls"][1] > shots["calls"][0]  # the answer re-judged the results
    assert "need_cad" in "\n".join(
        c["user"] for c in scripted.calls[shots["calls"][0] :]
    )  # as a constraint
    assert shots["answer"]["answer"] == "no" and "= no" in shots["after"]
    saved = rows_of(db, "SELECT answer FROM search_question WHERE key = 'need_cad'")
    assert [r["answer"] for r in saved] == ['"no"']


def test_a_followup_runs_a_new_search_persisted_under_the_same_root(
    monkeypatch: pytest.MonkeyPatch, db: str
) -> None:
    source = make_source(monkeypatch, db)
    result: dict[str, Any] = {}

    async def scenario(h: Harness) -> None:
        await h.done()
        await h.press("f", "down", "down", "down", "enter")
        await h.until(
            lambda: (
                "Plant stake sensor holder" in {r.title for r in h.state.rows.values()}
            )
        )
        await h.done()
        result["status"] = h.state.followups[1]["status"]
        result["rows"] = len(h.state.rows)

    drive(
        scenario,
        source=source,
        start=lambda: source.search(QUERY),
        invoke=FakeActions(),
    )
    assert result == {"status": "run", "rows": 9}
    sessions = rows_of(
        db,
        "SELECT s.query, s.kind, p.query AS parent FROM search_session s LEFT JOIN search_session p ON p.id = s.parent_id ORDER BY s.id",
    )
    follow = [r for r in sessions if r["kind"] == "followup"]
    assert [(r["query"], r["parent"]) for r in follow] == [
        ("plant stake sensor holder", QUERY)
    ]


def test_refine_searches_again_as_a_child_of_the_current_search_and_merges_the_hits(
    monkeypatch: pytest.MonkeyPatch, db: str
) -> None:
    source = make_source(monkeypatch, db)
    result: dict[str, Any] = {}

    async def scenario(h: Harness) -> None:
        await h.done()
        await h.press("r")
        for ch in "sensor enclosure":
            await h.press("space" if ch == " " else ch)
        await h.press("enter")
        await h.until(
            lambda: (
                "refine" not in h.state.busy
                and "Sensor enclosure 1" in {r.title for r in h.state.rows.values()}
            )
        )
        await h.done()
        result["session"] = h.state.session
        result["rows"] = len(h.state.rows)

    drive(
        scenario,
        source=source,
        start=lambda: source.search(QUERY),
        invoke=FakeActions(),
    )
    roots = rows_of(db, "SELECT uid FROM search_session WHERE parent_id IS NULL")
    assert (
        len(roots) == 1 and result["session"] == roots[0]["uid"]
    )  # the browser keeps the original root
    children = rows_of(
        db,
        "SELECT query, kind FROM search_session WHERE parent_id IS NOT NULL ORDER BY id",
    )
    assert ("sensor enclosure", "user") in [(r["query"], r["kind"]) for r in children]


def test_a_saved_search_is_resumed_from_the_library_without_a_model_call(
    monkeypatch: pytest.MonkeyPatch, db: str
) -> None:
    events, uid = seeded(monkeypatch, db)
    quiet = backend()
    source = make_source(monkeypatch, db, scripted=quiet)
    result: dict[str, Any] = {}

    async def scenario(h: Harness) -> None:
        await h.done()
        result["screen"] = h.screen()
        result["rows"] = len(h.state.rows)
        await h.press("f")
        result["panel"] = h.screen()

    drive(
        scenario, source=source, start=lambda: source.saved(uid), invoke=FakeActions()
    )
    assert result["rows"] == 8 and quiet.calls == []
    assert "fits 0.89" in result["screen"] and "a case for a probe" in result["screen"]
    assert "follow-up 2 [suggested]: 'greenhouse sensor mount'" in result["panel"]


def test_a_failing_event_stream_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, db: str
) -> None:
    def broken() -> Any:
        yield {
            "event": "start",
            "kind": "search",
            "query": "q",
            "llm": {"enabled": False},
        }
        raise OSError("disk on fire")

    source = make_source(monkeypatch, db)
    result: dict[str, str] = {}

    async def scenario(h: Harness) -> None:
        await h.until(lambda: "stream failed" in h.state.status)
        result["status"] = h.state.status
        await h.press("down")

    drive(scenario, source=source, start=broken, invoke=FakeActions())
    assert result["status"] == "stream failed: disk on fire"


# --------------------------------------------------------------------------- the terminal hand-offs
def test_the_agent_attaches_to_the_terminal_inside_a_suspended_app(
    monkeypatch: pytest.MonkeyPatch, db: str
) -> None:
    entered: list[str] = []

    @contextlib.contextmanager
    def suspended() -> Any:
        entered.append("suspended")
        yield
        entered.append("resumed")

    fake = FakeActions()

    async def scenario(h: Harness) -> None:
        await h.done()
        await h.press("a", "y")

    live(monkeypatch, db, fake, scenario, suspended=suspended)
    assert entered == ["suspended", "resumed"] and fake.names() == ["launch_agent"]
    assert fake.calls[0][1]["mode"] == "attach"


def test_the_picture_is_printed_on_the_real_terminal_when_the_preview_has_one(
    monkeypatch: pytest.MonkeyPatch, db: str
) -> None:
    class WithPicture(FakeActions):
        def __call__(self, name: str, **kwargs: Any) -> Any:
            result = super().__call__(name, **kwargs)
            if name == "preview":
                result["data"]["image"] = {
                    "url": "u",
                    "protocol": "kitty",
                    "sequence": "\x1b_Gf=100;PICTURE\x1b\\",
                }
            return result

    fake = WithPicture()
    entered: list[str] = []
    printed = io.StringIO()

    @contextlib.contextmanager
    def suspended() -> Any:
        entered.append("suspended")
        with contextlib.redirect_stdout(printed):
            yield

    async def scenario(h: Harness) -> None:
        await h.done()
        h.state.rows[h.state.current().key].card["image"] = "https://img.example/1.png"  # type: ignore[union-attr]
        await h.press("p")
        await h.until(lambda: h.state.preview is not None)
        assert "press i to draw it (kitty)" in h.screen()
        await h.press("i")

    live(monkeypatch, db, fake, scenario, suspended=suspended)
    assert entered == ["suspended"] and "PICTURE" in printed.getvalue()
    assert list(fake.names()) == [
        "preview"
    ]  # `i` drew the picture the preview already fetched


def test_a_terminal_without_a_picture_says_so_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch, db: str
) -> None:
    async def scenario(h: Harness) -> None:
        await h.done()
        await h.press("p")
        await h.until(lambda: h.state.preview is not None)
        await h.press("i")
        assert "no picture to draw" in h.state.status

    live(monkeypatch, db, FakeActions(), scenario)


# --------------------------------------------------------------------------- the optional dependency
def test_a_missing_textual_is_a_structured_127_naming_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a, **k: None if name == "textual" else real(name, *a, **k),
    )
    with pytest.raises(MissingDependency) as exc:
        browsecli.require_textual()
    assert exc.value.exit_code == 127
    rendered = exc.value.render()
    assert "uv sync --extra tui" in rendered and "textual" in rendered


# --------------------------------------------------------------------------- snapshots (80x24, 120x40)
def snapshot(
    monkeypatch: pytest.MonkeyPatch, db: str, size: tuple[int, int], *keys: str
) -> str:
    out: dict[str, str] = {}

    async def scenario(h: Harness) -> None:
        await h.done()
        await h.press(*keys)
        out["svg"] = h.app.export_screenshot(title="3d models browse")

    live(monkeypatch, db, FakeActions(), scenario, size=size)
    return out["svg"]


@pytest.mark.parametrize(
    ("size", "name"),
    [((80, 24), "browser-80x24.svg"), ((120, 40), "browser-120x40.svg")],
)
def test_the_svg_snapshots_match_the_committed_ones(
    monkeypatch: pytest.MonkeyPatch, db: str, size: tuple[int, int], name: str
) -> None:
    """The committed SVG is what the browser looks like; its TEXT layer must equal today's screen (regenerate with
    `THREED_UPDATE_SNAPSHOTS=1 pytest tests/test_library_browse_tui.py -k snapshots`)."""
    import os

    svg = snapshot(monkeypatch, db, size, "down", "space", "down", "space")
    current = svg_text(svg)
    target = SNAPSHOTS / name
    if os.environ.get("THREED_UPDATE_SNAPSHOTS"):
        SNAPSHOTS.mkdir(parents=True, exist_ok=True)
        target.write_text(svg + "\n", encoding="utf-8")
    assert target.is_file(), f"missing {target}: run with THREED_UPDATE_SNAPSHOTS=1"
    assert svg_text(target.read_text(encoding="utf-8")) == current
    assert "Soil probe case 1" in current and "2 selected" in current
    assert max(len(line) for line in current.splitlines()) <= size[0]
