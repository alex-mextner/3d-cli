"""The model browser as a state machine (#578): the REAL ranking events of an offline pipeline run (scripted
catalogue + scripted model, `browse_fixtures`) are folded into rows, and keys become effects. No terminal, no
thread, no network: this is the "fake terminal" the issue asks for - `render_screen` is the whole screen as text."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from browse_fixtures import QUERY, seeded

from errors import InvalidArgument, UsageError
from library import browse_run, browse_state
from library.browse_run import saved_events, select_rows
from library.browse_state import Act, BrowserState, Quit, Refine, Rerank


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("THREED_LIBRARY_DB", str(tmp_path / "library.db"))
    monkeypatch.setenv("THREED_KEYCHAIN", "off")
    monkeypatch.delenv("THREED_AI_CONFIG", raising=False)


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict[str, Any]]:
    return seeded(monkeypatch, str(tmp_path / "library.db"))[0]


def folded(events: list[dict[str, Any]], upto: str | None = None) -> BrowserState:
    state = BrowserState()
    for event in events:
        state.ingest(event)
        if upto is not None and event["event"] == upto:
            break
    return state


def keys(state: BrowserState, *names: str) -> list[Any]:
    out: list[Any] = []
    for name in names:
        out += state.press(name)
    return out


def screen(state: BrowserState, width: int = 100, height: int = 30) -> str:
    return "\n".join(
        text.rstrip() for text, _ in browse_state.render_screen(state, width, height)
    )


# --------------------------------------------------------------------------- the event stream
def test_results_appear_before_any_verdict_and_verdicts_update_rows_in_place(
    events: list[dict[str, Any]],
) -> None:
    """Phase 1 rows are on screen unjudged; the verdicts then change them, without adding rows."""
    before = folded(events, upto="phase")
    assert len(before.rows) == 6 and all(
        r.verdict is None for r in before.rows.values()
    )
    assert before.phase == "judging" and "judging 6" in before.busy
    assert "unjudged" not in screen(before) and "-" in screen(before)
    after = folded(events)
    assert len(after.rows) == 8 and after.phase == "done"
    assert {r.verdict for r in after.rows.values()} == {"fits", "maybe", "no"}
    probe = after.rows["printables:1"]
    assert (probe.verdict, probe.score, probe.rationale) == (
        "fits",
        0.89,
        "a case for a probe",
    )
    assert after.counts["results"] == 8 and after.status.startswith(
        "done: 8 results, 2 fit"
    )


def test_the_list_is_in_final_order_and_the_cursor_follows_its_row_when_verdicts_reorder(
    events: list[dict[str, Any]],
) -> None:
    state = BrowserState()
    for event in events:
        state.ingest(event)
        if event["event"] == "phase":
            break
    state.press("down")
    state.press("down")
    assert state.cursor == "printables:3"  # discovery order while unjudged
    for event in events[
        events.index(next(e for e in events if e["event"] == "phase")) + 1 :
    ]:
        state.ingest(event)
    order = [r.key for r in state.visible()]
    assert order[:2] == ["printables:1", "printables:3"] and order[-2:] == [
        "thingiverse:1",
        "thingiverse:2",
    ]
    assert (
        state.current() or state.visible()[0]
    ).key == "printables:3"  # the cursor row did not change
    assert [
        r.key for r in state.numbered()
    ] == order  # act numbers rows exactly as the screen orders them


def test_ingest_is_idempotent_so_a_rerank_that_re_emits_everything_adds_nothing(
    events: list[dict[str, Any]],
) -> None:
    state = folded(events)
    state.selected = {"printables:1"}
    for event in events:
        state.ingest(event)
    assert len(state.rows) == 8 and state.selected == {"printables:1"}
    assert len(state.questions) == 3 and sorted(state.followups) == [1, 2]


def test_questions_and_followups_arrive_with_their_types_and_choices(
    events: list[dict[str, Any]],
) -> None:
    state = folded(events)
    q = state.questions
    assert (q["need_cad"]["type"], q["need_cad"]["choices"]) == (
        "single",
        ["yes", "no"],
    )
    assert (q["extras"]["type"], q["extras"]["choices"]) == (
        "multi",
        ["lid", "stake", "cable gland"],
    )
    assert q["printer"]["type"] == "text" and q["printer"]["answer"] is None
    assert state.followups[2]["query"] == "greenhouse sensor mount"
    assert [x["query"] for x in state.queries] == [
        "sensor enclosure",
        "capacitive probe case",
    ]


def test_a_model_that_is_unavailable_is_a_note_not_a_failure() -> None:
    state = BrowserState()
    state.ingest(
        {
            "event": "warning",
            "code": "llm_unavailable",
            "message": "LLM: unavailable (no omp)",
        }
    )
    assert state.notes == [
        "LLM: unavailable (no omp)"
    ] and "LLM: unavailable" in screen(state)


# --------------------------------------------------------------------------- keys
def test_arrows_page_home_end_and_vi_keys_move_the_cursor_inside_the_list(
    events: list[dict[str, Any]],
) -> None:
    state = folded(events)
    order = [r.key for r in state.visible()]
    keys(state, "down", "down", "j")
    assert state.cursor == order[3]
    keys(state, "up", "k")
    assert state.cursor == order[1]
    keys(state, "end")
    assert state.cursor == order[-1]
    keys(state, "down")
    assert state.cursor == order[-1]  # no wrap, no crash
    keys(state, "home")
    assert state.cursor == order[0]
    keys(state, "pagedown")
    assert state.cursor == order[-1]  # 8 rows: a page is longer than the list
    keys(state, "pageup")
    assert state.cursor == order[0]


def test_space_selects_and_moves_on_and_A_selects_all_visible_then_none(
    events: list[dict[str, Any]],
) -> None:
    state = folded(events)
    order = [r.key for r in state.visible()]
    keys(state, "space", "space")
    assert state.selected == set(order[:2]) and state.cursor == order[2]
    keys(state, "up", "space")  # the second row, selected, is unselected again
    assert state.selected == {order[0]}
    keys(state, "A")
    assert state.selected == set(order)
    keys(state, "A")
    assert state.selected == set()


def test_action_keys_act_on_the_selection_or_else_the_row_under_the_cursor(
    events: list[dict[str, Any]],
) -> None:
    state = folded(events)
    order = [r.key for r in state.visible()]
    assert state.press("b") == [Act("open", (order[0],))]
    keys(state, "space", "space")
    assert state.press("b") == [Act("open", (order[0], order[1]))]
    assert state.press("p") == [Act("preview", (order[2],))] and state.mode == "preview"


@pytest.mark.parametrize(
    ("key", "action"), [("d", "download"), ("s", "slicer"), ("a", "agent")]
)
def test_download_slicer_and_agent_ask_first_and_show_the_equivalent_command(
    events: list[dict[str, Any]], key: str, action: str
) -> None:
    state = folded(events)
    keys(state, "space", "down", "space")
    picked = tuple(sorted(state.selected))
    assert state.press(key) == [] and state.mode == "confirm"
    text = screen(state)
    assert (
        f"{action} 2 models?" in text
        and f"--do {action} --yes" in text
        and str(state.session) in text
    )
    assert (
        state.press("n") == []
        and state.mode == "list"
        and state.selected == set(picked)
    )
    keys(state, key)
    effects = state.press("y")
    assert (
        effects
        == [Act(action, tuple(r.key for r in state._ordered() if r.key in picked))]
        and state.mode == "list"
    )
    keys(state, key)
    assert state.press("escape") == []  # escape cancels too
    keys(state, key)
    confirmed = state.press("enter")  # enter confirms
    assert len(confirmed) == 1 and isinstance(confirmed[0], Act)
    assert confirmed[0].action == action


def test_preview_walks_to_the_next_result_and_asks_for_its_own_preview(
    events: list[dict[str, Any]],
) -> None:
    state = folded(events)
    order = [r.key for r in state.visible()]
    state.press("enter")
    assert state.mode == "preview"
    assert state.press("down") == [Act("preview", (order[1],))]
    assert state.press("i") == [Act("image", (order[1],))]
    assert (
        state.press("d") == [] and state.mode == "confirm"
    )  # an action key works from the preview too
    state.press("n")
    state.press("p")
    assert state.mode == "preview"
    state.press("escape")
    assert state.mode == "list"


def test_filter_narrows_live_and_escape_restores_it(
    events: list[dict[str, Any]],
) -> None:
    state = folded(events)
    keys(state, "/", "k", "e", "y")
    assert state.mode == "filter" and {r.site for r in state.visible()} == {
        "thingiverse"
    }
    assert len(state.visible()) == 2 and "/ key" in screen(state)
    state.press("escape")
    assert state.filter == "" and len(state.visible()) == 8
    keys(state, "/", "e", "n", "c", "l", "o", "s", "u", "r", "e", "enter")
    assert state.mode == "list" and [r.title for r in state.visible()] == [
        "Sensor enclosure 1",
        "Sensor enclosure 2",
    ]
    state.press("escape")  # in the list: escape clears an applied filter
    assert state.filter == ""
    keys(state, "/", "n", "o", "p", "e", "enter")
    assert "no result matches the filter" in screen(state)


def test_filter_matches_verdict_licence_and_rationale_words(
    events: list[dict[str, Any]],
) -> None:
    state = folded(events)
    state.filter = "fits"
    assert [r.verdict for r in state.visible()] == ["fits", "fits"]
    state.filter = "cc-by"
    assert len(state.visible()) == 8


def test_refine_searches_again_and_empty_input_does_nothing(
    events: list[dict[str, Any]],
) -> None:
    state = folded(events)
    keys(state, "r", "p", "r", "o", "b", "e", "space", "h", "o", "l", "d", "e", "r")
    assert "refine query: probe holder" in screen(state)
    assert state.press("enter") == [Refine("probe holder")] and state.mode == "list"
    keys(state, "r")
    assert state.press("enter") == [] and state.mode == "list"
    keys(state, "r", "x", "escape")
    assert state.mode == "list"


def test_order_key_toggles_between_verdict_order_and_as_found(
    events: list[dict[str, Any]],
) -> None:
    state = folded(events)
    assert (
        state.visible()[0].key == "printables:1"
        and state.visible()[2].verdict == "maybe"
    )
    state.press("o")
    assert [r.position for r in state.visible()] == list(range(1, 9))
    assert "as found" in state.status
    state.press("o")
    assert state.order == "rank"


def test_help_lists_every_key_and_any_key_closes_it(
    events: list[dict[str, Any]],
) -> None:
    state = folded(events)
    state.press("?")
    text = screen(state, 100, 30)
    for fragment in (
        "move",
        "select",
        "preview",
        "browser",
        "download",
        "slicer",
        "agent",
        "follow-up",
        "refine",
        "filter",
        "quit",
    ):
        assert fragment in text, fragment
    state.press("x")
    assert state.mode == "list"


def test_q_and_ctrl_c_quit() -> None:
    assert BrowserState().press("q") == [Quit()]
    assert BrowserState().press("ctrl+c") == [Quit()]


def test_keys_on_an_empty_list_do_nothing_and_say_so() -> None:
    state = BrowserState()
    assert keys(state, "down", "space", "d", "b") == []
    assert state.status == "nothing to act on: no results yet"
    assert "waiting for the first results" in screen(state)


# --------------------------------------------------------------------------- follow-ups and questions
def test_a_single_choice_question_is_answered_inline_and_asks_for_a_rerank(
    events: list[dict[str, Any]],
) -> None:
    state = folded(events)
    state.press("f")
    assert state.mode == "followups" and "Do you need CAD files?" in screen(state)
    state.press("enter")
    assert state.mode == "choose" and "yes" in screen(state)
    state.press("down")
    assert state.press("enter") == [Rerank(answers=(("need_cad", "no"),))]
    assert state.mode == "list" and "judging again" in state.status


def test_a_multi_choice_question_takes_a_checklist(
    events: list[dict[str, Any]],
) -> None:
    state = folded(events)
    keys(state, "f", "down", "enter")
    assert state.mode == "choose"
    keys(state, "space", "down", "down", "space")
    assert "[x] lid" in screen(state) and "[x] cable gland" in screen(state)
    assert state.press("enter") == [Rerank(answers=(("extras", "lid,cable gland"),))]
    keys(
        state, "f", "down", "enter"
    )  # nothing ticked: the row under the cursor is the answer
    assert state.press("enter") == [Rerank(answers=(("extras", "lid"),))]
    keys(state, "f", "down", "enter")
    assert state.press("escape") == [] and state.mode == "followups"


def test_a_text_question_takes_typed_text_and_escape_goes_back(
    events: list[dict[str, Any]],
) -> None:
    state = folded(events)
    keys(state, "f", "down", "down", "enter")
    assert state.mode == "answer"
    keys(state, "K", "1", "space", "S", "E")
    assert "answer: K1 SE" in screen(state)
    keys(state, "backspace")
    assert state.press("enter") == [Rerank(answers=(("printer", "K1 S"),))]
    keys(state, "f", "down", "down", "enter", "x", "escape")
    assert state.mode == "followups"


def test_a_followup_is_run_with_enter_and_an_empty_panel_says_so(
    events: list[dict[str, Any]],
) -> None:
    state = folded(events)
    keys(state, "f", "down", "down", "down")
    assert state.press("enter") == [Rerank(follow=(1,))]
    assert "follow-up 1" in state.status
    empty = BrowserState()
    empty.press("f")
    assert "nothing planned yet" in screen(empty)


def test_a_followup_that_ran_is_marked_run_and_its_query_listed(
    events: list[dict[str, Any]],
) -> None:
    state = folded(events)
    state.ingest(
        {
            "event": "query",
            "status": "run",
            "search": "uid-x",
            "query": "plant stake sensor holder",
            "kind": "followup",
            "new": 1,
            "rationale": "",
        }
    )
    assert (
        state.followups[1]["status"] == "run"
        and state.followups[2]["status"] == "suggested"
    )
    state.press("f")
    assert "[run]" in screen(state) and "queries run" in screen(state)


# --------------------------------------------------------------------------- the screen
@pytest.mark.parametrize(("width", "height"), [(80, 24), (120, 40), (60, 12)])
def test_the_screen_is_exactly_the_terminal_size_and_holds_the_documented_columns(
    events: list[dict[str, Any]], width: int, height: int
) -> None:
    state = folded(events)
    lines = browse_state.render_screen(state, width, height)
    assert len(lines) == height and all(len(text) <= width for text, _ in lines)
    text = "\n".join(t for t, _ in lines)
    assert QUERY in text or width < 80
    assert "Soil probe case 1" in text and "fits" in text and "printables"[:4] in text
    assert "CC-BY" in text and "size ?" in text


def test_the_detail_pane_shows_rationale_licence_url_and_the_row_under_the_cursor(
    events: list[dict[str, Any]],
) -> None:
    state = folded(events)
    text = screen(state, 120, 40)
    assert "fits 0.89: a case for a probe" in text
    assert "https://printables.example/model/1" in text and "CC-BY" in text
    state.press("end")
    assert "a keychain, not a case" in screen(state, 120, 40)


def test_the_preview_panel_has_details_description_files_and_analytics(
    events: list[dict[str, Any]],
) -> None:
    state = folded(events)
    state.press("p")
    text = screen(state, 100, 40)
    for fragment in (
        "Soil probe case 1",
        "fits 0.89",
        "description",
        "files (1)",
        "part.stl",
        "analytics",
        "as a reference model",
        "picture: none listed",
    ):
        assert fragment in text, fragment
    state.rows["printables:1"].card["image"] = "https://x/y.png"
    assert "picture: loading" in screen(state, 100, 40)
    state.set_preview({"image": {"url": "u", "protocol": "kitty", "sequence": "ESC"}})
    assert "press i to draw it (kitty)" in screen(state, 100, 40)


def test_selected_rows_are_marked_and_counted(events: list[dict[str, Any]]) -> None:
    state = folded(events)
    keys(state, "space", "space")
    text = screen(state)
    assert text.count("[x]") == 2 and "2 selected" in text


# --------------------------------------------------------------------------- the CLI twin and selection grammar
def test_cli_line_names_the_session_the_rows_the_action_and_needs_yes_for_effects(
    events: list[dict[str, Any]],
) -> None:
    state = folded(events)
    rows = [state.rows["printables:1"], state.rows["thingiverse:2"]]
    assert (
        browse_state.cli_line("download", rows, "S1")
        == "3d models act S1 --do download --yes --select printables:1,thingiverse:2"
    )
    assert (
        browse_state.cli_line("preview", rows[:1], "S1")
        == "3d models act S1 --do preview --select printables:1"
    )
    assert "--do open --yes" in browse_state.cli_line(
        "open", rows[:1], None
    ) and "SEARCH_ID" in browse_state.cli_line("open", rows, None)


def test_select_rows_grammar_numbers_ranges_keys_sites_and_verdicts(
    events: list[dict[str, Any]],
) -> None:
    state = folded(events)
    order = [r.key for r in state.numbered()]
    assert [r.key for r in select_rows(state, select="1,3")] == [order[0], order[2]]
    assert [r.key for r in select_rows(state, select="2-4")] == order[1:4]
    assert [r.key for r in select_rows(state, select="thingiverse:1")] == [
        "thingiverse:1"
    ]
    assert len(select_rows(state, select="all")) == 8
    assert {r.site for r in select_rows(state, sites="thingiverse")} == {"thingiverse"}
    assert [r.verdict for r in select_rows(state, verdicts="fits")] == ["fits", "fits"]
    assert [r.key for r in select_rows(state, sites="printables", verdicts="no")] == []
    assert [
        r.key for r in select_rows(state, select="1-8", verdicts="fits,maybe")
    ] == order[:6]
    for bad, flag in (
        ("9", "--select"),
        ("0", "--select"),
        ("3-2", "--select"),
        ("nope", "--select"),
    ):
        with pytest.raises(InvalidArgument) as exc:
            select_rows(state, select=bad)
        assert exc.value.message.startswith(flag) or "--select" in exc.value.message
    with pytest.raises(InvalidArgument):
        select_rows(state, sites="cults")
    with pytest.raises(InvalidArgument):
        select_rows(state, verdicts="great")
    with pytest.raises(UsageError):
        select_rows(state)


# --------------------------------------------------------------------------- resuming from the library DB
def test_a_saved_search_resumes_with_the_same_rows_verdicts_questions_and_followups(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = str(tmp_path / "library.db")
    live, uid = seeded(monkeypatch, db)
    fresh = folded(live)
    resumed = BrowserState()
    for event in saved_events(uid, db):
        resumed.ingest(event)
    assert [r.key for r in resumed.numbered()] == [r.key for r in fresh.numbered()]
    assert {k: (r.verdict, r.score, r.rationale) for k, r in resumed.rows.items()} == {
        k: (r.verdict, r.score, r.rationale) for k, r in fresh.rows.items()
    }
    assert set(resumed.questions) == set(fresh.questions) and sorted(
        resumed.followups
    ) == [1, 2]
    assert resumed.session == uid and resumed.query == QUERY and resumed.phase == "done"
    assert resumed.counts == fresh.counts
    assert any(
        q["query"] == "sensor enclosure" for q in resumed.queries
    )  # the planner's query that ran
    again = list(saved_events("latest", db))
    assert (
        again[0]["event"] == "start"
        and again[-1]["event"] == "done"
        and again[-1]["search"] == uid
    )
    with pytest.raises(UsageError):
        list(saved_events("nonexistent-uid", db))


def test_row_text_is_one_line_with_verdict_size_cad_and_terms(
    events: list[dict[str, Any]],
) -> None:
    state = folded(events)
    line = browse_state.row_text(state.rows["printables:1"])
    assert (
        line.startswith("fits 0.89  Soil probe case 1  [printables]")
        and "size ? mm" in line
        and "CC-BY" in line
    )
    assert browse_run.slug("A b/c: d!") == "A-b-c-d"
