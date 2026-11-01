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
from pathlib import Path

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
    script: Path, root: Path, *args: str, **extra: str
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
