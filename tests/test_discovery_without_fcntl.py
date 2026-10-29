"""Command discovery must survive a platform without `fcntl` (native Windows): only the POSIX-only
commands drop out, every other command, including `solid` and `qymcad`, stays registered."""

from __future__ import annotations
from pathlib import Path
import subprocess
import sys

LIB = Path(__file__).resolve().parent.parent / "lib"


def test_discovery_without_fcntl_keeps_portable_commands() -> None:
    script = (
        "import sys\n"
        "sys.modules['fcntl'] = None\n"
        "from cli.registry import discover\n"
        "names = discover().names()\n"
        "print(len(names), 'solid' in names, 'qymcad' in names)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=LIB,
        env={"PYTHONPATH": str(LIB), "PATH": ""},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    count, has_solid, has_qymcad = result.stdout.split()
    assert int(count) > 50 and has_solid == "True" and has_qymcad == "True"


def test_other_import_errors_are_not_swallowed(tmp_path: Path) -> None:
    package = tmp_path / "brokencmds"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "bad.py").write_text(
        "import module_that_does_not_exist_anywhere\n", encoding="utf-8"
    )
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(tmp_path)!r})\n"
        "from cli.registry import discover\n"
        "discover('brokencmds')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=LIB,
        env={"PYTHONPATH": str(LIB), "PATH": ""},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0
    assert "module_that_does_not_exist_anywhere" in result.stderr
