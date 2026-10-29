# Finding a model as an agent: search, judge, answer, pick, download, hand over

How an agent (or a script) drives the model search of `3d models` without any terminal. Every step is a command with
`--json` / `--ndjson` and an exit code, built on the same `library.actions` functions the interactive browser uses
(`docs/commands/tui.md`). The transcript below is real output of the offline e2e fixtures (loopback site fakes, a fake
`omp` that judges by title number, a fake slicer, the recording fake `tmux` / `omp`); the uids and paths are shortened.

## The loop

```
search --rank --ndjson ──► read verdicts, questions, follow-ups ──► answer / follow / refine (re-ranks)
        ▲                                                                   │
        └───────────────────────────────────────────────────────────────────┘
                                                                            ▼
        act --do list ──► pick rows ──► act --do download ──► slicer | agent | queue | open
        (dry run first, then --yes)
```

| Step | Command | Reads |
|---|---|---|
| 1 search | `3d models search "QUERY" --rank --ndjson` | `3d.models.rank/1` events: `result`, `verdict`, `plan`, `question`, `query`, `done` (`docs/contracts/search-events.md`) |
| 2 read | `3d models act $S --do list --json` | `3d.models.act/1` with `results[]` (`n`, `key`, `verdict`, `score`, `rationale`, `size_mm`, `cad`, `license`, `downloaded`), `questions[]`, `followups[]` |
| 3 answer | `3d models act $S --answer KEY=VALUE --follow N --refine "NEW QUERY" --do list --json` | the same document after the re-rank (`--ndjson` streams the re-rank events first) |
| 4 select | `--select 1,3,5-7`, `--select printables:311253`, `--select-site printables`, `--select-verdict fits`, `--filter TEXT` | numbers are ranking positions of step 2; selectors combine (all must match) |
| 5 act | `--do preview\|open\|download\|slicer\|agent\|queue` | `results[]` of `3d.action/1`; anything that starts something is a **dry run until `--yes`** |

`$S` is the `search` uid from the `done` event (or `latest`: the newest search tree).

Exit codes: `0` done (or a dry run), `1` an action failed (read `results[].errors[]`), `2` bad usage (`--select 99`,
nothing selected, unknown `--do` / `--agent`, a bad answer: the message lists the accepted values), `127` a program is
missing (slicer, browser opener, agent, `omp` with `--llm require`): the error names the setting that fixes it.

## Transcript

```console
$ 3d models search case --site printables --limit 10 --rank --ndjson     # step 1, events elided
{"schema":"3d.models.rank/1","event":"start", ...}
...
{"schema":"3d.models.rank/1","event":"done","search":"01a16f87-...","counts":{"results":10,"fits":3,"maybe":2,"no":5,"unjudged":0}, ...}
```

Step 2 - read the ranked list, the questions and the follow-ups (trimmed to the first rows):

```console
$ 3d models act $S --do list --json
{ "schema": "3d.models.act/1", "search": "$S",
  "counts": {"results": 10, "fits": 3, "maybe": 2, "no": 5, "unjudged": 0},
  "results": [
    {"n": 1, "key": "printables:3", "title": "Printables model 3", "verdict": "fits", "score": 0.9,
     "rationale": "title number 3 is a fit", "downloaded": null},
    {"n": 2, "key": "printables:6", "title": "Printables model 6", "verdict": "fits", "score": 0.9, ...},
    {"n": 3, "key": "printables:9", ...}, {"n": 4, "key": "printables:5", "verdict": "maybe", ...}, ...],
  "questions": [
    {"key": "need_cad", "type": "single", "prompt": "Do you need CAD files?", "choices": ["yes", "no"], "answer": null},
    {"key": "max_size", "type": "text", "prompt": "Maximum size in mm?", "choices": [], "answer": null}],
  "followups": [{"n": 1, "query": "plant stake holder", "rationale": "a stake variant", "status": "suggested"}] }
# exit 0
```

Step 3 - answer a question and run follow-up 1 in one command, then list only what fits. A `single` answer must be one
of `choices`, a `multi` answer a comma-separated subset, a `text` answer any text; the answer is saved, applied as a
constraint and the results are judged again (the fake model here ignores the constraint, a real one does not):

```console
$ 3d models act $S --answer need_cad=yes --follow 1 --do list --json --select-verdict fits
{ "counts": {"results": 10, "fits": 3, ...},
  "results": [{"n": 1, "key": "printables:3", "verdict": "fits", "downloaded": null}, {"n": 2, ...}, {"n": 3, ...}],
  "questions": [["need_cad", "yes"], ["max_size", null]],
  "followups": [[1, "run"]] }
# exit 0
$ 3d models act $S --answer need_cad=perhaps --do list
models: got --answer need_cad='perhaps'; accepted: yes, no
  -> Run '3d models --help' for accepted values.
# exit 2
$ 3d models act $S --select 99 --do preview
models: got --select='99'; accepted: 1..10
  -> Run '3d models --help' for accepted values.
# exit 2
```

Step 5 - download is a dry run until `--yes`; the plan names the folders and the exact command:

```console
$ 3d models act $S --select-verdict fits --do download --dir ./parts
search $S  'case'  action download  (dry run)
   1. Printables model 3  [printables]  https://www.printables.com/model/3-model-3
   2. Printables model 6  [printables]  https://www.printables.com/model/6-model-6
   3. Printables model 9  [printables]  https://www.printables.com/model/9-model-9
  would download Printables model 3 to ./parts/printables-3-Printables-model-3
  would download Printables model 6 to ./parts/printables-6-Printables-model-6
  would download Printables model 9 to ./parts/printables-9-Printables-model-9
dry run: nothing was started. Run it with: 3d models act $S --do download --yes --dir ./parts --select printables:3,printables:6,printables:9
# exit 0

$ 3d models act $S --select 1 --do download --dir ./parts --yes --json
{ "action": "download", "ok": true, "dry_run": false, "exit_code": 0,
  "results": [{"schema": "3d.action/1", "action": "download", "ok": true,
    "data": {"source": "<uid>", "dir": "./parts/printables-3-Printables-model-3",
             "files": [{"name": "project_3.stl", "size": 684, "sha256": "c46d8c08..."},
                       {"name": "project_3.f3d", "size": 684, "sha256": "c46d8c08..."}],
             "license": "CC-BY", "role": null},
    "errors": [], "warnings": []}],
  "cli": "3d models act $S --do download --yes --dir ./parts --select printables:3" }
# exit 0
```

`source` is the `model_source` row in the library DB (URL, licence, author, file hashes); a model that is already
recorded with an existing folder is **reused**, never fetched twice:

```console
$ 3d models act $S --select 1 --do slicer --dir ./parts --slicer ~/bin/fake-slicer --yes
search $S  'case'  action slicer
   1. Printables model 3  [printables]  https://www.printables.com/model/3-model-3
  already downloaded: Printables model 3 -> ./parts/printables-3-Printables-model-3
  ok: started fake-slicer with 1 file(s)
equivalent: 3d models act $S --do slicer --yes --dir ./parts --select printables:3
# exit 0
```

Launch an agent on the picks. The brief it is seeded with carries the query, the URLs, the verdicts, the downloaded
paths and the `model.json` provenance; `--agent-mode tmux` starts it in a detached tmux session an agent can `tmux
attach` to later (`attach` takes this terminal, `detached` has none):

```console
$ 3d models act $S --select 1 --do agent --agent-mode tmux --prompt "judge this model" --dir ./parts --yes --json
{ "action": "agent", "ok": true, "exit_code": 0,
  "results": [{"schema": "3d.action/1", "action": "launch_agent", "ok": true,
    "data": {"backend": "omp", "command": ["omp", "@~/.local/state/3d-cli/agent-briefs/$S/brief.md", "judge this model"],
             "cwd": "./parts", "mode": "tmux", "brief": "~/.local/state/3d-cli/agent-briefs/$S/brief.md",
             "started": true, "tmux": "3d-search-01a16f87-3d89-7e7e-89af"},
    "errors": [], "warnings": []}] }
```

```markdown
# Model search brief

Original query: case
Search session (library DB): $S

Task: judge this model

## Selected models

1. Printables model 3
   - site: printables
   - url: https://www.printables.com/model/3-model-3
   - verdict: fits
   - why: title number 3 is a fit
   - licence: CC-BY
   - downloaded to: ./parts/printables-3-Printables-model-3
   - files: project_3.stl, project_3.f3d
   - provenance (model.json): {"source": {"site": "printables", "id": "3", "url": "https://www.printables.com/model/3-model-3"}, ...}
```

## Rules of thumb

- **Read before you act.** Step 2 is free (no network, no model). Numbers are ranking positions and change when a
  re-rank changes the order, so re-run `--do list` after `--answer` / `--follow` / `--refine` or select by key
  (`printables:3`), which never shifts.
- **Dry run first.** Without `--yes` nothing is downloaded, opened or started; the document says `dry_run: true` and the
  `cli` field is the command to run for real.
- **Licences.** `download` records the licence in the library DB and `model.json`; a paid model is refused (exit 1). Check
  `license` in the list before you hand a model to a slicer or an agent.
- **Questions are optional.** An unanswered question never blocks `act`; answering re-judges the pool.
- **No terminal needed, one is welcome.** The same session can be opened by a person with `3d models browse $S`
  (`docs/commands/tui.md`): the selection, verdicts and answers live in the library DB.
- **Configured programs.** The slicer, browser and agent come from `--slicer`, `--browser`, `--agent` or
  `~/.config/3d-cli/browser.json`; nothing is hard-coded and a missing program is exit 127 naming the setting.
