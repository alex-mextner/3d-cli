"""Behavioural tests for lib/slicing/orca_cli.py — finding the CLI, the slicing copy, failure classes,
the slice runner (against fake CLIs, no real slicer) and the vendor-preset reader."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from errors import MissingDependency, ThreeDError
from prep.contracts import StepResult
from slicing import orca_cli
from slicing.orca_cli import (
    KNOWN_QUIRKS,
    QUIRK_BY_ID,
    OrcaCli,
    SystemPresets,
    classify_failure,
    find_orca,
    slice_project,
    slicing_copy,
)

REPO = Path(__file__).resolve().parent.parent
PLATE = (
    b'  <plate>\n    <metadata key="plater_id" value="1"/>\n'
    b'    <model_instance>\n      <metadata key="object_id" value="2"/>\n    </model_instance>\n  </plate>\n'
)
SETTINGS_HEAD = b'<?xml version="1.0"?>\n<config>\n  <object id="2">\n    <metadata key="name" value="cat"/>\n  </object>\n'
SETTINGS_TAIL = b"  <assemble>\n  </assemble>\n</config>\n"
CFG = {"layer_height": "0.2", "wall_loops": "4", "filament_colour": ["#000000"]}


def make_project(path: Path) -> Path:
    members = {
        "[Content_Types].xml": b"<Types/>",
        "3D/3dmodel.model": b"<model>\x00\x01binary-ish</model>",
        "Metadata/model_settings.config": SETTINGS_HEAD + PLATE + SETTINGS_TAIL,
        "Metadata/plate_1.json": b'{"bbox_all": []}',
        "Metadata/project_settings.config": json.dumps(CFG, indent=4).encode(),
        "Metadata/layer_heights_profile.txt": b"object_id=1|0;0.2;10;0.2\n",
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def read_members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}


# ------------------------------------------------------------------ slicing_copy


def test_slicing_copy_drops_only_the_plate_block(tmp_path: Path) -> None:
    src = make_project(tmp_path / "a.3mf")
    dst = tmp_path / "b.3mf"
    slicing_copy(src, dst)
    before, after = read_members(src), read_members(dst)
    assert (
        b"<plate>" in before["Metadata/model_settings.config"]
    )  # the control: the source has the block
    assert after["Metadata/model_settings.config"] == SETTINGS_HEAD + SETTINGS_TAIL
    assert "Metadata/plate_1.json" in before
    assert "Metadata/plate_1.json" not in after
    assert set(before) - set(after) == {"Metadata/plate_1.json"}
    for name in set(after) - {"Metadata/model_settings.config"}:
        assert after[name] == before[name], name


def test_slicing_copy_set_cfg_changes_only_the_named_keys(tmp_path: Path) -> None:
    src = make_project(tmp_path / "a.3mf")
    dst = tmp_path / "b.3mf"
    slicing_copy(src, dst, set_cfg={"wall_loops": "6", "brand_new": "1"})
    cfg = json.loads(read_members(dst)["Metadata/project_settings.config"])
    assert cfg == {**CFG, "wall_loops": "6", "brand_new": "1"}


def test_slicing_copy_can_drop_the_layer_profile(tmp_path: Path) -> None:
    src = make_project(tmp_path / "a.3mf")
    kept, dropped = tmp_path / "k.3mf", tmp_path / "d.3mf"
    slicing_copy(src, kept)
    slicing_copy(src, dropped, drop_layer_profile=True)
    assert "Metadata/layer_heights_profile.txt" in read_members(kept)
    assert "Metadata/layer_heights_profile.txt" not in read_members(dropped)


def test_slicing_copy_set_cfg_needs_a_project_settings_member(tmp_path: Path) -> None:
    src = tmp_path / "bare.3mf"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
    with pytest.raises(MissingDependency):
        slicing_copy(src, tmp_path / "o.3mf", set_cfg={"a": "1"})


def test_slicing_copy_handles_several_plates(tmp_path: Path) -> None:
    src = tmp_path / "multi.3mf"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr(
            "Metadata/model_settings.config",
            (SETTINGS_HEAD + PLATE + PLATE + SETTINGS_TAIL),
        )
        z.writestr("Metadata/plate_1.json", "{}")
        z.writestr("Metadata/plate_2.json", "{}")
    slicing_copy(src, tmp_path / "o.3mf")
    after = read_members(tmp_path / "o.3mf")
    assert set(after) == {"Metadata/model_settings.config"}
    assert b"<plate>" not in after["Metadata/model_settings.config"]


# ------------------------------------------------------------------ classify_failure


@pytest.mark.parametrize("rc", [-11, 139])
def test_segfault_is_classified_with_the_plate_remediation(rc: int) -> None:
    f = classify_failure(rc, "", None, has_gcode=False)
    assert f is not None and f.kind == "segfault"
    assert "plate-block-segfault" in f.quirk_ids
    assert any("<plate>" in r for r in f.remediation)


def test_exclude_triangles_only_fails_when_nothing_was_written() -> None:
    log = "[error]   calc_exclude_triangles:Unable to create exclude triangles\n"
    bad = classify_failure(2, log, None, has_gcode=False)
    assert bad is not None and bad.kind == "exclude_triangles"
    assert any("--arrange 0" in r for r in bad.remediation)
    # the same line next to G-code is only a warning (RECIPE.md, accepted warning)
    assert classify_failure(0, log, {"return_code": 0}, has_gcode=True) is None


def test_result_json_return_code_is_reported_with_its_error_string() -> None:
    f = classify_failure(
        0, "", {"return_code": -101, "error_string": "arrange failed"}, has_gcode=True
    )
    assert f is not None and f.kind == "return_code"
    assert "-101" in f.reason and "arrange failed" in f.reason


def test_nonzero_exit_without_result_json_is_a_return_code_failure() -> None:
    f = classify_failure(3, "boom\n", None, has_gcode=False)
    assert f is not None and f.kind == "return_code" and "boom" in f.reason


def test_clean_exit_without_gcode_is_no_gcode() -> None:
    f = classify_failure(0, "", {"return_code": 0}, has_gcode=False)
    assert f is not None and f.kind == "no_gcode"


def test_clean_run_has_no_failure() -> None:
    assert classify_failure(0, "", {"return_code": 0}, has_gcode=True) is None


def test_timeout_and_lock_busy() -> None:
    t = classify_failure(124, "", None, has_gcode=False)
    assert t is not None and t.kind == "timeout"
    busy = classify_failure(
        75, "", None, has_gcode=False, heavy=True, lock_busy_code=75
    )
    assert busy is not None and busy.kind == "lock_busy"
    # 75 without the heavy wrapper is just a slicer exit code
    other = classify_failure(
        75, "", None, has_gcode=False, heavy=False, lock_busy_code=75
    )
    assert other is not None and other.kind == "return_code"


def test_every_failure_remediation_names_known_quirks() -> None:
    samples = [
        classify_failure(139, "", None, has_gcode=False),
        classify_failure(2, "calc_exclude_triangles", None, has_gcode=False),
        classify_failure(1, "", {"return_code": -101}, has_gcode=False),
    ]
    for f in samples:
        assert f is not None and f.quirk_ids
        assert all(q in QUIRK_BY_ID for q in f.quirk_ids)


# ------------------------------------------------------------------ find_orca


def _exe(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_find_orca_precedence(tmp_path: Path) -> None:
    home = tmp_path / "home"
    apprun = _exe(home / "Applications/snapmaker_orca/AppRun")
    local = _exe(home / ".local/bin/snapmaker-orca")
    envs = _exe(tmp_path / "slicer-env")
    orca_env = _exe(tmp_path / "orca-env")
    base = {"HOME": str(home), "PATH": ""}

    assert find_orca({**base, "ORCA": str(orca_env), "SLICER": str(envs)}) == OrcaCli(
        orca_env, "env ORCA"
    )
    assert find_orca({**base, "SLICER": str(envs)}) == OrcaCli(envs, "env SLICER")
    # an env var naming a missing file is skipped, not trusted
    assert find_orca({**base, "ORCA": str(tmp_path / "nope")}) is not None
    assert find_orca({**base, "ORCA": str(tmp_path / "nope")}).path == apprun  # type: ignore[union-attr]
    apprun.unlink()
    assert find_orca(base).path == local  # type: ignore[union-attr]
    local.unlink()
    assert find_orca(base) is None


def test_find_orca_falls_back_to_path(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    exe = _exe(bindir / "orca-slicer")
    found = find_orca({"HOME": str(tmp_path / "empty"), "PATH": str(bindir)})
    assert (
        found is not None and found.path == exe and found.source == "PATH orca-slicer"
    )


def test_find_orca_ignores_a_non_executable_file(tmp_path: Path) -> None:
    f = tmp_path / "orca"
    f.write_text("#!/bin/sh\n")
    f.chmod(0o644)
    assert find_orca({"HOME": str(tmp_path), "PATH": "", "ORCA": str(f)}) is None


def test_find_orca_can_probe_the_version(tmp_path: Path) -> None:
    exe = _exe(
        tmp_path / "orca", "#!/bin/sh\necho 'Snapmaker_Orca-01.10.01.99: Usage'\n"
    )
    env = {"HOME": str(tmp_path), "PATH": "", "ORCA": str(exe)}
    found = find_orca(env, probe_version=True)
    assert found is not None and found.version == "01.10.01.99"
    assert find_orca(env) is not None and find_orca(env).version is None  # type: ignore[union-attr]
    silent = _exe(tmp_path / "silent", "#!/bin/sh\nexit 0\n")
    got = find_orca(
        {"HOME": str(tmp_path), "PATH": "", "ORCA": str(silent)}, probe_version=True
    )
    assert got is not None and got.version == orca_cli.CLI_FALLBACK_VERSION


# ------------------------------------------------------------------ slice_project with a fake CLI


FAKE = f"""#!{sys.executable}
import json, os, signal, sys, time, zipfile
a = sys.argv[1:]
if "--help" in a:
    print("Snapmaker_Orca-01.10.01.50: usage"); sys.exit(0)
def opt(n):
    return a[a.index(n) + 1]
out = opt("--outputdir")
mode = os.environ.get("FAKE_MODE", "ok")
with zipfile.ZipFile(a[-1]) as z:
    members = z.namelist()
with open(os.path.join(out, "args.json"), "w") as fh:
    json.dump({{"argv": a, "members": members, "cwd": os.getcwd()}}, fh)
if mode == "segv":
    os.kill(os.getpid(), signal.SIGSEGV)
if mode == "exit139":
    sys.exit(139)
if mode == "exclude":
    print("[2026-10-20 17:22:34.1] [0x1] [error]   calc_exclude_triangles:Unable to create exclude triangles", file=sys.stderr)
    sys.exit(2)
if mode == "sleep":
    time.sleep(30)
if mode == "rc101":
    json.dump({{"return_code": -101, "error_string": "arrange failed", "sliced_plates": []}}, open(os.path.join(out, "result.json"), "w"))
    sys.exit(0)
if mode == "nogcode":
    json.dump({{"return_code": 0, "sliced_plates": []}}, open(os.path.join(out, "result.json"), "w"))
    sys.exit(0)
print("[2026-10-20 17:22:34.1] [0x1] [warning] cli mode, Current Snapmaker_Orca Version 01.10.01.50")
print("[2026-10-20 17:22:34.2] [0x1] [error] glfw init failed")
print("[2026-10-20 17:22:34.3] [0x1] [warning] SupportSpotsGenerator does not currently handle different materials")
open(os.path.join(out, "plate_1.gcode"), "w").write("; fake\\nG1 X1\\n")
json.dump({{"return_code": 0, "sliced_plates": [{{"id": 1, "warning_message": "result says: thin wall"}}]}}, open(os.path.join(out, "result.json"), "w"))
with zipfile.ZipFile(os.path.join(out, "sliced.3mf"), "w") as z:
    z.writestr("Metadata/slice_info.config", '<config><plate><warning msg="slice info: bridge" error_code="1001" level="1"/></plate></config>')
"""


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OrcaCli:
    monkeypatch.setenv(
        "THREED_HEAVY_HELD", "1"
    )  # as inside the machine lock: heavy_prefix() == []
    return OrcaCli(_exe(tmp_path / "fake-orca", FAKE), "test")


def run(tmp_path: Path, fake: OrcaCli, **kw: object) -> orca_cli.SliceOutcome:
    project = make_project(tmp_path / "p.3mf")
    return slice_project(project, tmp_path / "work", orca=fake, **kw)  # type: ignore[arg-type]


def test_slice_project_success_reads_outputs_and_warnings(
    tmp_path: Path, fake: OrcaCli
) -> None:
    out = run(tmp_path, fake)
    assert out.ok and out.failure is None
    assert out.gcode == (tmp_path / "work" / "plate_1.gcode")
    assert out.sliced_3mf is not None and out.sliced_3mf.exists()
    assert out.log_path.read_text().count("[warning]") >= 2
    msgs = {w.message: w.source for w in out.warnings}
    assert msgs["result says: thin wall"] == "result.json"
    assert msgs["slice info: bridge"] == "slice_info"
    assert any("SupportSpotsGenerator" in m for m in msgs)
    assert not any(
        "glfw" in m.lower() or "cli mode" in m.lower() for m in msgs
    )  # host noise left out
    assert out.verdict == "WARN"
    assert out.result["return_code"] == 0


def test_slice_project_passes_the_headless_flags_and_slices_the_plate_free_copy(
    tmp_path: Path, fake: OrcaCli
) -> None:
    out = run(tmp_path, fake)
    seen = json.loads((tmp_path / "work" / "args.json").read_text())
    argv = seen["argv"]
    work = str(tmp_path / "work")
    assert argv[argv.index("--arrange") + 1] == "0"
    assert argv[argv.index("--slice") + 1] == "0"
    assert argv[argv.index("--datadir") + 1] == f"{work}/dd"
    assert argv[argv.index("--outputdir") + 1] == work
    assert argv[argv.index("--export-3mf") + 1] == "sliced.3mf"
    assert argv[-1] == f"{work}/in.3mf"
    assert (
        "Metadata/plate_1.json" not in seen["members"]
    )  # the CLI got the plate-free copy
    assert (
        b"<plate>"
        not in read_members(tmp_path / "work" / "in.3mf")[
            "Metadata/model_settings.config"
        ]
    )
    assert (
        "flock" not in out.command
    )  # inside the lock heavy_prefix() adds nothing: never a nested flock


def test_stale_outputs_of_an_earlier_run_do_not_count(
    tmp_path: Path, fake: OrcaCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, fake)
    monkeypatch.setenv("FAKE_MODE", "exclude")
    out = run(tmp_path, fake)
    assert out.gcode is None and out.failure is not None


@pytest.mark.parametrize(
    ("mode", "kind"),
    [
        ("segv", "segfault"),
        ("exit139", "segfault"),
        ("exclude", "exclude_triangles"),
        ("rc101", "return_code"),
        ("nogcode", "no_gcode"),
    ],
)
def test_slice_project_classifies_failures(
    tmp_path: Path, fake: OrcaCli, monkeypatch: pytest.MonkeyPatch, mode: str, kind: str
) -> None:
    monkeypatch.setenv("FAKE_MODE", mode)
    out = run(tmp_path, fake)
    assert not out.ok and out.verdict == "FAIL"
    assert out.failure is not None and out.failure.kind == kind
    assert out.failure.remediation
    assert out.gcode is None
    assert out.summary().startswith(f"slice FAILED ({kind})")


def test_slice_project_times_out_with_gnu_timeout(
    tmp_path: Path, fake: OrcaCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODE", "sleep")
    started = __import__("time").monotonic()
    out = run(tmp_path, fake, timeout_s=1)
    assert out.failure is not None and out.failure.kind == "timeout"
    assert __import__("time").monotonic() - started < 20


def test_slice_project_times_out_without_gnu_timeout(
    tmp_path: Path, fake: OrcaCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODE", "sleep")
    monkeypatch.setattr(orca_cli.shutil, "which", lambda *_a, **_k: None)
    out = run(tmp_path, fake, timeout_s=1)
    assert out.failure is not None and out.failure.kind == "timeout"
    assert out.command[0] == str(fake.path)


def test_slice_project_without_a_cli_names_the_install_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orca_cli, "find_orca", lambda *a, **k: None)
    with pytest.raises(MissingDependency) as exc:
        slice_project(make_project(tmp_path / "p.3mf"), tmp_path / "w")
    assert "snapmaker_orca" in " ".join(exc.value.remediation)


def test_slice_outcome_is_a_step_result_with_plain_json(
    tmp_path: Path, fake: OrcaCli
) -> None:
    out = run(tmp_path, fake)
    assert isinstance(out, StepResult)
    json.dumps(out.to_dict())
    assert "plate_1.gcode" in out.summary()


@pytest.mark.skipif(
    find_orca() is None or os.environ.get("THREED_REAL_ORCA") != "1",
    reason="real slice: needs the Snapmaker Orca CLI and THREED_REAL_ORCA=1 (run under the heavy lock)",
)
def test_real_cli_refuses_its_own_unstamped_project(tmp_path: Path) -> None:
    cli = find_orca()
    assert cli is not None
    stl = tmp_path / "cube.stl"
    orca_cli._write_probe_stl(stl, 135.0, 135.0)
    r = subprocess.run(
        [
            str(cli.path),
            "--datadir",
            str(tmp_path / "dd0"),
            "--outputdir",
            str(tmp_path),
            "--export-3mf",
            "proj.3mf",
            str(stl),
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert (tmp_path / "proj.3mf").exists(), r.stdout[-500:] + r.stderr[-500:]
    refused = slice_project(
        tmp_path / "proj.3mf", tmp_path / "w0", orca=cli, heavy=False, timeout_s=600
    )
    assert (
        refused.failure is not None
        and "Unsupported 3MF version" in refused.failure.reason
    )


# ------------------------------------------------------------------ SystemPresets / reference settings


def write_json(path: Path, obj: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


@pytest.fixture
def vendors(tmp_path: Path) -> Path:
    res = tmp_path / "res"
    for vendor in ("Acme", "Snapmaker"):
        write_json(
            res / vendor / "process/fdm_common.json",
            {"name": "fdm_common", "layer_height": "0.2", "owner": vendor},
        )
    write_json(
        res / "Snapmaker/process/fdm_u1.json",
        {"name": "fdm_u1", "inherits": "fdm_common", "wall_loops": "2"},
    )
    write_json(
        res / "Snapmaker/process/0.20mm Standard.json",
        {
            "name": "0.20mm Standard",
            "inherits": "fdm_u1",
            "wall_loops": "3",
            "setting_id": "GP004",
        },
    )
    # file stem differs from the inner name (4 of 547 real Snapmaker files)
    write_json(
        res / "Snapmaker/process/odd-file.json",
        {"name": "Odd Name", "inherits": "fdm_u1"},
    )
    write_json(
        res / "Snapmaker/process/loop_a.json", {"name": "loop_a", "inherits": "loop_b"}
    )
    write_json(
        res / "Snapmaker/process/loop_b.json", {"name": "loop_b", "inherits": "loop_a"}
    )
    write_json(
        res / "Snapmaker/process/orphan.json", {"name": "orphan", "inherits": "ghost"}
    )
    write_json(
        res / "Snapmaker/machine/Snapmaker U1.json",
        {"name": "Snapmaker U1", "nozzle_diameter": ["0.4"] * 4},
    )
    write_json(
        res / "Snapmaker/filament/PLA.json", {"name": "PLA", "filament_type": ["PLA"]}
    )
    return res


def test_flat_merges_the_whole_chain_child_wins_and_same_vendor_parent(
    vendors: Path,
) -> None:
    flat = SystemPresets(vendors).flat("process", "0.20mm Standard")
    assert flat["wall_loops"] == "3"  # child over fdm_u1's 2
    assert flat["layer_height"] == "0.2"  # grandparent's value survives
    assert (
        flat["owner"] == "Snapmaker"
    )  # fdm_common of the child's own vendor, not Acme's
    assert "inherits" not in flat


def test_presets_are_found_by_inner_name_when_the_stem_differs(vendors: Path) -> None:
    sp = SystemPresets(vendors)
    assert sp.flat("process", "Odd Name")["wall_loops"] == "2"
    assert sp.path("process", "nope") is None


def test_chain_reports_missing_parent_and_cycles(vendors: Path) -> None:
    sp = SystemPresets(vendors)
    with pytest.raises(KeyError, match="ghost"):
        sp.chain("process", "orphan")
    with pytest.raises(ValueError, match="cycle"):
        sp.chain("process", "loop_a")


REF_FAKE = f"""#!{sys.executable}
import json, os, sys, zipfile
a = sys.argv[1:]
def opt(n):
    return a[a.index(n) + 1]
cfg = {{"argv": a}}
for f in opt("--load-settings").split(";") + opt("--load-filaments").split(";"):
    d = json.load(open(f))
    cfg[d["name"]] = d
with zipfile.ZipFile(os.path.join(opt("--outputdir"), opt("--export-3mf")), "w") as z:
    z.writestr("Metadata/project_settings.config", json.dumps(cfg))
"""


def test_reference_settings_hands_the_cli_flattened_presets(
    tmp_path: Path, vendors: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("THREED_HEAVY_HELD", "1")
    cli = OrcaCli(_exe(tmp_path / "ref-orca", REF_FAKE), "test")
    cfg = orca_cli.reference_project_settings(
        cli,
        SystemPresets(vendors),
        vendor="Snapmaker",
        machine="Snapmaker U1",
        process="0.20mm Standard",
        filaments=["PLA", "PLA"],
        bed_centre=(135.0, 135.0),
    )
    proc = cfg["0.20mm Standard"]
    assert (
        proc["layer_height"] == "0.2" and proc["wall_loops"] == "3"
    )  # grandparent flattened in
    assert proc["from"] == "system" and proc["instantiation"] == "true"
    argv = cfg["argv"]
    assert argv.count("--load-filaments") == 1
    assert (
        argv[argv.index("--load-filaments") + 1].count(";") == 1
    )  # two filament files
    assert argv[argv.index("--debug") + 1] == "1"


def test_reference_settings_names_the_missing_preset(
    tmp_path: Path, vendors: Path
) -> None:
    cli = OrcaCli(_exe(tmp_path / "x", "#!/bin/sh\nexit 0\n"), "test")
    with pytest.raises(MissingDependency) as exc:
        orca_cli.reference_project_settings(
            cli,
            SystemPresets(vendors),
            vendor="Snapmaker",
            machine="Snapmaker U1",
            process="no such preset",
            filaments=["PLA"],
            bed_centre=(1.0, 1.0),
            heavy=False,
        )
    assert "no such preset" in exc.value.message
    assert "resources/profiles" in " ".join(exc.value.remediation)


def test_reference_settings_fails_loudly_when_the_cli_writes_nothing(
    tmp_path: Path, vendors: Path
) -> None:
    cli = OrcaCli(_exe(tmp_path / "x", "#!/bin/sh\necho oops\nexit 3\n"), "test")
    with pytest.raises(ThreeDError) as exc:
        orca_cli.reference_project_settings(
            cli,
            SystemPresets(vendors),
            vendor="Snapmaker",
            machine="Snapmaker U1",
            process="0.20mm Standard",
            filaments=["PLA"],
            bed_centre=(1.0, 1.0),
            heavy=False,
        )
    assert "exit 3" in exc.value.message and "oops" in " ".join(exc.value.remediation)


def test_a_missing_opengl_runtime_is_a_missing_dependency_with_the_wrapper_hint(
    tmp_path: Path, vendors: Path
) -> None:
    """The Chekhov box's raw AppRun needs libOpenGL.so.0; the fix is a wrapper, not a retry."""
    script = "#!/bin/sh\necho 'Error: missing host OpenGL runtime library libOpenGL.so.0.' >&2\nexit 1\n"
    cli = OrcaCli(_exe(tmp_path / "x", script), "test")
    with pytest.raises(MissingDependency) as exc:
        orca_cli.reference_project_settings(
            cli,
            SystemPresets(vendors),
            vendor="Snapmaker",
            machine="Snapmaker U1",
            process="0.20mm Standard",
            filaments=["PLA"],
            bed_centre=(1.0, 1.0),
            heavy=False,
        )
    assert "OpenGL" in exc.value.message and "ORCA=" in " ".join(exc.value.remediation)


# ------------------------------------------------------------------ quirk catalogue


def test_quirk_catalogue_is_complete_and_unique() -> None:
    ids = [q.id for q in KNOWN_QUIRKS]
    assert len(ids) == len(set(ids))
    required = {
        "plate-block-segfault",
        "layer-range-segfault",
        "exclude-triangles",
        "prime-tower-single-object",
        "mixed-row-convention",
        "tool-index-offset",
    }
    assert required <= set(ids)
    for q in KNOWN_QUIRKS:
        assert all((q.symptom, q.cause, q.fix, q.evidence)), q.id


def test_every_quirk_id_appears_in_the_u1_cli_doc() -> None:
    doc = (REPO / "docs/printers/u1-orca-cli.md").read_text(encoding="utf-8")
    missing = [q.id for q in KNOWN_QUIRKS if f"`{q.id}`" not in doc]
    assert not missing, f"docs/printers/u1-orca-cli.md lacks quirk ids: {missing}"


def test_unsupported_3mf_version_points_at_the_application_tag() -> None:
    res = {"return_code": -24, "error_string": "Unsupported 3MF version."}
    f = classify_failure(0, "", res, has_gcode=False)
    assert f is not None and f.kind == "return_code"
    assert (
        "application-tag-newer" in f.quirk_ids and "Unsupported 3MF version" in f.reason
    )
