"""library/rankstore.py — what `3d models search --rank` keeps in the library DB, and how it is read back.

WHAT: the session TREE of one search (the root `search_session` plus every rewrite/wide/narrow/followup session
below it, linked by `parent_id`), the stored result rows with their verdict/score/rationale, the planner's
queries (`search_plan_query`), the clarifying questions and their answers (`search_question`) and the ledger of
model calls (`llm_call`). `library.rankflow` writes through these functions; `3d models searches show`,
`models followups`, `models rerank` and `library.actions.rank` read through them.

ACCESSED VIA: `library.rankflow`, `commands/models.py`, `library.actions`.

INVARIANTS:
  - Every function takes the library connection (`sqlite3.Row` rows) and never opens one; a failed write raises
    sqlite3.Error for the caller to degrade on (rankflow turns it into a `library` warning).
  - An answer is validated against its question: a single choice must be one of `choices`, a multi answer a
    comma-separated subset; a key no question asked becomes a free-text answer (`3d models search --answer K=V`).
  - Answers are stored as JSON (a string, or a list for multi) and read back as plain strings (`answers_map`).
  - Stdlib-only at module top (checked by tests/test_imports.py).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from errors import InvalidArgument, UsageError
from library import db as librarydb
from library.llm import CallRecord
from library.planner import PlannedQuery, Question
from printops import db


# --------------------------------------------------------------------------- the session tree
def tree_sessions(conn: sqlite3.Connection, root_id: int) -> list[sqlite3.Row]:
    """The root session and all its descendants, oldest first."""
    return conn.execute(
        "WITH RECURSIVE tree(id) AS (SELECT ? UNION SELECT s.id FROM search_session s JOIN tree t ON s.parent_id = t.id) "
        "SELECT * FROM search_session WHERE id IN (SELECT id FROM tree) ORDER BY id",
        (root_id,),
    ).fetchall()


def tree_queries(conn: sqlite3.Connection, root_id: int) -> list[str]:
    """Every query already run in the tree (the root's and each child's)."""
    return [str(r["query"]) for r in tree_sessions(conn, root_id)]


def session_results(conn: sqlite3.Connection, session_id: int) -> list[dict[str, Any]]:
    """The stored result rows of one session in the order the search returned them: the full record plus
    the verdict columns."""
    rows = conn.execute(
        "SELECT site, model_id, record, verdict, score, rationale FROM search_result WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    return [
        {
            "site": r["site"],
            "model_id": r["model_id"],
            "record": json.loads(r["record"]),
            "verdict": r["verdict"],
            "score": r["score"],
            "rationale": r["rationale"],
        }
        for r in rows
    ]


def session_row(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    """A session by full uid, integer id or `latest` (the same references `models searches show` takes)."""
    from library import search as searchcore

    return searchcore._resolve_session(conn, ref)


# --------------------------------------------------------------------------- questions and answers
def _decode_answer(raw: str | None) -> str | list[str] | None:
    return None if raw is None else json.loads(raw)


def add_questions(
    conn: sqlite3.Connection, root_id: int, questions: Iterable[Question]
) -> None:
    """Persist the planner's questions; a key that already exists for this search is left as it is."""
    for q in questions:
        librarydb.insert_row(
            conn,
            "search_question",
            {
                "session_id": root_id,
                "key": q.key,
                "type": q.type,
                "prompt": q.prompt,
                "choices": list(q.choices),
            },
            source_key=f"question:{root_id}:{q.key}",
        )


def list_questions(conn: sqlite3.Connection, root_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM search_question WHERE session_id = ? ORDER BY id", (root_id,)
    ).fetchall()
    return [
        {
            "id": r["uid"],
            "key": r["key"],
            "type": r["type"],
            "prompt": r["prompt"],
            "choices": json.loads(r["choices"]),
            "answer": _decode_answer(r["answer"]),
            "answered_at": r["answered_at"],
        }
        for r in rows
    ]


def normalise_answer(question: Mapping[str, Any], value: str) -> str | list[str]:
    """`value` checked against its question: single = one of the choices (case-insensitive), multi = a
    comma-separated subset, text = any non-empty text. Raises InvalidArgument with the accepted values."""
    text = " ".join(value.split())
    if not text:
        raise InvalidArgument(
            f"--answer {question['key']}",
            value,
            ["a non-empty value"],
            command="models",
        )
    choices = list(question["choices"])
    by_fold = {c.casefold(): c for c in choices}
    if question["type"] == "single":
        if text.casefold() not in by_fold:
            raise InvalidArgument(
                f"--answer {question['key']}", value, choices, command="models"
            )
        return by_fold[text.casefold()]
    if question["type"] == "multi":
        picked = [p.strip() for p in text.split(",") if p.strip()]
        bad = [p for p in picked if p.casefold() not in by_fold]
        if bad or not picked:
            raise InvalidArgument(
                f"--answer {question['key']}", value, choices, command="models"
            )
        return list(dict.fromkeys(by_fold[p.casefold()] for p in picked))
    return text


def set_answer(
    conn: sqlite3.Connection, root_id: int, key: str, value: str
) -> str | list[str]:
    """Record the answer to question `key` of this search (a key nobody asked becomes a free-text answer)."""
    questions = {q["key"]: q for q in list_questions(conn, root_id)}
    if key not in questions:
        if (
            not key
            or not key.replace("_", "").isalnum()
            or not key[0].isalpha()
            or key != key.lower()
            or len(key) > 32
        ):
            raise InvalidArgument(
                "--answer",
                key,
                ["KEY=VALUE with KEY like need_cad (a-z, 0-9, _)"],
                command="models",
            )
        librarydb.insert_row(
            conn,
            "search_question",
            {
                "session_id": root_id,
                "key": key,
                "type": "text",
                "prompt": f"{key} (given on the command line)",
                "choices": [],
            },
            source_key=f"question:{root_id}:{key}",
        )
        questions[key] = {"key": key, "type": "text", "choices": []}
    normalised = normalise_answer(questions[key], value)
    with db.transaction(conn):
        conn.execute(
            "UPDATE search_question SET answer = ?, answered_at = ? WHERE session_id = ? AND key = ?",
            (json.dumps(normalised, ensure_ascii=False), db.utc_now(), root_id, key),
        )
    return normalised


def answers_map(conn: sqlite3.Connection, root_id: int) -> dict[str, str]:
    """The answered questions as `{key: text}` (a multi answer is its choices joined by ", ")."""
    out: dict[str, str] = {}
    for q in list_questions(conn, root_id):
        answer = q["answer"]
        if answer is None:
            continue
        out[q["key"]] = ", ".join(answer) if isinstance(answer, list) else str(answer)
    return out


def parse_answer_args(items: Sequence[str]) -> dict[str, str]:
    """`--answer KEY=VALUE` (repeatable) as a dict; a repeated KEY keeps the last value."""
    parsed: dict[str, str] = {}
    for item in items:
        key, eq, value = item.partition("=")
        if not eq or not key.strip():
            raise InvalidArgument(
                "--answer", item, ["KEY=VALUE, e.g. need_cad=yes"], command="models"
            )
        parsed[key.strip()] = value
    return parsed


# --------------------------------------------------------------------------- planned queries
def add_plan_queries(
    conn: sqlite3.Connection,
    root_id: int,
    items: Sequence[tuple[PlannedQuery, str, str | None, int | None]],
) -> list[dict[str, Any]]:
    """Persist planner queries as `(query, status, skip_reason, run_session_id)` rows with the next `seq`;
    returns them as dicts (with their `uid`)."""
    start = int(
        conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM search_plan_query WHERE session_id = ?",
            (root_id,),
        ).fetchone()[0]
    )
    out: list[dict[str, Any]] = []
    for offset, (planned, status, skip_reason, run_session_id) in enumerate(
        items, start=1
    ):
        ins = librarydb.insert_row(
            conn,
            "search_plan_query",
            {
                "session_id": root_id,
                "seq": start + offset,
                "query": planned.query,
                "kind": planned.kind,
                "rationale": planned.rationale,
                "status": status,
                "skip_reason": skip_reason,
                "run_session_id": run_session_id,
            },
        )
        out.append({"id": ins.uid, "seq": start + offset})
    return out


def list_plan_queries(conn: sqlite3.Connection, root_id: int) -> list[dict[str, Any]]:
    """Every planner query of the search with its status; `followup` rows also carry their 1-based `n`."""
    rows = conn.execute(
        "SELECT p.*, s.uid AS run_uid FROM search_plan_query p LEFT JOIN search_session s ON s.id = p.run_session_id "
        "WHERE p.session_id = ? ORDER BY p.seq",
        (root_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    follow_no = 0
    for r in rows:
        n: int | None = None
        if r["kind"] == "followup":
            follow_no += 1
            n = follow_no
        out.append(
            {
                "id": r["uid"],
                "seq": r["seq"],
                "n": n,
                "query": r["query"],
                "kind": r["kind"],
                "rationale": r["rationale"],
                "status": r["status"],
                "skip_reason": r["skip_reason"],
                "search": r["run_uid"],
                "row_id": r["id"],
            }
        )
    return out


def mark_followup_run(
    conn: sqlite3.Connection, plan_row_id: int, session_id: int
) -> None:
    with db.transaction(conn):
        conn.execute(
            "UPDATE search_plan_query SET status = 'run', run_session_id = ? WHERE id = ?",
            (session_id, plan_row_id),
        )


def get_followup(conn: sqlite3.Connection, root_id: int, number: int) -> dict[str, Any]:
    """Suggested follow-up number `number` (1-based) of this search; UsageError naming the valid numbers."""
    followups = [p for p in list_plan_queries(conn, root_id) if p["kind"] == "followup"]
    if not 1 <= number <= len(followups):
        have = (
            f"1..{len(followups)}"
            if followups
            else "none (run `3d models search --rank` first)"
        )
        raise UsageError(
            f"no follow-up {number} for this search; available: {have}",
            command="models",
            remediation=["List them with `3d models followups ID`."],
        )
    return followups[number - 1]


# --------------------------------------------------------------------------- the call ledger
def add_llm_calls(
    conn: sqlite3.Connection, root_id: int, records: Iterable[CallRecord]
) -> None:
    """Persist the model calls not stored yet (each record once)."""
    for record in records:
        if record.persisted:
            continue
        librarydb.insert_row(
            conn,
            "llm_call",
            {
                "session_id": root_id,
                "role": record.role,
                "model": record.model,
                "status": record.status,
                "items": record.items,
                "elapsed_s": round(record.elapsed_s, 3),
                "input_tokens": record.input_tokens,
                "output_tokens": record.output_tokens,
                "cache_read_tokens": record.cache_read_tokens,
                "cache_write_tokens": record.cache_write_tokens,
                "cost_usd": record.cost_usd,
                "detail": record.detail or None,
            },
        )
        record.persisted = True


def list_llm_calls(conn: sqlite3.Connection, root_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM llm_call WHERE session_id = ? ORDER BY id", (root_id,)
    ).fetchall()
    return [
        {
            "role": r["role"],
            "model": r["model"],
            "status": r["status"],
            "items": r["items"],
            "elapsed_s": r["elapsed_s"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "cache_read_tokens": r["cache_read_tokens"],
            "cache_write_tokens": r["cache_write_tokens"],
            "cost_usd": r["cost_usd"],
            "detail": r["detail"],
        }
        for r in rows
    ]
