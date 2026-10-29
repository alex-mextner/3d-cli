"""Every browser key has a flag twin that returns the SAME `3d.action/1` result (#578). Here one saved search is
acted on twice: with `bin/3d models act ... --yes --json` and with real key presses in the Textual app (headless
Pilot, the real `library.actions`, the same fake slicer / browser / agent executables). The two results of each
action are compared after the only things that may differ are masked: the download folder, the model_source uid
and the process id. No real network (loopback fakes), slicer, browser or agent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fake_programs import calls
from search_fixtures import BulkServer

from .test_models_browse_workflows import World, doc, wait_for
from .workflow_helper import isolated_env

pytest.importorskip("textual")
from library import rankflow  # noqa: E402
from library import search as searchcore  # noqa: E402
from library.browse_run import ActParams, PipelineSource  # noqa: E402
from library.browse_state import BrowserState  # noqa: E402
from library.llm import LlmConfig  # noqa: E402
from tui_harness import Harness, drive  # noqa: E402


def mask(result: dict[str, Any], folders: list[Path], uids: list[str]) -> str:
    """The result as stable text: folders, uids and pids replaced by placeholders."""
    text = json.dumps(result, sort_keys=True, indent=1)
    for n, folder in enumerate(folders):
        text = text.replace(str(folder), f"<dir{n}>")
    for uid in uids:
        text = text.replace(uid, "<uid>")
    return text


def keys_pressed(
    world: World, monkeypatch: pytest.MonkeyPatch, params: ActParams, *keys: str
) -> list[dict[str, Any]]:
    """Open the saved search in the Textual app (real actions) and press `keys`; returns the action results."""
    from library import actions

    # the in-process run sees the environment the CLI runs got: fake omp/tmux first on PATH, isolated HOME and XDG
    for name, value in {**isolated_env(world.tmp), **world.env}.items():
        monkeypatch.setenv(name, value)
    source = PipelineSource(
        rankflow.RankOptions(LlmConfig(), enabled=False),
        searchcore.SearchOptions(db_path=str(world.tmp / "library-tui.db")),
    )
    # the TUI reads the saved search from the CLI's library, and records its own downloads in a second one
    reader = PipelineSource(
        source.rank, searchcore.SearchOptions(db_path=str(world.tmp / "library.db"))
    )
    out: list[dict[str, Any]] = []  # 3d.action/1 documents

    async def scenario(h: Harness) -> None:
        await h.until(lambda: h.state.phase == "done")
        await h.press(*keys)
        await h.app.settled(timeout=20)
        out.extend(dict(r) for o in h.app.outcomes for r in o.results)

    drive(
        scenario,
        state=BrowserState(query="case"),
        source=source,
        start=lambda: reader.saved(world.uid),
        invoke=actions.invoke,
        params=params,
    )
    return out


@pytest.fixture
def world(tmp_path: Path) -> Any:
    with BulkServer(total=60) as server:
        yield World(tmp_path, server)


def test_the_key_and_its_flag_twin_return_the_same_action_result(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cli_dir, tui_dir = tmp_path / "cli-parts", tmp_path / "tui-parts"
    slicer = f"{world.slicer} --open {{paths}}"
    browser = f"{world.browser} --tab {{url}}"
    base = ["--select", "1", "--yes", "--json"]

    # --- the CLI side: preview, open, download, slicer, agent
    cli_preview = doc(world.act("--select", "1", "--do", "preview", "--json"))[
        "results"
    ][0]
    cli_open = doc(world.act(*base, "--do", "open", "--browser", browser))["results"][0]
    cli_download = doc(world.act(*base, "--do", "download", "--dir", str(cli_dir)))[
        "results"
    ][0]
    cli_slicer = doc(
        world.act(*base, "--do", "slicer", "--slicer", slicer, "--dir", str(cli_dir))
    )["results"][0]
    cli_agent = doc(
        world.act(
            *base,
            "--do",
            "agent",
            "--agent-mode",
            "detached",
            "--prompt",
            "judge",
            "--dir",
            str(cli_dir),
        )
    )["results"][0]

    # --- the key side: the same selection (the top row), the same settings, different folders / library
    params = ActParams(
        db_path=str(tmp_path / "library-tui.db"),
        dest_dir=str(tui_dir),
        slicer=slicer,
        browser=browser,
        agent_mode="detached",
        agent_prompt="judge",
        image=None,
    )
    results = keys_pressed(
        world, monkeypatch, params, "p", "escape", "b", "d", "y", "s", "y", "a", "y"
    )
    by_action = {r["action"]: r for r in results}
    assert [r["action"] for r in results] == [
        "preview",
        "open_browser",
        "download",
        "open_slicer",
        "launch_agent",
    ]

    cli_folder = Path(cli_download["data"]["dir"])
    tui_folder = Path(by_action["download"]["data"]["dir"])
    assert (
        cli_folder.parent == cli_dir
        and tui_folder.parent == tui_dir
        and cli_folder.name == tui_folder.name
    )
    uids = [cli_download["data"]["source"], by_action["download"]["data"]["source"]]
    folders = [cli_folder, tui_folder]
    pairs = {
        "preview": (cli_preview, by_action["preview"]),
        "open_browser": (cli_open, by_action["open_browser"]),
        "download": (cli_download, by_action["download"]),
        "open_slicer": (cli_slicer, by_action["open_slicer"]),
        "launch_agent": (cli_agent, by_action["launch_agent"]),
    }
    for name, (from_cli, from_key) in pairs.items():
        for r in (from_cli, from_key):
            assert (
                r["schema"] == "3d.action/1" and r["ok"] is True and r["action"] == name
            )
        a = json.loads(mask(from_cli, [cli_folder, cli_dir], uids))
        b = json.loads(mask(from_key, [tui_folder, tui_dir], uids))
        for side in (a, b):  # a process id is the one thing two runs never share
            if "pid" in side["data"]:
                assert side["data"]["pid"] > 0
                side["data"]["pid"] = "<pid>"
        assert a == b, name
    assert len(folders) == 2

    # both sides really started the same programs
    argv_slicer = [c["argv"][:2] for c in wait_for(world.slicer_log, 2)]
    assert argv_slicer == [["fake-slicer", "--open"]] * 2
    assert (
        len(calls(world.browser_log)) == 2 or len(wait_for(world.browser_log, 2)) == 2
    )
