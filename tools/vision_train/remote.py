"""remote.py — run tier (b) training on the user's own GPU server; nothing large ever enters git.

    remote.py preflight                         read-only look at the server: GPU, RAM, disk, load,
                                                python/uv/docker/rsync (no files written, nothing started)
    remote.py plan RUN --dataset DIR [...]      print the exact commands `run` would execute
    remote.py run RUN --dataset DIR --yes       sync inputs, train under a hard timeout, pull small artifacts
    remote.py pull RUN [--weights]              fetch OUT/ again (--weights: also .pt/.onnx, into the
                                                local cache dir, for publish.py)
    remote.py clean RUN --yes [--all]           free the run directory's venv, caches, copies of lib/data;
                                                out/ (weights, metrics, log: the server-side copy of the
                                                artifacts) stays unless --all

Host: THREED_TRAIN_HOST (default 65.21.40.51); the ssh user, key and port come from ~/.ssh/config
(THREED_TRAIN_USER overrides the user). Remote base directory: THREED_TRAIN_DIR (default
~/3d-cli-train); everything of a run lives under <base>/<RUN>/ (venv, Hugging Face cache, data,
out), so `clean` removes all of it. No secret is ever sent: the Hugging Face token stays on this
machine (publish.py pushes weights from here after `pull --weights`).

`run` is the only command that starts anything on the server, and it needs --yes. The remote
command is ONE ssh session: `timeout --kill-after` bounds the training (--max-hours, default 2),
a trap kills the children on exit or disconnect, a pid file names the run, and the venv is built
only when requirements.txt changed. Pulled artifacts are small by construction: rsync
--max-size=20m and no *.pt/*.onnx/*.safetensors/embeddings unless --weights.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE.parent.parent / "lib"
DEFAULT_HOST = "65.21.40.51"
SSH_OPTS = [
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=15",
    "-o",
    "ServerAliveInterval=30",
    "-o",
    "ServerAliveCountMax=4",
]
HEAVY = ("*.pt", "*.onnx", "*.safetensors", "embeddings.npy")
PREFLIGHT = """\
echo "== uptime"; uptime
echo "== gpu"; nvidia-smi --query-gpu=name,memory.total,memory.used,driver_version --format=csv,noheader 2>&1 || echo "nvidia-smi: missing"
echo "== gpu processes"; nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader 2>&1 || true
echo "== memory (MiB)"; free -m
echo "== disk"; df -h "$HOME"
echo "== tools"; for t in python3 uv docker rsync timeout; do command -v "$t" || echo "$t: missing"; done
python3 --version 2>&1
echo "== top cpu"; ps -eo pid,pcpu,pmem,etime,comm --sort=-pcpu | head -6
"""


def host() -> str:
    h = os.environ.get("THREED_TRAIN_HOST", DEFAULT_HOST)
    user = os.environ.get("THREED_TRAIN_USER")
    return f"{user}@{h}" if user else h


def base_dir() -> str:
    return os.environ.get("THREED_TRAIN_DIR", "~/3d-cli-train")


def run_dir(run: str) -> str:
    if (
        not run
        or not all(c.isalnum() or c in "-_." for c in run)
        or run.startswith(".")
    ):
        raise SystemExit(f"run name {run!r}: use letters, digits, - _ . only")
    return f"{base_dir()}/{run}"


def local_out(run: str) -> Path:
    return (
        Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
        / "3d-cli"
        / "train"
        / "runs"
        / run
    )


def ssh_cmd(remote: str) -> list[str]:
    return ["ssh", *SSH_OPTS, host(), remote]


def rsync_cmd(src: str, dst: str, *extra: str) -> list[str]:
    return [
        "rsync",
        "-az",
        "--partial",
        "-e",
        "ssh " + " ".join(SSH_OPTS),
        *extra,
        src,
        dst,
    ]


def requirements_hash() -> str:
    return hashlib.sha256((HERE / "requirements.txt").read_bytes()).hexdigest()[:16]


def remote_script(run: str, mode: str, train_args: list[str], max_hours: float) -> str:
    """The one command the server executes for `run` (bash -c, no dependence on a login shell)."""
    d = run_dir(run)
    secs = int(max_hours * 3600)
    train = " ".join(
        shlex.quote(a) for a in ["tools/train.py", mode, "data", "out", *train_args]
    )
    # `~` must expand on the server, so the directory is written as $HOME-relative, never quoted
    d = d.replace("~", "$HOME", 1) if d.startswith("~") else shlex.quote(d)
    return f"""set -euo pipefail
cd {d}
echo $$ > run.pid
trap 'pkill -TERM -P $$ 2>/dev/null || true; rm -f run.pid' EXIT
export HF_HOME="$PWD/hf" TMPDIR="$PWD/tmp" PYTHONUNBUFFERED=1 PYTHONPATH="$PWD/lib"
mkdir -p out "$TMPDIR"
if [ ! -f .venv/.ok-{requirements_hash()} ]; then
  (command -v uv >/dev/null && uv venv .venv -q && uv pip install -q -p .venv/bin/python -r tools/requirements.txt) \\
    || (python3 -m venv .venv && .venv/bin/pip install -q -r tools/requirements.txt)
  touch .venv/.ok-{requirements_hash()}
fi
timeout --signal=TERM --kill-after=60 {secs} nice -n 10 .venv/bin/python {train} 2>&1 | tee out/train.log
"""


def plan(
    run: str, dataset: Path, mode: str, train_args: list[str], max_hours: float
) -> list[list[str]]:
    d = run_dir(run)
    remote = (
        d if not d.startswith("~") else d
    )  # rsync/ssh both expand ~ on the server side
    sync = [
        ssh_cmd(f"mkdir -p {remote}/lib {remote}/tools {remote}/data {remote}/out"),
        rsync_cmd(
            f"{LIB}/", f"{host()}:{remote}/lib/", "--delete", "--exclude=__pycache__"
        ),
        rsync_cmd(
            f"{HERE}/", f"{host()}:{remote}/tools/", "--delete", "--exclude=__pycache__"
        ),
        rsync_cmd(
            f"{dataset}/",
            f"{host()}:{remote}/data/",
            "--delete",
            "--exclude=review",
            "--exclude=README.md",
        ),
    ]
    train = ssh_cmd(
        f"bash -c {shlex.quote(remote_script(run, mode, train_args, max_hours))}"
    )
    return [*sync, train, *pull_commands(run, weights=False)]


def pull_commands(run: str, *, weights: bool) -> list[list[str]]:
    out = local_out(run)
    excludes = [] if weights else [f"--exclude={p}" for p in HEAVY]
    size = [] if weights else ["--max-size=20m"]
    return [
        ["mkdir", "-p", str(out)],
        rsync_cmd(f"{host()}:{run_dir(run)}/out/", f"{out}/", *excludes, *size),
    ]


def show(cmds: list[list[str]]) -> None:
    for c in cmds:
        print(
            "$ "
            + " ".join(shlex.quote(a) if "\n" not in a else shlex.quote(a) for a in c)
        )


def execute(cmds: list[list[str]]) -> int:
    for c in cmds:
        print("$ " + shlex.join(c[:3]) + " ...", flush=True)
        r = subprocess.run(c, check=False)
        if r.returncode != 0:
            print(f"failed ({r.returncode}): {c[0]}", file=sys.stderr)
            return r.returncode
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight")
    for name in ("plan", "run"):
        p = sub.add_parser(name)
        p.add_argument("run")
        p.add_argument("--dataset", type=Path, required=True)
        p.add_argument("--mode", choices=("head", "finetune"), default="head")
        p.add_argument("--max-hours", type=float, default=2.0)
        p.add_argument("train_args", nargs="*", help="after --: passed to train.py")
        if name == "run":
            p.add_argument(
                "--yes", action="store_true", help="actually run on the server"
            )
    pl = sub.add_parser("pull")
    pl.add_argument("run")
    pl.add_argument("--weights", action="store_true")
    cl = sub.add_parser("clean")
    cl.add_argument("run")
    cl.add_argument("--yes", action="store_true")
    cl.add_argument("--all", action="store_true", help="also delete out/ (the server copy of the artifacts)")
    args = ap.parse_args(argv)
    if args.cmd == "preflight":
        return subprocess.run(
            ssh_cmd(f"bash -c {shlex.quote(PREFLIGHT)}"), check=False
        ).returncode
    if args.cmd in ("plan", "run"):
        if not (args.dataset / "labels.jsonl").is_file():
            raise SystemExit(
                f"{args.dataset}: no labels.jsonl (build it with dataset.py build)"
            )
        cmds = plan(args.run, args.dataset, args.mode, args.train_args, args.max_hours)
        if args.cmd == "plan" or not args.yes:
            show(cmds)
            print(
                f"\n(dry run: add --yes to `run` to execute; host {host()}, max {args.max_hours} h)"
            )
            return 0
        # the logs come back even when training failed or timed out: pull always runs
        n_pull = len(pull_commands(args.run, weights=False))
        rc = execute(cmds[:-n_pull])
        pull_rc = execute(cmds[-n_pull:])
        return rc or pull_rc
    if args.cmd == "pull":
        cmds = pull_commands(args.run, weights=args.weights)
        return execute(cmds)
    d = run_dir(args.run)
    # by default `out/` (weights, metrics, train.log: the server-side copy of the artifacts) stays
    keep = "" if args.all else " && cd " + d + " && rm -rf -- .venv hf tmp lib tools data run.pid"
    cmd = ssh_cmd(f"rm -rf -- {d}" if args.all else f"test -d {d}{keep}")
    if not args.yes:
        show([cmd])
        return 0
    return execute([cmd])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
