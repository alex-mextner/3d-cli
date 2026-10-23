"""hostrun.py — runs ON the training machine (stdlib only, system python3): bootstrap and a guarded exec.

`remote.py run` ships this file next to the training scripts and calls it inside ONE ssh session:

    python3 tools/hostrun.py session --python 3.12 --max-hours H [guard options] -- CMD ...
        bootstrap, then exec
    python3 tools/hostrun.py bootstrap [--python 3.12]
        uv + Python + the venv of tools/requirements.txt, all under the run directory (nothing in
        ~/.local, ~/.cache or the system; no sudo). Idempotent: a marker holds the requirements hash.
    python3 tools/hostrun.py exec --max-hours H [--max-load 12] [--start-load 8] [--vram-cap-mib 4608]
                                  [--threads 6] [--no-gpu] -- CMD ...
        the guard. Refuses to start when the machine is busy; then runs CMD at nice 10 with at most
        --threads CPU threads in its own process group and watches it every 2 s:
          * 1-minute load average > --max-load        -> kill the group, outcome "load"
          * VRAM used by CMD's processes > the cap     -> kill, outcome "vram_cap" (the cap is also
                                                         handed to CMD as THREED_VRAM_CAP_MIB)
          * less than 300 MiB of the card left        -> kill, outcome "gpu_contention" (someone
                                                         else started using the GPU)
          * wall time > --max-hours                    -> TERM, then KILL after 20 s, "timeout"
        SIGTERM/SIGINT/SIGHUP (ssh dropped) kill the group too; the pid file is always removed.
        out/GUARD.json records outcome, seconds, peak VRAM of CMD and peak load average; CMD's
        output goes to the ssh session and to out/run.log.

Exit codes of `exec`: 0 CMD succeeded, 1 CMD failed (its code in GUARD.json), 3 aborted by the guard
(load / vram_cap / gpu_contention / a signal), 4 timeout, 5 refused to start.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
RUN = HERE.parent  # <base>/<run>/ : tools/ sits directly under it
KILL_GRACE_S = 20.0
POLL_S = 2.0
MIN_FREE_MIB_LEFT = 300  # of the card, for the user's desktop


# ---- machine probes (all read-only) ----------------------------------------------------------------


def load1() -> float:
    return float(Path("/proc/loadavg").read_text().split()[0])


def _smi(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout


def gpu_memory_mib() -> tuple[int, int] | None:
    """(used, total) MiB of GPU 0, or None without an NVIDIA driver."""
    text = _smi("--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits")
    if not text:
        return None
    try:
        used, total = (int(v) for v in text.splitlines()[0].split(","))
    except ValueError:
        return None
    return used, total


def gpu_apps_mib() -> dict[int, int]:
    """pid -> MiB of every compute process on the card."""
    text = (
        _smi("--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits")
        or ""
    )
    apps: dict[int, int] = {}
    for line in text.splitlines():
        try:
            pid, mib = (int(v) for v in line.split(","))
        except ValueError:
            continue
        apps[pid] = apps.get(pid, 0) + mib
    return apps


def descendants(root: int) -> set[int]:
    """root and every process below it (from /proc/<pid>/stat parent links)."""
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(entry.name))
    found, todo = {root}, [root]
    while todo:
        for kid in children.get(todo.pop(), []):
            if kid not in found:
                found.add(kid)
                todo.append(kid)
    return found


# ---- the guard -------------------------------------------------------------------------------------


def verdict(
    *,
    load: float,
    max_load: float,
    ours_mib: int,
    cap_mib: int,
    gpu: tuple[int, int] | None,
    elapsed_s: float,
    max_s: float,
) -> str | None:
    """The reason to stop CMD now, or None. Pure: the unit tests drive it with numbers."""
    if load > max_load:
        return "load"
    if ours_mib > cap_mib:
        return "vram_cap"
    if gpu is not None and gpu[1] - gpu[0] < MIN_FREE_MIB_LEFT:
        return "gpu_contention"
    if elapsed_s > max_s:
        return "timeout"
    return None


def start_gate(
    *,
    load: float,
    start_load: float,
    gpu: tuple[int, int] | None,
    cap_mib: int,
    needs_gpu: bool,
) -> str | None:
    """Why CMD must not start at all (None = go)."""
    if load > start_load:
        return f"load average {load:.1f} > {start_load:g}: the machine is busy"
    if needs_gpu and gpu is None:
        return "no NVIDIA GPU visible (nvidia-smi failed)"
    if needs_gpu and gpu is not None and gpu[1] - gpu[0] < cap_mib:
        return f"only {gpu[1] - gpu[0]} MiB of VRAM free, the cap needs {cap_mib}: someone is using the GPU"
    return None


def kill_group(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, sig)


def _pump(proc: subprocess.Popen[str], log: Any) -> None:
    """Copy CMD's output to our stdout (the ssh session) and to out/run.log."""
    assert proc.stdout is not None
    for line in proc.stdout:
        log.write(line)
        log.flush()
        try:
            sys.stdout.write(line)
            sys.stdout.flush()
        except OSError:
            pass  # ssh dropped: the signal handler stops CMD; the log keeps the rest


def _child_env(args: argparse.Namespace) -> dict[str, str]:
    env = dict(os.environ)
    for var in (
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        env[var] = str(args.threads)
    env["THREED_VRAM_CAP_MIB"] = str(args.vram_cap_mib)
    env["THREED_MAX_THREADS"] = str(args.threads)
    return env


def run_guarded(cmd: list[str], args: argparse.Namespace) -> int:
    out = RUN / "out"
    out.mkdir(exist_ok=True)
    pidfile = RUN / "run.pid"
    cap = int(args.vram_cap_mib)
    refusal = start_gate(
        load=load1(),
        start_load=args.start_load,
        gpu=gpu_memory_mib(),
        cap_mib=cap,
        needs_gpu=not args.no_gpu,
    )
    if refusal:
        (out / "GUARD.json").write_text(
            json.dumps({"outcome": "refused", "reason": refusal}) + "\n"
        )
        print(f"hostrun: refusing to start: {refusal}", file=sys.stderr, flush=True)
        return 5
    t0 = time.time()
    log = (out / "run.log").open("a")
    proc = subprocess.Popen(  # noqa: PLW1509 — nice must happen in the child before exec
        cmd,
        start_new_session=True,
        env=_child_env(args),
        preexec_fn=lambda: os.nice(10),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    pump = threading.Thread(target=_pump, args=(proc, log), daemon=True)
    pump.start()
    pidfile.write_text(f"{os.getpid()}\n{proc.pid}\n")
    stop: dict[str, str | None] = {"reason": None}

    def on_signal(signum: int, _frame: Any) -> None:
        stop["reason"] = f"signal_{signal.Signals(signum).name}"

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, on_signal)
    peak_vram = peak_load = 0.0
    outcome = None
    try:
        while proc.poll() is None:
            ours = descendants(proc.pid)
            ours_mib = sum(m for p, m in gpu_apps_mib().items() if p in ours)
            load = load1()
            peak_vram, peak_load = max(peak_vram, ours_mib), max(peak_load, load)
            outcome = stop["reason"] or verdict(
                load=load,
                max_load=args.max_load,
                ours_mib=ours_mib,
                cap_mib=cap,
                gpu=gpu_memory_mib(),
                elapsed_s=time.time() - t0,
                max_s=args.max_hours * 3600.0,
            )
            if outcome:
                print(
                    f"hostrun: stopping CMD: {outcome} (load {load:.1f}, VRAM of CMD {ours_mib} MiB)",
                    file=sys.stderr,
                    flush=True,
                )
                kill_group(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(KILL_GRACE_S)
                except subprocess.TimeoutExpired:
                    kill_group(proc.pid, signal.SIGKILL)
                break
            time.sleep(POLL_S)
    finally:
        # never leave CMD or its workers behind, whatever happened above
        kill_group(proc.pid, signal.SIGKILL)
        pidfile.unlink(missing_ok=True)
    code = proc.wait()
    pump.join(timeout=5)
    log.close()
    result = {
        "outcome": outcome or ("ok" if code == 0 else "failed"),
        "exit_code": code,
        "seconds": round(time.time() - t0, 1),
        "peak_vram_mib_of_cmd": int(peak_vram),
        "vram_cap_mib": cap,
        "peak_load1": round(peak_load, 2),
        "max_load": args.max_load,
        "threads": args.threads,
    }
    (out / "GUARD.json").write_text(json.dumps(result, indent=2) + "\n")
    print("hostrun: " + json.dumps(result), flush=True)
    if outcome == "timeout":
        return 4
    if outcome:
        return 3
    return 0 if code == 0 else 1


# ---- bootstrap -------------------------------------------------------------------------------------


def bootstrap(python: str) -> int:
    """uv, Python and the venv under the run directory; skipped when the marker matches."""
    req = HERE / "requirements.txt"
    digest = hashlib.sha256(req.read_bytes() + python.encode()).hexdigest()[:16]
    marker = RUN / ".venv" / f".ok-{digest}"
    if marker.exists():
        print("hostrun: environment up to date", flush=True)
        return 0
    uv_dir = RUN / "uv"
    env = dict(os.environ)
    env.update(
        UV_INSTALL_DIR=str(uv_dir),
        UV_NO_MODIFY_PATH="1",
        UV_CACHE_DIR=str(RUN / "uvcache"),
        UV_PYTHON_INSTALL_DIR=str(RUN / "python"),
        TMPDIR=str(RUN / "tmp"),
        UV_NO_PROGRESS="1",
    )
    (RUN / "tmp").mkdir(exist_ok=True)
    uv = uv_dir / "uv"
    steps: list[list[str]] = []
    if not uv.exists():
        steps.append(["sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"])
    venv_python = str(RUN / ".venv" / "bin" / "python")
    steps += [
        [str(uv), "python", "install", python],
        [str(uv), "venv", "--python", python, str(RUN / ".venv")],
        [str(uv), "pip", "install", "-p", venv_python, "-r", str(req)],
    ]
    for step in steps:
        print("hostrun: $ " + " ".join(step[:4]) + " ...", flush=True)
        rc = subprocess.run(
            ["nice", "-n", "10", *step], env=env, check=False
        ).returncode
        if rc != 0:
            print(
                f"hostrun: bootstrap step failed ({rc}): {step[0]}",
                file=sys.stderr,
                flush=True,
            )
            return rc
    marker.touch()
    return 0


def _exec_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--max-hours", type=float, required=True)
    p.add_argument("--max-load", type=float, default=12.0)
    p.add_argument("--start-load", type=float, default=8.0)
    p.add_argument("--vram-cap-mib", type=int, default=4608)
    p.add_argument("--threads", type=int, default=6)
    p.add_argument(
        "--no-gpu",
        action="store_true",
        help="CMD does not use the GPU: skip the VRAM gate",
    )
    p.add_argument("command", nargs=argparse.REMAINDER)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("bootstrap").add_argument("--python", default="3.12")
    session = sub.add_parser("session")
    session.add_argument("--python", default="3.12")
    _exec_arguments(session)
    _exec_arguments(sub.add_parser("exec"))
    args = ap.parse_args(argv)
    if args.cmd in ("bootstrap", "session"):
        rc = bootstrap(args.python)
        if rc != 0 or args.cmd == "bootstrap":
            return rc
    cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not cmd:
        ap.error("exec/session need a command after --")
    return run_guarded(cmd, args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
