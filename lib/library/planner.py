"""library/planner.py — the sonnet-class query planner of `3d models search --rank` (#577).

WHAT: when a search found few (or no) results the classifier calls `fits`, the planner reworks the query into
marketplace vocabulary: it turns a descriptive phrase into technical terms, synonyms and other-language titles and
returns SEVERAL queries — `rewrite` (same intent, new words), `wide` (the general category, SIMILAR models) and
`narrow` (a precise variant) — plus `followups` (queries worth trying next, not run now) and clarifying `questions`
(single choice, multiple choice or free text; "need CAD files?", "maximum size?"). The model may use omp's
`web_search` and `read` (web fetch) tools to learn the technical names; its output is strict JSON
(`3d.models.plan/1`), validated here, retried once when invalid.

ACCESSED VIA: `library.rankflow` (decides WHEN to plan, runs the queries, persists plan and questions).

INVARIANTS:
  - A plan that must run queries (`mode="rewrite"`) is only complete with >= 1 wide and >= 1 narrow query; when
    the retry still lacks one the valid rest is used and the gap is reported in `Plan.problems`, never hidden.
  - A query equal (after normalisation) to one already run in this search tree, or to another in the plan, is
    skipped with a reason (`dedupe_queries`); the planner is told what was already run so it rarely repeats one.
  - Questions already answered are never asked again; keys are `[a-z][a-z0-9_]{0,31}` and unique.
  - Stdlib-only at module top (checked by tests/test_imports.py).
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from library.llm import (
    InvalidAnswer,
    LlmClient,
    LlmFailure,
    extract_json_object,
    retry_prompt,
)

SCHEMA = "3d.models.plan/1"
RUN_KINDS = ("rewrite", "wide", "narrow")
QUESTION_TYPES = ("single", "multi", "text")
MODES = ("rewrite", "suggest")
QUERY_MAX = 120
MAX_FOLLOWUPS = 4
MAX_QUESTIONS = 3
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

PLAN_SYSTEM = f"""You are the query planner of a 3D-model search tool (Printables, MakerWorld, Thingiverse, Cults3D).
Marketplace search is keyword search over titles and tags: descriptive phrases and sentences find little; technical names, part names, synonyms and the sites' own vocabulary find more. Models are also titled in other languages (English, German, French, Chinese, Russian, ...).
You get the user's request, how the search went (counts and the top results with a verdict) and the queries already run.
Tools: you MAY use web_search and read (read fetches a URL) to learn what the thing is called; keep it to a few lookups.
Answer with ONLY one JSON object, no prose, no code fence:
{{"schema":"{SCHEMA}","summary":"one sentence: what was wrong and what you changed","queries":[{{"query":"...","kind":"rewrite|wide|narrow","rationale":"..."}}],"followups":[{{"query":"...","rationale":"..."}}],"questions":[{{"key":"need_cad","type":"single|multi|text","prompt":"...","choices":["yes","no"]}}]}}
queries (mode rewrite only; use [] in mode suggest) are RUN NOW:
  rewrite - the same intent in marketplace vocabulary (a descriptive phrase becomes technical terms, synonyms, or another language)
  wide    - broader: the general category, or SIMILAR models that could be adapted
  narrow  - more specific: a precise variant, part name, size or standard
  Give at least one wide and at least one narrow query. 2 to 6 words each. Never repeat a query already run.
followups: up to {MAX_FOLLOWUPS} further queries the user may want next (NOT run now), each with a one-line reason.
questions: up to {MAX_QUESTIONS} short clarifying questions whose answer would sharpen the ranking (need CAD or STEP files? maximum size? printer or material? single part or assembly?). type single = pick one of choices, multi = pick several of choices, text = free answer with no choices. key = a snake_case id. Do not ask a question whose key is in the already-answered list."""


@dataclass(frozen=True)
class PlannedQuery:
    query: str
    kind: str
    rationale: str = ""

    def to_json(self) -> dict[str, str]:
        return {"query": self.query, "kind": self.kind, "rationale": self.rationale}


@dataclass(frozen=True)
class Question:
    key: str
    type: str
    prompt: str
    choices: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "type": self.type,
            "prompt": self.prompt,
            "choices": list(self.choices),
        }


@dataclass(frozen=True)
class Plan:
    summary: str
    queries: tuple[PlannedQuery, ...] = ()
    followups: tuple[PlannedQuery, ...] = ()
    questions: tuple[Question, ...] = ()
    problems: tuple[str, ...] = field(default=())

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "summary": self.summary,
            "queries": [q.to_json() for q in self.queries],
            "followups": [q.to_json() for q in self.followups],
            "questions": [q.to_json() for q in self.questions],
            "problems": list(self.problems),
        }


# --------------------------------------------------------------------------- queries
def normalize_query(query: str) -> str:
    """Case, accents, punctuation and spacing folded away: the identity of a query for de-duplication."""
    folded = unicodedata.normalize("NFKC", query).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", folded).split())


def dedupe_queries(
    planned: Sequence[PlannedQuery], tried: Collection[str]
) -> tuple[list[PlannedQuery], list[tuple[PlannedQuery, str]]]:
    """`(kept, skipped with reason)`: a query already run in this search tree, or repeated inside the plan,
    is skipped. `tried` holds raw query strings (normalised here)."""
    seen = {normalize_query(q) for q in tried}
    kept: list[PlannedQuery] = []
    skipped: list[tuple[PlannedQuery, str]] = []
    for item in planned:
        key = normalize_query(item.query)
        if key in seen:
            skipped.append((item, "already run"))
            continue
        seen.add(key)
        kept.append(item)
    return kept, skipped


def select_queries(queries: Sequence[PlannedQuery], limit: int) -> list[PlannedQuery]:
    """At most `limit` queries in the planner's order, but the first wide and the first narrow always make it."""
    chosen: list[PlannedQuery] = []
    for kind in ("wide", "narrow"):
        first = next((q for q in queries if q.kind == kind), None)
        if first is not None:
            chosen.append(first)
    for q in queries:
        if len(chosen) >= limit:
            break
        if q not in chosen:
            chosen.append(q)
    return [q for q in queries if q in chosen]


# --------------------------------------------------------------------------- the prompt
def user_prompt(
    query: str,
    answers: Mapping[str, str],
    mode: str,
    digest: Sequence[Mapping[str, Any]],
    counts: Mapping[str, int],
    tried: Sequence[str],
    sites: Sequence[str],
    max_queries: int,
) -> str:
    lines = [
        f"Mode: {mode}"
        + (
            f" (return up to {max_queries} queries to run now)"
            if mode == "rewrite"
            else " (return queries: [])"
        ),
        f"User request: {query}",
        f"Sites: {', '.join(sites)}",
        "Search outcome: " + ", ".join(f"{k} {v}" for k, v in counts.items()),
    ]
    if answers:
        lines.append(
            "Already answered (constraints, do not ask again): "
            + "; ".join(f"{k} = {v}" for k, v in sorted(answers.items()))
        )
    lines.append("Queries already run: " + json.dumps(list(tried), ensure_ascii=False))
    lines.append("Top results (verdict, title, why):")
    lines.append(json.dumps(list(digest), ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines)


# --------------------------------------------------------------------------- the answer
def _text(value: object, width: int) -> str | None:
    if not isinstance(value, str):
        return None
    flat = " ".join(value.split())
    return flat[:width].rstrip() if flat else None


def _parse_queries(
    raw: object, problems: list[str], *, runnable: bool
) -> list[PlannedQuery]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        problems.append("`queries`/`followups` must be a list")
        return []
    out: list[PlannedQuery] = []
    for position, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            problems.append(f"query {position} is not an object")
            continue
        text = _text(item.get("query"), QUERY_MAX)
        if text is None:
            problems.append(f"query {position}: `query` must be a non-empty string")
            continue
        kind = item.get("kind") if runnable else "followup"
        if kind not in (RUN_KINDS if runnable else ("followup",)):
            problems.append(
                f"query {position} ({text!r}): kind {kind!r} must be one of {', '.join(RUN_KINDS)}"
            )
            continue
        assert isinstance(kind, str)
        out.append(PlannedQuery(text, kind, _text(item.get("rationale"), 200) or ""))
    return out


def _parse_questions(
    raw: object, problems: list[str], answered: Collection[str]
) -> list[Question]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        problems.append("`questions` must be a list")
        return []
    out: list[Question] = []
    keys: set[str] = set(answered)
    for position, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            problems.append(f"question {position} is not an object")
            continue
        key, qtype, prompt = (
            item.get("key"),
            item.get("type"),
            _text(item.get("prompt"), 200),
        )
        choices_raw = item.get("choices", [])
        if not isinstance(key, str) or not KEY_RE.match(key):
            problems.append(
                f"question {position}: `key` must match [a-z][a-z0-9_]{{0,31}}"
            )
            continue
        if key in keys:
            continue  # already answered or repeated: not asked twice, not an error
        if qtype not in QUESTION_TYPES:
            problems.append(
                f"question {key}: type {qtype!r} must be one of {', '.join(QUESTION_TYPES)}"
            )
            continue
        if prompt is None:
            problems.append(f"question {key}: `prompt` must be a non-empty string")
            continue
        if not isinstance(choices_raw, list) or not all(
            isinstance(c, str) and c.strip() for c in choices_raw
        ):
            problems.append(f"question {key}: `choices` must be a list of strings")
            continue
        choices = tuple(dict.fromkeys(_text(c, 60) or "" for c in choices_raw))
        if qtype == "text" and choices:
            problems.append(f"question {key}: a text question takes no choices")
            continue
        if qtype != "text" and len(choices) < 2:
            problems.append(
                f"question {key}: a {qtype} question needs at least 2 choices"
            )
            continue
        keys.add(key)
        out.append(Question(key, str(qtype), prompt, choices))
    return out


def parse_plan(text: str, *, mode: str, answered: Collection[str] = ()) -> Plan:
    """The validated plan of one model answer. Raises InvalidAnswer when the answer is not a plan object at all;
    entries that are wrong land in `Plan.problems` and are dropped, so the good parts survive."""
    doc = extract_json_object(text)
    problems: list[str] = []
    runnable = mode == "rewrite"
    queries = (
        _parse_queries(doc.get("queries"), problems, runnable=True) if runnable else []
    )
    followups = _parse_queries(doc.get("followups"), problems, runnable=False)[
        :MAX_FOLLOWUPS
    ]
    questions = _parse_questions(doc.get("questions"), problems, answered)[
        :MAX_QUESTIONS
    ]
    summary = _text(doc.get("summary"), 300) or ""
    if runnable:
        kinds = {q.kind for q in queries}
        problems.extend(
            f"the plan needs at least one `{needed}` query"
            for needed in ("wide", "narrow")
            if needed not in kinds
        )
    if not (queries or followups or questions):
        raise InvalidAnswer(
            problems or ["the plan holds no queries, followups or questions"]
        )
    return Plan(
        summary, tuple(queries), tuple(followups), tuple(questions), tuple(problems)
    )


def make_plan(
    client: LlmClient,
    model: str,
    *,
    mode: str,
    user: str,
    answered: Collection[str] = (),
    system: str = PLAN_SYSTEM,
) -> Plan:
    """Ask the planner; ONE retry names the problems of the first answer. After the retry the valid part is used
    (problems stay in the plan); an answer that is no plan at all raises LlmFailure('invalid')."""
    config = client.config
    prompt = user
    best: Plan | None = None
    last: list[str] = []
    for attempt in (1, 2):
        try:
            handle = client.call(
                "plan",
                model,
                system,
                prompt,
                items=1,
                tools=config.planner_tools,
                timeout=config.planner_timeout_s,
            )
        except LlmFailure:
            if best is not None:
                return best
            raise
        try:
            plan = parse_plan(handle.text, mode=mode, answered=answered)
        except InvalidAnswer as exc:
            handle.invalid("; ".join(exc.problems))
            last = exc.problems
        else:
            if not plan.problems:
                return plan
            handle.invalid("; ".join(plan.problems))
            last = list(plan.problems)
            best = plan
        if attempt == 1:
            prompt = retry_prompt(user, last)
    if best is None:
        raise LlmFailure("invalid", "; ".join(last)[:300])
    return best
