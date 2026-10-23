"""tools/vision_train: the dataset builder with its sanitation, the Hugging Face publisher (dry run by
default, token handling, read-back verification), the training-server wrapper (preflight is
read-only, plan prints exact commands, run needs --yes, pulls stay small) and the metric helpers of
train.py. No network, no server, no Hugging Face: ssh/rsync/HF calls are replaced by recorders."""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools" / "vision_train"


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"vt_{name}", TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"vt_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


def _np() -> Any:
    return pytest.importorskip("numpy")


def _cv2() -> Any:
    return pytest.importorskip("cv2")


# ---- remote.py ---------------------------------------------------------------------------------------


def test_remote_targets_the_laptop_by_default_and_honours_the_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = _load("remote")
    for var in (
        "THREED_TRAIN_HOST",
        "THREED_TRAIN_USER",
        "THREED_TRAIN_DIR",
        "THREED_WEIGHTS_HOST",
    ):
        monkeypatch.delenv(var, raising=False)
    assert remote.host() == "home" and remote.base_dir() == "~/work-3d-cli-vision"
    assert remote.weights_host() == "65.21.40.51"
    monkeypatch.setenv("THREED_TRAIN_HOST", "gpu.example.test")
    monkeypatch.setenv("THREED_TRAIN_USER", "trainer")
    monkeypatch.setenv("THREED_WEIGHTS_HOST", "backup.example.test")
    assert (
        remote.host() == "trainer@gpu.example.test"
        and remote.weights_host() == "backup.example.test"
    )


@pytest.mark.parametrize("bad", ["", "../x", "a b", "a;rm", ".hidden", "a/b"])
def test_run_names_cannot_escape_the_base_directory(bad: str) -> None:
    remote = _load("remote")
    with pytest.raises(SystemExit):
        remote.run_dir(bad)


def test_preflight_is_read_only_and_reports_free_vram_load_and_leftovers() -> None:
    remote = _load("remote")
    script = remote.PREFLIGHT
    forbidden_words = (
        "rm ",
        "mkdir",
        "kill ",
        "systemctl",
        "docker run",
        "pip install",
        "apt",
        " > ",
        ">>",
        "tee",
        "chmod",
        "mv ",
    )
    for forbidden in forbidden_words:
        assert forbidden not in script, forbidden
    for wanted in (
        "nvidia-smi",
        "memory.free",
        "free -m",
        "df -h",
        "uptime",
        "python3",
        "uv",
        "docker",
        "pgrep",
    ):
        assert wanted in script


def test_plan_syncs_bootstraps_and_runs_under_the_guard_with_the_laptop_limits(
    tmp_path: Path,
) -> None:
    remote = _load("remote")
    (tmp_path / "labels.jsonl").write_text("")
    cmds = remote.plan("run1", tmp_path, "experiment.py", ["--folds", "5"], 1.5)
    flat = [" ".join(c) for c in cmds]
    assert (
        flat[0].startswith("ssh -o BatchMode=yes")
        and " home " in flat[0]
        and "~/work-3d-cli-vision/run1/lib" in flat[0]
    )
    assert any(
        c.startswith("rsync -az") and "/lib/" in c and "--exclude=__pycache__" in c
        for c in flat
    )
    assert any(f"{tmp_path}/" in c and "--exclude=review" in c for c in flat)
    script = remote.remote_script("run1", "experiment.py", ["--folds", "5"], 1.5)
    assert "cd $HOME/work-3d-cli-vision/run1" in script  # only its own directory
    assert (
        "timeout --signal=TERM --kill-after=60 7200" in script
    )  # 1.5 h job + 30 min bootstrap allowance
    assert (
        "python3 tools/hostrun.py session --python 3.12 --max-hours 1.5 --max-load 12 --start-load 8"
        in script
    )
    assert (
        "--vram-cap-mib 4608 --threads 6" in script
    )  # <= 4.5 GiB of the 6 GiB card, <= 6 threads
    assert ".venv/bin/python tools/experiment.py data out --folds 5" in script
    assert (
        'HF_HOME="$PWD/hf"' in script
        and "HF_TOKEN" not in script
        and "HF_API_KEY" not in script
    )
    assert "sudo" not in script and "/usr" not in script
    pull = flat[-1]
    assert "--max-size=20m" in pull and all(
        f"--exclude={p}" in pull for p in ("*.pt", "*.onnx", "*.safetensors", "*.npy")
    )


def test_pull_with_weights_drops_the_excludes_into_the_local_cache_not_the_repo() -> (
    None
):
    remote = _load("remote")
    heavy = remote.pull_commands("run1", weights=True)[1]
    assert "--max-size" not in " ".join(heavy) and "--exclude" not in " ".join(heavy)
    assert "/3d-cli/train/runs/run1/" in heavy[-1]
    assert str(Path(__file__).resolve().parents[1]) not in heavy[-1]


def test_run_without_yes_is_a_dry_run_and_with_yes_pulls_the_logs_even_after_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    remote = _load("remote")
    (tmp_path / "labels.jsonl").write_text("")
    ran: list[list[str]] = []

    class Done:
        def __init__(self, code: int) -> None:
            self.returncode = code

    def fake_run(cmd: list[str], check: bool = False) -> Done:
        ran.append(cmd)
        return Done(1 if len(ran) == 2 else 0)

    monkeypatch.setattr(remote.subprocess, "run", fake_run)
    assert remote.main(["run", "r1", "--dataset", str(tmp_path)]) == 0
    assert ran == [] and "dry run" in capsys.readouterr().out
    assert remote.main(["plan", "r1", "--dataset", str(tmp_path)]) == 0 and ran == []
    assert remote.main(["run", "r1", "--dataset", str(tmp_path), "--yes"]) == 1
    # mkdir ok, first rsync failed: the sync/job steps stop there, the pull still runs (logs)
    assert (
        ran[0][0] == "ssh"
        and ran[1][0] == "rsync"
        and ran[-1][0] == "rsync"
        and "out/" in ran[-1][-2]
    )
    assert len(ran) == 4  # mkdir, failed rsync, local mkdir of the pull dir, pull rsync
    with pytest.raises(SystemExit, match="not a script"):
        remote.main(
            ["plan", "r1", "--dataset", str(tmp_path), "--entry", "../etc/passwd"]
        )


def test_clean_frees_everything_but_out_unless_all_and_never_touches_other_dirs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    remote = _load("remote")
    ran: list[Any] = []

    def fake_ssh(*a: Any, **k: Any) -> Any:
        ran.append(a)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(remote.subprocess, "run", fake_ssh)
    assert remote.main(["clean", "r1"]) == 0 and ran == []  # dry run
    out = capsys.readouterr().out
    assert (
        "rm -rf -- uv uvcache python .venv hf tmp lib tools data run.pid" in out
    )  # out/ stays
    assert "$HOME/work-3d-cli-vision/r1" in out
    assert remote.main(["clean", "r1", "--all"]) == 0 and ran == []
    assert "rm -rf -- $HOME/work-3d-cli-vision/r1" in capsys.readouterr().out
    assert remote.main(["clean", "r1", "--all", "--yes"]) == 0 and len(ran) == 1


def test_verify_idle_looks_for_our_work_dir_without_matching_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = _load("remote")
    cmd = shlex.split(remote.verify_idle_command()[-1])[-1]  # the script bash receives
    assert "pgrep -u \"$USER\" -fa '[w]ork-3d-cli-vision'" in cmd and "exit 1" in cmd
    ran: list[Any] = []

    def fake_ssh(c: list[str], check: bool = False) -> Any:
        ran.append(c)
        return types.SimpleNamespace(returncode=1)

    monkeypatch.setattr(remote.subprocess, "run", fake_ssh)
    assert remote.main(["verify-idle"]) == 1 and ran


def test_mirror_sends_only_weights_and_json_to_the_second_copy_host_and_runs_nothing_there(
    capsys: pytest.CaptureFixture[str],
) -> None:
    remote = _load("remote")
    cmds = remote.mirror_commands("r1")
    assert (
        cmds[0][-1] == "mkdir -p ~/3d-cli-weights/r1" and cmds[0][-2] == "65.21.40.51"
    )
    sync = " ".join(cmds[1])
    assert (
        "--include=*.pt" in sync
        and "--exclude=*" in sync
        and "65.21.40.51:~/3d-cli-weights/r1/" in sync
    )
    assert (
        remote.main(["mirror", "r1"]) == 0 and "mkdir -p" in capsys.readouterr().out
    )  # dry run prints


# ---- hostrun.py: the guard that keeps the laptop usable -----------------------------------------------


def _guard_args(**over: Any) -> Any:
    base = {
        "max_hours": 0.01,
        "max_load": 1000.0,
        "start_load": 1000.0,
        "vram_cap_mib": 4608,
        "threads": 3,
        "no_gpu": True,
        "command": [],
    }
    return types.SimpleNamespace(**{**base, **over})


def test_verdict_stops_on_load_vram_cap_a_full_card_and_the_clock() -> None:
    hostrun = _load("hostrun")
    base: dict[str, Any] = {
        "load": 5.0,
        "max_load": 12.0,
        "ours_mib": 3000,
        "cap_mib": 4608,
        "gpu": (3500, 6144),
        "elapsed_s": 10.0,
        "max_s": 3600.0,
    }
    assert hostrun.verdict(**base) is None
    assert hostrun.verdict(**{**base, "load": 12.5}) == "load"
    assert hostrun.verdict(**{**base, "ours_mib": 4609}) == "vram_cap"
    assert (
        hostrun.verdict(**{**base, "gpu": (5900, 6144)}) == "gpu_contention"
    )  # < 300 MiB left for the desktop
    assert (
        hostrun.verdict(**{**base, "gpu": None}) is None
    )  # no GPU: nothing to protect
    assert hostrun.verdict(**{**base, "elapsed_s": 3601.0}) == "timeout"


def test_start_gate_refuses_a_busy_machine_and_a_used_gpu() -> None:
    hostrun = _load("hostrun")
    ok: dict[str, Any] = {
        "load": 3.0,
        "start_load": 8.0,
        "gpu": (26, 6144),
        "cap_mib": 4608,
        "needs_gpu": True,
    }
    assert hostrun.start_gate(**ok) is None
    assert "busy" in hostrun.start_gate(**{**ok, "load": 9.0})
    assert "VRAM free" in hostrun.start_gate(**{**ok, "gpu": (2000, 6144)})
    assert "no NVIDIA GPU" in hostrun.start_gate(**{**ok, "gpu": None})
    assert hostrun.start_gate(**{**ok, "gpu": None, "needs_gpu": False}) is None


def _run_guard(
    hostrun: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    **over: Any,
) -> tuple[int, dict[str, Any]]:
    monkeypatch.setattr(hostrun, "RUN", tmp_path)
    monkeypatch.setattr(hostrun, "POLL_S", 0.05)
    monkeypatch.setattr(hostrun, "KILL_GRACE_S", 2.0)
    rc = hostrun.run_guarded([sys.executable, "-c", code], _guard_args(**over))
    return rc, json.loads((tmp_path / "out" / "GUARD.json").read_text())


def test_guard_runs_a_command_niced_with_capped_threads_and_logs_its_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hostrun = _load("hostrun")
    code = "import os; print('nice', os.nice(0), 'omp', os.environ['OMP_NUM_THREADS'], 'cap', os.environ['THREED_VRAM_CAP_MIB'])"
    rc, result = _run_guard(hostrun, tmp_path, monkeypatch, code)
    assert (
        rc == 0
        and result["outcome"] == "ok"
        and result["threads"] == 3
        and result["vram_cap_mib"] == 4608
    )
    log = (tmp_path / "out" / "run.log").read_text()
    expected = f"nice {min(19, os.nice(0) + 10)} omp 3 cap 4608"  # 10 more than our own niceness
    assert expected in log and expected in capsys.readouterr().out
    assert not (tmp_path / "run.pid").exists()  # always removed


def test_guard_reports_a_failing_command_and_stops_a_runaway_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostrun = _load("hostrun")
    rc, result = _run_guard(hostrun, tmp_path, monkeypatch, "raise SystemExit(7)")
    assert rc == 1 and result["outcome"] == "failed" and result["exit_code"] == 7
    started = time.time()
    rc, result = _run_guard(
        hostrun, tmp_path, monkeypatch, "import time; time.sleep(60)", max_hours=0.0003
    )  # ~1 s
    assert rc == 4 and result["outcome"] == "timeout" and time.time() - started < 15
    assert not (tmp_path / "run.pid").exists()


def test_guard_kills_the_whole_process_group_when_the_load_goes_over_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostrun = _load("hostrun")
    marker = tmp_path / "grandchild.alive"
    child = f"import time, pathlib\nwhile True:\n    pathlib.Path({str(marker)!r}).touch()\n    time.sleep(0.05)\n"
    code = f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', {child!r}])\ntime.sleep(60)\n"
    monkeypatch.setattr(hostrun, "load1", lambda: 13.0)
    monkeypatch.setattr(
        hostrun, "start_gate", lambda **k: None
    )  # let it start, then the watchdog sees load 13
    rc, result = _run_guard(
        hostrun, tmp_path, monkeypatch, code, max_load=12.0, max_hours=1.0
    )
    assert rc == 3 and result["outcome"] == "load" and result["peak_load1"] == 13.0
    marker.unlink(missing_ok=True)
    time.sleep(0.3)
    assert not marker.exists()  # the grandchild is dead too: nothing left running


def test_guard_refuses_to_start_on_a_busy_machine_and_writes_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostrun = _load("hostrun")
    monkeypatch.setattr(hostrun, "load1", lambda: 9.5)
    rc, result = _run_guard(
        hostrun, tmp_path, monkeypatch, "print('never runs')", start_load=8.0
    )
    assert rc == 5 and result["outcome"] == "refused" and "busy" in result["reason"]
    assert not (tmp_path / "out" / "run.log").exists()


def test_bootstrap_installs_only_under_the_run_directory_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostrun = _load("hostrun")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "requirements.txt").write_text("torch\n")
    monkeypatch.setattr(hostrun, "RUN", tmp_path)
    monkeypatch.setattr(hostrun, "HERE", tmp_path / "tools")
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(cmd: list[str], env: dict[str, str], check: bool = False) -> Any:
        calls.append((cmd, env))
        if "venv" in cmd:
            (tmp_path / ".venv").mkdir(exist_ok=True)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(hostrun.subprocess, "run", fake_run)
    assert hostrun.bootstrap("3.12") == 0
    joined = [" ".join(c) for c, _ in calls]
    assert "curl -LsSf https://astral.sh/uv/install.sh" in joined[0]
    assert any("python install 3.12" in j for j in joined) and any(
        "pip install" in j and "requirements.txt" in j for j in joined
    )
    assert all(c[:3] == ["nice", "-n", "10"] for c, _ in calls)
    for _, env in calls:  # everything lands in the run directory
        assert env["UV_INSTALL_DIR"].startswith(str(tmp_path)) and env[
            "UV_CACHE_DIR"
        ].startswith(str(tmp_path))
        assert env["UV_PYTHON_INSTALL_DIR"].startswith(str(tmp_path)) and env[
            "TMPDIR"
        ].startswith(str(tmp_path))
        assert env["UV_NO_MODIFY_PATH"] == "1"
    n = len(calls)
    assert (
        hostrun.bootstrap("3.12") == 0 and len(calls) == n
    )  # the marker makes the second call a no-op


# ---- dataset.py: sanitation --------------------------------------------------------------------------


def test_crop_keep_and_metadata_detection() -> None:
    np = _np()
    ds = _load("dataset")
    img = np.zeros((100, 200, 3), np.uint8)
    assert ds.crop_keep(img, [0, 0.3, 1, 1]).shape == (70, 200, 3)
    clean = b"\xff\xd8\xff\xe0\x00\x04\x00\x00\xff\xda\x00\x02\xff\xd9"
    exif = b"\xff\xd8\xff\xe1\x00\x08Exif\x00\x00\xff\xda\x00\x02\xff\xd9"
    assert ds.has_metadata(clean) == []
    assert ds.has_metadata(exif) == ["APP1"]


def test_encoding_strips_exif_and_a_qr_code_is_painted_over() -> None:
    np = _np()
    cv2 = _cv2()
    ds = _load("dataset")
    enc = getattr(cv2, "QRCodeEncoder", None)
    if enc is None:
        pytest.skip("this OpenCV build cannot encode QR codes")
    qr = enc.create().encode("https://example.invalid/serial/123456")
    qr = cv2.resize(qr, (180, 180), interpolation=cv2.INTER_NEAREST)
    frame = np.full((400, 600, 3), 90, np.uint8)
    frame[100:280, 200:380] = cv2.cvtColor(qr, cv2.COLOR_GRAY2BGR)
    assert cv2.QRCodeDetector().detectAndDecode(frame)[0]  # decodable before
    masked, n = ds.mask_qr(frame)
    assert n >= 1 and not cv2.QRCodeDetector().detectAndDecode(masked)[0]
    assert ds.has_metadata(ds.encode(masked)) == []


def test_a_frame_with_a_face_or_person_hit_is_refused_not_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    np = _np()
    _cv2()
    ds = _load("dataset")
    frame = np.full((90, 160, 3), 120, np.uint8)
    data, rep = ds.sanitise(frame, [0, 0, 1, 1])
    assert rep["refused"] is False and rep["person_hits"] == {
        "face_front": 0,
        "face_profile": 0,
        "person": 0,
    }
    monkeypatch.setattr(
        ds, "person_hits", lambda img: {"face_front": 1, "face_profile": 0, "person": 0}
    )
    data, rep = ds.sanitise(frame, [0, 0, 1, 1])
    assert rep["refused"] is True and rep["person_hits"]["face_front"] == 1
    # build keeps such a frame OUT of the published set: a private training frame only
    out = tmp_path / "out"
    ds.build(out, json.loads(_tiny_sources(tmp_path).read_text()), third_party=False)
    rows = [
        json.loads(line) for line in (out / "labels.jsonl").read_text().splitlines()
    ]
    assert {r["provenance"] for r in rows} == {"own_private"}
    assert all(r["file"].startswith("private/") for r in rows)
    assert ds.published_rows(out) == [] and ds.check(out) == [
        "no own frames to publish"
    ]
    assert (out / "TRAINING_SHA256").is_file()


def _tiny_sources(tmp_path: Path) -> Path:
    cv2 = _cv2()
    np = _np()
    for i in range(6):
        cv2.imwrite(
            str(tmp_path / f"a{i}.jpg"), np.full((60, 100, 3), 30 + 20 * i, np.uint8)
        )
    cv2.imwrite(str(tmp_path / "spag.jpg"), np.full((60, 100, 3), 200, np.uint8))
    src = tmp_path / "sources.json"
    src.write_text(
        json.dumps(
            {
                "own": [
                    {
                        "id": "healthy-a",
                        "glob": str(tmp_path / "a*.jpg"),
                        "label": "healthy",
                        "printer": "p1",
                        "group": "g1",
                        "split": "train",
                        "every": 2,
                        "keep": [0, 0, 1, 1],
                    },
                    {
                        "id": "spag",
                        "glob": str(tmp_path / "spag.jpg"),
                        "label": "spaghetti",
                        "printer": "p1",
                        "group": "g2",
                        "split": "test",
                        "every": 1,
                        "keep": [0, 0, 1, 1],
                    },
                ],
                "third_party": {},
            }
        )
    )
    return src


def test_build_writes_hashed_names_labels_and_a_clean_checkable_manifest(
    tmp_path: Path,
) -> None:
    ds = _load("dataset")
    out = tmp_path / "out"
    ds.build(out, json.loads(_tiny_sources(tmp_path).read_text()), third_party=False)
    rows = [
        json.loads(line) for line in (out / "labels.jsonl").read_text().splitlines()
    ]
    assert {r["label"] for r in rows} == {"healthy", "spaghetti"} and len(
        rows
    ) == 4  # every=2 keeps 3 of 6
    for r in rows:
        assert (
            r["file"] == f"own/frames/{r['id']}.jpg"
            and r["id"] == r["sha256"][:16]
            and r["provenance"] == "own"
        )
        assert "a0" not in r["file"] and "spag" not in r["file"]  # no original names
    report = json.loads((out / "sanitation.json").read_text())
    assert report["own"]["healthy-a"]["published"] == 3 and report["frames"]["own"] == 4
    assert (out / "DATASET_SHA256").read_text().strip() == report["dataset_sha256"]
    ds.cmd_card(out, "abc123")
    assert ds.check(out) == []
    assert (
        "abc123" in (out / "README.md").read_text()
        and "CC-BY-4.0" in (out / "README.md").read_text()
    )


def test_check_catches_tampering_metadata_and_secret_looking_text(
    tmp_path: Path,
) -> None:
    ds = _load("dataset")
    out = tmp_path / "out"
    ds.build(out, json.loads(_tiny_sources(tmp_path).read_text()), third_party=False)
    ds.cmd_card(out, "abc123")
    assert ds.check(out) == []
    first = out / json.loads((out / "labels.jsonl").read_text().splitlines()[0])["file"]
    original = first.read_bytes()
    first.write_bytes(original[:2] + b"\xff\xe1\x00\x08Exif\x00\x00" + original[2:])
    problems = ds.check(out)
    assert any("sha256 differs" in p for p in problems) and any(
        "APP1" in p for p in problems
    )
    first.write_bytes(original)
    (out / "README.md").write_text(
        (out / "README.md").read_text()
        + "\nprinter at 192.168.0.26 and /home/someone\n"
    )
    problems = ds.check(out)
    assert any("192.168.0.26" in p for p in problems) and any(
        "/home/someone" in p for p in problems
    )


def test_third_party_rows_stay_out_of_everything_that_is_published(
    tmp_path: Path,
) -> None:
    ds = _load("dataset")
    pub = _load("publish")
    out = tmp_path / "out"
    ds.build(out, json.loads(_tiny_sources(tmp_path).read_text()), third_party=False)
    rows = [
        json.loads(line) for line in (out / "labels.jsonl").read_text().splitlines()
    ]
    rows.append(
        {
            **rows[0],
            "id": "t" * 16,
            "file": "third_party/frames/tttt.jpg",
            "provenance": "third_party",
            "source": "somewhere",
            "licence": "none",
        }
    )
    (out / "labels.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    )
    ds.cmd_card(out, "abc123")
    names = [n for n, _ in pub.dataset_operations(out)]
    assert all(not n.startswith("third_party") for n in names)
    labels = dict(pub.dataset_operations(out))["labels.jsonl"].decode()
    assert "somewhere" not in labels and "tttt" not in labels
    assert (
        json.loads(dict(pub.dataset_operations(out))["sanitation.json"])["third_party"]
        == "not published"
    )


# ---- publish.py ---------------------------------------------------------------------------------------


def test_token_comes_from_the_env_or_only_the_hf_api_key_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pub = _load("publish")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OTHER_SECRET=do-not-read\nHF_API_KEY='hf_abc123'\nTRAILING=x\n"
    )
    monkeypatch.setattr(pub, "ENV_FILE", env_file)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    assert pub.load_token() == "hf_abc123"
    monkeypatch.setenv("HF_TOKEN", "from-env")
    assert pub.load_token() == "from-env"
    monkeypatch.delenv("HF_TOKEN")
    env_file.write_text("OTHER_SECRET=do-not-read\n")
    with pytest.raises(SystemExit, match="no Hugging Face token"):
        pub.load_token()
    assert pub.redact("Bearer hf_abc123 failed", "hf_abc123") == "Bearer <token> failed"


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        ({"auth": {"accessToken": {"role": "write"}}}, True),
        ({"auth": {"accessToken": {"role": "read"}}}, False),
        (
            {
                "auth": {
                    "accessToken": {
                        "fineGrained": {"scoped": [{"permissions": ["repo.write"]}]}
                    }
                }
            },
            True,
        ),
        (
            {
                "auth": {
                    "accessToken": {
                        "fineGrained": {
                            "scoped": [{"permissions": ["repo.content.read"]}]
                        }
                    }
                }
            },
            False,
        ),
        ({}, False),
    ],
)
def test_write_scope_detection(info: dict[str, Any], expected: bool) -> None:
    assert _load("publish").can_write(info) is expected


def test_dataset_publish_is_a_dry_run_that_never_touches_the_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ds = _load("dataset")
    pub = _load("publish")
    out = tmp_path / "out"
    ds.build(out, json.loads(_tiny_sources(tmp_path).read_text()), third_party=False)
    monkeypatch.setattr(
        pub,
        "api",
        lambda: (_ for _ in ()).throw(AssertionError("dry run must not authenticate")),
    )
    assert pub.cmd_dataset(out, "owner/repo", publish=False) == 0
    text = capsys.readouterr().out
    assert (
        "dry run" in text
        and "sanitation clean" in text
        and "third-party frames excluded" in text
    )


def test_dataset_publish_refuses_when_a_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ds = _load("dataset")
    pub = _load("publish")
    out = tmp_path / "out"
    ds.build(out, json.loads(_tiny_sources(tmp_path).read_text()), third_party=False)
    monkeypatch.setattr(
        pub,
        "_module",
        lambda name: types.SimpleNamespace(
            cmd_card=lambda *a: None,
            check=lambda folder: ["x.jpg: face/person detector hit {'person': 1}"],
        ),
    )
    assert pub.cmd_dataset(out, "owner/repo", publish=True) == 1
    assert "REFUSING TO PUBLISH" in capsys.readouterr().err


def test_publish_commits_once_and_reads_every_file_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pub = _load("publish")
    committed: list[Any] = []

    class FakeApi:
        def create_repo(
            self, repo: str, repo_type: str, private: bool, exist_ok: bool
        ) -> None:
            assert private is False and repo_type == "dataset"

        def create_commit(
            self, repo: str, operations: list[Any], commit_message: str, repo_type: str
        ) -> Any:
            committed.append(operations)
            return types.SimpleNamespace(oid="c0ffee" * 6 + "c0ff")

    hub: Any = types.ModuleType("huggingface_hub")
    hub.CommitOperationAdd = lambda path, data: types.SimpleNamespace(
        path_in_repo=path, data=data
    )
    store: dict[str, bytes] = {}

    def fake_download(
        repo: str, name: str, repo_type: str, revision: str, token: str, local_dir: str
    ) -> str:
        target = Path(local_dir) / name.replace("/", "_")
        target.write_bytes(store[name])
        return str(target)

    hub.hf_hub_download = fake_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    files = [("a.txt", b"alpha"), ("own/frames/x.jpg", b"\xff\xd8beta")]
    store.update(dict(files))
    assert (
        pub.commit_and_verify(FakeApi(), "tok", "owner/repo", "dataset", files, "msg")
        == 0
    )
    assert len(committed) == 1 and [op.path_in_repo for op in committed[0]] == [
        "a.txt",
        "own/frames/x.jpg",
    ]
    out = capsys.readouterr().out
    assert (
        '"verified": true' in out
        and "https://huggingface.co/datasets/owner/repo" in out
        and "tok" not in out
    )
    store["a.txt"] = b"alpHa"  # the hub returns different bytes of the same length
    assert (
        pub.commit_and_verify(FakeApi(), "tok", "owner/repo", "dataset", files, "msg")
        == 1
    )
    captured = capsys.readouterr()
    assert "VERIFY FAILED a.txt" in captured.err and '"verified": false' in captured.out


def test_model_publish_refuses_a_head_for_another_model_and_secrets_in_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _np()
    pub = _load("publish")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
    import vision_model as vm

    (tmp_path / "metrics.json").write_text("{}")
    (tmp_path / "tier-b-head.json").write_text(
        json.dumps({"model": "other", "classes": {}})
    )
    assert pub.cmd_model(tmp_path, "o/m", "o/d", "abc", None, publish=False) == 1
    assert "this build embeds" in capsys.readouterr().err
    (tmp_path / "tier-b-head.json").write_text(
        json.dumps({"model": vm.MODEL_TAG, "classes": {}})
    )
    assert pub.cmd_model(tmp_path, "o/m", "o/d", "abc", None, publish=False) == 0
    assert "dry run" in capsys.readouterr().out
    (tmp_path / "metrics.json").write_text('{"note": "host 10.0.0.5"}')
    assert pub.cmd_model(tmp_path, "o/m", "o/d", "abc", None, publish=False) == 1
    assert "REFUSING" in capsys.readouterr().err


# ---- train.py helpers ---------------------------------------------------------------------------------


def test_train_rows_split_mapping_and_the_smoke_subset(tmp_path: Path) -> None:
    train = _load("train")
    rows = [
        {
            "id": str(i),
            "file": f"f{i}.jpg",
            "label": lab,
            "split": split,
            "provenance": "own",
        }
        for i, (lab, split) in enumerate(
            [("healthy", "tune")] * 20
            + [("healthy", "test")] * 20
            + [("spaghetti", "train")] * 20
            + [("spaghetti", "test")] * 20
        )
    ]
    (tmp_path / "labels.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    full = train.load_rows(tmp_path)
    assert {r["split"] for r in full} == {"train", "test"} and sum(
        r["split"] == "train" for r in full
    ) == 40
    smoke = train.load_rows(tmp_path, 48)
    assert len(smoke) == 24 and {(r["label"], r["split"]) for r in smoke} == {
        ("healthy", "train"),
        ("healthy", "test"),
        ("spaghetti", "train"),
        ("spaghetti", "test"),
    }


def test_train_tables_are_the_eval_model_tables() -> None:
    """The server's metrics are computed by the same functions as `vision eval --model`."""
    np = _np()
    train = _load("train")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
    import vision_model as vm
    from vision_detect import _table

    rows = [{"split": "test", "provenance": "own", "label": "spaghetti"}] * 4 + [
        {"split": "test", "provenance": "own", "label": "healthy"}
    ] * 6
    scores = {"spaghetti": [0.9, 0.8, 0.7, 0.2, 0.1, 0.1, 0.0, 0.3, 0.2, 0.0]}
    t = train.class_tables(np.zeros((10, 4)), rows, scores)["spaghetti/own"]
    assert t["frames"] == {"pos": 4, "neg": 6}
    assert t["per_frame"] == _table(
        [0.9, 0.8, 0.7, 0.2], [0.1, 0.1, 0.0, 0.3, 0.2, 0.0], vm.EVAL_THRESHOLDS
    )
    assert 0.9 < t["auroc"] <= 1.0
    assert "spaghetti/third_party" not in train.class_tables(
        np.zeros((10, 4)), rows, scores
    )
