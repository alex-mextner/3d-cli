"""Helpers and generated cases for real-CLI e2e tests."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from cli.registry import discover

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
THREED: Final = REPO_ROOT / "bin" / "3d"
CUBE: Final = REPO_ROOT / "examples" / "cube.scad"

COMMANDS: Final = [cmd.name for cmd in discover().commands()]
CWD_VARIANTS: Final = ("repo", "examples", "tests", "docs", "temp", "temp-nested")
ENV_VARIANTS: Final = (
    "baseline",
    "no-uv",
    "preset-openscadpath",
    "empty-pythonpath",
    "term-dumb",
    "path-copy",
)


@dataclass(frozen=True)
class CliCase:
    id: str
    argv: list[str]
    expected_returncodes: tuple[int, ...]
    stdout_contains: tuple[str, ...] = ()
    stderr_contains: tuple[str, ...] = ()
    cwd_variant: str = "repo"
    env_variant: str = "baseline"
    timeout: int = 10
    env_extra: dict[str, str] = field(default_factory=dict)


def _cwd_for_variant(variant: str, temp_root: Path) -> Path:
    if variant == "repo":
        return REPO_ROOT
    if variant == "examples":
        return REPO_ROOT / "examples"
    if variant == "tests":
        return REPO_ROOT / "tests"
    if variant == "docs":
        return REPO_ROOT / "docs"
    if variant == "temp":
        return temp_root / "cwd"
    if variant == "temp-nested":
        return temp_root / "cwd" / "nested"
    raise ValueError(f"unknown cwd variant: {variant}")


def _env_for_variant(variant: str, temp_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    config_home = temp_root / "config"
    data_home = temp_root / "data"
    bootstrap_dir = config_home / "3d-cli"
    bootstrap_dir.mkdir(parents=True, exist_ok=True)
    data_home.mkdir(parents=True, exist_ok=True)
    (bootstrap_dir / ".bootstrapped").write_text("", encoding="utf-8")

    env.update(
        {
            "HOME": str(temp_root / "home"),
            "REPO_ROOT": str(REPO_ROOT),
            "XDG_CONFIG_HOME": str(config_home),
            "XDG_DATA_HOME": str(data_home),
            "PYTHONWARNINGS": "error",
        }
    )
    env.pop("PYTHONPATH", None)

    if variant == "baseline":
        return env
    if variant == "no-uv":
        env["PY3D_NO_UV"] = "1"
        return env
    if variant == "preset-openscadpath":
        env["OPENSCADPATH"] = str(REPO_ROOT / "libs")
        return env
    if variant == "empty-pythonpath":
        env["PYTHONPATH"] = ""
        return env
    if variant == "term-dumb":
        env["TERM"] = "dumb"
        env["NO_COLOR"] = "1"
        return env
    if variant == "path-copy":
        env["PATH"] = os.environ.get("PATH", "")
        return env
    raise ValueError(f"unknown env variant: {variant}")


def run_3d(case: CliCase) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="3d-e2e-") as temp:
        temp_root = Path(temp)
        cwd = _cwd_for_variant(case.cwd_variant, temp_root)
        cwd.mkdir(parents=True, exist_ok=True)
        env = _env_for_variant(case.env_variant, temp_root)
        env.update(case.env_extra)

        return subprocess.run(
            [sys.executable, str(THREED), *case.argv],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=case.timeout,
        )


TOP_LEVEL_CASES: Final = [
    CliCase(
        id=f"top-help-{cwd_variant}-{env_variant}",
        argv=["help"],
        expected_returncodes=(0,),
        stdout_contains=("USAGE", "3d <command>"),
        cwd_variant=cwd_variant,
        env_variant=env_variant,
    )
    for cwd_variant in CWD_VARIANTS
    for env_variant in ENV_VARIANTS
] + [
    CliCase(
        id=f"top-bare-help-{cwd_variant}-{env_variant}",
        argv=[],
        expected_returncodes=(0,),
        stdout_contains=("USAGE", "3d <command>"),
        cwd_variant=cwd_variant,
        env_variant=env_variant,
    )
    for cwd_variant in CWD_VARIANTS
    for env_variant in ENV_VARIANTS
] + [
    CliCase(
        id=f"top-version-{cwd_variant}-{env_variant}",
        argv=["version"],
        expected_returncodes=(0,),
        stdout_contains=("3d v",),
        cwd_variant=cwd_variant,
        env_variant=env_variant,
    )
    for cwd_variant in CWD_VARIANTS
    for env_variant in ENV_VARIANTS
] + [
    CliCase(
        id=f"top-unknown-{cwd_variant}-{env_variant}",
        argv=["definitely-not-a-command"],
        expected_returncodes=(2,),
        stderr_contains=("unknown command",),
        cwd_variant=cwd_variant,
        env_variant=env_variant,
    )
    for cwd_variant in CWD_VARIANTS
    for env_variant in ENV_VARIANTS
]

PARAMS_CASES: Final = [
    CliCase(
        id=f"params-json-absolute-{env_variant}",
        argv=["params", str(CUBE), "--json"],
        expected_returncodes=(0,),
        stdout_contains=('"name": "width"', '"value": "20"'),
        env_variant=env_variant,
    )
    for env_variant in ENV_VARIANTS
] + [
    CliCase(
        id=f"params-text-absolute-{env_variant}",
        argv=["params", str(CUBE)],
        expected_returncodes=(0,),
        stdout_contains=("width", "outer width"),
        env_variant=env_variant,
    )
    for env_variant in ENV_VARIANTS
] + [
    CliCase(
        id=f"params-json-repo-relative-{env_variant}",
        argv=["params", "examples/cube.scad", "--json"],
        expected_returncodes=(0,),
        stdout_contains=('"name": "height"',),
        env_variant=env_variant,
    )
    for env_variant in ENV_VARIANTS
] + [
    CliCase(
        id=f"params-text-examples-relative-{env_variant}",
        argv=["params", "cube.scad"],
        expected_returncodes=(0,),
        stdout_contains=("height", "wall"),
        cwd_variant="examples",
        env_variant=env_variant,
    )
    for env_variant in ENV_VARIANTS
] + [
    CliCase(
        id=f"params-no-args-{env_variant}",
        argv=["params"],
        expected_returncodes=(1,),
        stdout_contains=("3d params",),
        env_variant=env_variant,
    )
    for env_variant in ENV_VARIANTS
] + [
    CliCase(
        id=f"params-missing-file-{env_variant}",
        argv=["params", "missing.scad"],
        expected_returncodes=(2,),
        stderr_contains=("file not found", "missing.scad"),
        env_variant=env_variant,
    )
    for env_variant in ENV_VARIANTS
]
