from __future__ import annotations

import json
import pathlib

import pytest

import ollama
from commands import ollama as ollama_command
from errors import InvalidArgument, UsageError


def test_validate_endpoint_accepts_local_http_endpoint() -> None:
    assert ollama.validate_endpoint("localhost:11434") == "http://localhost:11434"
    assert ollama.validate_endpoint("http://127.0.0.1:11434/") == "http://127.0.0.1:11434"
    assert ollama.validate_endpoint("http://[::1]:11434") == "http://[::1]:11434"


def test_validate_endpoint_rejects_remote_hosts() -> None:
    with pytest.raises(InvalidArgument) as exc:
        ollama.validate_endpoint("https://ollama.example.com")

    assert exc.value.exit_code == 2
    assert exc.value.flag == "--endpoint"
    assert "local Ollama endpoint" in exc.value.render(color=False)


@pytest.mark.parametrize(
    "endpoint",
    ["http://localhost:abc", "http://127.0.0.1:99999", "localhost:"],
)
def test_validate_endpoint_rejects_malformed_ports(endpoint: str) -> None:
    with pytest.raises(InvalidArgument) as exc:
        ollama.validate_endpoint(endpoint)

    assert exc.value.flag == "--endpoint"
    assert "valid local Ollama endpoint" in exc.value.render(color=False)


def test_load_config_validates_endpoint_without_network(tmp_path: pathlib.Path) -> None:
    config_path = tmp_path / "ollama.json"
    config_path.write_text(
        json.dumps({"endpoint": "http://localhost:11434", "model": "llama3.2"}),
        encoding="utf-8",
    )

    cfg = ollama.load_config(config_path)

    assert cfg.endpoint == "http://localhost:11434"
    assert cfg.model == "llama3.2"


def test_load_config_rejects_non_object_json(tmp_path: pathlib.Path) -> None:
    config_path = tmp_path / "ollama.json"
    config_path.write_text("[]", encoding="utf-8")

    with pytest.raises(UsageError) as exc:
        ollama.load_config(config_path)

    assert "config must be a JSON object" in exc.value.render(color=False)


def test_load_config_wraps_unreadable_config_path(tmp_path: pathlib.Path) -> None:
    with pytest.raises(UsageError) as exc:
        ollama.load_config(tmp_path)

    rendered = exc.value.render(color=False)
    assert "could not read config file" in rendered
    assert "Traceback" not in rendered


def test_plan_generate_request_is_dry_run_payload() -> None:
    plan = ollama.plan_generate_request(
        endpoint="http://localhost:11434",
        model="llama3.2",
        prompt="Make this model hollow.",
        system="Return OpenSCAD only.",
    )

    assert plan.method == "POST"
    assert plan.url == "http://localhost:11434/api/generate"
    assert plan.body == {
        "model": "llama3.2",
        "prompt": "Make this model hollow.",
        "stream": False,
        "system": "Return OpenSCAD only.",
    }


def test_command_dry_run_prints_request_plan(capsys: pytest.CaptureFixture[str]) -> None:
    code = ollama_command.run(
        [
            "--endpoint",
            "localhost:11434",
            "--model",
            "llama3.2",
            "--prompt",
            "Describe the next CAD edit.",
            "--dry-run",
        ]
    )

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert code == 0
    assert payload["dry_run"] is True
    assert payload["request"]["url"] == "http://localhost:11434/api/generate"
    assert payload["request"]["body"]["model"] == "llama3.2"
    assert payload["request"]["body"]["prompt"] == "Describe the next CAD edit."


def test_command_endpoint_override_ignores_unused_config_endpoint(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "ollama.json"
    config_path.write_text(
        json.dumps({"endpoint": "https://ollama.example.com", "model": "llama3.2"}),
        encoding="utf-8",
    )

    code = ollama_command.run(
        [
            "--config",
            str(config_path),
            "--endpoint",
            "localhost:11434",
            "--prompt",
            "Describe the next CAD edit.",
            "--dry-run",
        ]
    )

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert code == 0
    assert payload["request"]["url"] == "http://localhost:11434/api/generate"
    assert payload["request"]["body"]["model"] == "llama3.2"


def test_command_skips_broken_default_config_when_cli_supplies_defaults(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_home = tmp_path / "config"
    app_config = config_home / "3d-cli"
    app_config.mkdir(parents=True)
    (app_config / "ollama.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    code = ollama_command.run(
        [
            "--endpoint",
            "localhost:11434",
            "--model",
            "llama3.2",
            "--prompt",
            "Describe the next CAD edit.",
        ]
    )

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert code == 0
    assert payload["request"]["url"] == "http://localhost:11434/api/generate"
    assert payload["request"]["body"]["model"] == "llama3.2"


def test_command_rejects_value_option_followed_by_flag() -> None:
    with pytest.raises(UsageError) as exc:
        ollama_command.run(["--model", "llama3.2", "--prompt", "--dry-run"])

    rendered = exc.value.render(color=False)
    assert "--prompt requires a value" in rendered


def test_command_accepts_freeform_prompt_and_system_values_starting_with_dash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = ollama_command.run(
        [
            "--model",
            "llama3.2",
            "--prompt",
            "-D depth=40 changes the fixture depth.",
            "--system",
            "- Return OpenSCAD advice only.",
            "--dry-run",
        ]
    )

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert code == 0
    assert payload["request"]["body"]["prompt"] == "-D depth=40 changes the fixture depth."
    assert payload["request"]["body"]["system"] == "- Return OpenSCAD advice only."
