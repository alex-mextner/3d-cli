"""library/llm.py — the model-call plumbing of `3d models search --rank` (#577): config, call guards, a call ledger.

WHAT: `load_llm_config()` reads the `llm` section of `~/.config/3d-cli/ai.json` (models, batch size, timeouts,
call and token guards, the rewrite threshold, the planner's omp tools, prompt overrides) and applies CLI
overrides. `LlmClient` wraps ONE `ai.backends.Backend` and makes every model call go through the same guards:
a hard cap on the number of calls, an optional token budget, a per-call timeout, and a ledger row per call
(role, model, status, latency, tokens). A failed call raises `LlmFailure(code, message)`; the callers turn it into
the one line `LLM: unavailable (...)` and keep the deterministic results.

ACCESSED VIA: `library.ranking` (haiku-class relevance), `library.planner` (sonnet-class query planning),
`library.rankflow` (the pipeline that owns the client for one run).

INVARIANTS:
  - No model name is hard-coded outside `DEFAULTS`; `ai.json` and the CLI flags override every value.
  - `omp` is only used when the user asked for ranking (`--rank` or `llm.enabled` in ai.json); it is never
    auto-picked, and `--no-llm` never constructs a backend.
  - Calls are counted BEFORE they run (`reserve`), so parallel batches cannot overshoot `max_calls`.
  - Stdlib-only at module top (checked by tests/test_imports.py).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from ai.backends import Backend, BackendError, Completion
from errors import InvalidArgument, MissingDependency

ROLES = ("classify", "plan")
CALL_STATUSES = ("ok", "invalid", "timeout", "error", "budget")

DEFAULTS: dict[str, Any] = {
    "enabled": False,  # ranking is opt-in: `--rank` on the command line, or `llm.enabled: true` here
    "backend": "omp",
    "classifier": "haiku",
    "planner": "sonnet",
    "batch_size": 25,
    "parallel": 4,
    "timeout_s": 120.0,
    "planner_timeout_s": 300.0,
    "max_calls": 12,
    "token_budget": 0,  # 0 = no budget
    "rewrite_below": 3,
    "max_queries": 4,
    "planner_tools": ["web_search", "read"],
    "thinking": "off",
}
CONFIG_KEYS = (*DEFAULTS, "prompts")
PROMPT_KEYS = ("classify", "plan")


class LlmFailure(Exception):
    """A model call that did not give a usable answer. `code`: unavailable | timeout | invalid | budget | error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LlmConfig:
    enabled: bool = False
    backend: str = "omp"
    classifier: str = "haiku"
    planner: str = "sonnet"
    batch_size: int = 25
    parallel: int = 4
    timeout_s: float = 120.0
    planner_timeout_s: float = 300.0
    max_calls: int = 12
    token_budget: int = 0
    rewrite_below: int = 3
    max_queries: int = 4
    planner_tools: tuple[str, ...] = ("web_search", "read")
    thinking: str = "off"
    prompts: Mapping[str, str] = field(default_factory=dict)

    def prompt(self, name: str, default: str) -> str:
        return self.prompts.get(name) or default


def _bad(key: str, value: object, accepted: str) -> InvalidArgument:
    return InvalidArgument(f"llm.{key}", repr(value), [accepted], command="models")


def _positive_int(key: str, value: object, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _bad(key, value, f"an integer >= {minimum}")
    return value


def _positive_float(key: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise _bad(key, value, "a number > 0 (seconds)")
    return float(value)


def _text(key: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _bad(key, value, "a non-empty string")
    return value.strip()


def config_from_mapping(raw: Mapping[str, object]) -> LlmConfig:
    """Validate an `llm` mapping (ai.json section or merged overrides) into an LlmConfig."""
    unknown = sorted(set(raw) - set(CONFIG_KEYS))
    if unknown:
        raise InvalidArgument(
            "llm", ", ".join(unknown), list(CONFIG_KEYS), command="models"
        )
    values: dict[str, Any] = {}
    for key, value in raw.items():
        if key == "enabled":
            if not isinstance(value, bool):
                raise _bad(key, value, "true or false")
            values[key] = value
        elif key in ("backend", "classifier", "planner", "thinking"):
            values[key] = _text(key, value)
        elif key in ("batch_size", "parallel", "max_calls"):
            values[key] = _positive_int(key, value)
        elif key == "max_queries":
            values[key] = _positive_int(
                key, value, minimum=2
            )  # one wide + one narrow at least
        elif key in ("rewrite_below", "token_budget"):
            values[key] = _positive_int(key, value, minimum=0)
        elif key in ("timeout_s", "planner_timeout_s"):
            values[key] = _positive_float(key, value)
        elif key == "planner_tools":
            if not isinstance(value, list) or not all(
                isinstance(t, str) and t for t in value
            ):
                raise _bad(
                    key, value, 'a list of omp tool names, e.g. ["web_search", "read"]'
                )
            values[key] = tuple(value)
        elif key == "prompts":
            if (
                not isinstance(value, dict)
                or set(value) - set(PROMPT_KEYS)
                or not all(isinstance(v, str) for v in value.values())
            ):
                raise _bad(
                    key,
                    value,
                    f"an object with string values for {', '.join(PROMPT_KEYS)}",
                )
            values[key] = dict(value)
    return LlmConfig(**values)


def load_llm_config(
    overrides: Mapping[str, object] | None = None, *, path: str | None = None
) -> LlmConfig:
    """`llm` section of ai.json (absent = DEFAULTS) with `overrides` on top; a CLI flag left unset is not in it."""
    from ai import load_backend_config

    section = load_backend_config(path).get("llm", {})
    if not isinstance(section, dict):
        raise _bad("llm", section, "a JSON object")
    merged: dict[str, object] = {**section, **(overrides or {})}
    return config_from_mapping(merged)


# --------------------------------------------------------------------------- the call ledger
@dataclass
class CallRecord:
    """One model call as it is persisted in `llm_call` and reported in the `done` event."""

    role: str
    model: str
    status: str
    items: int = 0
    elapsed_s: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    detail: str = ""
    persisted: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "model": self.model,
            "status": self.status,
            "items": self.items,
            "elapsed_s": round(self.elapsed_s, 3),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": self.cost_usd,
            "detail": self.detail or None,
        }

    @property
    def total_tokens(self) -> int:
        return sum(
            v or 0
            for v in (
                self.input_tokens,
                self.output_tokens,
                self.cache_read_tokens,
                self.cache_write_tokens,
            )
        )


class CallHandle:
    """The record of a call that returned text: the caller marks it `invalid` when the text fails the schema."""

    def __init__(self, record: CallRecord, completion: Completion) -> None:
        self.record = record
        self.completion = completion

    @property
    def text(self) -> str:
        return self.completion.text

    def invalid(self, detail: str) -> None:
        self.record.status = "invalid"
        self.record.detail = detail[:300]


class LlmClient:
    """Guarded access to one backend: every call is reserved against `max_calls` and the token budget, timed,
    and recorded. Thread-safe: the classifier runs batches in parallel through one client."""

    def __init__(
        self,
        backend: Backend,
        config: LlmConfig,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.backend = backend
        self.config = config
        self.records: list[CallRecord] = []
        self._lock = threading.Lock()
        self._reserved = 0
        self._sleep = sleep

    # -- guards ------------------------------------------------------------------------------
    def reserve(self, role: str, model: str, items: int) -> CallRecord:
        """Count a call BEFORE it runs. Raises LlmFailure('budget') when the cap or the token budget is spent."""
        with self._lock:
            problem = None
            if self._reserved >= self.config.max_calls:
                problem = (
                    f"call limit reached ({self.config.max_calls}; raise --max-calls)"
                )
            elif (
                self.config.token_budget
                and self.tokens_used() >= self.config.token_budget
            ):
                problem = f"token budget spent ({self.tokens_used()} >= {self.config.token_budget}; raise --token-budget)"
            if problem is not None:
                self.records.append(
                    CallRecord(role, model, "budget", items, detail=problem)
                )
                raise LlmFailure("budget", problem)
            self._reserved += 1
            record = CallRecord(role, model, "error", items)
            self.records.append(record)
            return record

    def tokens_used(self) -> int:
        return sum(r.total_tokens for r in self.records)

    def calls_made(self) -> int:
        return sum(1 for r in self.records if r.status != "budget")

    # -- one call ------------------------------------------------------------------------------
    def call(
        self,
        role: str,
        model: str,
        system: str,
        user: str,
        *,
        items: int = 0,
        tools: tuple[str, ...] = (),
        timeout: float | None = None,
    ) -> CallHandle:
        """Run one model call. Returns a handle on success; raises LlmFailure (unavailable | timeout | error |
        budget) otherwise. The call's record is complete either way."""
        record = self.reserve(role, model, items)
        limit = timeout if timeout is not None else self.config.timeout_s
        started = time.monotonic()
        try:
            completion = self.backend.complete_detailed(
                system, user, model=model, tools=tools, timeout=limit
            )
        except MissingDependency as exc:
            record.status, record.detail = "error", exc.message
            raise LlmFailure("unavailable", exc.message) from exc
        except BackendError as exc:
            timed_out = "timed out" in exc.message
            record.status = "timeout" if timed_out else "error"
            record.detail = exc.message[:300]
            raise LlmFailure("timeout" if timed_out else "error", exc.message) from exc
        finally:
            record.elapsed_s = time.monotonic() - started
        record.status = "ok"
        record.input_tokens = completion.input_tokens
        record.output_tokens = completion.output_tokens
        record.cache_read_tokens = completion.cache_read_tokens
        record.cache_write_tokens = completion.cache_write_tokens
        record.cost_usd = completion.cost_usd
        if completion.model:
            record.detail = completion.model
        return CallHandle(record, completion)

    # -- reporting -----------------------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """The `llm` block of the `done` event: calls by status and the token / cost totals."""
        by_status: dict[str, int] = {}
        for r in self.records:
            by_status[r.status] = by_status.get(r.status, 0) + 1
        known = [
            r
            for r in self.records
            if r.input_tokens is not None or r.output_tokens is not None
        ]
        costs = [r.cost_usd for r in self.records if r.cost_usd is not None]
        return {
            "backend": self.backend.name,
            "calls": self.calls_made(),
            "by_status": by_status,
            "elapsed_s": round(sum(r.elapsed_s for r in self.records), 3),
            "input_tokens": sum(r.input_tokens or 0 for r in known) if known else None,
            "output_tokens": sum(r.output_tokens or 0 for r in known)
            if known
            else None,
            "cache_read_tokens": sum(r.cache_read_tokens or 0 for r in known)
            if known
            else None,
            "cache_write_tokens": sum(r.cache_write_tokens or 0 for r in known)
            if known
            else None,
            "total_tokens": self.tokens_used() if known else None,
            "cost_usd": round(sum(costs), 6) if costs else None,
            "calls_detail": [r.to_json() for r in self.records],
        }


# --------------------------------------------------------------------------- answers
class InvalidAnswer(ValueError):
    """The model answered, but not with the JSON this schema requires. `problems` names each defect (it is
    what the one retry tells the model)."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def extract_json_object(text: str) -> dict[str, Any]:
    """The JSON object in a model answer: plain, in a ``` fence, or surrounded by prose. Raises InvalidAnswer."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0]
    first, last = body.find("{"), body.rfind("}")
    if first < 0 or last <= first:
        raise InvalidAnswer(["the answer holds no JSON object"])
    try:
        doc = json.loads(body[first : last + 1])
    except json.JSONDecodeError as exc:
        raise InvalidAnswer(
            [f"the JSON does not parse ({exc.msg} at char {exc.pos})"]
        ) from exc
    if not isinstance(doc, dict):
        raise InvalidAnswer(["the answer must be a JSON object"])
    return doc


def retry_prompt(user: str, problems: list[str]) -> str:
    """The second attempt's user message: the original request plus what was wrong with the first answer."""
    listed = "\n".join(f"- {p}" for p in problems[:8])
    return (
        f"{user}\n\nYour previous answer was rejected:\n{listed}\n"
        "Answer again with ONLY the JSON object that matches the schema, nothing before or after it."
    )


def with_overrides(config: LlmConfig, **values: Any) -> LlmConfig:
    """`config` with the non-None `values` applied (CLI flags that were given)."""
    return replace(config, **{k: v for k, v in values.items() if v is not None})
