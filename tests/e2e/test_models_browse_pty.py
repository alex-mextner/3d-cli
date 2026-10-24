"""The browser on a real pseudo-terminal (#578): `bin/3d models search ... --tui` and `models browse` run under a pty,
a terminal emulator (pyte) turns what they draw into text, and keys are typed when their prompt is on the screen.
The sites are loopback fakes and the model is a fake `omp`; no real network, model, slicer or agent."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from llm_fixtures import write_fake_omp
from marketplace_fixtures import write_fake_curl
from pty_helper import run_pty
from search_fixtures import BulkServer

from .test_models_rank_workflows import PLAN
from .workflow_helper import THREED, isolated_env

pytest.importorskip("textual")
pytest.importorskip("pyte")

DOWN, UP, ENTER, ESC = "\x1b[B", "\x1b[A", "\r", "\x1b"


def make_env(server: BulkServer, tmp_path: Path, **extra: str) -> dict[str, str]:
    omp, _ = write_fake_omp(
        tmp_path, {"plan": PLAN, "fits_mod": 3, "delay_classify": 1.0}
    )
    binary, _ = write_fake_curl(tmp_path / "curl-bin")
    env = isolated_env(tmp_path)
    env.update(
        {
            **server.env(),
            "THREED_CURL_IMPERSONATE": str(binary),
            "THREED_KEYCHAIN": "off",
            "THREED_OMP": str(omp),
            "THREED_LIBRARY_DB": str(tmp_path / "library.db"),
            "TERM": "xterm-256color",
            "THREED_IMAGE_PROTOCOL": "none",
            **extra,
        }
    )
    env.pop("THREED_NO_TUI", None)
    return env


def sessions(tmp_path: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(tmp_path / "library.db")
    try:
        return conn.execute(
            "SELECT query, kind FROM search_session ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_search_tui_streams_results_then_verdicts_and_every_panel_opens(
    tmp_path: Path,
) -> None:
    with BulkServer(total=60) as server:
        env = make_env(server, tmp_path)
        run = run_pty(
            [
                sys.executable,
                str(THREED),
                "models",
                "search",
                "case",
                "--site",
                "printables",
                "--limit",
                "10",
                "--tui",
            ],
            env=env,
            cwd=str(tmp_path),
            script=[
                (r"done: 10 results", " "),  # select the first row
                (r"1 selected", "?"),
                (r"Keys", "x"),  # any key closes the help
                (r"1 selected", "f"),
                (r"Follow-ups and questions", ESC),
                (r"\[x\]", "p"),
                (r"files \(", ESC),
                (r"\[x\]", "q"),
            ],
            size=(120, 40),
            timeout=90,
        )
    shots = "\n=====\n".join(run.screens)
    assert run.exit_code == 0, shots
    assert (
        "1 selected" in shots and "launch an agent" in shots
    )  # the help listed the agent key
    assert (
        "? need_cad (single): Do you need CAD files?" in shots
        and "follow-up 1 [suggested]" in shots
    )
    assert "files (" in shots and "description" in shots  # the preview panel
    assert sessions(tmp_path)[0] == ("case", "user")


def test_the_unjudged_list_is_on_screen_while_the_model_is_still_thinking(
    tmp_path: Path,
) -> None:
    with BulkServer(total=60) as server:
        env = make_env(server, tmp_path)
        run = run_pty(
            [
                sys.executable,
                str(THREED),
                "models",
                "search",
                "case",
                "--site",
                "printables",
                "--limit",
                "10",
                "--tui",
            ],
            env=env,
            cwd=str(tmp_path),
            script=[(r"10 results", ""), (r"done: 10 results", "q")],
            size=(100, 30),
            timeout=90,
        )
    early, late = run.screens[0], run.screens[1]
    assert run.exit_code == 0, late
    assert (
        "judging 10 results" in early
        and "Printables model 1" in early
        and "fits" not in early.split("\n", 1)[1]
    )
    assert (
        "fits  0.90" in late and "judging" not in late
    )  # the verdicts arrived on the same screen


def test_without_the_tui_extra_on_a_terminal_it_is_exit_127_with_the_install_line(
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "blocker"
    blocker.mkdir()
    (blocker / "sitecustomize.py").write_text(
        "import sys\nsys.modules['textual'] = None\n", encoding="utf-8"
    )
    with BulkServer(total=60) as server:
        env = make_env(server, tmp_path, PYTHONPATH=str(blocker))
        run = run_pty(
            [
                sys.executable,
                str(THREED),
                "models",
                "search",
                "case",
                "--site",
                "printables",
                "--tui",
            ],
            env=env,
            cwd=str(tmp_path),
            size=(100, 30),
            timeout=60,
        )
        plain = run_pty(
            [sys.executable, str(THREED), "models", "browse", "latest"],
            env=make_env(server, tmp_path, PYTHONPATH=str(blocker), THREED_NO_TUI="1"),
            cwd=str(tmp_path),
            size=(100, 30),
            timeout=60,
        )
    assert run.exit_code == 127, run.final
    assert "textual" in run.final and "uv sync --extra tui" in run.final
    assert (
        not (tmp_path / "library.db").exists() or sessions(tmp_path) == []
    )  # failed before any search ran
    assert (
        plain.exit_code == 2 and "no saved search" in plain.final
    )  # THREED_NO_TUI: no UI, and a clear error


def test_browse_resumes_a_saved_search_on_a_terminal_without_any_model_call(
    tmp_path: Path,
) -> None:
    with BulkServer(total=60) as server:
        env = make_env(server, tmp_path)
        seed = run_pty(
            [
                sys.executable,
                str(THREED),
                "models",
                "search",
                "case",
                "--site",
                "printables",
                "--limit",
                "10",
                "--rank",
                "--ndjson",
            ],
            env=env,
            cwd=str(tmp_path),
            timeout=90,
        )
        assert seed.exit_code == 0
        no_omp = {**env, "THREED_OMP": str(tmp_path / "does-not-exist")}
        run = run_pty(
            [sys.executable, str(THREED), "models", "browse", "latest"],
            env=no_omp,
            cwd=str(tmp_path),
            script=[
                (r"done: 10 results", DOWN),
                (r"fits", " "),
                (r"1 selected", "a"),
                (r"agent 1 model\?", "n"),
                (r"cancelled", "q"),
            ],
            size=(120, 40),
            timeout=60,
        )
    shots = "\n=====\n".join(run.screens)
    assert run.exit_code == 0, shots
    assert "fits  0.90" in shots and "agent 1 model?" in shots
    assert "equivalent: 3d models act" in shots and "--do agent --yes" in shots
    assert "cancelled" in shots


def test_a_small_terminal_keeps_the_status_and_key_lines_on_screen(
    tmp_path: Path,
) -> None:
    with BulkServer(total=60) as server:
        env = make_env(server, tmp_path)
        run = run_pty(
            [
                sys.executable,
                str(THREED),
                "models",
                "search",
                "case",
                "--site",
                "printables",
                "--limit",
                "10",
                "--tui",
                "--no-llm",
            ],
            env=env,
            cwd=str(tmp_path),
            script=[(r"10 results", "q")],
            size=(80, 24),
            timeout=60,
        )
    assert run.exit_code == 0
    lines = run.screens[0].splitlines()
    assert len(lines) <= 24 and max(len(line) for line in lines) <= 80
    assert "done:" in "\n".join(lines[-2:]) and "help" in lines[-1]
