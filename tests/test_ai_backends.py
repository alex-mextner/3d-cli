"""Unit tests for the pluggable AI backend layer (lib/ai/).

These never call a real model: MockBackend is deterministic and the resolver is
exercised through the $THREED_AI_MOCK_RESPONSE hook and shutil.which monkeypatching.
"""
from __future__ import annotations

import pathlib

import pytest

from ai import load_backend_config
from ai.backends import (
    BACKEND_ORDER,
    ClaudeBackend,
    CodexBackend,
    MockBackend,
    OllamaBackend,
    OpencodeBackend,
    resolve_backend,
)
from ai.backends import MOCK_RESPONSE_ENV
from errors import InvalidArgument, MissingDependency


# ── MockBackend: deterministic, offline ──────────────────────────────────────
def test_mock_backend_returns_constructor_response_verbatim() -> None:
    backend = MockBackend("CONVERGED")
    assert backend.name == "mock"
    assert backend.available() is True
    out = backend.complete("system", "user", images=[pathlib.Path("/nope.png")])
    assert out == "CONVERGED"
    # Deterministic: same inputs, same output, and independent of the arguments.
    assert backend.complete("other", "prompt") == "CONVERGED"


def test_mock_backend_reads_env_when_no_constructor_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MOCK_RESPONSE_ENV, '{"param":"width","target":30}')
    assert MockBackend().complete("", "") == '{"param":"width","target":30}'


def test_mock_backend_has_stable_default_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MOCK_RESPONSE_ENV, raising=False)
    assert MockBackend().complete("", "").startswith("MOCK:")


# ── resolver: explicit / config / first-available / mock fallback ────────────
def test_resolve_explicit_name_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MOCK_RESPONSE_ENV, raising=False)
    monkeypatch.setattr("ai.backends.shutil.which", lambda name: "/usr/bin/" + name)
    assert isinstance(resolve_backend("codex"), CodexBackend)
    assert isinstance(resolve_backend("claude"), ClaudeBackend)
    assert isinstance(resolve_backend("opencode"), OpencodeBackend)


def test_resolve_config_backend_used_when_no_explicit_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MOCK_RESPONSE_ENV, raising=False)
    monkeypatch.setattr("ai.backends.shutil.which", lambda name: "/usr/bin/" + name)
    backend = resolve_backend(config={"backend": "opencode"})
    assert isinstance(backend, OpencodeBackend)


def test_resolve_first_available_is_claude_first_not_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MOCK_RESPONSE_ENV, raising=False)
    # Everything present -> auto-pick must be the head of BACKEND_ORDER (claude).
    monkeypatch.setattr("ai.backends.shutil.which", lambda name: "/usr/bin/" + name)
    assert BACKEND_ORDER[0] == "claude"
    assert isinstance(resolve_backend(), ClaudeBackend)


def test_resolve_skips_unavailable_and_picks_next(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MOCK_RESPONSE_ENV, raising=False)
    # claude absent, codex present -> codex is chosen (ollama never reached).
    monkeypatch.setattr(
        "ai.backends.shutil.which",
        lambda name: "/usr/bin/codex" if name == "codex" else None,
    )
    assert isinstance(resolve_backend(), CodexBackend)


def test_resolve_falls_back_to_mock_under_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MOCK_RESPONSE_ENV, "CONVERGED")
    monkeypatch.setattr("ai.backends.shutil.which", lambda name: None)
    backend = resolve_backend()
    assert isinstance(backend, MockBackend)
    assert backend.complete("", "") == "CONVERGED"


def test_resolve_explicit_mock_always(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MOCK_RESPONSE_ENV, raising=False)
    assert isinstance(resolve_backend("mock"), MockBackend)
    assert isinstance(resolve_backend(config={"backend": "mock"}), MockBackend)


def test_resolve_mock_env_overrides_configured_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    # A stray ai.json must not make the suite hit a real backend: the env hook wins.
    monkeypatch.setenv(MOCK_RESPONSE_ENV, "CONVERGED")
    monkeypatch.setattr("ai.backends.shutil.which", lambda name: "/usr/bin/" + name)
    assert isinstance(resolve_backend(config={"backend": "codex"}), MockBackend)


def test_resolve_explicit_name_beats_mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # An explicit --backend is the strongest signal, above the env hook.
    monkeypatch.setenv(MOCK_RESPONSE_ENV, "CONVERGED")
    monkeypatch.setattr("ai.backends.shutil.which", lambda name: "/usr/bin/" + name)
    assert isinstance(resolve_backend("codex"), CodexBackend)


def test_resolve_unknown_name_raises_invalid_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MOCK_RESPONSE_ENV, raising=False)
    with pytest.raises(InvalidArgument) as exc:
        resolve_backend("gpt5")
    assert exc.value.flag == "backend"
    assert "gpt5" in exc.value.got


def test_resolve_explicit_unavailable_raises_missing_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MOCK_RESPONSE_ENV, raising=False)
    monkeypatch.setattr("ai.backends.shutil.which", lambda name: None)
    with pytest.raises(MissingDependency) as exc:
        resolve_backend("codex")
    assert exc.value.exit_code == 127
    assert "codex" in exc.value.render(color=False)


def test_resolve_none_available_raises_missing_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MOCK_RESPONSE_ENV, raising=False)
    monkeypatch.setattr("ai.backends.shutil.which", lambda name: None)
    # Ollama's availability is a socket probe; force it closed so nothing resolves.
    monkeypatch.setattr(OllamaBackend, "available", lambda self: False)
    with pytest.raises(MissingDependency) as exc:
        resolve_backend()
    rendered = exc.value.render(color=False)
    for name in BACKEND_ORDER:
        assert name in rendered


# ── missing-binary path on complete() ────────────────────────────────────────
def test_complete_on_absent_binary_raises_missing_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ai.backends.shutil.which", lambda name: None)
    with pytest.raises(MissingDependency):
        ClaudeBackend().complete("sys", "user")


# ── config loader keeps `backend` absent when unset ─────────────────────────
def test_load_backend_config_returns_empty_when_missing(tmp_path: pathlib.Path) -> None:
    assert load_backend_config(tmp_path / "nope.json") == {}


def test_load_backend_config_does_not_inject_default_backend(tmp_path: pathlib.Path) -> None:
    cfg = tmp_path / "ai.json"
    cfg.write_text('{"model": "llava"}', encoding="utf-8")
    loaded = load_backend_config(cfg)
    assert loaded == {"model": "llava"}
    assert "backend" not in loaded  # so resolve_backend falls through to auto


def test_load_backend_config_fails_closed_on_malformed_existing_file(tmp_path: pathlib.Path) -> None:
    from ai_tools import AIConfigError

    cfg = tmp_path / "ai.json"
    cfg.write_text("{not json", encoding="utf-8")
    with pytest.raises(AIConfigError):
        load_backend_config(cfg)


def test_load_backend_config_fails_closed_on_non_object(tmp_path: pathlib.Path) -> None:
    from ai_tools import AIConfigError

    cfg = tmp_path / "ai.json"
    cfg.write_text('["not", "an", "object"]', encoding="utf-8")
    with pytest.raises(AIConfigError):
        load_backend_config(cfg)


# ── ollama endpoint/model wiring (no socket opened) ─────────────────────────
def test_ollama_backend_reads_endpoint_and_model_from_config() -> None:
    backend = OllamaBackend({"endpoint": "http://127.0.0.1:9999/", "model": "llava"})
    assert backend.endpoint == "http://127.0.0.1:9999"
    assert backend.model == "llava"
    assert backend._host_port() == ("127.0.0.1", 9999)


def test_ollama_complete_without_model_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai.backends import BackendError

    backend = OllamaBackend({"endpoint": "http://127.0.0.1:11434"})
    with pytest.raises(BackendError):
        backend.complete("", "hi")


def test_ollama_available_requires_a_model_even_with_reachable_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pretend the socket always connects; availability must still be False without a model.
    class _Sock:
        def __enter__(self) -> "_Sock":
            return self

        def __exit__(self, *a: object) -> None:
            return None

    monkeypatch.setattr("ai.backends.socket.create_connection", lambda *a, **k: _Sock())
    assert OllamaBackend({"endpoint": "http://127.0.0.1:11434"}).available() is False
    assert OllamaBackend({"endpoint": "http://127.0.0.1:11434", "model": "llava"}).available() is True


# ── vision capability contract ──────────────────────────────────────────────
def test_vision_capability_flags_are_declared() -> None:
    monkeypatch_free = {
        ClaudeBackend().name: ClaudeBackend().supports_images,
        CodexBackend().name: CodexBackend().supports_images,
        OpencodeBackend().name: OpencodeBackend().supports_images,
        OllamaBackend().name: OllamaBackend().supports_images,
        MockBackend().name: MockBackend().supports_images,
    }
    assert monkeypatch_free == {
        "claude": False,
        "codex": True,
        "opencode": False,
        "ollama": True,
        "mock": False,
    }


# ── match_loop drives the backend (no real model call) ──────────────────────
def test_match_loop_critic_drives_mock_backend(tmp_path: pathlib.Path) -> None:
    import match_loop

    constants = tmp_path / "part.scad"
    constants.write_text("width = 4;\ncube([width, 4, 4]);\n", encoding="utf-8")
    changelog = tmp_path / "changelog.md"  # absent -> changelog_text() returns "(empty)"

    backend = MockBackend('{"param":"width","target":30}')
    edit = match_loop.critic_backend(
        backend, str(constants), ["width"], best=0.5,
        metric="IoU", better="higher", work=str(tmp_path), changelog=str(changelog),
    )
    assert edit == {"param": "width", "current": None, "target": 30.0}


def test_match_loop_critic_treats_backend_error_as_no_improve(tmp_path: pathlib.Path) -> None:
    import match_loop
    from ai.backends import Backend, BackendError

    constants = tmp_path / "part.scad"
    constants.write_text("width = 4;\n", encoding="utf-8")

    class _Boom(Backend):
        name = "boom"

        def available(self) -> bool:
            return True

        def complete(self, system, user, images=None, timeout=0.0):  # type: ignore[no-untyped-def]
            raise BackendError("kaboom", command="ai")

    edit = match_loop.critic_backend(
        _Boom(), str(constants), ["width"], best=None,
        metric="IoU", better="higher", work=str(tmp_path), changelog=str(tmp_path / "cl.md"),
    )
    assert edit is None
