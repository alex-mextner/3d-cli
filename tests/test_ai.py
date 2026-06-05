from __future__ import annotations

import json
import pathlib
import shlex

import pytest

from errors import InvalidArgument, UsageError


def test_load_config_uses_defaults_when_file_is_absent(tmp_path: pathlib.Path) -> None:
    from ai_tools import DEFAULT_BACKEND, DEFAULT_TEMPERATURE, load_config

    cfg = load_config(tmp_path / "missing-ai.json", require_exists=False)

    assert cfg.backend == DEFAULT_BACKEND
    assert cfg.model is None
    assert cfg.temperature == DEFAULT_TEMPERATURE
    assert cfg.tool_overrides == {}


def test_load_config_rejects_missing_explicit_file(tmp_path: pathlib.Path) -> None:
    from ai_tools import AIConfigError, load_config

    with pytest.raises(AIConfigError) as excinfo:
        load_config(tmp_path / "missing-ai.json")

    assert excinfo.value.command == "ai"
    assert "config file not found" in excinfo.value.message


def test_default_config_path_uses_shared_xdg_config_dir(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_tools import default_config_path

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("THREED_AI_CONFIG", raising=False)

    assert default_config_path() == tmp_path / "xdg" / "3d-cli" / "ai.json"


def test_build_prompt_bundle_applies_configured_tool_overrides(tmp_path: pathlib.Path) -> None:
    from ai_tools import AIRequest, build_prompt_bundle, load_config

    cfg_path = tmp_path / "ai.json"
    cfg_path.write_text(
        json.dumps(
            {
                "backend": "opencode",
                "model": "qwen2.5-coder",
                "temperature": 0.2,
                "tools": {
                    "critique": {
                        "system": "Custom system for {tool}/{operator}.",
                        "preflight": [
                            "render {target} --multi",
                            "score {target} {reference}",
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    target = tmp_path / "model.scad"
    ref = tmp_path / "photo.png"
    target.write_text("cube(1);", encoding="utf-8")
    ref.write_bytes(b"png")

    bundle = build_prompt_bundle(
        AIRequest(tool="critique", operator="review", target=target, reference=ref, context="focus on scale"),
        load_config(cfg_path),
    )

    assert bundle.backend == "opencode"
    assert bundle.model == "qwen2.5-coder"
    assert bundle.temperature == 0.2
    assert bundle.preflight_commands == [
        f"3d render {target} --multi",
        f"3d score {target} {ref}",
    ]
    assert "Custom system for critique/review." in bundle.system_prompt
    assert "focus on scale" in bundle.user_prompt
    assert "No network call has been made" in bundle.user_prompt


def test_prompt_bundle_quotes_paths_with_spaces(tmp_path: pathlib.Path) -> None:
    from ai_tools import AIRequest, build_prompt_bundle, load_config

    model_dir = tmp_path / "dir  with  spaces"
    model_dir.mkdir()
    target = model_dir / "model  with  space.scad"
    ref = model_dir / "reference  photo.png"
    target.write_text("cube(1);", encoding="utf-8")
    ref.write_bytes(b"png")

    bundle = build_prompt_bundle(
        AIRequest(tool="match", operator="review", target=target, reference=ref),
        load_config(tmp_path / "missing-ai.json", require_exists=False),
    )

    assert bundle.preflight_commands[0] == (
        f"3d fit-camera {shlex.quote(str(target))} {shlex.quote(str(ref))}"
    )


def test_critique_with_reference_adds_reference_score_preflight(tmp_path: pathlib.Path) -> None:
    from ai_tools import AIRequest, build_prompt_bundle, load_config

    target = tmp_path / "model.scad"
    ref = tmp_path / "photo.png"
    target.write_text("cube(1);", encoding="utf-8")
    ref.write_bytes(b"png")

    bundle = build_prompt_bundle(
        AIRequest(tool="critique", operator="review", target=target, reference=ref),
        load_config(tmp_path / "missing-ai.json", require_exists=False),
    )

    assert bundle.preflight_commands == [
        f"3d render {target} --multi",
        f"3d check {target}",
        f"3d score {target} {ref}",
    ]


def test_system_only_tool_override_inherits_default_preflight(tmp_path: pathlib.Path) -> None:
    from ai_tools import AIConfig, AIRequest, ToolPromptConfig, build_prompt_bundle

    target = tmp_path / "model.scad"
    target.write_text("cube(1);", encoding="utf-8")
    cfg = AIConfig(
        tool_overrides={
            "design": ToolPromptConfig(system="Custom system only."),
        }
    )

    bundle = build_prompt_bundle(AIRequest(tool="design", operator="review", target=target), cfg)

    assert bundle.system_prompt == "Custom system only."
    assert bundle.preflight_commands == [
        f"3d params {target} --json",
        f"3d check {target}",
    ]


def test_reference_preflight_requires_reference_argument(tmp_path: pathlib.Path) -> None:
    from ai_tools import AIRequest, build_prompt_bundle, load_config

    target = tmp_path / "model.scad"
    target.write_text("cube(1);", encoding="utf-8")

    with pytest.raises(UsageError) as excinfo:
        build_prompt_bundle(
            AIRequest(tool="match", operator="review", target=target),
            load_config(tmp_path / "missing-ai.json", require_exists=False),
        )

    assert excinfo.value.command == "ai"
    assert "--ref PATH" in excinfo.value.message


def test_malformed_system_prompt_template_raises_structured_config_error(tmp_path: pathlib.Path) -> None:
    from ai_tools import AIConfig, AIConfigError, AIRequest, ToolPromptConfig, build_prompt_bundle

    target = tmp_path / "model.scad"
    target.write_text("cube(1);", encoding="utf-8")
    cfg = AIConfig(
        tool_overrides={
            "design": ToolPromptConfig(system="Use JSON like {missing_key}.", preflight=()),
        }
    )

    with pytest.raises(AIConfigError) as excinfo:
        build_prompt_bundle(AIRequest(tool="design", operator="review", target=target), cfg)

    assert excinfo.value.command == "ai"


def test_prompt_template_attribute_access_raises_structured_config_error(tmp_path: pathlib.Path) -> None:
    from ai_tools import AIConfig, AIConfigError, AIRequest, ToolPromptConfig, build_prompt_bundle

    target = tmp_path / "model.scad"
    target.write_text("cube(1);", encoding="utf-8")
    cfg = AIConfig(
        tool_overrides={
            "design": ToolPromptConfig(system="Use {target.name}.", preflight=()),
        }
    )

    with pytest.raises(AIConfigError) as excinfo:
        build_prompt_bundle(AIRequest(tool="design", operator="review", target=target), cfg)

    assert excinfo.value.command == "ai"


def test_ai_command_prints_json_prompt_bundle_without_network_call(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from commands.ai import run

    target = tmp_path / "part.scad"
    target.write_text("cube(1);", encoding="utf-8")

    assert run(["design", "review", str(target), "--backend", "mock", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["tool"] == "design"
    assert payload["operator"] == "review"
    assert payload["backend"] == "mock"
    assert payload["target"] == str(target)
    assert payload["network_call"] is False
    assert payload["preflight_commands"]
    assert "prompt" in payload


def test_ai_design_do_allows_new_target_path(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from commands.ai import run

    target = tmp_path / "new-part.scad"

    assert run(["design", "do", str(target), "--backend", "mock", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["tool"] == "design"
    assert payload["operator"] == "do"
    assert payload["target"] == str(target)
    assert payload["preflight_commands"] == []


def test_ai_design_do_rejects_directory_target(tmp_path: pathlib.Path) -> None:
    from commands.ai import run

    with pytest.raises(UsageError) as excinfo:
        run(["design", "do", str(tmp_path)])

    assert excinfo.value.command == "ai"
    assert "directory" in excinfo.value.message


def test_ai_command_accepts_equals_style_value_flags(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from commands.ai import run

    target = tmp_path / "part.scad"
    target.write_text("cube(1);", encoding="utf-8")

    assert run(["design", "review", str(target), "--backend=mock", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["backend"] == "mock"


def test_ai_command_rejects_empty_equals_style_value(tmp_path: pathlib.Path) -> None:
    from commands.ai import run

    target = tmp_path / "part.scad"
    target.write_text("cube(1);", encoding="utf-8")

    with pytest.raises(UsageError) as excinfo:
        run(["design", "review", str(target), "--backend=", "--json"])

    assert excinfo.value.command == "ai"
    assert "needs a value" in excinfo.value.message


def test_ai_command_rejects_unknown_operator_with_structured_error(tmp_path: pathlib.Path) -> None:
    from commands.ai import run

    target = tmp_path / "part.scad"
    target.write_text("cube(1);", encoding="utf-8")

    with pytest.raises(InvalidArgument) as excinfo:
        run(["design", "edit", str(target)])

    assert excinfo.value.command == "ai"
    assert excinfo.value.flag == "operator"
    assert excinfo.value.accepted == ["do", "review", "loop"]
