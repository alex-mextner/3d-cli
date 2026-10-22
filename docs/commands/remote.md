# `3d remote` — run it on the big machine

Pushes files to an ssh box, runs a command there under the box's `heavy` wrapper (one job at a time,
memory cap, logs), and pulls the results back. The reference box is a 16-core / 30 GB Ubuntu machine
with `~/projects/<name>-<date>/{work,out,logs,cache}` and `~/projects/bin/heavy`. Issue:
[#431](https://github.com/alex-mextner/3d-cli/issues/431).

Why: the Chekhov session ran every slow mesh step on a shared workstation, then needed a 20-minute agent
task to provision the box and hand-typed rsync/ssh lines (a ~1.7 MB/s uplink makes a typo expensive).

## Usage

```
3d remote hosts
3d remote push [--host H] [--project P] PATH [PATH...]
3d remote run  [--host H] [--project P] [--name N] [--wait] [--no-heavy] [--push PATH]... [--pull [SUBDIR]] [--to DIR] -- CMD [ARG...]
3d remote pull [--host H] [--project P] [--sub SUBDIR] [--to DIR]
3d remote exec [--host H] [--project P] -- CMD [ARG...]
3d remote status [--host H] [--project P]
```

Config `~/.config/3d-cli/remote.yaml` (or `$THREED_REMOTE_CONFIG`):

```yaml
default: heavy
hosts:
  heavy:
    ssh: ultra@65.21.40.51
    root: projects            # remote dir holding <project>/{work,out,logs}, relative to $HOME
    heavy: projects/bin/heavy # the box's lock wrapper; null to run commands directly
```

- `--host` takes a name from the config or an ad hoc `user@host`; without it the config default (or the only
  host) is used.
- `--project` defaults to `<current dir>-<YYYY-MM-DD>`, the `projects/<name>-<date>` convention of the box.
- `push` rsyncs into `<root>/<project>/work/` (skips `.git`, `__pycache__`, `.venv`).
- `run` does `cd <work> && heavy -n NAME [--wait] -- CMD` over ssh and returns the remote exit code;
  `--wait` queues behind another heavy job instead of failing (exit 75); `--no-heavy` runs CMD directly;
  `--push PATH` copies files first (a failed copy stops before running); `--pull [SUBDIR]` copies
  `<project>/out/` (or SUBDIR) into `--to` (default `./remote-out`) afterwards — also after a failed
  command, because partial outputs and logs are the diagnosis (`--no-pull-on-fail` turns that off).
- `status` shows the box's load, the newest logs of the project and whether the heavy lock is free.
- Uses key-based ssh only (`BatchMode=yes`); nothing here prompts for a password.

Exit code: the remote command's (or rsync's on a failed push/pull), 2 for bad arguments, 127 when `ssh` or `rsync` is missing.

Examples:

```
3d remote hosts
3d remote push --project chekhov cat.stl phones.json
3d remote run --project chekhov --name prep --wait --push cat.stl --pull -- 3d prep cat.stl --out ../out
3d remote run --no-heavy -- nproc
3d remote pull --project chekhov --sub logs --to ./logs
3d remote exec --project chekhov -- ls out
3d remote status --project chekhov
```

Never send G-code, 3MF or STL to a chat from the box; copy them back with `pull` instead.
