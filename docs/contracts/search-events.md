# Search ranking events contract (umbrella #570, S6 #577)

What `3d models search --rank`, `rerank` and `followups --follow/--answer` produce, in a form agents (`--ndjson`) and
the terminal UI (S7, #578) can rely on. The implementation is `lib/library/rankflow.py`; the text view
(`lib/library/rankcli.py`) and `--ndjson` are renderers of the same event stream.

## Two phases

1. **Phase 1** — the S5 search runs (`3d.models.search/2`) and its results are emitted in the deterministic order of
   the search (site order of `search.sites`, then the site's own rank). Nothing waits on a model. The text view
   prints the normal result list and flushes stdout at this point.
2. **Phase 2** — `phase` marks the start of model work. The classifier's `verdict` events arrive batch by batch (a
   consumer re-sorts in place); the planner may add `plan`, `query`, more `result`/`verdict` events, and finally
   `question` / suggested-`query` events; `done` carries the final ranking.

Without ranking (`--no-llm`, or `3d models search ... --ndjson` without `--rank`) the stream ends after phase 1:
`start, search, result*, done` (`done.llm` is `null`, every result `unjudged`). When the model is unavailable the
stream is the same plus ONE `warning` event with `code: "llm_unavailable"`.

## Event envelope (`3d.models.rank/1`)

`--ndjson` prints one JSON object per line, flushed per event. Every event has:

| key | meaning |
|---|---|
| `schema` | always `"3d.models.rank/1"` |
| `event` | one of `start, search, phase, result, verdict, plan, query, question, warning, done` |
| `seq` | 1, 2, 3 ... strictly increasing within one run, no gaps |
| `t` | seconds since the run started (3 decimals) |

New event kinds and new keys MAY be added without changing the schema version; a consumer ignores what it does not
know. Removing or re-typing a key bumps the version (`/2`).

## Events

**`start`** (first): `kind` (`search` for a new search, `session` for `rerank` / `followups`), `query`, `llm`
(`enabled`, `backend`, `classifier`, `planner`, `batch_size`, `rewrite_below`, `max_calls`; null models when
disabled), `answers` (`{key: value}` constraints in force).

**`search`**: `search` (session uid, or null when the library DB could not be written), `query`, `sites`,
`counts` (`{site: n}`), `timings`, `errors` (`[{site, kind, message, remediation}]` — a site that failed).

**`result`**: `search` (the session uid that found it), `position` (1-based, stable, in discovery order across the
whole run), `site`, `id`, `rank` (its rank inside its own search), `title`, `url`, `query` (the query that found
it), `kind` (`user` for the original search, else `rewrite | wide | narrow | followup`), `card` (the full result
record of `3d.models.search/2`). A (site, id) appears in `result` events at most once per run.

**`phase`**: `phase: 2`, `status: "start"`, `results` (how many results phase 2 will judge). Emitted only when a
model backend is ready, before the first call.

**`verdict`**: `search`, `position`, `site`, `id` (identify the earlier `result`), `verdict` (`fits | maybe | no`),
`score` (0..1), `rationale` (one line), `cached` (answered from the library DB, no model call), `model`. Within one
batch verdicts are ordered by `position`; batches arrive in completion order.

**`plan`**: `status` `running | ready | failed`, `mode` (`rewrite`: too few fits, queries will run; `suggest`: only
follow-ups and questions), `model`, `trigger` (e.g. `"1 fits < rewrite_below 3"`). `ready` adds `summary`, `queries`
(the ones that will run: `{query, kind, rationale}`), `skipped` (`{query, kind, rationale, reason}`: a duplicate of
a query already run, or over `--max-queries`), `followups`, `questions` (`{key, type, prompt, choices}`) and
`problems` (what was invalid in the planner's answer; the valid rest is used). `failed` adds `reason`.

**`query`**: `status` `run` — a planner query or a followed suggestion was executed: `search` (its own session
uid), `query`, `kind` (`rewrite | wide | narrow | followup`), `rationale`, `parent` (the root search uid),
`counts` (`{site: n}`), `results`, `new` (results added to the pool), `duplicates` (already in the pool; they get
the pooled verdict on their own session row), `errors`. Its `result` events follow immediately.
`status` `suggested` — a follow-up the user may run: `id`, `n` (1-based follow-up number for `--follow N`), `query`,
`kind: "followup"`, `rationale`, `parent`, `search: null`.

**`question`**: `search` (the root search uid), `id` (uid), `key` (`[a-z][a-z0-9_]{0,31}`, what `--answer KEY=VALUE`
takes), `type` (`single | multi | text`), `prompt`, `choices` (empty for `text`), `answer` (null, or the answer:
a string, or a list of strings for `multi`). A single answer MUST be one of `choices`; a multi answer is a
comma-separated subset; a text answer is any non-empty text.

**`warning`**: `code`, `message`, and context keys. Codes: `llm_unavailable` (`message` is exactly
`LLM: unavailable (<why>)`; `role` `classify | plan`, `reason` `unavailable | timeout | invalid | budget | error`;
at most one per role per run), `search_warning` (a site note), `library` (the DB could not be written),
`plan_incomplete`, `query_failed`, `follow_failed`.

**`done`** (last, exactly once): `search`, `query`, `counts` (`{results, fits, maybe, no, unjudged}`), `ranking`
(every result in final order: `{position, site, id, search, verdict, score, rationale}`; `verdict` is null for an
unjudged result), `queries` (the queries run), `questions` (with their current answers), `llm` (null when no model
was used, else `{backend, calls, by_status, elapsed_s, input_tokens, output_tokens, cache_read_tokens,
cache_write_tokens, total_tokens, cost_usd, calls_detail[]}`; token fields are null when the backend does not report
them), `degraded` (the reason a model was unavailable, else null), `elapsed_s`.

## Order guarantees

- `start` is first, `done` is last and unique; nothing follows `done`.
- `search` precedes every `result`; all phase-1 `result` events precede `phase`; `phase` precedes every `verdict`.
- A `verdict` always follows the `result` it refers to. A `query` event with status `run` precedes the `result`
  events of that query; the verdicts of new results come after all of them.
- `plan` (`running`, then `ready` or `failed`) comes after the original results' verdicts; `question` and
  suggested-`query` events come after every `verdict` and before `done`.
- The final order is `done.ranking`: fits, maybe, unjudged, no; inside a class by `score` descending, then
  `position`. A consumer that sorts live uses exactly that key.

## Persistence

Every verdict, planner query, question, answer and model call is written to the library DB as it happens
(`search_result.verdict/score/rationale`, `search_plan_query`, `search_question`, `llm_call`, `llm_verdict_cache`;
`docs/contracts/library-db.md`). Planner queries that ran are `search_session` rows with `kind rewrite | wide |
narrow | followup` and `parent_id` = the root search. `3d models searches show ID --json` returns the root document
with `verdict`/`score`/`rationale` on judged results plus `queries` and `questions`.

## Non-interactive drive (agents)

```bash
3d models search "soil moisture sensor probe case" --rank --ndjson          # read questions + follow-ups
3d models rerank SEARCH_ID --answer need_cad=yes --answer max_size=60 --follow 1 --ndjson
3d models followups SEARCH_ID --json                                        # state of questions and follow-ups
```

`--answer` re-judges the results with the answers as constraints; `--follow N` runs follow-up N as a `followup`
session under the same root and judges its hits. Exit code 0 unless no site answered (as `search`); a model failure
is never an exit code. `--llm require` exits 127 before the search when omp is missing.

## In-process contract (S7, the TUI)

- `library.rankflow.Pipeline(RankOptions(config), SearchOptions(...)).search(query)` and `.session(ref)` are
  generators of the events above; iterate them on a worker thread and post the events to the UI. `RankOptions`
  fields: `config` (`library.llm.load_llm_config(overrides)`), `enabled`, `require`, `rewrite`, `answers`,
  `follow`, `refresh`, `backend` (injected backend, tests). After the run `pipeline.doc`, `.failures`, `.degraded`,
  `.items`, `.ranked()` hold the outcome. Closing the generator early is safe (the DB connection is closed).
- `library.actions.rank(session, mode="deterministic" | "llm", limit=None, db_path=None, classifier=None,
  planner=None, rewrite=True, answers=None, follow=(), refresh=False, require=False)` is the one-shot action: it
  drains the same stream and returns `{session, query, ranked[{result, session, site, model_id, rank, score,
  verdict, rationale}], counts, queries, questions, followups, llm}` (`3d.action/1`). An unavailable model is a
  warning `llm_unavailable`, not an error; `require=True` makes it an `error` result. UI keys map to:
  rank all → `rank(mode="llm")`; answer a question → `rank(mode="llm", answers={key: value})`; run a follow-up →
  `rank(mode="llm", follow=[n])`; show what was saved → `rank(mode="deterministic")` (no model call).
- Questions to render: `question.type` `single` → a list to pick one of `choices`; `multi` → a checklist; `text` →
  an input line. Submit the answer back with `--answer KEY=VALUE` / `answers={key: value}`.
