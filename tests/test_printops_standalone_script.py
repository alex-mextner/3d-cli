"""The standalone installer `3d init --env --spoolman script` writes (printops.standalone_script).

The script is generated from the repo checkout and then RUN here against `docker`/`curl` shims
that record their calls: no real container, no network.
"""

from __future__ import annotations

import base64
import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest
from printops_env_fakes import fake_docker_bin

from printops import standalone_script

REPO = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ the standalone script


@pytest.fixture(scope="module")
def script_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return standalone_script.write(
        REPO, "test-version", tmp_path_factory.mktemp("script") / "spoolman-setup.sh"
    )


def test_script_is_valid_bash_and_shellcheck_clean(script_file: Path) -> None:
    assert os.access(script_file, os.X_OK)
    subprocess.run(["bash", "-n", str(script_file)], check=True)
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", str(script_file)], check=True)


def test_script_carries_the_compose_stack_and_is_deterministic(
    script_file: Path, tmp_path: Path
) -> None:
    text = script_file.read_text(encoding="utf-8")
    assert (
        "3d-cli test-version" in text and not re.search(r"@[A-Z_0-9]+@", text)
    )
    payload = text.split("<<'PRINTOPS_PAYLOAD'\n", 1)[1].split("\nPRINTOPS_PAYLOAD", 1)[
        0
    ]
    with tarfile.open(
        fileobj=io.BytesIO(base64.b64decode(payload)), mode="r:gz"
    ) as tar:
        names = tar.getnames()
        compose = tar.extractfile("services/3d-api/compose.yaml")
        assert compose is not None and b"ghcr.io/donkie/spoolman" in compose.read()
    assert {"bin/3d", "pyproject.toml", "services/3d-api/Dockerfile"} <= set(names)
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)
    assert (
        standalone_script.write(
            REPO, "test-version", tmp_path / "again.sh"
        ).read_bytes()
        == script_file.read_bytes()
    )


def _run_script(
    script: Path, root: Path, *args: str, cwd: Path | None = None, **extra: str
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{fake_docker_bin(root)}{os.pathsep}{env['PATH']}",
            "FAKE_LOG": str(root / "calls.log"),
            "FAKE_TOKENS": str(root / "tokens.txt"),
            "PRINTOPS_DIR": str(root / "base"),
            "PRINTOPS_HOST": "10.1.2.3",
            "PRINTOPS_BACKUP_TIMER": "0",
            "WAIT_SECONDS": "30",
            "PRINTOPS_CONTAINER_PREFIX": "pogb",
            "PRINTOPS_SPOOLMAN_PORT": "17912",
            "PRINTOPS_API_PORT": "18765",
            "TOKEN_NAME": "cli-test",
            **extra,
        }
    )
    return subprocess.run(
        ["/bin/bash", str(script), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=cwd,
    )


def test_script_dry_run_checks_prerequisites_and_changes_nothing(
    script_file: Path, tmp_path: Path
) -> None:
    r = _run_script(script_file, tmp_path, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "dry run" in r.stdout and not (tmp_path / "base").exists()


def test_script_fails_clearly_without_docker(script_file: Path, tmp_path: Path) -> None:
    r = _run_script(script_file, tmp_path, "--dry-run", PATH="/nonexistent-bin")
    assert r.returncode != 0 and "docker is required" in r.stderr


def test_script_install_is_idempotent_and_prints_the_paste_line(
    script_file: Path, tmp_path: Path
) -> None:
    first = _run_script(script_file, tmp_path)
    assert first.returncode == 0, first.stderr
    assert (
        "Spoolman  http://10.1.2.3:17912" in first.stdout
        and "3d-api    http://10.1.2.3:18765" in first.stdout
    )
    assert (
        "3d init --env --no-input --api http://10.1.2.3:18765 --token po_fakeminted"
        in first.stdout
    )
    assert "token: po_fakeminted" in first.stdout
    calls = (tmp_path / "calls.log").read_text()
    assert (
        "docker compose -p pogb -f services/3d-api/compose.yaml up -d --build --remove-orphans"
        in calls
    )
    for env_line in (
        "PRINTOPS_SPOOLMAN_PORT=17912",
        "PRINTOPS_API_PORT=18765",
        "PRINTOPS_CONTAINER_PREFIX=pogb",
        f"PRINTOPS_DATA_DIR={tmp_path}/base/data",
        f"PRINTOPS_UID={os.getuid()}",
    ):
        assert env_line in calls
    assert (
        tmp_path / "base" / "src" / "services" / "3d-api" / "compose.yaml"
    ).is_file()
    assert (tmp_path / "base" / "data" / "spoolman").is_dir()
    second = _run_script(script_file, tmp_path)
    assert second.returncode == 0, second.stderr
    assert "already unpacked" in second.stderr + second.stdout
    assert (
        "already exists and is never shown again" in second.stdout
        and "po_fakeminted" not in second.stdout
    )
    assert (tmp_path / "tokens.txt").read_text().count("cli-test") == 1  # minted once


def test_script_spoolman_only_skips_the_api(script_file: Path, tmp_path: Path) -> None:
    r = _run_script(script_file, tmp_path, SPOOLMAN_ONLY="1")
    assert r.returncode == 0, r.stderr
    assert "up -d spoolman" in (tmp_path / "calls.log").read_text()
    assert (
        "token create" not in (tmp_path / "calls.log").read_text()
        and "3d-api    http" not in r.stdout
    )


def test_script_rejects_a_truncated_payload(script_file: Path, tmp_path: Path) -> None:
    text = script_file.read_text(encoding="utf-8")
    lines = text.split("\n")
    start = lines.index("  cat <<'PRINTOPS_PAYLOAD'") + 1
    broken = tmp_path / "broken.sh"
    broken.write_text(
        "\n".join(lines[: start + 5] + lines[lines.index("PRINTOPS_PAYLOAD") :]),
        encoding="utf-8",
    )
    r = _run_script(broken, tmp_path / "run")
    assert r.returncode != 0


def test_script_waits_for_the_top_level_health_status(script_file: Path, tmp_path: Path) -> None:
    degraded = '{"status":"degraded","spoolman":{"ok":false,"extra_fields":{"status":"ok"}}}'
    bad = _run_script(script_file, tmp_path / "a", WAIT_SECONDS="1", FAKE_API_HEALTH=degraded)
    assert bad.returncode != 0 and "3d-api did not become healthy" in bad.stderr
    assert "3d-api is healthy" not in bad.stdout
    ok = '{"status":"ok","spoolman":{"ok":true,"extra_fields":{"status":"degraded"}}}'
    good = _run_script(script_file, tmp_path / "b", FAKE_API_HEALTH=ok)
    assert good.returncode == 0, good.stderr
    assert "3d-api is healthy" in good.stdout


def test_script_resolves_a_relative_base_dir_before_compose_sees_it(
    script_file: Path, tmp_path: Path
) -> None:
    here = tmp_path / "here"
    here.mkdir()
    r = _run_script(script_file, tmp_path, cwd=here, PRINTOPS_DIR="./printops")
    assert r.returncode == 0, r.stderr
    calls = (tmp_path / "calls.log").read_text()
    assert f"PRINTOPS_DATA_DIR={here.resolve()}/printops/data" in calls
    assert f"PRINTOPS_BACKUP_DIR={here.resolve()}/printops/backup" in calls
    assert (here / "printops" / "data" / "spoolman").is_dir()  # prepared where compose mounts it


def test_script_token_name_is_compared_literally(script_file: Path, tmp_path: Path) -> None:
    (tmp_path / "tokens.txt").write_text("cli-axb        read,write  now  sha256 abcdef\n", encoding="utf-8")
    r = _run_script(script_file, tmp_path, TOKEN_NAME="cli-a.b")  # '.' is not a wildcard
    assert r.returncode == 0, r.stderr
    assert "po_fakeminted" in r.stdout
    assert "cli-a.b " in (tmp_path / "tokens.txt").read_text()


def test_script_does_not_open_spoolman_to_every_browser_origin(
    script_file: Path, tmp_path: Path
) -> None:
    default = _run_script(script_file, tmp_path / "a")
    assert default.returncode == 0, default.stderr
    calls = (tmp_path / "a" / "calls.log").read_text()
    assert "PRINTOPS_SPOOLMAN_CORS_ORIGIN=http://10.1.2.3:17912\n" in calls and "CORS_ORIGIN=*" not in calls
    custom = _run_script(script_file, tmp_path / "b", PRINTOPS_SPOOLMAN_CORS_ORIGIN="http://fluidd.lan")
    assert custom.returncode == 0, custom.stderr
    assert "PRINTOPS_SPOOLMAN_CORS_ORIGIN=http://fluidd.lan\n" in (tmp_path / "b" / "calls.log").read_text()


# ------------------------------------------------------------------ writing the script file


def test_write_never_follows_a_planted_tmp_symlink(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("precious", encoding="utf-8")
    out = tmp_path / "spoolman-setup.sh"
    for planted in (out.with_name(out.name + ".tmp"), out.with_name("." + out.name + ".tmp")):
        planted.symlink_to(victim)
    standalone_script.write(REPO, "test-version", out)
    assert victim.read_text(encoding="utf-8") == "precious"
    assert out.is_file() and not out.is_symlink() and os.access(out, os.X_OK)
    assert (out.stat().st_mode & 0o777) == 0o755


def test_concurrent_writers_use_their_own_temp_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "spoolman-setup.sh"
    both_rendered = threading.Barrier(2, timeout=30)

    def render(_root: Path, version: str) -> str:
        both_rendered.wait()  # both writers hold their text before either touches the disk
        return f"#!/bin/sh\n# {version}\n"

    names: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def mkstemp(**kw: Any) -> tuple[int, str]:
        fd, name = real_mkstemp(**kw)
        names.append(name)
        return fd, name

    monkeypatch.setattr(standalone_script, "render", render)
    monkeypatch.setattr(standalone_script.tempfile, "mkstemp", mkstemp)
    errors: list[BaseException] = []

    def writer(version: str) -> None:
        try:
            standalone_script.write(REPO, version, out)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(v,)) for v in ("one", "two")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors
    assert len(names) == 2 and len(set(names)) == 2
    assert out.read_text(encoding="utf-8") in ("#!/bin/sh\n# one\n", "#!/bin/sh\n# two\n")
    assert [p.name for p in tmp_path.iterdir()] == [out.name]  # no temp file left behind
