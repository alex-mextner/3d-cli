# ─────────────────────────────────────────────────────────────────────────────
# ai/backends.py — pluggable, backend-agnostic AI text/vision completion layer.
#
# WHAT / WHY
#   The generative-modeling pipeline (the match-loop critic, future `3d ai`
#   executors) needs to ask "an AI" for a completion WITHOUT hard-wiring a single
#   vendor. Before this module the critic shelled out to `codex` directly, so codex
#   was a HARD dependency of the whole reference-match loop. This module turns that
#   into ONE selectable backend among several.
#
#   Every backend shells out via subprocess (or, for Ollama, a stdlib HTTP POST).
#   NOTHING here imports a heavy dep at module top level — the file is stdlib-only,
#   so importing it never drags in numpy/trimesh/torch and never breaks the offline
#   `3d help`/`render` guarantee (see tests/test_imports.py for the contract).
#
# HOW IT'S REACHED
#   `resolve_backend(name, config=...)` picks a backend by explicit name, else the
#   `ai.json` `backend` field, else the first AVAILABLE one in BACKEND_ORDER, else a
#   structured MissingDependency. In a test/mock context ($THREED_AI_MOCK_RESPONSE
#   set, or backend=="mock") it returns the deterministic MockBackend — never a
#   network call. IMPORTANT: the auto-pick order starts at `claude`, NOT `codex`:
#   there is deliberately no hard codex dependency.
#
# INVARIANTS
#   - `complete()` returns the model's text (stdout+stderr merged, since several CLIs
#     log to stderr and print the answer there too). It raises MissingDependency if
#     the backend binary is absent and BackendError on timeout.
#   - MockBackend NEVER touches the network and is fully deterministic.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import abc
import base64
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from errors import InvalidArgument, MissingDependency, ThreeDError

# Auto-resolution order. Deliberately claude-first — codex is NOT the default so the
# pipeline has no hard codex dependency. `mock` is never auto-picked; it is only
# selected explicitly or via the $THREED_AI_MOCK_RESPONSE test hook.
BACKEND_ORDER: tuple[str, ...] = ("claude", "codex", "opencode", "ollama")
# `omp` is selectable by name but NEVER auto-picked: it drives the owner's subscription agent runner, which
# must be an explicit choice (`--rank`, `ai.json` `llm.backend`), not a silent fallback.
EXPLICIT_ONLY_BACKENDS: tuple[str, ...] = ("omp",)
VALID_BACKENDS: tuple[str, ...] = BACKEND_ORDER + EXPLICIT_ONLY_BACKENDS + ("mock",)

MOCK_RESPONSE_ENV = "THREED_AI_MOCK_RESPONSE"
DEFAULT_TIMEOUT = 1200.0

_INSTALL_HINTS = {
    "claude": "npm i -g @anthropic-ai/claude-code  (provides the `claude` CLI)",
    "codex": "npm i -g @openai/codex  (provides the `codex` CLI)",
    "opencode": "curl -fsSL https://opencode.ai/install | bash  (provides `opencode`)",
    "ollama": "brew install ollama && ollama serve  (local model server on :11434)",
    "omp": "install omp (the oh-my-pi agent runner) and put it on PATH, or set $THREED_OMP to its path",
}

OMP_BINARY_ENV = "THREED_OMP"


class BackendError(ThreeDError):
    """An AI backend ran but did not produce a usable answer (timeout, transport
    failure). Exit 1. Distinct from MissingDependency (the binary/server is absent)."""

    exit_code = 1


def _combine_prompt(system: str, user: str) -> str:
    """Fold a system+user pair into one prompt for CLIs that take a single string."""
    system = (system or "").strip()
    user = (user or "").strip()
    if system and user:
        return f"{system}\n\n{user}"
    return system or user


def _run_capture(
    cmd: list[str],
    *,
    stdin: str | None,
    timeout: float,
    name: str,
) -> str:
    """Run a backend CLI, returning stdout+stderr merged. Raises BackendError on
    timeout. Nonzero exit is NOT fatal: several CLIs exit nonzero yet still print a
    parseable answer, so the caller inspects the text (this mirrors the original
    critic_codex behavior, which only special-cased the timeout)."""
    try:
        p = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise BackendError(
            f"{name} timed out after {timeout:g}s",
            command="ai",
            remediation=[
                "Raise the timeout, simplify the prompt, or pick another backend."
            ],
        ) from e
    return (p.stdout or "") + (p.stderr or "")


@dataclass(frozen=True)
class Completion:
    """One completion with what the backend could measure: the model that answered and its token use.
    Every usage field is None when the backend does not report it (only `omp --mode json` does)."""

    text: str
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None

    @property
    def total_tokens(self) -> int | None:
        parts = (
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
        )
        return None if all(p is None for p in parts) else sum(p or 0 for p in parts)


class Backend(abc.ABC):
    """A backend-agnostic AI completion provider.

    Subclasses expose a stable `name`, report `available()` without side effects on
    the answer, and implement `complete()` to return the model's text.
    """

    name: str
    # Whether complete() actually consumes the `images` argument. A False backend
    # silently drops attachments, so a vision task (the match critic) must warn when it
    # falls on one. Kept as a capability flag rather than reordering auto-pick, because
    # the pipeline deliberately has NO hard codex dependency (codex is image-capable but
    # must not be forced as the default).
    supports_images: bool = False

    @abc.abstractmethod
    def available(self) -> bool:
        """True iff this backend can be invoked right now (binary on PATH / server up
        AND any config it needs to actually run is present)."""

    @abc.abstractmethod
    def complete(
        self,
        system: str,
        user: str,
        images: list[pathlib.Path] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> str:
        """Return the model's text completion for the system+user prompt.

        `images` are optional local paths for vision-capable backends; a backend that
        cannot consume them degrades gracefully (ignores them, noting it on stderr).
        """

    def _require_binary(self, binary: str) -> None:
        if shutil.which(binary) is None:
            raise MissingDependency(
                f"the `{binary}` CLI",
                install=_INSTALL_HINTS.get(
                    self.name, f"install {binary} and put it on PATH"
                ),
                degrades=f"the '{self.name}' AI backend is unavailable",
                command="ai",
            )

    def complete_detailed(
        self,
        system: str,
        user: str,
        *,
        model: str | None = None,
        tools: tuple[str, ...] = (),
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Completion:
        """`complete()` plus usage. `model` and `tools` are honoured by backends that select a model per call
        (omp); the others answer with their own configured model and no tools."""
        return Completion(self.complete(system, user, timeout=timeout))


class ClaudeBackend(Backend):
    """Anthropic `claude` CLI in print mode (`claude -p <prompt>`).

    Print mode takes a single prompt argument, so system+user are folded together.
    Image attachments are NOT supported via `-p`: if `images` are passed they are
    ignored (with a one-line note on stderr) rather than failing the run.
    """

    name = "claude"

    def available(self) -> bool:
        return shutil.which("claude") is not None

    def complete(
        self,
        system: str,
        user: str,
        images: list[pathlib.Path] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> str:
        self._require_binary("claude")
        if images:
            print(
                "    claude backend: images are not supported via `claude -p`; ignoring "
                f"{len(images)} attachment(s).",
                file=sys.stderr,
                flush=True,
            )
        prompt = _combine_prompt(system, user)
        return _run_capture(
            ["claude", "-p", prompt],
            stdin=None,
            timeout=timeout,
            name="claude",
        )


class CodexBackend(Backend):
    """OpenAI `codex exec --sandbox read-only`; the prompt is fed on stdin and images
    are attached with `-i <path>` — mirroring the original match-loop critic call so
    codex behavior is preserved bit-for-bit when it is the selected backend."""

    name = "codex"
    supports_images = True

    def available(self) -> bool:
        return shutil.which("codex") is not None

    def complete(
        self,
        system: str,
        user: str,
        images: list[pathlib.Path] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> str:
        self._require_binary("codex")
        cmd = ["codex", "exec", "--sandbox", "read-only"]
        for img in images or []:
            if pathlib.Path(img).exists():
                cmd += ["-i", str(img)]
        prompt = _combine_prompt(system, user)
        return _run_capture(cmd, stdin=prompt, timeout=timeout, name="codex")


class OpencodeBackend(Backend):
    """`opencode run <prompt>` non-interactive mode. Best-effort image support: the
    CLI does not take image flags in its stable surface, so `images` are ignored with
    a note. Availability is by `shutil.which`."""

    name = "opencode"

    def available(self) -> bool:
        return shutil.which("opencode") is not None

    def complete(
        self,
        system: str,
        user: str,
        images: list[pathlib.Path] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> str:
        self._require_binary("opencode")
        if images:
            print(
                "    opencode backend: image attachments are not wired; ignoring "
                f"{len(images)} attachment(s).",
                file=sys.stderr,
                flush=True,
            )
        prompt = _combine_prompt(system, user)
        return _run_capture(
            ["opencode", "run", prompt],
            stdin=None,
            timeout=timeout,
            name="opencode",
        )


class OllamaBackend(Backend):
    """Local Ollama server over its HTTP `/api/generate` endpoint (stdlib urllib —
    no `requests`). Vision models receive images as base64 in the `images` field.

    Config keys (from ai.json): `endpoint` (default http://127.0.0.1:11434) and
    `model` (required at call time; a vision task needs a multimodal model such as
    `llava` / `llama3.2-vision`)."""

    name = "ollama"
    supports_images = True
    DEFAULT_ENDPOINT = "http://127.0.0.1:11434"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        endpoint = config.get("endpoint") or self.DEFAULT_ENDPOINT
        self.endpoint = str(endpoint).rstrip("/")
        model = config.get("model")
        self.model = (
            str(model).strip() if isinstance(model, str) and model.strip() else None
        )

    def _host_port(self) -> tuple[str, int]:
        parsed = urlparse(self.endpoint)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return host, port

    def available(self) -> bool:
        # A running server with NO model configured cannot actually complete(), so it
        # must not win auto-pick (which would silently no-op every critic round). Require
        # both a reachable endpoint and a model name.
        if not self.model:
            return False
        host, port = self._host_port()
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            return False

    def complete(
        self,
        system: str,
        user: str,
        images: list[pathlib.Path] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> str:
        if not self.model:
            raise BackendError(
                "ollama backend needs a model name",
                command="ai",
                remediation=['Set {"model": "llava"} in ~/.config/3d-cli/ai.json.'],
            )
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": _combine_prompt(system, user),
            "stream": False,
        }
        if system.strip():
            body["system"] = system.strip()
            body["prompt"] = user.strip()
        encoded = self._encode_images(images)
        if encoded:
            body["images"] = encoded
        return self._post_generate(body, timeout)

    @staticmethod
    def _encode_images(images: list[pathlib.Path] | None) -> list[str]:
        out: list[str] = []
        for img in images or []:
            path = pathlib.Path(img)
            if path.exists():
                out.append(base64.b64encode(path.read_bytes()).decode("ascii"))
        return out

    def _post_generate(self, body: dict[str, Any], timeout: float) -> str:
        url = f"{self.endpoint}/api/generate"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as e:
            raise BackendError(
                f"ollama request to {url} failed: {e}",
                command="ai",
                remediation=[
                    "Start the server (`ollama serve`) and check the endpoint in ai.json."
                ],
            ) from e
        except (json.JSONDecodeError, ValueError) as e:
            raise BackendError(
                f"ollama returned a non-JSON response: {e}", command="ai"
            ) from e
        return str(payload.get("response", ""))


def omp_binary() -> str:
    """The omp executable: `$THREED_OMP` when set (tests point it at a fake), else `omp` on PATH."""
    return os.environ.get(OMP_BINARY_ENV) or "omp"


def _omp_usage(message: dict[str, Any]) -> dict[str, float]:
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return {}
    out: dict[str, float] = {}
    for key in ("input", "output", "cacheRead", "cacheWrite"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = value
    cost = usage.get("cost")
    if isinstance(cost, dict) and isinstance(cost.get("total"), (int, float)):
        out["cost"] = float(cost["total"])
    return out


def parse_omp_events(stdout: str) -> Completion | None:
    """The answer of an `omp --mode json` run: the text of the LAST assistant message plus usage summed over
    every assistant message (a tool-using run has several). None when stdout holds no assistant message
    (a fake or an older omp printing plain text)."""
    text: str | None = None
    model: str | None = None
    totals: dict[str, float] = {}
    error: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("message") if isinstance(event, dict) else None
        if (
            event.get("type") != "message_end"
            or not isinstance(message, dict)
            or message.get("role") != "assistant"
        ):
            continue
        parts = message.get("content")
        chunks = (
            [
                c.get("text", "")
                for c in parts
                if isinstance(c, dict) and c.get("type") == "text"
            ]
            if isinstance(parts, list)
            else []
        )
        if message.get("stopReason") == "error":
            error = str(message.get("errorMessage") or "the model call failed")
        if chunks:
            text = "".join(chunks)
        if isinstance(message.get("model"), str):
            model = message["model"]
        for key, value in _omp_usage(message).items():
            totals[key] = totals.get(key, 0) + value
    if error is not None and not text:
        raise BackendError(f"omp: {error}", command="ai")
    if text is None:
        return None

    def tokens(key: str) -> int | None:
        return int(totals[key]) if key in totals else None

    return Completion(
        text,
        model=model,
        input_tokens=tokens("input"),
        output_tokens=tokens("output"),
        cache_read_tokens=tokens("cacheRead"),
        cache_write_tokens=tokens("cacheWrite"),
        cost_usd=totals.get("cost"),
    )


class OmpBackend(Backend):
    """`omp -p` (the owner's agent runner) in print mode: ONE non-interactive, session-less model call.

    `complete_detailed(model=..., tools=...)` selects the model per call (`--model haiku`, fuzzy-matched by omp)
    and the omp tools the run may use (none by default: `--no-tools`; the query planner gets `web_search` and
    `read`). `--mode json` makes omp print one JSON event per line, which gives the answer AND the token use;
    a plain-text stdout is accepted as the answer with unknown usage. The prompt runs in a scratch directory
    with skills, rules, extensions and LSP off so a project's AGENTS.md never leaks into a ranking call.
    NEVER auto-picked (EXPLICIT_ONLY_BACKENDS): a subscription CLI must be chosen, not discovered.
    """

    name = "omp"

    def __init__(self, binary: str | None = None, *, thinking: str = "off") -> None:
        self.binary = binary or omp_binary()
        self.thinking = thinking

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def complete(
        self,
        system: str,
        user: str,
        images: list[pathlib.Path] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> str:
        return self.complete_detailed(system, user, timeout=timeout).text

    def complete_detailed(
        self,
        system: str,
        user: str,
        *,
        model: str | None = None,
        tools: tuple[str, ...] = (),
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Completion:
        self._require_binary(self.binary)
        cmd = [
            self.binary,
            "-p",
            "--mode",
            "json",
            "--no-session",
            "--no-title",
            "--no-skills",
            "--no-rules",
            "--no-extensions",
            "--no-lsp",
            "--thinking",
            self.thinking,
        ]
        if model:
            cmd += ["--model", model]
        cmd += ["--tools", ",".join(tools)] if tools else ["--no-tools"]
        if system.strip():
            cmd += ["--system-prompt", system.strip()]
        cmd.append(user.strip())
        try:
            with tempfile.TemporaryDirectory(prefix="3d-omp-") as scratch:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=scratch,
                    stdin=subprocess.DEVNULL,
                )
        except subprocess.TimeoutExpired as e:
            raise BackendError(
                f"omp timed out after {timeout:g}s",
                command="ai",
                remediation=[
                    "Raise --llm-timeout, lower --batch-size, or pick a faster model (--classifier haiku)."
                ],
            ) from e
        except OSError as e:
            raise BackendError(f"could not run {self.binary}: {e}", command="ai") from e
        parsed = parse_omp_events(proc.stdout or "")
        if parsed is None:
            plain = (proc.stdout or "").strip()
            if proc.returncode != 0 or not plain:
                tail = " ".join(((proc.stderr or "") or plain).split())[-300:]
                raise BackendError(
                    f"omp exited {proc.returncode} without an answer"
                    + (f": {tail}" if tail else ""),
                    command="ai",
                    remediation=[
                        'Run `omp -p "hello"` by hand to see why; check `omp login` for the model\'s provider.'
                    ],
                )
            parsed = Completion(plain)
        if parsed.model is None and model:
            parsed = Completion(
                parsed.text,
                model,
                parsed.input_tokens,
                parsed.output_tokens,
                parsed.cache_read_tokens,
                parsed.cache_write_tokens,
                parsed.cost_usd,
            )
        return parsed


class MockBackend(Backend):
    """Deterministic, offline backend for tests. Returns a canned response supplied via
    the constructor, else `$THREED_AI_MOCK_RESPONSE`, else a fixed sentinel. NEVER
    performs a network call and ignores its prompt/images so output is reproducible."""

    name = "mock"

    def __init__(self, response: str | None = None) -> None:
        if response is None:
            response = os.environ.get(MOCK_RESPONSE_ENV)
        self.response = (
            response if response is not None else "MOCK: no response configured"
        )

    def available(self) -> bool:
        return True

    def complete(
        self,
        system: str,
        user: str,
        images: list[pathlib.Path] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> str:
        return self.response


def _construct(name: str, config: dict[str, Any] | None) -> Backend:
    if name == "claude":
        return ClaudeBackend()
    if name == "codex":
        return CodexBackend()
    if name == "opencode":
        return OpencodeBackend()
    if name == "ollama":
        return OllamaBackend(config)
    if name == "omp":
        return OmpBackend()
    if name == "mock":
        return MockBackend()
    raise InvalidArgument("backend", name, list(VALID_BACKENDS), command="ai")


def resolve_backend(
    name: str | None = None,
    *,
    config: dict[str, Any] | None = None,
) -> Backend:
    """Pick a Backend.

    Order of precedence:
      1. explicit `name` (the CLI `--backend`) — honored; raises MissingDependency if
         that named backend is not available (except `mock`, always available).
      2. $THREED_AI_MOCK_RESPONSE set (and no explicit real `name`) — the deterministic
         mock. This test/offline hook OVERRIDES a configured `backend`, so a stray
         `ai.json` cannot make the suite hit a real model.
      3. `config["backend"]` — honored like an explicit name.
      4. no selection — the first AVAILABLE backend in BACKEND_ORDER (claude-first).
      5. nothing available — a structured MissingDependency listing install options.

    An unknown name raises InvalidArgument.
    """
    config = config or {}
    mock_env_set = os.environ.get(MOCK_RESPONSE_ENV) is not None

    # Explicit CLI name is the strongest signal.
    if name == "mock":
        return MockBackend()
    if name is None and mock_env_set:
        return MockBackend()  # test/offline hook overrides any configured backend

    config_backend = (
        config.get("backend") if isinstance(config.get("backend"), str) else None
    )
    chosen = name or config_backend
    if chosen == "mock":
        return MockBackend()

    if chosen is not None:
        backend = _construct(chosen, config)
        if not backend.available():
            raise MissingDependency(
                f"the '{chosen}' AI backend",
                install=_INSTALL_HINTS.get(
                    chosen, f"install {chosen} and put it on PATH"
                ),
                degrades=f"the explicitly selected '{chosen}' backend cannot run",
                command="ai",
            )
        return backend

    for candidate in BACKEND_ORDER:
        backend = _construct(candidate, config)
        if backend.available():
            return backend

    raise MissingDependency(
        "any AI backend (claude, codex, opencode, ollama)",
        install=(
            "install one of: "
            + " | ".join(f"{n}: {_INSTALL_HINTS[n]}" for n in BACKEND_ORDER)
            + f"  — or set ${MOCK_RESPONSE_ENV} for a deterministic mock"
        ),
        degrades="AI-assisted commands (match critic, `3d ai`) cannot call a model",
        command="ai",
    )
