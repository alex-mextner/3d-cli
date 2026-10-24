-- 0030_llm_ranking.sql — LLM relevance and query planning of `3d models search --rank` (umbrella #570, S6 #577).
--   llm_verdict_cache   one verdict per (judged query, model, result, result-card hash): a repeat run, a rerank
--                       or a second session that finds the same card makes no model call. `query` is the user's
--                       question PLUS the answered constraints, so a new answer re-judges, a repeat does not.
--   search_plan_query   every query the planner proposed: kinds wide|narrow|rewrite were run (status `run`,
--                       run_session_id = the search_session it created, whose parent_id is the root search),
--                       kind followup is only suggested (status `suggested`) until `--follow N` runs it.
--   search_question     the planner's clarifying questions (type single|multi|text) and the user's answers.
--   llm_call            one row per model call of a search (role, model, status, latency, token use), so cost and
--                       failures stay inspectable after the run.
-- search_result.verdict/score/rationale already exist (0001); the ranker fills them.
CREATE TABLE llm_verdict_cache (
    query       TEXT NOT NULL,
    model       TEXT NOT NULL,
    site        TEXT NOT NULL,
    model_id    TEXT NOT NULL,
    input_hash  TEXT NOT NULL,
    verdict     TEXT NOT NULL CHECK (verdict IN ('fits', 'maybe', 'no')),
    score       REAL NOT NULL CHECK (score >= 0 AND score <= 1),
    rationale   TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (query, model, site, model_id, input_hash)
);

CREATE TABLE search_plan_query (
    id             INTEGER PRIMARY KEY,
    uid            TEXT NOT NULL UNIQUE,
    source_key     TEXT UNIQUE,
    session_id     INTEGER NOT NULL REFERENCES search_session(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    query          TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('wide', 'narrow', 'followup', 'rewrite')),
    rationale      TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL CHECK (status IN ('suggested', 'run', 'skipped')),
    skip_reason    TEXT,
    run_session_id INTEGER REFERENCES search_session(id) ON DELETE SET NULL,
    created_at     TEXT NOT NULL,
    UNIQUE (session_id, seq)
);

CREATE TABLE search_question (
    id          INTEGER PRIMARY KEY,
    uid         TEXT NOT NULL UNIQUE,
    source_key  TEXT UNIQUE,
    session_id  INTEGER NOT NULL REFERENCES search_session(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('single', 'multi', 'text')),
    prompt      TEXT NOT NULL,
    choices     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(choices) AND json_type(choices) = 'array'),
    answer      TEXT CHECK (answer IS NULL OR json_valid(answer)),
    answered_at TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (session_id, key)
);

CREATE TABLE llm_call (
    id                 INTEGER PRIMARY KEY,
    uid                TEXT NOT NULL UNIQUE,
    source_key         TEXT UNIQUE,
    session_id         INTEGER NOT NULL REFERENCES search_session(id) ON DELETE CASCADE,
    role               TEXT NOT NULL CHECK (role IN ('classify', 'plan')),
    model              TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('ok', 'invalid', 'timeout', 'error', 'budget')),
    items              INTEGER NOT NULL DEFAULT 0,
    elapsed_s          REAL,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    cache_read_tokens  INTEGER,
    cache_write_tokens INTEGER,
    cost_usd           REAL,
    detail             TEXT,
    created_at         TEXT NOT NULL
);

CREATE INDEX llm_call_by_session ON llm_call(session_id);
