from __future__ import annotations

import json

import pytest

import hardware
from commands import hardware as hardware_command
from errors import UsageError


def test_build_report_summarizes_available_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hardware.env, "detect_os", lambda: "macos")
    monkeypatch.setattr(hardware.env, "find_openscad", lambda: "/opt/bin/openscad")
    monkeypatch.setattr(hardware.env, "find_magick", lambda: "magick")
    monkeypatch.setattr(hardware.env, "find_slicer", lambda: ("orca", "/opt/bin/orca-slicer"))
    monkeypatch.setattr(hardware.env, "resolve_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(hardware.env, "py_has_module", lambda mod: True)
    monkeypatch.setattr(hardware.shutil, "which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(hardware.os, "access", lambda path, mode: False)
    monkeypatch.setattr(hardware.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(hardware.platform, "machine", lambda: "arm64")

    report = hardware.build_report()

    assert report.os_name == "macos"
    assert report.cpu_count == 8
    assert report.machine == "arm64"
    assert report.is_valid()
    assert report.item("openscad").status == "PASS"
    assert (
        report.item("python mesh stack").detail
        == f"all {len(hardware.env.PY_MESH_MODULES)} modules importable by /usr/bin/python3"
    )
    assert report.item("slicer").capability == "slice"


def test_build_report_warns_when_uv_can_resolve_missing_mesh_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hardware.env, "detect_os", lambda: "linux-apt")
    monkeypatch.setattr(hardware.env, "find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr(hardware.env, "find_magick", lambda: "/usr/bin/magick")
    monkeypatch.setattr(hardware.env, "find_slicer", lambda: None)
    monkeypatch.setattr(hardware.env, "install_cmd", lambda tool: f"install {tool}")
    monkeypatch.setattr(hardware.env, "resolve_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(hardware.env, "py_has_module", lambda mod: mod in {"trimesh", "numpy"})
    monkeypatch.setattr(hardware.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(hardware.os, "access", lambda path, mode: False)

    report = hardware.build_report()

    assert not report.is_valid()
    assert report.item("python mesh stack").status == "WARN"
    assert "uv resolves per-call" in report.item("python mesh stack").detail
    assert report.item("slicer").status == "MISSING"


def test_build_report_uses_valid_pip_install_for_multiple_missing_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hardware.env, "detect_os", lambda: "linux-apt")
    monkeypatch.setattr(hardware.env, "find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr(hardware.env, "find_magick", lambda: "/usr/bin/magick")
    monkeypatch.setattr(hardware.env, "find_slicer", lambda: ("orca", "/usr/bin/orca"))
    monkeypatch.setattr(hardware.env, "install_cmd", lambda tool: f"install {tool}")
    monkeypatch.setattr(hardware.env, "resolve_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(hardware.env, "py_has_module", lambda mod: mod == "trimesh")
    monkeypatch.setattr(hardware.shutil, "which", lambda name: None)
    monkeypatch.setattr(hardware.os, "access", lambda path, mode: True)

    report = hardware.build_report()

    install = report.item("python mesh stack").install
    assert install is not None
    assert "," not in install
    assert "pip install" in install


def test_hardware_command_json_list_outputs_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = hardware.HardwareReport(
        os_name="macos",
        machine="arm64",
        cpu_count=10,
        items=[
            hardware.HardwareItem(
                name="openscad",
                capability="render/export",
                status="PASS",
                detail="/opt/bin/openscad",
                required=True,
            )
        ],
    )
    monkeypatch.setattr(hardware_command, "build_report", lambda: report)

    rc = hardware_command.run(["list", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["machine"] == "arm64"
    assert payload["items"][0]["name"] == "openscad"


def test_hardware_validate_returns_nonzero_for_missing_required(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = hardware.HardwareReport(
        os_name="linux-apt",
        machine="x86_64",
        cpu_count=4,
        items=[
            hardware.HardwareItem(
                name="slicer",
                capability="slice",
                status="MISSING",
                detail="not found",
                required=True,
                install="install slicer",
            )
        ],
    )
    monkeypatch.setattr(hardware_command, "build_report", lambda: report)

    rc = hardware_command.run(["validate"])

    assert rc == 1
    assert "HARDWARE: FAIL" in capsys.readouterr().out


def test_hardware_unknown_subcommand_is_structured() -> None:
    with pytest.raises(UsageError):
        hardware_command.run(["bogus"])


def test_hardware_help_after_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    assert hardware_command.run(["list", "--help"]) == 0
    assert "3d hardware <list|validate>" in capsys.readouterr().out
