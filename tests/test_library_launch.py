"""The programs behind the browser's keys (#578): which slicer / browser / agent is started and with which argv.
Everything runs against FAKE executables that record their argv (`browse_fixtures.fake_program`); no real slicer,
browser, agent or tmux is ever started, and nothing is configured from the host's own files."""

from __future__ import annotations

import json
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fake_programs import calls, fake_program

from cli import termimage
from errors import InvalidArgument, MissingDependency
from library import actions, launch


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A PATH holding only the fakes of this test, and a config/state dir of its own."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for var in (
        "THREED_SLICER",
        "THREED_BROWSER",
        "THREED_AGENT",
        "BROWSER",
        "THREED_BROWSER_CONFIG",
    ):
        monkeypatch.delenv(var, raising=False)
    return bindir


def wait_for(log: Path, count: int = 1, timeout: float = 10.0) -> list[dict[str, Any]]:
    """A started program runs detached: poll its log until it has `count` lines."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        seen = calls(log)
        if len(seen) >= count:
            return seen
        time.sleep(0.05)
    raise AssertionError(
        f"{log.name} has {len(calls(log))} calls after {timeout}s, wanted {count}"
    )


def model_file(tmp_path: Path, name: str = "case.3mf") -> Path:
    path = tmp_path / "dl" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"3mf")
    return path


# --------------------------------------------------------------------------- slicer discovery and launch
def test_the_slicer_is_found_in_preference_order_orca_snapmaker_creality_bambu(
    _env: Path,
) -> None:
    for name in ("bambu-studio", "CrealityPrint", "snapmaker-orca"):
        fake_program(_env, name)
    assert launch.resolve_slicer().kind == "snapmaker-orca"
    fake_program(_env, "orca-slicer")
    assert launch.resolve_slicer().kind == "orca"
    for kind in ("snapmaker-orca", "creality-print", "bambu-studio"):
        found = launch.resolve_slicer(kind)
        assert found.kind == kind and Path(found.argv[0]).is_file()


def test_a_configured_slicer_beats_detection_and_the_env_beats_the_config(
    _env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_program(_env, "orca-slicer")
    mine, _ = fake_program(_env, "my-slicer")
    other, _ = fake_program(_env, "other-slicer")
    config = tmp_path / "xdg-config" / "3d-cli" / "browser.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"slicer": "my-slicer --fresh {paths}"}), encoding="utf-8"
    )
    chosen = launch.resolve_slicer()
    assert chosen.kind == "custom" and chosen.argv == (str(mine), "--fresh", "{paths}")
    assert launch.slicer_argv(chosen, ["/a.3mf", "/b.stl"]) == [
        str(mine),
        "--fresh",
        "/a.3mf",
        "/b.stl",
    ]
    monkeypatch.setenv("THREED_SLICER", "other-slicer")
    assert launch.resolve_slicer().argv == (str(other),)
    assert (
        launch.resolve_slicer("orca").kind == "orca"
    )  # an explicit argument beats both


def test_a_slicer_command_without_a_placeholder_gets_the_files_appended_and_path_takes_the_first() -> (
    None
):
    plain = launch.Slicer("custom", ("/bin/slicer",))
    assert launch.slicer_argv(plain, ["a", "b"]) == ["/bin/slicer", "a", "b"]
    single = launch.Slicer("custom", ("/bin/slicer", "--open={path}"))
    assert launch.slicer_argv(single, ["a", "b"]) == ["/bin/slicer", "--open=a"]


def test_no_slicer_is_a_missing_dependency_that_names_the_setting() -> None:
    with pytest.raises(MissingDependency) as exc:
        launch.resolve_slicer()
    assert exc.value.exit_code == 127 and "THREED_SLICER" in "\n".join(
        exc.value.remediation
    )
    with pytest.raises(MissingDependency):
        launch.resolve_slicer("not-installed-slicer")
    with pytest.raises(MissingDependency):
        launch.resolve_slicer("orca")  # a kind that is not installed


def test_the_best_model_file_is_a_3mf_then_a_mesh_then_cad() -> None:
    assert launch.best_model_file(["a.stl", "b.3mf", "readme.md"]) == "b.3mf"
    assert launch.best_model_file(["a.STEP", "b.obj"]) == "b.obj"
    assert launch.best_model_file(["a.STEP"]) == "a.STEP"
    assert launch.best_model_file(["readme.md", "model.json"]) is None


def test_open_slicer_dry_run_starts_nothing_and_a_real_run_starts_the_configured_program(
    _env: Path, tmp_path: Path
) -> None:
    binary, log = fake_program(_env, "snapmaker-orca")
    first, second = model_file(tmp_path, "one.3mf"), model_file(tmp_path, "two.stl")
    dry = actions.open_slicer([str(first), str(second)], slicer="snapmaker-orca")
    assert (
        dry["schema"] == "3d.action/1" and dry["ok"] and dry["data"]["started"] is False
    )
    assert dry["data"]["command"] == [str(binary), str(first), str(second)]
    time.sleep(0.2)
    assert calls(log) == []
    real = actions.open_slicer(
        [str(first), str(second)], slicer="snapmaker-orca", dry_run=False
    )
    assert (
        real["data"]["started"] is True
        and real["data"]["pid"] > 0
        and real["data"]["slicer"] == "snapmaker-orca"
    )
    assert wait_for(log)[0]["argv"] == ["snapmaker-orca", str(first), str(second)]


def test_open_slicer_reports_a_missing_file_and_a_missing_slicer_as_results_through_invoke(
    _env: Path, tmp_path: Path
) -> None:
    missing = actions.open_slicer(str(tmp_path / "nope.3mf"))
    assert missing["ok"] is False and missing["errors"][0]["code"] == "not_found"
    existing = model_file(tmp_path)
    result = actions.invoke("open_slicer", path=str(existing))
    assert result["ok"] is False and result["errors"][0]["code"] == "missing_dependency"
    assert "THREED_SLICER" in result["errors"][0]["remediation"][0]
    assert (
        actions.invoke("open_slicer", path=[])["errors"][0]["code"]
        == "invalid_argument"
    )


# --------------------------------------------------------------------------- the web browser
def test_open_browser_uses_the_configured_command_with_the_url_in_place(
    _env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary, log = fake_program(_env, "my-browser")
    monkeypatch.setenv("THREED_BROWSER", "my-browser --new-window {url}")
    dry = actions.open_browser(
        ["https://www.printables.com/model/1", "https://makerworld.com/en/models/2"]
    )
    assert dry["data"]["commands"] == [
        [str(binary), "--new-window", "https://www.printables.com/model/1"],
        [str(binary), "--new-window", "https://makerworld.com/en/models/2"],
    ]
    assert dry["data"]["started"] is False and calls(log) == []
    actions.open_browser("https://www.printables.com/model/1", dry_run=False)
    assert wait_for(log)[0]["argv"] == [
        "my-browser",
        "--new-window",
        "https://www.printables.com/model/1",
    ]
    monkeypatch.delenv("THREED_BROWSER")
    monkeypatch.setenv("BROWSER", "my-browser")
    assert actions.open_browser("https://x.test/a")["data"]["commands"] == [
        [str(binary), "https://x.test/a"]
    ]


def test_open_browser_refuses_non_http_urls_and_names_a_missing_opener(
    _env: Path,
) -> None:
    with pytest.raises(InvalidArgument):
        actions.open_browser("file:///etc/passwd")
    with pytest.raises(InvalidArgument):
        actions.open_browser([])
    result = actions.invoke(
        "open_browser", urls="https://x.test/a"
    )  # no xdg-open / open on the fake PATH
    assert result["ok"] is False and result["errors"][0]["code"] == "missing_dependency"
    assert "THREED_BROWSER" in result["errors"][0]["remediation"][0]


# --------------------------------------------------------------------------- the agent
CONTEXT: dict[str, Any] = {
    "query": "soil moisture sensor probe case",
    "session": "01a-search-uid",
    "models": [
        {
            "title": "Probe case",
            "site": "printables",
            "url": "https://www.printables.com/model/1",
            "verdict": "fits",
            "rationale": "a case for a probe",
            "license": "CC-BY",
            "size_mm": [52.0, 30.0, 20.0],
            "dir": "/dl/printables-1",
            "files": ["case.3mf"],
            "provenance": {
                "source_url": "https://www.printables.com/model/1",
                "sha256": "abc",
            },
        }
    ],
}


def test_launch_agent_dry_run_reports_the_command_and_writes_nothing(
    _env: Path, tmp_path: Path
) -> None:
    binary, log = fake_program(_env, "omp")
    work = tmp_path / "work"
    work.mkdir()
    res = actions.launch_agent("", cwd=str(work), context=CONTEXT, mode="detached")
    data = res["data"]
    assert (
        res["ok"]
        and data["started"] is False
        and data["backend"] == "omp"
        and data["mode"] == "detached"
    )
    assert (
        data["command"][0] == str(binary) and data["command"][1] == f"@{data['brief']}"
    )
    assert (
        "search brief" in data["command"][2].lower()
        or data["brief"] in data["command"][2]
    )
    assert not Path(data["brief"]).exists() and calls(log) == []


def test_launch_agent_detached_writes_the_brief_and_starts_the_agent_in_the_downloads_dir(
    _env: Path, tmp_path: Path
) -> None:
    _, log = fake_program(_env, "omp")
    work = tmp_path / "work"
    work.mkdir()
    res = actions.launch_agent(
        "compare the cases",
        cwd=str(work),
        context=CONTEXT,
        dry_run=False,
        mode="detached",
    )
    assert res["ok"] and res["data"]["started"] is True and res["data"]["pid"] > 0
    ran = wait_for(log)[0]
    assert ran["cwd"] == str(work.resolve())
    brief = Path(res["data"]["brief"])
    assert ran["argv"][1] == f"@{brief}" and ran["argv"][2] == "compare the cases"
    text = brief.read_text(encoding="utf-8")
    for fragment in (
        "Original query: soil moisture sensor probe case",
        "Task: compare the cases",
        "Probe case",
        "https://www.printables.com/model/1",
        "verdict: fits",
        "why: a case for a probe",
        "licence: CC-BY",
        "size (mm): 52x30x20",
        "downloaded to: /dl/printables-1",
        "files: case.3mf",
        '"sha256": "abc"',
    ):
        assert fragment in text, fragment
    assert (
        json.loads((brief.parent / "context.json").read_text(encoding="utf-8"))
        == CONTEXT
    )
    assert str(tmp_path / "xdg-state") in str(
        brief
    )  # never inside the user's working directory
    assert not list(work.iterdir())


def test_launch_agent_in_tmux_starts_a_named_detached_session(
    _env: Path, tmp_path: Path
) -> None:
    fake_program(_env, "omp")
    log = tmp_path / "tmux.jsonl"
    tmux = _env / "tmux"
    tmux.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "sys.exit(1 if sys.argv[1] == 'has-session' else 0)\n",
        encoding="utf-8",
    )
    tmux.chmod(tmux.stat().st_mode | stat.S_IXUSR)
    res = actions.launch_agent(
        "go", cwd=str(tmp_path), context=CONTEXT, dry_run=False, mode="tmux"
    )
    assert res["ok"] and res["data"]["tmux"] == "3d-search-01a-search-uid"
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert lines[0][:2] == ["has-session", "-t"]
    new = lines[1]
    assert new[:5] == [
        "new-session",
        "-d",
        "-s",
        "3d-search-01a-search-uid",
        "-c",
    ] and new[5] == str(tmp_path.resolve())
    assert "omp" in new[6] and "brief.md" in new[6]


def test_launch_agent_attached_runs_on_this_terminal_and_reports_the_exit_code(
    _env: Path, tmp_path: Path
) -> None:
    _, log = fake_program(_env, "claude", code=3)
    res = actions.launch_agent(
        "x",
        cwd=str(tmp_path),
        backend="claude",
        context=CONTEXT,
        dry_run=False,
        mode="attach",
    )
    assert (
        res["ok"]
        and res["data"]["exit_code"] == 3
        and res["data"]["backend"] == "claude"
    )
    argv = calls(log)[0]["argv"]
    assert (
        argv[0] == "claude" and len(argv) == 2 and argv[1] == "x"
    )  # claude takes the prompt only


def test_the_agent_command_and_prompt_come_from_the_config_and_placeholders_are_filled(
    _env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary, _ = fake_program(_env, "my-agent")
    cfg = tmp_path / "browser.json"
    cfg.write_text(
        json.dumps(
            {
                "agent": {
                    "command": ["my-agent", "--cwd", "{cwd}", "--brief", "{brief}"],
                    "prompt": "read {brief}",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("THREED_BROWSER_CONFIG", str(cfg))
    res = actions.launch_agent("", cwd=str(tmp_path), context=CONTEXT, mode="detached")
    command = res["data"]["command"]
    brief = res["data"]["brief"]
    assert command == [
        str(binary),
        "--cwd",
        str(tmp_path.resolve()),
        "--brief",
        brief,
        f"read {brief}",
    ]


def test_a_missing_agent_program_and_bad_arguments_are_structured(
    _env: Path, tmp_path: Path
) -> None:
    missing = actions.invoke("launch_agent", prompt="x", cwd=str(tmp_path))
    assert (
        missing["errors"][0]["code"] == "missing_dependency"
        and "THREED_AGENT" in missing["errors"][0]["remediation"][0]
    )
    assert (
        actions.invoke("launch_agent", prompt="x", backend="vim")["errors"][0]["code"]
        == "invalid_argument"
    )
    assert (
        actions.invoke("launch_agent", prompt="x", mode="telepathy")["errors"][0][
            "code"
        ]
        == "invalid_argument"
    )
    gone = actions.launch_agent("x", cwd=str(tmp_path / "nope"))
    assert gone["ok"] is False and gone["errors"][0]["code"] == "not_found"


def test_a_bad_config_file_is_an_invalid_argument_naming_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = tmp_path / "browser.json"
    monkeypatch.setenv("THREED_BROWSER_CONFIG", str(cfg))
    assert launch.load_config() == {}
    cfg.write_text("{nope", encoding="utf-8")
    with pytest.raises(InvalidArgument) as exc:
        launch.load_config()
    assert str(cfg) in exc.value.message
    cfg.write_text("[1]", encoding="utf-8")
    with pytest.raises(InvalidArgument):
        launch.load_config()
    cfg.write_text(json.dumps({"agent": {"command": 5}}), encoding="utf-8")
    with pytest.raises(InvalidArgument):
        launch.plan_agent("", cwd=str(tmp_path), backend="omp", bundle_key="k")


def test_read_provenance_reads_model_json_and_ignores_garbage(tmp_path: Path) -> None:
    assert launch.read_provenance(tmp_path) is None
    (tmp_path / "model.json").write_text(
        '{"url": "https://x/1", "license": "CC-BY"}', encoding="utf-8"
    )
    assert launch.read_provenance(tmp_path) == {
        "url": "https://x/1",
        "license": "CC-BY",
    }
    (tmp_path / "model.json").write_text("[]", encoding="utf-8")
    assert launch.read_provenance(tmp_path) is None


# --------------------------------------------------------------------------- the preview action and terminal images
CARD: dict[str, Any] = {
    "site": "printables",
    "id": "1",
    "title": "Probe  case",
    "url": "https://www.printables.com/model/1",
    "author": "maker",
    "license": "CC-BY",
    "price": "",
    "summary": "A case\nfor a probe",
    "tags": ["case"],
    "image": "https://img.example/1.png",
    "files": [
        {
            "name": "case.3mf",
            "size": 1200,
            "class": "slicer",
            "ext": ".3mf",
            "ref": "x",
            "kind": "x",
        }
    ],
    "cad": False,
    "cad_formats": [],
    "analytics": {
        "file_count": 1,
        "formats": {".3mf": 1},
        "total_bytes": 1200,
        "reference": {"suitable": False},
        "secret": 1,
    },
}


def test_preview_returns_the_pane_content_without_a_picture_unless_asked() -> None:
    res = actions.preview({**CARD, "verdict": "fits", "score": 0.9, "rationale": "yes"})
    d = res["data"]
    assert res["ok"] and res["action"] == "preview" and d["title"] == "Probe  case"
    assert (
        d["description"] == "A case for a probe"
        and d["license"] == "CC-BY"
        and d["price"] is None
    )
    assert d["files"] == [{"name": "case.3mf", "size": 1200, "class": "slicer"}]
    assert "secret" not in d["analytics"] and d["analytics"]["file_count"] == 1
    assert (d["verdict"], d["score"]) == ("fits", 0.9)
    assert d["image"] == {
        "url": "https://img.example/1.png",
        "protocol": None,
        "sequence": None,
    }
    with pytest.raises(InvalidArgument):
        actions.preview(CARD, image="ascii-art")


@pytest.mark.parametrize("protocol", ["kitty", "iterm2", "sixel", "blocks"])
def test_preview_draws_the_picture_with_the_asked_protocol(
    monkeypatch: pytest.MonkeyPatch, protocol: str
) -> None:
    seen: list[Any] = []

    def fake_render(
        wanted: Any, proto: str, *, cols: int = 20, budget_s: float = 0
    ) -> dict[str, str]:
        seen.append((list(wanted), proto, cols))
        return {"https://img.example/1.png": f"<{proto}-picture>"}

    monkeypatch.setattr("library.images.render_images", fake_render)
    d = actions.preview(CARD, image=protocol, cols=30)["data"]["image"]
    assert d["protocol"] == protocol and d["sequence"] == f"<{protocol}-picture>"
    assert seen == [([("printables", "https://img.example/1.png")], protocol, 30)]


def test_a_picture_that_cannot_be_fetched_is_a_warning_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("library.images.render_images", lambda *a, **k: {})
    res = actions.preview(CARD, image="kitty")
    assert res["ok"] and res["data"]["image"]["sequence"] is None
    assert res["warnings"][0]["code"] == "image_unavailable"
    assert actions.preview({**CARD, "image": ""}, image="kitty")["warnings"] == []


def test_auto_means_nothing_without_a_tty_and_blocks_on_a_colour_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "library.images.render_images",
        lambda *a, **k: {"https://img.example/1.png": "x"},
    )
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
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    assert actions.preview(CARD, image="auto")["data"]["image"]["protocol"] is None
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    assert actions.preview(CARD, image="auto")["data"]["image"]["protocol"] == "blocks"
    monkeypatch.setenv("THREED_IMAGE_PROTOCOL", "sixel")
    assert actions.preview(CARD, image="auto")["data"]["image"]["protocol"] == "sixel"


@pytest.mark.parametrize(
    ("env", "tty", "expected"),
    [
        ({"KITTY_WINDOW_ID": "1", "TERM": "xterm-256color"}, True, "kitty"),
        ({"TERM_PROGRAM": "iTerm.app", "TERM": "xterm-256color"}, True, "iterm2"),
        ({"TERM": "foot"}, True, "sixel"),
        ({"TERM": "xterm-256color"}, True, "blocks"),
        ({"TERM": "xterm-256color", "COLORTERM": "truecolor"}, False, None),
        ({"TERM": "dumb"}, True, None),
        ({"TERM": ""}, True, None),
        ({"TERM": "xterm-256color", "NO_COLOR": "1"}, True, None),
        ({"TERM": "xterm-256color", "THREED_IMAGE_PROTOCOL": "none"}, True, None),
        ({"TERM": "xterm-256color", "THREED_IMAGE_PROTOCOL": "kitty"}, False, "kitty"),
        ({"TERM": "xterm-256color", "TMUX": "/tmp/tmux"}, True, "blocks"),
    ],
)
def test_preview_protocol_degrades_from_graphics_to_blocks_to_nothing(
    env: dict[str, str], tty: bool, expected: str | None
) -> None:
    assert termimage.preview_protocol(env, is_tty=tty) == expected


def test_half_block_images_are_ansi_cells_two_pixels_per_row() -> None:
    import io

    from PIL import Image

    buf = io.BytesIO()
    image = Image.new("RGB", (8, 8), (255, 0, 0))
    image.putpixel((0, 0), (0, 0, 255))
    image.save(buf, format="PNG")
    out = termimage.encode_blocks(buf.getvalue(), 4)
    assert out is not None
    lines = out.splitlines()
    assert len(lines) == 2 and lines[0].count("▀") == 4 and lines[0].endswith("\x1b[0m")
    assert "\x1b[38;2;" in lines[0] and "\x1b[48;2;" in lines[0]
    assert termimage.encode(buf.getvalue(), "blocks", cols=4) == out
    assert termimage.encode_blocks(b"not an image", 4) is None
    tall = io.BytesIO()
    Image.new("RGB", (4, 400), (0, 255, 0)).save(tall, format="PNG")
    tall_lines = (
        termimage.encode_blocks(tall.getvalue(), 10, max_rows=5) or ""
    ).splitlines()
    assert len(tall_lines) == 5  # a tall picture is shrunk to the rows it may take
