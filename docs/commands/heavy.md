# `3d heavy` — one heavy job at a time, with a memory cap

Runs a command alone on the machine under `/tmp/3d-cli-heavy.lock` with a systemd memory/CPU
cap and `nice`. This is the wrapper AGENTS.md asks for on every heavy run (a full test suite, a big
mesh job, a slice), as a command that cannot deadlock itself. Issue:
[#431](https://github.com/alex-mextner/3d-cli/issues/431).

Why: the Chekhov session lost 25 minutes (and 4 more re-running) when a proof script wrapped itself in
`flock /tmp/3d-cli-heavy.lock ...` while it was already started inside the same `flock`: the inner call
waited for its own parent until the 1500 s command timeout.

## Usage

```
3d heavy [--wait SECONDS] [--mem SIZE] [--cpu PERCENT] [--nice N] -- CMD [ARG...]
3d heavy status
3d heavy prefix [--mem SIZE] [--cpu PERCENT] [--wait SECONDS]
```

- The wrapped command runs with `THREED_HEAVY_HELD=1`; inside it `3d heavy`, `3d prep` and
  `cli.heavy.heavy_prefix()` see the marker and run directly instead of locking again. Scripts that
  hand-roll the lock keep the old hazard: use `$(3d heavy prefix)` or `3d heavy --` instead.
- `--wait` is the longest queue time (default 1800 s). When it runs out the exit code is 75 and
  `3d heavy status` shows the holder.
- `--mem` (default 6G), `--cpu` (default 600%) and `--nice` (default 10) size the cap. Without systemd
  (macOS, containers) only the lock and `nice` apply.
- `status` prints `held by pid N: COMMAND` or `free` (needs `lslocks`).
- `prefix` prints the argv prefix for a shell script: `$(3d heavy prefix) python3 job.py`.
- `THREED_HEAVY_LOCK=/path` moves the lock file (tests, other boxes).

Exit code: the wrapped command's own, 75 when the wait ran out, 2 for bad arguments.

Examples:

```
3d heavy -- 3d prep model.stl --printer u1
3d heavy --wait 600 --mem 12G -- python3 big_mesh_job.py
3d heavy --cpu 400% --nice 15 -- 3d slice part.3mf
3d heavy status
$(3d heavy prefix) python3 job.py        # shell scripts: wrap one command by hand
```
