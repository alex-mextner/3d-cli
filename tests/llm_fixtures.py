"""Shared fixtures for the LLM ranking tests (#577): a scripted model backend, a scripted marketplace catalogue and
a fake `omp` executable.

`ScriptedBackend` stands in for `ai.backends.Backend`: the test hands it a function `(role, model, user) -> str |
Exception` and reads back every call it received. `FakeCatalog` replaces `marketplaces.catalog.search` so a query
returns exactly the listed models, without HTTP. `write_fake_omp` writes an executable that behaves like
`omp -p --mode json`: it records its argv, sleeps if told to, and answers from a JSON script — verdicts decided by a
rule on the title, a plan, or deliberately broken output."""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ai.backends import Backend, BackendError, Completion
from marketplaces.base import Model, ModelFile
from marketplaces.catalog import SiteResult

Handler = Callable[[str, str, str], "str | BaseException"]

RESULTS_RE = re.compile(r"^Results \(JSON\):\n(\[.*\])$", re.MULTILINE)
NUMBERS_RE = re.compile(r"numbered ([0-9, ]+)\.")


def items_in(user: str) -> list[dict[str, Any]]:
    """The result cards of a classifier prompt."""
    match = RESULTS_RE.search(user)
    assert match is not None, user
    items: list[dict[str, Any]] = json.loads(match.group(1))
    return items


def numbers_in(user: str) -> list[int]:
    match = NUMBERS_RE.search(user)
    assert match is not None, user
    return [int(n) for n in match.group(1).split(",")]


def verdicts_json(
    user: str, decide: Callable[[dict[str, Any]], tuple[str, float, str]]
) -> str:
    """A valid classifier answer: `decide(card)` gives `(verdict, score, rationale)` per result in `user`."""
    entries = []
    for card in items_in(user):
        verdict, score, why = decide(card)
        entries.append(
            {"n": card["n"], "verdict": verdict, "score": score, "rationale": why}
        )
    return json.dumps({"schema": "3d.models.verdicts/1", "verdicts": entries})


class ScriptedBackend(Backend):
    """Answers from `handler(role, model, user)`; an Exception it returns is raised (BackendError = timeout/error)."""

    name = "scripted"

    def __init__(self, handler: Handler, *, delay_s: float = 0.0) -> None:
        self.handler = handler
        self.delay_s = delay_s
        self.calls: list[dict[str, Any]] = []
        self.events: list[str] = []
        self._lock = threading.Lock()

    def available(self) -> bool:
        return True

    def complete(
        self, system: str, user: str, images: Any = None, timeout: float = 0.0
    ) -> str:
        return self.complete_detailed(system, user).text

    def complete_detailed(
        self,
        system: str,
        user: str,
        *,
        model: str | None = None,
        tools: tuple[str, ...] = (),
        timeout: float = 0.0,
    ) -> Completion:
        role = "plan" if "query planner" in system else "classify"
        with self._lock:
            self.calls.append(
                {
                    "role": role,
                    "model": model,
                    "tools": tools,
                    "user": user,
                    "timeout": timeout,
                }
            )
            self.events.append(f"call:{role}")
        if self.delay_s:
            time.sleep(self.delay_s)
        out = self.handler(role, model or "", user)
        if isinstance(out, BaseException):
            raise out
        return Completion(
            out,
            model=model,
            input_tokens=100,
            output_tokens=20,
            cache_read_tokens=1000,
            cost_usd=0.01,
        )

    def roles(self) -> list[str]:
        return [c["role"] for c in self.calls]


def timeout_error() -> BackendError:
    return BackendError("omp timed out after 1s", command="ai")


# --------------------------------------------------------------------------- the catalogue
def model(
    site: str,
    n: int,
    title: str,
    *,
    files: Sequence[tuple[str, int]] = (("part.stl", 50_000),),
) -> Model:
    return Model(
        site=site,
        id=str(n),
        title=title,
        url=f"https://{site}.example/model/{n}",
        author="maker",
        likes=n,
        downloads=n * 3,
        license="CC-BY",
        summary=f"{title} - a printable model",
        tags=("case", "sensor"),
        files=tuple(ModelFile(name=name, size=size) for name, size in files),
        files_known=True,
    )


class FakeCatalog:
    """`catalog.search` stand-in: `answers[query]` = `{site: [Model, ...]}` (an unknown query finds nothing).
    Records every `(query, sites, limit)` it was asked."""

    def __init__(self, answers: dict[str, dict[str, list[Model]]]) -> None:
        self.answers = answers
        self.asked: list[tuple[str, tuple[str, ...], int]] = []
        self._lock = threading.Lock()

    def __call__(
        self,
        query: str,
        sites: Sequence[str],
        limit: int,
        *,
        details: bool = True,
        store: Any = None,
    ) -> list[SiteResult]:
        with self._lock:
            self.asked.append((query, tuple(sites), limit))
        found = self.answers.get(query, {})
        return [SiteResult(site, tuple(found.get(site, ())[:limit])) for site in sites]

    def queries(self) -> list[str]:
        return [q for q, _, _ in self.asked]


# --------------------------------------------------------------------------- the fake omp binary
FAKE_OMP = """#!{python}
import json, os, re, sys, time
state = {state!r}
args = sys.argv[1:]
log = os.path.join(state, "calls.jsonl")
script = json.load(open(os.path.join(state, "script.json"), encoding="utf-8"))
system = ""
model = ""
for i, a in enumerate(args):
    if a == "--system-prompt":
        system = args[i + 1]
    if a == "--model":
        model = args[i + 1]
prompt = args[-1]
role = "plan" if "query planner" in system else "classify"
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"role": role, "model": model, "argv": args}}) + "\\n")
time.sleep(script.get("delay_" + role, 0))
mode = script.get("mode_" + role, "ok")
if mode == "fail":
    sys.stderr.write("fake omp: no credentials\\n")
    sys.exit(3)
if mode == "garbage":
    text = "I am sorry, I cannot produce JSON today."
elif role == "plan":
    text = "```json\\n" + json.dumps(script["plan"]) + "\\n```"
else:
    m = re.search(r"^Results \\(JSON\\):\\n(\\[.*\\])$", prompt, re.M)
    items = json.loads(m.group(1))
    out = []
    for c in items:
        num = int(re.findall(r"(\\d+)", c["title"])[-1]) if re.findall(r"(\\d+)", c["title"]) else 0
        if num % script.get("fits_mod", 10) == 0:
            v, s, r = "fits", 0.9, "title number " + str(num) + " is a fit"
        elif num % script.get("maybe_mod", 5) == 0:
            v, s, r = "maybe", 0.5, "similar enough"
        else:
            v, s, r = "no", 0.1, "another thing"
        out.append({{"n": c["n"], "verdict": v, "score": s, "rationale": r}})
    text = "```json\\n" + json.dumps({{"schema": "3d.models.verdicts/1", "verdicts": out}}) + "\\n```"
usage = {{"input": 12, "output": 34, "cacheRead": 56, "cacheWrite": 0, "cost": {{"total": 0.001}}}}
msg = {{"role": "assistant", "content": [{{"type": "text", "text": text}}], "model": "fake-" + (model or "default"), "usage": usage, "stopReason": "stop"}}
print(json.dumps({{"type": "session"}}))
print(json.dumps({{"type": "message_end", "message": msg}}))
print(json.dumps({{"type": "agent_end"}}))
"""


def write_fake_omp(directory: Path, script: dict[str, Any]) -> tuple[Path, Path]:
    """`(binary, state_dir)`: an executable that answers like `omp -p --mode json` from `script` and logs each
    call to `state_dir/calls.jsonl` (`fake_calls`). Point `$THREED_OMP` at the binary."""
    directory.mkdir(parents=True, exist_ok=True)
    state = directory / "omp-state"
    state.mkdir(exist_ok=True)
    (state / "script.json").write_text(json.dumps(script), encoding="utf-8")
    binary = directory / "fake-omp"
    binary.write_text(
        FAKE_OMP.format(python=sys.executable, state=str(state)), encoding="utf-8"
    )
    binary.chmod(0o755)
    return binary, state


def fake_calls(state: Path) -> list[dict[str, Any]]:
    log = state / "calls.jsonl"
    if not log.is_file():
        return []
    return [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
