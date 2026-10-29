"""Readable e2e workflows for `3d models act` / `browse` (#578): the real bin/3d against the loopback fakes of the
sites (tests/search_fixtures.py), a FAKE `omp` that ranks (tests/llm_fixtures.py) and FAKE slicer / browser / agent
executables that record their argv. An agent drives search -> read verdicts -> answer -> select -> download ->
open in the slicer -> launch an agent without any terminal; every browser key has its flag twin and returns the same
`3d.action/1` results as the key. No real site, model, slicer, browser or agent is ever started."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from fake_programs import calls, fake_program
from llm_fixtures import write_fake_omp
from marketplace_fixtures import write_fake_curl
from search_fixtures import BulkServer

from .test_models_rank_workflows import PLAN
from .workflow_helper import agent_spawns, isolated_env, run_cli


def _env(server: BulkServer, tmp_path: Path, omp: Path, **extra: str) -> dict[str, str]:
    binary, _ = write_fake_curl(tmp_path / "curl-bin")
    return {
        **server.env(),
        "THREED_CURL_IMPERSONATE": str(binary),
        "THREED_KEYCHAIN": "off",
        "THREED_OMP": str(omp),
        "THREED_LIBRARY_DB": str(tmp_path / "library.db"),
        **extra,
    }


def run(
    tmp_path: Path, env: dict[str, str], *args: str, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return run_cli(tmp_path, "models", *args, env_extra=env, cwd=cwd)


def doc(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert proc.stdout.strip(), proc.stderr
    out = json.loads(proc.stdout)
    assert isinstance(out, dict)
    return out


class World:
    """A seeded library: one ranked search of 10 printables models, fake program binaries, the env to run them."""

    def __init__(self, tmp_path: Path, server: BulkServer) -> None:
        self.tmp = tmp_path
        omp, self.omp_state = write_fake_omp(tmp_path, {"plan": PLAN, "fits_mod": 3})
        self.slicer, self.slicer_log = fake_program(tmp_path / "fakes", "fake-slicer")
        self.browser, self.browser_log = fake_program(
            tmp_path / "fakes", "fake-browser"
        )
        self.env = _env(server, tmp_path, omp)
        first = run(
            tmp_path,
            self.env,
            "search",
            "case",
            "--site",
            "printables",
            "--limit",
            "10",
            "--rank",
            "--ndjson",
        )
        assert first.returncode == 0, first.stderr
        events = [json.loads(line) for line in first.stdout.splitlines()]
        self.uid = str(events[-1]["search"])

    def act(self, *args: str) -> subprocess.CompletedProcess[str]:
        return run(self.tmp, self.env, "act", self.uid, *args)

    def listing(self, *args: str) -> dict[str, Any]:
        return doc(self.act("--do", "list", "--json", *args))


@pytest.fixture
def world(tmp_path: Path) -> Any:
    with BulkServer(total=60) as server:
        yield World(tmp_path, server)


def sessions(world: World, kind: str) -> list[tuple[str, str]]:
    """`(query, parent uid)` of the child searches of this kind saved in the library DB."""
    conn = sqlite3.connect(world.tmp / "library.db")
    try:
        return conn.execute(
            "SELECT s.query, p.uid FROM search_session s JOIN search_session p ON p.id = s.parent_id "
            "WHERE s.kind = ? ORDER BY s.id",
            (kind,),
        ).fetchall()
    finally:
        conn.close()


def wait_for(log: Path, count: int = 1) -> list[dict[str, Any]]:
    end = time.monotonic() + 10
    while time.monotonic() < end:
        if len(calls(log)) >= count:
            return calls(log)
        time.sleep(0.05)
    raise AssertionError(f"{log.name}: {len(calls(log))} calls, wanted {count}")


# --------------------------------------------------------------------------- reading the verdicts
def test_an_agent_reads_the_ranked_list_with_numbers_verdicts_questions_and_followups(
    world: World,
) -> None:
    """An agent runs act --do list and reads the ranked rows with numbers, verdicts, questions and follow-ups."""
    listing = world.listing()
    assert (
        listing["schema"] == "3d.models.act/1"
        and listing["action"] == "list"
        and listing["ok"] is True
    )
    assert listing["search"] == world.uid and listing["query"] == "case"
    rows = listing["results"]
    assert [r["n"] for r in rows] == list(range(1, len(rows) + 1)) and len(rows) >= 10
    assert (
        rows[0]["verdict"] == "fits"
        and rows[0]["rationale"]
        and rows[0]["key"].startswith("printables:")
    )
    assert {r["verdict"] for r in rows} == {"fits", "maybe", "no"}
    order = ["fits", "maybe", "no"]
    assert [r["verdict"] for r in rows] == sorted(
        (r["verdict"] for r in rows), key=order.index
    )  # the ranking order
    assert {q["key"] for q in listing["questions"]} == {"need_cad", "max_size"}
    assert [(f["n"], f["query"]) for f in listing["followups"]] == [
        (1, "plant stake holder")
    ]
    assert listing["counts"]["fits"] == sum(r["verdict"] == "fits" for r in rows)


def test_selectors_combine_and_a_filter_narrows_the_numbers_stay_the_rankings(
    world: World,
) -> None:
    """A user combines selectors and a filter: only matching rows stay, numbered as in the ranking."""
    everything = world.listing()["results"]
    fits = world.listing("--select-verdict", "fits")["results"]
    assert fits and all(r["verdict"] == "fits" for r in fits)
    assert [r["n"] for r in fits] == [
        r["n"] for r in everything if r["verdict"] == "fits"
    ]  # numbers did not shift
    pair = world.listing("--select", "2-3")["results"]
    assert [r["n"] for r in pair] == [2, 3]
    one = world.listing("--select", everything[4]["key"])["results"]
    assert [r["n"] for r in one] == [5]
    both = world.listing("--select-site", "printables", "--select-verdict", "no")[
        "results"
    ]
    assert both and all(r["verdict"] == "no" for r in both)
    titled = world.listing("--filter", everything[0]["title"])["results"]
    assert [r["title"] for r in titled] == [everything[0]["title"]]
    text = world.act("--do", "list")
    assert (
        text.returncode == 0
        and "question need_cad (single)" in text.stdout
        and "follow-up 1 [suggested]" in text.stdout
    )


# --------------------------------------------------------------------------- dry run by default
def test_nothing_is_downloaded_or_started_without_yes_and_the_plan_names_the_command(
    world: World, tmp_path: Path
) -> None:
    """A user runs an action without --yes: nothing starts and the plan names the command it would run."""
    dest = tmp_path / "dl"
    dry = doc(
        world.act("--select", "1,2", "--do", "download", "--dir", str(dest), "--json")
    )
    assert (
        dry["dry_run"] is True
        and dry["results"] == []
        and dry["ok"] is True
        and dry["exit_code"] == 0
    )
    assert [p.startswith("would download") for p in dry["plan"]] == [True, True]
    assert dry["cli"].endswith(
        "--do download --yes --dir "
        + str(dest)
        + " --select "
        + ",".join(s["key"] for s in dry["selected"])
    )
    assert not dest.exists()
    for do in ("slicer", "open", "agent"):
        out = world.act(
            "--select",
            "1",
            "--do",
            do,
            "--dir",
            str(dest),
            "--agent-mode",
            "detached",
            "--slicer",
            str(world.slicer),
            "--browser",
            f"{world.browser} {{url}}",
        )
        assert out.returncode == 0, (do, out.stderr)
        assert "dry run: nothing was started" in out.stdout or "(dry run)" in out.stdout
    assert (
        not dest.exists()
        and calls(world.slicer_log) == []
        and calls(world.browser_log) == []
    )
    assert agent_spawns(tmp_path) == []


# --------------------------------------------------------------------------- download -> slicer -> agent
def _download_pick(world: World, pick: dict[str, Any], dest: Path) -> Path:
    args = ("--select", str(pick["n"]), "--do", "download", "--dir", str(dest), "--yes", "--json")
    got = doc(world.act(*args))
    assert got["ok"] is True and got["exit_code"] == 0 and got["dry_run"] is False
    result = got["results"][0]
    assert result["schema"] == "3d.action/1"
    assert result["action"] == "download" and result["ok"] is True
    folder = Path(result["data"]["dir"])
    assert folder.parent == dest and (folder / "model.json").is_file()
    assert result["data"]["files"]
    assert result["data"]["source"]  # recorded as a model_source in the library DB
    again = doc(world.act(*args))
    assert again["results"] == []
    assert any("already downloaded" in p for p in again["plan"])  # never overwritten
    shown = world.listing("--select", str(pick["n"]))["results"][0]
    assert shown["downloaded"] == str(folder)
    return folder


def _open_in_slicer(world: World, pick: dict[str, Any], dest: Path, folder: Path) -> None:
    sliced = doc(
        world.act(
            "--select",
            str(pick["n"]),
            "--do",
            "slicer",
            "--slicer",
            f"{world.slicer} --open {{paths}}",
            "--yes",
            "--json",
            "--dir",
            str(dest),
        )
    )
    assert [r["action"] for r in sliced["results"]] == ["open_slicer"]
    assert sliced["exit_code"] == 0
    started = sliced["results"][0]["data"]
    assert started["started"] is True and started["slicer"] == "custom"
    ran = wait_for(world.slicer_log)[0]
    assert ran["argv"][:2] == ["fake-slicer", "--open"]
    assert ran["argv"][2].startswith(str(folder))
    assert ran["argv"][2].endswith((".stl", ".3mf"))


def _launch_agent(
    world: World, tmp_path: Path, pick: dict[str, Any], dest: Path, folder: Path
) -> None:
    agent = doc(
        world.act(
            "--select",
            str(pick["n"]),
            "--do",
            "agent",
            "--agent-mode",
            "tmux",
            "--prompt",
            "judge this model",
            "--yes",
            "--json",
            "--dir",
            str(dest),
        )
    )
    launched = agent["results"][0]
    assert launched["action"] == "launch_agent" and launched["ok"]
    assert launched["data"]["backend"] == "omp"
    new_session = next(
        s["argv"]
        for s in agent_spawns(tmp_path)
        if s["argv"][:2] == ["tmux", "new-session"]
    )
    assert launched["data"]["tmux"] in new_session
    assert new_session[new_session.index("-c") + 1] == str(dest.resolve())
    brief_path = Path(launched["data"]["brief"])
    brief = brief_path.read_text(encoding="utf-8")
    assert "Original query: case" in brief and "Task: judge this model" in brief
    assert pick["url"] in brief and f"downloaded to: {folder}" in brief
    assert '"license"' in brief or "provenance" in brief
    assert str(brief_path) in new_session[-1]
    assert "judge this model" in new_session[-1]


def test_download_then_slicer_then_agent_is_the_documented_agent_workflow(
    world: World, tmp_path: Path
) -> None:
    """An agent downloads the picks, opens them in the slicer and then launches an agent, as the docs describe."""
    dest = tmp_path / "dl"
    rows = world.listing("--select-verdict", "fits")["results"]
    pick = rows[0]
    folder = _download_pick(world, pick, dest)
    assert folder.is_dir()
    _open_in_slicer(world, pick, dest, folder)
    _launch_agent(world, tmp_path, pick, dest, folder)


def test_open_starts_the_configured_browser_for_every_selected_page(
    world: World,
) -> None:
    """A user opens the selected pages: the configured browser starts once per page."""
    urls = [r["url"] for r in world.listing("--select", "1,2")["results"]]
    out = doc(
        world.act(
            "--select",
            "1,2",
            "--do",
            "browser",
            "--browser",
            f"{world.browser} --tab {{url}}",
            "--yes",
            "--json",
        )
    )
    assert (
        out["results"][0]["data"]["urls"] == urls
        and out["results"][0]["data"]["started"] is True
    )
    ran = wait_for(world.browser_log, 2)
    assert sorted(r["argv"][2] for r in ran) == sorted(urls) and {
        r["argv"][1] for r in ran
    } == {"--tab"}


def test_preview_is_read_only_and_needs_no_yes(world: World) -> None:
    """A user previews rows: nothing is written and --yes is not needed."""
    out = doc(world.act("--select", "1", "--do", "preview", "--json"))
    result = out["results"][0]
    assert (
        result["action"] == "preview"
        and result["ok"]
        and result["data"]["files"]
        and result["data"]["verdict"] == "fits"
    )
    assert (
        result["data"]["image"]["sequence"] is None
    )  # no picture unless --images asks for one
    assert (
        doc(world.act("--select", "1", "--do", "preview-url", "--json"))["action"]
        == "preview"
    )


# --------------------------------------------------------------------------- answers, follow-ups, refinements
def test_answer_and_follow_twins_rerank_and_persist_under_the_same_root(
    world: World,
) -> None:
    """A user answers a question and follows a query through act: it behaves like rerank and saves under the same search root."""
    streamed = world.act(
        "--answer", "need_cad=yes", "--follow", "1", "--do", "list", "--ndjson"
    )
    assert streamed.returncode == 0, streamed.stderr
    lines = [json.loads(line) for line in streamed.stdout.splitlines()]
    events = [e for e in lines if e.get("schema") == "3d.models.rank/1"]
    assert (
        events[0]["event"] == "start"
        and events[0]["answers"]["need_cad"] == "yes"
        and events[-1]["event"] == "done"
    )
    assert [
        (e["query"], e["kind"])
        for e in events
        if e["event"] == "query" and e["status"] == "run"
    ][-1] == ("plant stake holder", "followup")
    final = lines[-1]
    assert final["schema"] == "3d.models.act/1" and final["action"] == "list"
    assert (
        next(q for q in final["questions"] if q["key"] == "need_cad")["answer"] == "yes"
    )
    assert [(f["n"], f["status"]) for f in final["followups"]] == [(1, "run")]
    # the fake site answers every query with the same models, so the follow-up adds sessions, not new rows
    assert sessions(world, "followup") == [("plant stake holder", world.uid)]
    bad = world.act("--answer", "need_cad=perhaps", "--do", "list")
    assert bad.returncode == 2 and "accepted: yes, no" in bad.stderr


def test_refine_searches_again_under_the_root_and_merges_the_hits(world: World) -> None:
    """A user refines a saved search: a child search runs under the same root and its hits merge in."""
    out = world.act("--refine", "probe holder", "--do", "list", "--ndjson")
    assert out.returncode == 0, out.stderr
    final = json.loads(out.stdout.splitlines()[-1])
    assert final["search"] == world.uid and final["action"] == "list"
    assert sessions(world, "user") == [("probe holder", world.uid)]


# --------------------------------------------------------------------------- exit codes and errors
def test_exit_codes_follow_the_repo_convention(world: World, tmp_path: Path) -> None:
    """A user makes mistakes with act and browse: bad usage exits 2 and a missing program exits 127."""
    missing_library = run_cli(
        tmp_path / "other",
        "models",
        "act",
        "--do",
        "list",
        env_extra={"THREED_LIBRARY_DB": str(tmp_path / "other" / "x.db")},
    )
    assert (
        missing_library.returncode == 2 and "no saved search" in missing_library.stderr
    )
    assert world.act("--select", "99", "--do", "preview").returncode == 2
    assert "accepted: 1.." in world.act("--select", "99", "--do", "preview").stderr
    assert world.act("--do", "preview").returncode == 2  # nothing selected
    assert world.act("--select", "1", "--do", "teleport").returncode == 2
    assert world.act("--select", "1", "--do", "agent", "--agent", "vim").returncode == 2
    assert (
        world.act("--select", "1", "--do", "list", "--json", "--ndjson").returncode == 2
    )
    assert world.act("--select-verdict", "great", "--do", "list").returncode == 2
    # no slicer installed: 127 with the setting that fixes it, and the download that came first is not lost
    nothing = run_cli(
        tmp_path,
        "models",
        "act",
        world.uid,
        "--select",
        "1",
        "--do",
        "slicer",
        "--yes",
        "--dir",
        str(tmp_path / "dl2"),
        "--json",
        env_extra={**world.env, "THREED_SLICER": "no-such-slicer"},
    )
    out = json.loads(nothing.stdout)
    assert nothing.returncode == 127 and out["exit_code"] == 127 and out["ok"] is False
    assert out["results"][-1]["errors"][0]["code"] == "missing_dependency"
    assert "THREED_SLICER" in out["results"][-1]["errors"][0]["remediation"][0]
    assert out["results"][0]["action"] == "download" and out["results"][0]["ok"] is True


# --------------------------------------------------------------------------- the interactive browser without a terminal
def test_browse_without_a_terminal_is_a_numbered_prompt_driven_by_stdin(
    world: World, tmp_path: Path
) -> None:
    """A user pipes commands into browse without a terminal: the numbered prompt answers them."""
    env = isolated_env(tmp_path) | world.env
    proc = subprocess.run(
        [
            "python3",
            str(Path(__file__).resolve().parents[2] / "bin" / "3d"),
            "models",
            "browse",
            world.uid,
            "--images=none",
            "--browser",
            f"{world.browser} {{url}}",
        ],
        input="1,2\nf\nb\nanswer need_cad no\nq\n",
        env=env,
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "search " + world.uid in out and "fits" in out and "selected:" in out
    assert "question need_cad (single)" in out and "follow-up 1 [suggested]" in out
    wait_for(world.browser_log, 2)  # `b` opened both selected pages
    assert "need_cad" in out and ("re-ranked" in out or "fits" in out)


def test_search_tui_without_a_terminal_prints_exactly_the_normal_search(
    world: World, tmp_path: Path
) -> None:
    """Piped output is unchanged by --tui (and THREED_NO_TUI=1 never starts the UI either)."""
    plain = run(
        tmp_path,
        world.env,
        "search",
        "case",
        "--site",
        "printables",
        "--limit",
        "5",
        "--no-save",
        "--no-llm",
    )
    tui = run(
        tmp_path,
        world.env,
        "search",
        "case",
        "--site",
        "printables",
        "--limit",
        "5",
        "--no-save",
        "--no-llm",
        "--tui",
    )
    no_tui = run(
        tmp_path,
        {**world.env, "THREED_NO_TUI": "1"},
        "search",
        "case",
        "--site",
        "printables",
        "--limit",
        "5",
        "--no-save",
        "--no-llm",
        "--tui",
    )

    def strip(out: str) -> str:
        return "\n".join(
            line for line in out.splitlines() if not line.startswith("printables (")
        )

    assert plain.returncode == tui.returncode == no_tui.returncode == 0
    assert strip(plain.stdout) == strip(tui.stdout) == strip(no_tui.stdout)


def test_the_help_and_the_docs_name_every_new_flag_with_an_example(
    tmp_path: Path,
) -> None:
    """A user reads the help and the docs: every new flag is named with an example."""
    out = run_cli(tmp_path, "models", "--help").stdout
    for flag in (
        "--tui",
        "--plain",
        "--select",
        "--select-site",
        "--select-verdict",
        "--do",
        "--yes",
        "--slicer",
        "--browser",
        "--agent",
        "--agent-mode",
        "--prompt",
        "--agent-cwd",
        "--refine",
        "--filter",
        "--session",
    ):
        assert flag in out, flag
    assert "browse [SEARCH_ID]" in out and "act [SEARCH_ID]" in out
