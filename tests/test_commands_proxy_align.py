from __future__ import annotations

from typing import Any

import pytest

from commands.proxy_align import run
from errors import InputNotFound, InvalidArgument, UsageError


def test_proxy_align_no_args() -> None:
    assert run([]) == 1


def test_proxy_align_help() -> None:
    assert run(["--help"]) == 0
    assert run(["cad.stl", "--help"]) == 0


def test_proxy_align_requires_existing_inputs() -> None:
    with pytest.raises(InputNotFound):
        run(["missing-cad.stl", "missing-proxy.stl"])


def test_proxy_align_rejects_unknown_option(tmp_path: Any) -> None:
    cad = tmp_path / "cad.stl"
    proxy = tmp_path / "proxy.stl"
    cad.write_text("solid cad\nendsolid cad\n", encoding="utf-8")
    proxy.write_text("solid proxy\nendsolid proxy\n", encoding="utf-8")
    with pytest.raises(UsageError):
        run([str(cad), str(proxy), "--bogus"])


def test_proxy_align_rejects_missing_flag_value(tmp_path: Any) -> None:
    cad = tmp_path / "cad.stl"
    proxy = tmp_path / "proxy.stl"
    cad.write_text("solid cad\nendsolid cad\n", encoding="utf-8")
    proxy.write_text("solid proxy\nendsolid proxy\n", encoding="utf-8")
    with pytest.raises(UsageError):
        run([str(cad), str(proxy), "--out"])
    with pytest.raises(UsageError):
        run([str(cad), str(proxy), "--out", "--json"])


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--samples", "20"),
        ("--icp-steps", "-1"),
        ("--yaw-step", "0.001"),
        ("--yaw-step", "nan"),
        ("--pitch", ""),
        ("--roll", "front"),
    ],
)
def test_proxy_align_rejects_invalid_numeric_values(tmp_path: Any, flag: str, value: str) -> None:
    cad = tmp_path / "cad.stl"
    proxy = tmp_path / "proxy.stl"
    cad.write_text("solid cad\nendsolid cad\n", encoding="utf-8")
    proxy.write_text("solid proxy\nendsolid proxy\n", encoding="utf-8")
    with pytest.raises(InvalidArgument):
        run([str(cad), str(proxy), flag, value])


def test_proxy_align_rejects_candidate_grid_that_is_too_large(tmp_path: Any) -> None:
    cad = tmp_path / "cad.stl"
    proxy = tmp_path / "proxy.stl"
    cad.write_text("solid cad\nendsolid cad\n", encoding="utf-8")
    proxy.write_text("solid proxy\nendsolid proxy\n", encoding="utf-8")
    pitch = ",".join(str(i) for i in range(15))
    with pytest.raises(UsageError, match="candidate grid"):
        run([str(cad), str(proxy), "--yaw-step", "1", "--pitch", pitch])


def test_proxy_align_dispatches_tool(monkeypatch: Any, tmp_path: Any) -> None:
    cad = tmp_path / "cad.stl"
    proxy = tmp_path / "proxy.stl"
    cad.write_text("solid cad\nendsolid cad\n", encoding="utf-8")
    proxy.write_text("solid proxy\nendsolid proxy\n", encoding="utf-8")
    seen: dict[str, Any] = {}

    def fake_exec_tool(deps: str, script: str, args: list[str]) -> int:
        seen["deps"] = deps
        seen["script"] = script
        seen["args"] = args
        return 0

    monkeypatch.setattr("commands.proxy_align.exec_tool", fake_exec_tool)
    assert run([str(cad), str(proxy), "--out", "match/proxy", "--json"]) == 0
    assert seen["deps"] == "trimesh,numpy,scipy"
    assert seen["script"] == "proxy_align.py"
    assert seen["args"] == [
        "--cad",
        str(cad),
        "--proxy",
        str(proxy),
        "--out",
        "match/proxy",
        "--json",
    ]
