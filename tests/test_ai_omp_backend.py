"""Unit tests for the `omp` AI backend (#577): the command line it builds, how it reads `omp --mode json`
(recorded from a real haiku run, tests/fixtures/omp/), plain-text and failure handling, timeouts, the missing binary,
and that omp is selectable by name but never auto-picked."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ai.backends import (
    BACKEND_ORDER,
    VALID_BACKENDS,
    BackendError,
    Completion,
    OmpBackend,
    parse_omp_events,
    resolve_backend,
)
from errors import MissingDependency

FIXTURE = Path(__file__).parent / "fixtures" / "omp" / "print-json-haiku.jsonl"

ARGV_PRINTER = """#!{python}
import json, os, sys
print(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}))
"""


def fake(tmp_path: Path, body: str) -> Path:
    binary = tmp_path / "omp"
    binary.write_text(body.replace("{python}", sys.executable), encoding="utf-8")
    binary.chmod(0o755)
    return binary


def test_a_real_print_mode_run_gives_the_text_the_model_and_the_token_use() -> None:
    parsed = parse_omp_events(FIXTURE.read_text(encoding="utf-8"))
    assert parsed is not None
    assert (
        parsed.text == '```json\n{"ok": true}\n```'
        and parsed.model == "claude-haiku-4-5"
    )
    assert (
        parsed.input_tokens,
        parsed.output_tokens,
        parsed.cache_read_tokens,
        parsed.cache_write_tokens,
    ) == (3, 13, 0, 29795)
    assert parsed.total_tokens == 29811 and parsed.cost_usd == pytest.approx(0.059658)


def test_usage_is_summed_over_every_assistant_message_and_the_last_text_wins() -> None:
    def message(text: str, tokens: int) -> str:
        msg = {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": "m",
            "usage": {"input": tokens, "output": 1},
        }
        return json.dumps({"type": "message_end", "message": msg})

    parsed = parse_omp_events(
        "\n".join(
            ["not json", message("thinking out loud", 10), message("the answer", 20)]
        )
    )
    assert parsed == Completion("the answer", "m", 30, 2, None, None, None)


def test_an_error_stop_reason_without_text_is_a_backend_error() -> None:
    msg = {
        "role": "assistant",
        "content": [],
        "stopReason": "error",
        "errorMessage": "401 invalid credentials",
    }
    with pytest.raises(BackendError, match="401 invalid credentials"):
        parse_omp_events(json.dumps({"type": "message_end", "message": msg}))


def test_stdout_without_events_is_not_parsed() -> None:
    assert parse_omp_events("just text") is None


def test_the_command_is_a_session_less_toolless_scratch_dir_print_run(
    tmp_path: Path,
) -> None:
    backend = OmpBackend(str(fake(tmp_path, ARGV_PRINTER)), thinking="off")
    out = backend.complete_detailed(
        "system rules", "  user text  ", model="haiku", timeout=30
    )
    doc = json.loads(out.text)
    argv = doc["argv"]
    assert (
        argv[:2] == ["-p", "--mode"] and argv[2] == "json" and argv[-1] == "user text"
    )
    for flag in (
        "--no-session",
        "--no-title",
        "--no-skills",
        "--no-rules",
        "--no-extensions",
        "--no-lsp",
        "--no-tools",
    ):
        assert flag in argv
    assert (
        argv[argv.index("--model") + 1] == "haiku"
        and argv[argv.index("--thinking") + 1] == "off"
    )
    assert argv[argv.index("--system-prompt") + 1] == "system rules"
    # a scratch dir (deleted after the call): no project AGENTS.md reaches the model
    assert doc["cwd"] != str(Path.cwd()) and Path(doc["cwd"]).name.startswith("3d-omp-")


def test_tools_replace_no_tools_and_an_empty_system_prompt_is_omitted(
    tmp_path: Path,
) -> None:
    backend = OmpBackend(str(fake(tmp_path, ARGV_PRINTER)))
    argv = json.loads(
        backend.complete_detailed("", "q", tools=("web_search", "read")).text
    )["argv"]
    assert (
        argv[argv.index("--tools") + 1] == "web_search,read"
        and "--no-tools" not in argv
    )
    assert "--system-prompt" not in argv and "--model" not in argv


def test_a_nonzero_exit_without_an_answer_names_the_cause(tmp_path: Path) -> None:
    binary = fake(
        tmp_path,
        "#!{python}\nimport sys\nsys.stderr.write('no credentials for anthropic\\n')\nsys.exit(3)\n",
    )
    with pytest.raises(BackendError) as exc:
        OmpBackend(str(binary)).complete_detailed("s", "u")
    assert (
        "exited 3" in exc.value.message
        and "no credentials for anthropic" in exc.value.message
    )


def test_a_slow_omp_is_a_timeout_error(tmp_path: Path) -> None:
    binary = fake(tmp_path, "#!{python}\nimport time\ntime.sleep(30)\n")
    with pytest.raises(BackendError, match="timed out after 0.5s"):
        OmpBackend(str(binary)).complete_detailed("s", "u", timeout=0.5)


def test_plain_text_stdout_is_accepted_with_unknown_usage(tmp_path: Path) -> None:
    binary = fake(tmp_path, "#!{python}\nprint('{\"ok\": true}')\n")
    out = OmpBackend(str(binary)).complete_detailed("s", "u", model="haiku")
    assert (
        out.text == '{"ok": true}' and out.total_tokens is None and out.model == "haiku"
    )


def test_a_missing_binary_is_a_missing_dependency_with_the_install_hint(
    tmp_path: Path,
) -> None:
    backend = OmpBackend(str(tmp_path / "nope"))
    assert backend.available() is False
    with pytest.raises(MissingDependency) as exc:
        backend.complete_detailed("s", "u")
    assert exc.value.exit_code == 127 and "THREED_OMP" in str(exc.value.remediation)


def test_omp_is_selectable_by_name_and_never_auto_picked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "omp" in VALID_BACKENDS and "omp" not in BACKEND_ORDER
    binary = fake(tmp_path, ARGV_PRINTER)
    monkeypatch.setenv("THREED_OMP", str(binary))
    monkeypatch.delenv("THREED_AI_MOCK_RESPONSE", raising=False)
    assert isinstance(resolve_backend("omp"), OmpBackend)
    monkeypatch.setattr(
        "ai.backends.shutil.which",
        lambda name: str(binary) if name == str(binary) else None,
    )
    with pytest.raises(
        MissingDependency
    ):  # omp is on the system, nothing else is: still no silent pick
        resolve_backend()
