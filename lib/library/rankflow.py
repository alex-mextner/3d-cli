"""library/rankflow.py — the two-phase streaming pipeline behind `3d models search --rank` (#577).

WHAT: one generator of EVENTS (`3d.models.rank/1`, docs/contracts/search-events.md) that the CLI prints, the
`--ndjson` mode serialises line by line, and the TUI (S7, #578) consumes in-process. PHASE 1 runs the S5 search and
yields `search` + one `result` per hit, in the deterministic order of the search, BEFORE any model call starts.
PHASE 2 judges every result with the classifier model (batched, parallel, cached) and yields a `verdict` as each
batch completes; when too few results `fit` the planner model reworks the query (`plan`), the rewrite / wide /
narrow queries are run, merged by (site, id) and judged too (`query`, `result`, `verdict`); finally the planner's
follow-up suggestions and clarifying questions are persisted and yielded (`question`, `query` with status
`suggested`) and `done` carries the final ranking. `--no-llm` (or a pipeline whose model is unavailable) ends after
phase 1 with the S5 results unchanged.

ACCESSED VIA: `commands/models.py` (`search --rank`, `rerank`, `followups`), `library.actions.rank`, S7's TUI.
Entry points: `Pipeline(...).search(query)` for a new search, `Pipeline(...).session(ref)` for a saved one
(rerank, answers, `--follow N`).

INVARIANTS:
  - Nothing before the `search` event waits on a model; no model is built when `RankOptions.enabled` is False.
  - A model failure never raises: it becomes ONE `warning` event (`code: llm_unavailable`, text `LLM: unavailable
    (...)`) per role and the deterministic results stand. `require=True` is the single exception: a missing
    backend raises MissingDependency (exit 127) BEFORE the search.
  - Every verdict, plan query, question, answer and model call is written to the library DB as it happens (best
    effort: a DB that cannot be written adds a warning), so `3d models searches show` sees what the stream showed.
  - Results are merged by (site, id): a hit already in the pool is not shown twice; a duplicate found by a
    rewrite gets the pooled verdict on its own session row.
  - Stdlib-only at module top (checked by tests/test_imports.py).
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any

from ai.backends import Backend, OmpBackend, resolve_backend
from cli.resources import pool_size
from errors import MissingDependency, ThreeDError, UsageError
from library import planner, ranking
from library.llm import LlmClient, LlmConfig, LlmFailure
from library.ranking import Entry, Verdict
from library.textview import clip, flat

EVENT_SCHEMA = "3d.models.rank/1"
EVENTS = (
    "start",
    "search",
    "phase",
    "result",
    "verdict",
    "plan",
    "query",
    "question",
    "warning",
    "done",
)
SUMMARY_TOP = 12  # results the planner sees


@dataclass
class RankOptions:
    """What a run does beyond the S5 search. `enabled` False = the deterministic path (no model, ever)."""

    config: LlmConfig
    enabled: bool = True
    require: bool = (
        False  # --llm require: no backend -> MissingDependency (127) before any search
    )
    rewrite: bool = True  # False = --rewrite off: no planner call (no rewrite, no follow-ups, no questions)
    answers: Mapping[str, str] = field(default_factory=dict)
    follow: Sequence[int] = ()
    refresh: bool = False  # ignore cached verdicts
    backend: Backend | None = (
        None  # an injected backend (tests); else resolved from config.backend
    )


@dataclass
class Item:
    """One result in the merged pool."""

    site: str
    model_id: str
    record: dict[str, Any]
    entry: Entry
    session_uid: str | None
    session_id: int | None
    position: int
    found_by: str
    kind: str
    verdict: Verdict | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.site, self.model_id)


SearchFn = Callable[[str, Any], Any]


class Pipeline:
    """One ranking run. Create it, then iterate `search(query)` or `session(ref)`; afterwards `doc`, `failures`
    and `degraded` describe the outcome for the CLI's exit code and text view."""

    def __init__(
        self,
        rank: RankOptions,
        search_options: Any,
        *,
        search_fn: SearchFn | None = None,
    ) -> None:
        from library import search as searchcore

        self.rank = rank
        self.config = rank.config
        self.options = search_options
        self.search_fn: SearchFn = search_fn or searchcore.run_search
        self.doc: dict[str, Any] = {}
        self.failures: dict[str, ThreeDError] = {}
        self.degraded: str | None = None
        self.client: LlmClient | None = None
        self.items: list[Item] = []
        self.by_key: dict[tuple[str, str], Item] = {}
        self.query = ""
        self.root_uid: str | None = None
        self.root_id: int | None = None
        self.answers: dict[str, str] = dict(rank.answers)
        self.conn: sqlite3.Connection | None = None
        self.queries_run: list[dict[str, Any]] = []
        self.questions: list[dict[str, Any]] = []
        self._seq = 0
        self._started = time.monotonic()
        self._warned: set[str] = set()
        self._dead = False  # the backend is unusable (missing, or the call budget is spent): no more model calls

    # ------------------------------------------------------------------ events
    def _ev(self, name: str, **fields: Any) -> dict[str, Any]:
        self._seq += 1
        return {
            "schema": EVENT_SCHEMA,
            "event": name,
            "seq": self._seq,
            "t": round(time.monotonic() - self._started, 3),
            **fields,
        }

    def _warn(self, code: str, message: str, **extra: Any) -> dict[str, Any]:
        return self._ev("warning", code=code, message=message, **extra)

    def _llm_warning(self, role: str, failure: LlmFailure) -> dict[str, Any] | None:
        """The ONE `LLM: unavailable (...)` warning per role; later failures stay in the call ledger."""
        if role in self._warned:
            return None
        self._warned.add(role)
        prefix = "" if role == "classify" else "planner: "
        self.degraded = self.degraded or failure.message
        return self._warn(
            "llm_unavailable",
            f"LLM: unavailable ({prefix}{failure.message})",
            role=role,
            reason=failure.code,
        )

    # ------------------------------------------------------------------ setup
    def _open(self) -> list[dict[str, Any]]:
        """The library connection (cache + persistence) unless saving is off; warnings when it cannot be opened."""
        if not getattr(self.options, "save", True):
            return []
        try:
            from library import db as librarydb

            self.conn = librarydb.open_library(getattr(self.options, "db_path", None))
        except (sqlite3.Error, OSError) as exc:
            return [
                self._warn(
                    "library", f"library database unavailable, ranking not saved: {exc}"
                )
            ]
        return []

    def _prepare_llm(self) -> str | None:
        """Build the client; None when ready, else why not (raises when `require` and the backend is missing)."""
        if not self.rank.enabled:
            return "disabled"
        try:
            backend = self.rank.backend or resolve_backend(self.config.backend)
        except MissingDependency as exc:
            if self.rank.require:
                raise
            return exc.message
        if isinstance(backend, OmpBackend):
            backend.thinking = self.config.thinking
        self.client = LlmClient(backend, self.config)
        return None

    # ------------------------------------------------------------------ the pool
    def _add(
        self,
        record: dict[str, Any],
        session_uid: str | None,
        session_id: int | None,
        found_by: str,
        kind: str,
    ) -> Item | None:
        entry = ranking.entry_for(record)
        if entry.key in self.by_key:
            return None
        item = Item(
            entry.site,
            entry.model_id,
            record,
            entry,
            session_uid,
            session_id,
            len(self.items) + 1,
            found_by,
            kind,
        )
        self.items.append(item)
        self.by_key[entry.key] = item
        return item

    def _result_event(self, item: Item) -> dict[str, Any]:
        r = item.record
        return self._ev(
            "result",
            search=item.session_uid,
            position=item.position,
            site=item.site,
            id=item.model_id,
            rank=r.get("rank"),
            title=r.get("title"),
            url=r.get("url"),
            query=item.found_by,
            kind=item.kind,
            card=r,
        )

    def _session_id(self, uid: str | None) -> int | None:
        if uid is None or self.conn is None:
            return None
        row = self.conn.execute(
            "SELECT id FROM search_session WHERE uid = ?", (uid,)
        ).fetchone()
        return int(row["id"]) if row else None

    def ranked(self) -> list[Item]:
        """The pool in final order: fits, maybe, unjudged, no; by score inside a class; search order last."""
        return sorted(
            self.items,
            key=lambda i: ranking.sort_key(
                i.verdict.verdict if i.verdict else None,
                i.verdict.score if i.verdict else None,
                i.position,
            ),
        )

    def counts(self) -> dict[str, int]:
        out = {
            "results": len(self.items),
            "fits": 0,
            "maybe": 0,
            "no": 0,
            "unjudged": 0,
        }
        for item in self.items:
            out[item.verdict.verdict if item.verdict else "unjudged"] += 1
        return out

    # ------------------------------------------------------------------ phase 1
    def _start(self, kind: str, query: str) -> dict[str, Any]:
        cfg = self.config
        return self._ev(
            "start",
            kind=kind,
            query=query,
            llm={
                "enabled": self.rank.enabled,
                "backend": cfg.backend if self.rank.enabled else None,
                "classifier": cfg.classifier if self.rank.enabled else None,
                "planner": cfg.planner
                if self.rank.enabled and self.rank.rewrite
                else None,
                "batch_size": cfg.batch_size,
                "rewrite_below": cfg.rewrite_below,
                "max_calls": cfg.max_calls,
            },
            answers=dict(self.answers),
        )

    def _search_event(self, doc: dict[str, Any]) -> dict[str, Any]:
        return self._ev(
            "search",
            search=doc.get("search_id"),
            query=doc["query"],
            sites=doc["sites"],
            counts={s: len(r) for s, r in doc["results"].items()},
            timings=doc.get("timings", {}),
            errors=doc.get("errors", []),
        )

    def search(self, query: str) -> Iterator[dict[str, Any]]:
        """A new search: phase 1 first, then (when enabled) phase 2."""
        self.query = query
        unavailable = self._prepare_llm()
        yield self._start("search", query)
        try:
            yield from self._open()
            run = self.search_fn(query, replace(self.options, kind="user"))
            self.doc, self.failures = run.doc, run.failures
            self.root_uid = self.doc.get("search_id")
            self.root_id = self._session_id(self.root_uid)
            for warning in self.doc.get("warnings", []):
                yield self._warn(
                    "search_warning",
                    f"{warning['site']}: {warning['message']}",
                    site=warning["site"],
                )
            yield self._search_event(self.doc)
            for site in self.doc["sites"]:
                for record in self.doc["results"].get(site, []):
                    item = self._add(record, self.root_uid, self.root_id, query, "user")
                    if item is not None:
                        yield self._result_event(item)
            yield from self._persist_answers()
            yield from self._phase_two(unavailable)
        finally:
            self._close()

    def session(self, ref: str) -> Iterator[dict[str, Any]]:
        """A saved search (and its whole tree): re-emit its results, then judge / plan / follow as `search` does.
        This is `3d models rerank` and `3d models followups --answer/--follow`."""
        from library import rankstore
        from library import search as searchcore

        warnings = self._open()
        if self.conn is None:
            raise UsageError(
                "the library database is unavailable, there is no saved search to rank",
                command="models",
                remediation=[w["message"] for w in warnings]
                or ["Check `3d library status`."],
            )
        row = rankstore.session_row(self.conn, ref)
        self.root_id, self.root_uid, self.query = (
            int(row["id"]),
            str(row["uid"]),
            str(row["query"]),
        )
        for key, value in self.rank.answers.items():
            rankstore.set_answer(self.conn, self.root_id, key, value)
        self.answers = rankstore.answers_map(self.conn, self.root_id)
        unavailable = self._prepare_llm()
        try:
            self.doc = searchcore.session_doc(self.conn, str(row["uid"]))
            yield self._start("session", self.query)
            yield from warnings
            yield self._search_event(self.doc)
            for session in rankstore.tree_sessions(self.conn, self.root_id):
                for stored in rankstore.session_results(self.conn, int(session["id"])):
                    item = self._add(
                        stored["record"],
                        str(session["uid"]),
                        int(session["id"]),
                        str(session["query"]),
                        str(session["kind"]),
                    )
                    if item is not None:
                        yield self._result_event(item)
            yield from self._phase_two(
                unavailable,
                planned_before=bool(
                    rankstore.list_plan_queries(self.conn, self.root_id)
                ),
            )
        finally:
            self._close()

    def _close(self) -> None:
        if self.conn is None:
            return
        try:
            if self.client is not None and self.root_id is not None:
                from library import rankstore

                rankstore.add_llm_calls(self.conn, self.root_id, self.client.records)
        except sqlite3.Error:
            pass
        finally:
            self.conn.close()
            self.conn = None

    def _persist_answers(self) -> Iterator[dict[str, Any]]:
        """Store the `--answer` values given on a NEW search as answered questions of its root session."""
        if not self.answers or self.conn is None or self.root_id is None:
            return
        from library import rankstore

        try:
            for key, value in self.answers.items():
                rankstore.set_answer(self.conn, self.root_id, key, value)
        except sqlite3.Error as exc:
            yield self._warn("library", f"answers not saved: {exc}")

    # ------------------------------------------------------------------ phase 2
    def _phase_two(
        self, unavailable: str | None, *, planned_before: bool = False
    ) -> Iterator[dict[str, Any]]:
        if unavailable is not None and self.rank.enabled:
            self.degraded = unavailable
            self._dead = True
            yield self._warn(
                "llm_unavailable",
                f"LLM: unavailable ({unavailable})",
                role="classify",
                reason="unavailable",
            )
        elif self.client is not None:
            yield self._ev("phase", phase=2, status="start", results=len(self.items))
            yield from self._judge(self.items)
        if planned_before:
            yield from self._replay_plan()
        elif self.client is not None and self.rank.rewrite and self._can_plan():
            yield from self._plan_and_run()
        for number in self.rank.follow:
            yield from self._follow(number)
        yield self._done()

    def _can_plan(self) -> bool:
        """The planner runs when the classifier worked (or there was nothing to classify): a dead backend or a
        classifier that judged none of the results says the planner would fail too."""
        if self._dead:
            return False
        return not self.items or any(i.verdict is not None for i in self.items)

    def _judge(self, items: Sequence[Item]) -> Iterator[dict[str, Any]]:
        client = self.client
        if client is None or not items:
            return
        by_key = {i.entry.key: i for i in items}
        model = self.config.classifier
        for outcome in ranking.classify(
            client,
            self.conn,
            self.query,
            self.answers,
            [i.entry for i in items],
            model=model,
            refresh=self.rank.refresh,
        ):
            for key, verdict in sorted(
                outcome.verdicts.items(), key=lambda kv: by_key[kv[0]].position
            ):
                item = by_key[key]
                item.verdict = verdict
                if item.session_id is not None:
                    ranking.persist_verdict(
                        self.conn, item.session_id, item.entry, verdict
                    )
                yield self._ev(
                    "verdict",
                    search=item.session_uid,
                    position=item.position,
                    site=item.site,
                    id=item.model_id,
                    verdict=verdict.verdict,
                    score=verdict.score,
                    rationale=verdict.rationale,
                    cached=verdict.cached,
                    model=model,
                )
            if outcome.failure is not None:
                if outcome.failure.code in ("unavailable", "budget"):
                    self._dead = True
                warning = self._llm_warning("classify", outcome.failure)
                if warning is not None:
                    yield warning

    def _tried(self) -> list[str]:
        if self.conn is not None and self.root_id is not None:
            from library import rankstore

            return rankstore.tree_queries(self.conn, self.root_id)
        return [self.query, *(q["query"] for q in self.queries_run)]

    def _digest(self) -> list[dict[str, Any]]:
        top = self.ranked()[:SUMMARY_TOP]
        return [
            {
                "verdict": i.verdict.verdict if i.verdict else "unjudged",
                "title": clip(flat(str(i.record.get("title") or "")), 90),
                "site": i.site,
                "why": i.verdict.rationale if i.verdict else "",
            }
            for i in top
        ]

    def _plan_and_run(self) -> Iterator[dict[str, Any]]:
        client = self.client
        if client is None:
            return
        fits = self.counts()["fits"]
        mode = "rewrite" if fits < self.config.rewrite_below else "suggest"
        trigger = (
            f"{fits} fits < rewrite_below {self.config.rewrite_below}"
            if mode == "rewrite"
            else f"{fits} fits"
        )
        model = self.config.planner
        yield self._ev(
            "plan", status="running", mode=mode, model=model, trigger=trigger
        )
        user = planner.user_prompt(
            self.query,
            self.answers,
            mode,
            self._digest(),
            self.counts(),
            self._tried(),
            list(self.options.sites),
            self.config.max_queries,
        )
        try:
            plan = planner.make_plan(
                client, model, mode=mode, user=user, answered=self.answers.keys()
            )
        except LlmFailure as failure:
            warning = self._llm_warning("plan", failure)
            yield self._ev(
                "plan",
                status="failed",
                mode=mode,
                model=model,
                trigger=trigger,
                reason=failure.message,
            )
            if warning is not None:
                yield warning
            return
        kept, skipped = planner.dedupe_queries(plan.queries, self._tried())
        chosen = (
            planner.select_queries(kept, self.config.max_queries)
            if mode == "rewrite"
            else []
        )
        dropped = [(q, "over --max-queries") for q in kept if q not in chosen]
        skipped = [*skipped, *dropped]
        yield self._ev(
            "plan",
            status="ready",
            mode=mode,
            model=model,
            trigger=trigger,
            summary=plan.summary,
            queries=[q.to_json() for q in chosen],
            skipped=[{**q.to_json(), "reason": why} for q, why in skipped],
            followups=[q.to_json() for q in plan.followups],
            questions=[q.to_json() for q in plan.questions],
            problems=list(plan.problems),
        )
        for problem in plan.problems:
            yield self._warn("plan_incomplete", f"plan: {problem}")
        stored: list[tuple[planner.PlannedQuery, str, str | None, int | None]] = []
        if chosen:
            for event, entry in self._run_queries(chosen):
                yield event
                if entry is not None:
                    stored.append(entry)
        stored += [(q, "skipped", why, None) for q, why in skipped]
        followups = self._store_followups(plan, stored)
        yield from self._judge_new()
        yield from self._emit_questions(plan.questions)
        for fields in followups:
            yield self._ev("query", **fields)

    def _store_followups(
        self,
        plan: planner.Plan,
        stored: list[tuple[planner.PlannedQuery, str, str | None, int | None]],
    ) -> list[dict[str, Any]]:
        tried = {planner.normalize_query(q) for q in self._tried()}
        fresh = [
            f for f in plan.followups if planner.normalize_query(f.query) not in tried
        ]
        rows: list[dict[str, Any]] = []
        if self.conn is not None and self.root_id is not None:
            from library import rankstore

            try:
                rankstore.add_plan_queries(
                    self.conn,
                    self.root_id,
                    [*stored, *((f, "suggested", None, None) for f in fresh)],
                )
                rows = [
                    p
                    for p in rankstore.list_plan_queries(self.conn, self.root_id)
                    if p["kind"] == "followup"
                ]
            except sqlite3.Error:
                rows = []
        fields = []
        for number, f in enumerate(fresh, start=1):
            row = next((r for r in rows if r["query"] == f.query), None)
            fields.append(
                {
                    "status": "suggested",
                    "id": row["id"] if row else None,
                    "n": row["n"] if row else number,
                    "query": f.query,
                    "kind": "followup",
                    "rationale": f.rationale,
                    "parent": self.root_uid,
                    "search": None,
                }
            )
        return fields

    def _emit_questions(
        self, questions: Sequence[planner.Question]
    ) -> Iterator[dict[str, Any]]:
        if not questions:
            return
        stored: dict[str, dict[str, Any]] = {}
        if self.conn is not None and self.root_id is not None:
            from library import rankstore

            try:
                rankstore.add_questions(self.conn, self.root_id, questions)
                stored = {
                    q["key"]: q
                    for q in rankstore.list_questions(self.conn, self.root_id)
                }
            except sqlite3.Error as exc:
                yield self._warn("library", f"questions not saved: {exc}")
        for q in questions:
            row = stored.get(q.key)
            payload = {
                "id": row["id"] if row else q.key,
                **q.to_json(),
                "answer": row["answer"] if row else None,
            }
            self.questions.append(payload)
            yield self._ev("question", search=self.root_uid, **payload)

    def _replay_plan(self) -> Iterator[dict[str, Any]]:
        """A saved search that was planned before: show its stored questions and follow-ups again, plan nothing."""
        if self.conn is None or self.root_id is None:
            return
        from library import rankstore

        for q in rankstore.list_questions(self.conn, self.root_id):
            self.questions.append(q)
            yield self._ev("question", search=self.root_uid, **q)
        for p in rankstore.list_plan_queries(self.conn, self.root_id):
            if p["kind"] == "followup" and p["status"] == "suggested":
                yield self._ev(
                    "query",
                    status="suggested",
                    id=p["id"],
                    n=p["n"],
                    query=p["query"],
                    kind="followup",
                    rationale=p["rationale"],
                    parent=self.root_uid,
                    search=None,
                )

    # ------------------------------------------------------------------ running extra queries
    def _search_one(
        self, planned: planner.PlannedQuery
    ) -> tuple[Any, ThreeDError | None]:
        options = replace(
            self.options,
            kind=planned.kind,
            parent_session=self.root_uid if self.options.save else None,
            idempotency_key=None,
        )
        try:
            return self.search_fn(planned.query, options), None
        except ThreeDError as exc:
            return None, exc

    def _run_queries(
        self, planned: Sequence[planner.PlannedQuery]
    ) -> Iterator[
        tuple[
            dict[str, Any],
            tuple[planner.PlannedQuery, str, str | None, int | None] | None,
        ]
    ]:
        """Run `planned` in parallel, merge new hits into the pool and yield `query` + `result` events (judging is
        done by `_judge_new` once, so the new hits of every query share batches)."""
        with ThreadPoolExecutor(max_workers=pool_size(len(planned))) as pool:
            outcomes = list(pool.map(self._search_one, planned))
        for query, (run, error) in zip(planned, outcomes, strict=True):
            if run is None:
                yield (
                    self._warn(
                        "query_failed",
                        f"query {query.query!r} failed: {error.message if error else 'no answer'}",
                    ),
                    None,
                )
                continue
            doc = run.doc
            uid = doc.get("search_id")
            session_id = self._session_id(uid)
            new: list[Item] = []
            duplicates = 0
            for site in doc["sites"]:
                for record in doc["results"].get(site, []):
                    item = self._add(record, uid, session_id, query.query, query.kind)
                    if item is not None:
                        new.append(item)
                        continue
                    duplicates += 1
                    pooled = self.by_key[(str(record["site"]), str(record["id"]))]
                    if pooled.verdict is not None and session_id is not None:
                        ranking.persist_verdict(
                            self.conn, session_id, pooled.entry, pooled.verdict
                        )
            counts = {s: len(r) for s, r in doc["results"].items()}
            info = {
                "status": "run",
                "search": uid,
                "query": query.query,
                "kind": query.kind,
                "rationale": query.rationale,
                "parent": self.root_uid,
                "counts": counts,
                "results": sum(counts.values()),
                "new": len(new),
                "duplicates": duplicates,
                "errors": doc.get("errors", []),
            }
            self.queries_run.append(info)
            yield self._ev("query", **info), (query, "run", None, session_id)
            for item in new:
                yield self._result_event(item), None

    def _judge_new(self) -> Iterator[dict[str, Any]]:
        pending = [i for i in self.items if i.verdict is None and i.kind != "user"]
        if pending and not self._dead:
            yield from self._judge(pending)

    def _follow(self, number: int) -> Iterator[dict[str, Any]]:
        """Run suggested follow-up `number` (1-based) as a `followup` session under the root and judge its hits."""
        if self.conn is None or self.root_id is None:
            yield self._warn(
                "follow_failed", "--follow needs the library database (a saved search)"
            )
            return
        from library import rankstore

        try:
            row = rankstore.get_followup(self.conn, self.root_id, number)
        except UsageError as exc:
            yield self._warn("follow_failed", exc.message)
            return
        if row["status"] == "run":
            yield self._warn(
                "follow_failed",
                f"follow-up {number} was already run (search {row['search']})",
            )
            return
        planned = planner.PlannedQuery(row["query"], "followup", row["rationale"])
        for event, entry in self._run_queries([planned]):
            yield event
            if entry is not None and entry[3] is not None:
                rankstore.mark_followup_run(self.conn, row["row_id"], entry[3])
        yield from self._judge_new()

    # ------------------------------------------------------------------ done
    def _done(self) -> dict[str, Any]:
        ranked = self.ranked()
        questions = self.questions
        if self.conn is not None and self.root_id is not None:
            from library import rankstore

            with contextlib.suppress(sqlite3.Error):
                questions = rankstore.list_questions(self.conn, self.root_id)
        return self._ev(
            "done",
            search=self.root_uid,
            query=self.query,
            counts=self.counts(),
            ranking=[
                {
                    "position": i.position,
                    "site": i.site,
                    "id": i.model_id,
                    "search": i.session_uid,
                    "verdict": i.verdict.verdict if i.verdict else None,
                    "score": i.verdict.score if i.verdict else None,
                    "rationale": i.verdict.rationale if i.verdict else None,
                }
                for i in ranked
            ],
            queries=self.queries_run,
            questions=questions,
            llm=self.client.summary() if self.client is not None else None,
            degraded=self.degraded,
            elapsed_s=round(time.monotonic() - self._started, 3),
        )


def collect(events: Iterator[dict[str, Any]]) -> dict[str, Any]:
    """Drain `events` and return the `done` event (what a non-streaming caller needs); raises RuntimeError when
    the stream ended without one."""
    done: dict[str, Any] | None = None
    for event in events:
        if event["event"] == "done":
            done = event
    if done is None:
        raise RuntimeError("the ranking stream ended without a `done` event")
    return done
