"""remote.py — run tier (b) experiments/training on the user's laptop over ssh; nothing large enters git.

    remote.py preflight                         read-only look at the machine: GPU incl. free VRAM, RAM,
                                                disk, load, python/uv/docker/rsync (writes nothing, starts nothing)
    remote.py plan RUN --dataset DIR [...]      print the exact commands `run` would execute
    remote.py run RUN --dataset DIR --yes       sync inputs, bootstrap, run under the guard, pull small artifacts
    remote.py pull RUN [--weights]              fetch out/ again (--weights: also .pt/.onnx, into the local cache)
    remote.py clean RUN --yes [--all]           free the run dir (venv, caches, data); out/ stays unless --all
    remote.py verify-idle                       fail if anything of ours still runs on the machine
    remote.py mirror RUN [--yes]                rsync pulled weights to the second-copy host (no execution there)

Host: THREED_TRAIN_HOST (default `home`, the owner's laptop on tailscale; user, key and port come from
~/.ssh/config, THREED_TRAIN_USER overrides the user). Directory: THREED_TRAIN_DIR (default
~/work-3d-cli-vision); a run lives in <dir>/<RUN>/ and ONLY there: its own uv, Python, venv, Hugging
Face cache, data and out/. No sudo/root, nothing in ~/.local or the system, no secret is ever sent
(the Hugging Face token stays on this machine; weights are pulled here and published from here).

The laptop is the user's machine, so everything is polite (hostrun.py, `exec`): `nice -n 10`, at most
6 CPU threads, GPU memory <= 4.5 GiB of its 6 GiB (the cap is handed to the script and enforced by a
watchdog that kills the job), refusal to start when the load average is above 8 or the GPU is in use,
a kill when the load average goes above 12 or less than 300 MiB of VRAM is left, a hard --max-hours
timeout, a pid file, and a kill of the whole process group when ssh drops. Finish with
`pull`, `clean --all` and `verify-idle` so nothing stays behind.

The second copy of weights: 65.21.40.51 (THREED_WEIGHTS_HOST) has NO GPU. It is only a rsync target
for `mirror`; nothing runs there.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE.parent.parent / "lib"
DEFAULT_HOST = "home"
DEFAULT_DIR = "~/work-3d-cli-vision"
WEIGHTS_HOST = "65.21.40.51"
WEIGHTS_DIR = "~/3d-cli-weights"
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
HEAVY = ("*.pt", "*.onnx", "*.safetensors", "*.npy")
# what the laptop is allowed to be asked for (see the module docstring); hostrun.py enforces it
LIMITS = {"threads": 6, "vram_cap_mib": 4608, "max_load": 12.0, "start_load": 8.0}
PREFLIGHT = """\
echo "== uptime / load"; uptime; nproc
echo "== gpu (free VRAM)"; nvidia-smi --query-gpu=name,driver_version,memory.total,memory.used,memory.free --format=csv,noheader 2>&1 || echo "nvidia-smi: missing"
echo "== gpu processes"; nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader 2>&1 || true
echo "== memory (MiB)"; free -m
echo "== disk"; df -h "$HOME"
echo "== tools"; for t in python3 uv docker rsync curl timeout nice pgrep; do command -v "$t" || echo "$t: missing"; done
python3 --version 2>&1
echo "== top cpu"; ps -eo pid,pcpu,pmem,etime,comm --sort=-pcpu | head -6
echo "== our leftovers"; pgrep -u "$USER" -f '[w]ork-3d-cli-vision' || echo "none"
"""


def host() -> str:
    h = os.environ.get("THREED_TRAIN_HOST", DEFAULT_HOST)
    user = os.environ.get("THREED_TRAIN_USER")
    return f"{user}@{h}" if user else h


def weights_host() -> str:
    return os.environ.get("THREED_WEIGHTS_HOST", WEIGHTS_HOST)


def base_dir() -> str:
    return os.environ.get("THREED_TRAIN_DIR", DEFAULT_DIR)


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


def remote_script(
    run: str,
    entry: str,
    entry_args: list[str],
    max_hours: float,
    *,
    python: str = "3.12",
) -> str:
    """The one command the machine executes for `run`: bootstrap, then the guarded entry script.
    `timeout` is the outer bound on the whole session (bootstrap included); hostrun.py enforces
    --max-hours on the job itself and the load/VRAM guard."""
    d = run_dir(run)
    d = d.replace("~", "$HOME", 1) if d.startswith("~") else shlex.quote(d)
    outer = int((max_hours + 0.5) * 3600)
    job = ["--", ".venv/bin/python", f"tools/{entry}", "data", "out", *entry_args]
    session = " ".join(
        [
            f"python3 tools/hostrun.py session --python {python}",
            f"--max-hours {max_hours:g} --max-load {LIMITS['max_load']:g} --start-load {LIMITS['start_load']:g}",
            f"--vram-cap-mib {LIMITS['vram_cap_mib']} --threads {LIMITS['threads']}",
            *(shlex.quote(a) for a in job),
        ]
    )
    return f"""set -euo pipefail
cd {d}
export HF_HOME="$PWD/hf" TMPDIR="$PWD/tmp" PYTHONUNBUFFERED=1 PYTHONPATH="$PWD/lib"
mkdir -p out tmp
exec timeout --signal=TERM --kill-after=60 {outer} {session}
"""


def plan(
    run: str, dataset: Path, entry: str, entry_args: list[str], max_hours: float
) -> list[list[str]]:
    d = run_dir(run)
    sync = [
        ssh_cmd(f"mkdir -p {d}/lib {d}/tools {d}/data {d}/out"),
        rsync_cmd(f"{LIB}/", f"{host()}:{d}/lib/", "--delete", "--exclude=__pycache__"),
        rsync_cmd(
            f"{HERE}/", f"{host()}:{d}/tools/", "--delete", "--exclude=__pycache__"
        ),
        rsync_cmd(
            f"{dataset}/",
            f"{host()}:{d}/data/",
            "--delete",
            "--exclude=review",
            "--exclude=README.md",
        ),
    ]
    train = ssh_cmd(
        f"bash -c {shlex.quote(remote_script(run, entry, entry_args, max_hours))}"
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


def clean_command(run: str, *, everything: bool) -> list[str]:
    d = run_dir(run)
    d_sh = d.replace("~", "$HOME", 1) if d.startswith("~") else shlex.quote(d)
    if everything:
        return ssh_cmd(f"bash -c {shlex.quote(f'rm -rf -- {d_sh}')}")
    keep = "uv uvcache python .venv hf tmp lib tools data run.pid"
    return ssh_cmd(
        f"bash -c {shlex.quote(f'test -d {d_sh} && cd {d_sh} && rm -rf -- {keep}')}"
    )


def verify_idle_command() -> list[str]:
    """Fails (exit 1) when any process of ours still mentions the work dir. The `[w]` keeps pgrep from
    matching its own command line."""
    return ssh_cmd(
        "bash -c "
        + shlex.quote(
            "if pgrep -u \"$USER\" -fa '[w]ork-3d-cli-vision'; then echo 'STILL RUNNING' >&2; exit 1; else echo 'idle: nothing of ours is running'; fi"
        )
    )


def mirror_commands(run: str) -> list[list[str]]:
    out = local_out(run)
    dest = f"{WEIGHTS_DIR}/{run}"
    return [
        ["ssh", *SSH_OPTS, weights_host(), f"mkdir -p {dest}"],
        rsync_cmd(
            f"{out}/",
            f"{weights_host()}:{dest}/",
            "--include=*.pt",
            "--include=*.onnx",
            "--include=*.safetensors",
            "--include=*.json",
            "--exclude=*",
        ),
    ]


def show(cmds: list[list[str]]) -> None:
    for c in cmds:
        print("$ " + shlex.join(c))


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
    sub.add_parser("verify-idle")
    for name in ("plan", "run"):
        p = sub.add_parser(name)
        p.add_argument("run")
        p.add_argument("--dataset", type=Path, required=True)
        p.add_argument(
            "--entry",
            default="experiment.py",
            help="script of tools/vision_train/ to run (default experiment.py)",
        )
        p.add_argument("--max-hours", type=float, default=2.0)
        p.add_argument("entry_args", nargs="*", help="after --: passed to the script")
        if name == "run":
            p.add_argument(
                "--yes", action="store_true", help="actually run on the machine"
            )
    pl = sub.add_parser("pull")
    pl.add_argument("run")
    pl.add_argument("--weights", action="store_true")
    cl = sub.add_parser("clean")
    cl.add_argument("run")
    cl.add_argument("--yes", action="store_true")
    cl.add_argument("--all", action="store_true", help="also delete out/ (pull first!)")
    mi = sub.add_parser("mirror")
    mi.add_argument("run")
    mi.add_argument("--yes", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "preflight":
        return subprocess.run(
            ssh_cmd(f"bash -c {shlex.quote(PREFLIGHT)}"), check=False
        ).returncode
    if args.cmd == "verify-idle":
        return subprocess.run(verify_idle_command(), check=False).returncode
    if args.cmd in ("plan", "run"):
        if not (args.dataset / "labels.jsonl").is_file():
            raise SystemExit(
                f"{args.dataset}: no labels.jsonl (build it with dataset.py build)"
            )
        if not (HERE / args.entry).is_file():
            raise SystemExit(f"{args.entry}: not a script of {HERE}")
        cmds = plan(args.run, args.dataset, args.entry, args.entry_args, args.max_hours)
        if args.cmd == "plan" or not args.yes:
            show(cmds)
            print(
                f"\n(dry run: add --yes to `run` to execute; host {host()}, max {args.max_hours} h)"
            )
            return 0
        # the logs come back even when the job failed or the guard stopped it: the pull always runs
        n_pull = len(pull_commands(args.run, weights=False))
        rc = execute(cmds[:-n_pull])
        pull_rc = execute(cmds[-n_pull:])
        return rc or pull_rc
    if args.cmd == "pull":
        return execute(pull_commands(args.run, weights=args.weights))
    if args.cmd == "mirror":
        cmds = mirror_commands(args.run)
        if not args.yes:
            show(cmds)
            return 0
        return execute(cmds)
    cmd = clean_command(args.run, everything=args.all)
    if not args.yes:
        show([cmd])
        return 0
    return execute([cmd])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
