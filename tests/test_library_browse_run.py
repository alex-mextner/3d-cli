"""What the browser's keys do, below the screen (#578): `browse_run.perform` turns a key / `--do` into calls of
`library.actions` (faked here, nothing starts), `browse_plain` drives the same effects from typed lines, and the
flag layer (`browsecli`) validates what a person typed. Real ranking events from the offline pipeline."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from browse_fixtures import QUERY, FakeActions, backend, make_source, seeded

from errors import InvalidArgument, MissingDependency, UsageError
from library import actions, browse_plain, browse_run, browsecli
from library import db as librarydb
from library.browse_run import ActParams, perform
from library.browse_state import BrowserState


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("THREED_LIBRARY_DB", str(tmp_path / "library.db"))
    monkeypatch.setenv("THREED_KEYCHAIN", "off")
    monkeypatch.delenv("THREED_AI_CONFIG", raising=False)
    monkeypatch.delenv("THREED_NO_TUI", raising=False)


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> BrowserState:
    events, _ = seeded(monkeypatch, str(tmp_path / "library.db"))
    s = BrowserState()
    for event in events:
        s.ingest(event)
    return s


def run(
    action: str,
    state: BrowserState,
    fake: FakeActions,
    *numbers: int,
    known: dict[str, str] | None = None,
    **params: Any,
) -> browse_run.ActOutcome:
    ranked = state.numbered()
    rows = [ranked[n - 1] for n in numbers]
    return perform(
        action,
        rows,
        session=state.session,
        query=state.query,
        params=ActParams(**params),
        known=known,
        invoke=fake,
    )


def record_source(
    db: str, state: BrowserState, folder: Path, row_number: int, files: list[str]
) -> str:
    """What a real `download` recorded: a model_source row naming the folder (and the files in it)."""
    row = state.numbered()[row_number - 1]
    conn = librarydb.open_library(db)
    try:
        inserted = librarydb.insert_row(
            conn,
            "model_source",
            {
                "site": row.site,
                "model_id": row.model_id,
                "url": row.url,
                "license": "CC-BY",
                "author": "maker",
                "dir": str(folder),
                "files": [{"name": n, "size": 1, "sha256": "x"} for n in files],
                "sha256": None,
                "role": None,
                "vitamin_kind": None,
                "analytics": {},
            },
            source_key=f"source:{row.site}:{row.model_id}",
        )
        return str(inserted.uid)
    finally:
        conn.close()


# --------------------------------------------------------------------------- perform
def test_each_action_calls_the_matching_library_action_with_the_selected_rows(
    state: BrowserState, tmp_path: Path
) -> None:
    fake = FakeActions(download_root=tmp_path / "dl")
    one, two = state.numbered()[0], state.numbered()[1]
    pv = run("preview", state, fake, 1, image="auto")
    assert (
        pv.ok
        and fake.calls[-1][0] == "preview"
        and fake.calls[-1][1]["image"] == "auto"
    )
    assert (
        fake.calls[-1][1]["card"]["verdict"] == "fits"
    )  # the card carries the verdict the browser knows
    ob = run("open", state, fake, 1, 2, browser="b {url}", dry_run=True)
    assert fake.calls[-1] == (
        "open_browser",
        {"urls": [one.url, two.url], "browser": "b {url}", "dry_run": True},
    )
    assert ob.cli.endswith("--do open --yes --select " + f"{one.key},{two.key}")
    dl = run(
        "download",
        state,
        fake,
        1,
        db_path=str(tmp_path / "library.db"),
        dest_dir=str(tmp_path / "out"),
    )
    call = fake.calls[-1][1]
    assert fake.calls[-1][0] == "download" and call["url"] == one.url
    assert call["dest_dir"] == str(
        tmp_path / "out" / f"printables-{one.model_id}-Soil-probe-case-1"
    )
    assert dl.folders[one.key] and dl.ok and dl.exit_code == 0


def test_a_dry_run_downloads_and_starts_nothing_and_says_what_it_would_do(
    state: BrowserState, tmp_path: Path
) -> None:
    fake = FakeActions()
    dry = run(
        "download", state, fake, 1, 2, dry_run=True, dest_dir=str(tmp_path / "out")
    )
    assert fake.calls == [] and dry.results == [] and dry.dry_run is True
    assert [p.startswith("would download") for p in dry.plan] == [True, True]
    assert dry.to_json(state.numbered()[:2])["dry_run"] is True
    slicer = run("slicer", state, fake, 1, dry_run=True)
    assert (
        fake.calls == []
        and slicer.plan[-1] == "then open the downloaded files in the slicer"
    )
    queue = run("queue", state, fake, 1, dry_run=True)
    assert (
        fake.calls == [] and queue.plan[-1] == "then add each model to the print queue"
    )
    nothing = run("agent", state, fake, dry_run=True)
    assert nothing.plan == ["nothing selected"]


def test_slicer_downloads_first_then_opens_the_best_file_of_each_model(
    state: BrowserState, tmp_path: Path
) -> None:
    fake = FakeActions(download_root=tmp_path / "dl")
    out = run("slicer", state, fake, 1, 2, slicer="orca")
    assert fake.names() == ["download", "download", "open_slicer"]
    paths = fake.calls[-1][1]["path"]
    assert len(paths) == 2 and all(p.endswith("part.stl") for p in paths)
    assert (
        fake.calls[-1][1]["slicer"] == "orca" and fake.calls[-1][1]["dry_run"] is False
    )
    assert out.ok and [r["action"] for r in out.results] == [
        "download",
        "download",
        "open_slicer",
    ]


def test_a_failed_download_stops_the_slicer_step_and_the_error_is_the_exit_code(
    state: BrowserState,
) -> None:
    def failing(name: str, **kwargs: Any) -> Any:
        return actions.error_result(
            name, [actions.issue("error", "printables: blocked")]
        )

    out = perform(
        "slicer",
        state.numbered()[:1],
        session="S",
        query="q",
        params=ActParams(),
        invoke=failing,
    )
    assert (
        [r["action"] for r in out.results] == ["download"]
        and out.ok is False
        and out.exit_code == 1
    )
    assert "download failed: printables: blocked" in out.summary()


def test_a_model_already_in_the_library_is_reused_not_downloaded_again(
    state: BrowserState, tmp_path: Path
) -> None:
    folder = tmp_path / "kept"
    folder.mkdir()
    (folder / "case.3mf").write_bytes(b"x")
    (folder / "model.json").write_text(
        json.dumps({"source": {"url": "u"}, "license": "CC-BY"}), encoding="utf-8"
    )
    db = str(tmp_path / "library.db")
    record_source(db, state, folder, 1, ["case.3mf"])
    fake = FakeActions()
    out = run("slicer", state, fake, 1, db_path=db)
    assert fake.names() == ["open_slicer"] and fake.calls[0][1]["path"] == [
        str(folder / "case.3mf")
    ]
    assert any("already downloaded" in p for p in out.plan)
    again = run("download", state, fake, 1, db_path=db)
    assert (
        fake.names() == ["open_slicer"] and again.results == [] and again.ok
    )  # nothing fetched, nothing failed
    # the folder is gone: the record no longer counts
    (folder / "case.3mf").unlink()
    (folder / "model.json").unlink()
    folder.rmdir()
    assert browse_run.local_copy(db, state.numbered()[0]) is None


def test_the_agent_brief_has_urls_downloaded_paths_and_model_json_provenance(
    state: BrowserState, tmp_path: Path
) -> None:
    folder = tmp_path / "dl" / "printables-case"
    folder.mkdir(parents=True)
    (folder / "case.3mf").write_bytes(b"x")
    provenance = {
        "source": {"site": "printables", "id": "3", "url": "https://x/3"},
        "license": "CC-BY",
    }
    (folder / "model.json").write_text(json.dumps(provenance), encoding="utf-8")
    db = str(tmp_path / "library.db")
    record_source(db, state, folder, 1, ["case.3mf"])
    fake = FakeActions()
    out = run(
        "agent",
        state,
        fake,
        1,
        2,
        db_path=db,
        agent_prompt="compare",
        agent_mode="tmux",
        agent_backend="claude",
    )
    call = fake.calls[0][1]
    assert (
        fake.calls[0][0] == "launch_agent"
        and call["prompt"] == "compare"
        and call["backend"] == "claude"
    )
    assert call["mode"] == "tmux" and call["cwd"] == str(
        folder.parent
    )  # where the (single) download lives
    ctx = call["context"]
    assert ctx["query"] == QUERY and ctx["session"] == state.session
    first, second = ctx["models"]
    assert (
        first["url"].endswith("/1")
        and first["dir"] == str(folder)
        and first["files"] == ["case.3mf"]
    )
    assert (
        first["provenance"] == provenance
        and first["verdict"] == "fits"
        and first["license"] == "CC-BY"
    )
    assert "dir" not in second  # not downloaded: no invented path
    assert out.ok


def test_queue_downloads_then_adds_a_queued_make_per_model(
    state: BrowserState, tmp_path: Path
) -> None:
    folder = tmp_path / "kept"
    folder.mkdir()
    (folder / "case.stl").write_bytes(b"x")
    db = str(tmp_path / "library.db")
    uid = record_source(db, state, folder, 1, ["case.stl"])
    fake = FakeActions()
    run("queue", state, fake, 1, db_path=db)
    name, kwargs = fake.calls[0]
    assert (
        name == "queue"
        and kwargs["design"] == "Soil-probe-case-1"
        and kwargs["source"] == uid
    )
    assert kwargs["source_key"] == "browse:printables:1"


def test_perform_rejects_an_unknown_action(state: BrowserState) -> None:
    with pytest.raises(InvalidArgument):
        perform(
            "teleport",
            state.numbered()[:1],
            session="S",
            query="q",
            params=ActParams(),
            invoke=FakeActions(),
        )


# --------------------------------------------------------------------------- the numbered prompt
def prompt(
    state: BrowserState,
    lines: str,
    fake: FakeActions,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    **params: Any,
) -> str:
    source = make_source(monkeypatch, str(tmp_path / "library.db"), scripted=backend())
    out = io.StringIO()
    state2 = BrowserState(query=QUERY)
    code = browse_plain.run_plain(
        state2,
        source,
        ActParams(db_path=str(tmp_path / "library.db"), **params),
        start=lambda: source.saved(str(state.session)),
        invoke=fake,
        stdin=io.StringIO(lines),
        stdout=out,
    )
    assert code == 0
    return out.getvalue()


def test_the_prompt_selects_numbers_previews_and_asks_before_it_downloads(
    state: BrowserState, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeActions(download_root=tmp_path / "dl")
    text = prompt(
        state, "1,3\np 2\nd\nn\nd 1-2\ny\nb\nq\n", fake, monkeypatch, tmp_path
    )
    ranked = state.numbered()
    assert "selected: " + f"{ranked[0].key}, {ranked[2].key}" in text
    assert (
        "description" in text and "files (1)" in text
    )  # `p 2` printed the preview panel
    assert (
        "download 2 model(s)" in text
        and "equivalent: 3d models act" in text
        and "cancelled" in text
    )
    names = fake.names()
    assert (
        names.count("download") == 2
        and names[0] == "preview"
        and names[-1] == "open_browser"
    )
    assert fake.calls[-1][1]["urls"] == [
        ranked[0].url,
        ranked[2].url,
    ]  # `b` used the selection that was still there


def test_the_prompt_answers_questions_and_runs_followups_through_the_same_effects(
    state: BrowserState, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    text = prompt(
        state,
        "f\nanswer need_cad no\nf\nfollow 1\nrefine sensor enclosure\nfilter plant\nx 7\nq\n",
        FakeActions(),
        monkeypatch,
        tmp_path,
    )
    assert (
        "question need_cad (single): Do you need CAD files?" in text
        and "follow-up 1 [suggested]" in text
    )
    assert "= no" in text  # the answer was saved and shown after the re-rank
    assert "follow-up 1 [run]" not in text or "[run]" in text
    assert "unknown command 'x'" in text and "give row numbers" not in text
    assert "Plant stake sensor holder" in text  # the follow-up's hit joined the list


def test_the_prompt_reports_a_failing_stream_and_keeps_reading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Broken:
        def search(self, query: str, *, parent: str | None = None) -> Any:
            raise OSError("no network")
            yield {}

        def session(self, ref: str, **kw: Any) -> Any:
            raise OSError("no network")
            yield {}

        def saved(self, ref: str) -> Any:
            raise OSError("no library")
            yield {}

    out = io.StringIO()
    code = browse_plain.run_plain(
        BrowserState(),
        Broken(),
        ActParams(),
        start=lambda: Broken().saved("x"),
        stdin=io.StringIO("l\nd\nq\n"),
        stdout=out,
    )
    assert code == 0
    text = out.getvalue()
    assert "error: no library" in text and "nothing to act on" in text


# --------------------------------------------------------------------------- the flag layer
def test_act_params_validate_the_backend_and_mode_and_default_by_front_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    import sys

    tui = browsecli.act_params({}, dry_run=False, tui=True)
    cli = browsecli.act_params({}, dry_run=True, tui=False)
    assert (tui.agent_mode, cli.agent_mode, cli.image, cli.dry_run) == (
        "attach",
        "auto",
        None,
        True,
    )
    assert tui.image is None  # stdout is not a terminal here: no picture
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    for var in (
        "THREED_IMAGE_PROTOCOL",
        "KITTY_WINDOW_ID",
        "TERM_PROGRAM",
        "LC_TERMINAL",
        "TMUX",
        "STY",
        "NO_COLOR",
    ):
        monkeypatch.delenv(var, raising=False)
    assert browsecli.act_params({}, dry_run=False, tui=True).image == "blocks"
    monkeypatch.setenv("THREED_IMAGE_PROTOCOL", "kitty")
    assert (
        browsecli.act_params({"--images": "auto"}, dry_run=False, tui=True).image
        == "kitty"
    )
    assert (
        browsecli.act_params({"--images": "none"}, dry_run=False, tui=True).image
        is None
    )
    assert (
        browsecli.act_params({"--images": "kitty"}, dry_run=False, tui=False).image
        == "kitty"
    )
    for bad in ({"--agent": "vim"}, {"--agent-mode": "telepathy"}):
        with pytest.raises(InvalidArgument):
            browsecli.act_params(bad, dry_run=False, tui=False)


def test_a_full_screen_ui_needs_a_terminal_on_both_ends_and_no_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    monkeypatch.delenv("THREED_NO_TUI", raising=False)
    for stdin, stdout, expected in (
        (True, True, True),
        (True, False, False),
        (False, True, False),
    ):
        monkeypatch.setattr(sys.stdin, "isatty", lambda v=stdin: v, raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda v=stdout: v, raising=False)
        assert browsecli.interactive() is expected
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setenv("THREED_NO_TUI", "1")
    assert browsecli.interactive() is False


def test_act_names_a_missing_search_and_refuses_two_ids(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="no saved search"):
        browsecli.act([], {"--db": str(tmp_path / "none.db")}, {})
    with pytest.raises(UsageError, match="at most one"):
        browsecli.act(["a", "b"], {}, {})
    with pytest.raises(UsageError, match="at most one"):
        browsecli.browse(["a", "b"], {}, {})


def test_missing_textual_is_a_127_with_the_install_line(
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
    assert exc.value.exit_code == 127 and "uv sync --extra tui" in exc.value.render()
