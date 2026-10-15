"""`3d ha deploy` (lib/ha_deploy.py): manifest/snippet/compose rules, and whole deploys against a
local stand-in for the HA host (tests/ha_deploy_fixtures.py)."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from ha_deploy_fixtures import GIT_ENV_LEAKS, ENTITIES_OK, World, build_world, git

import ha_deploy
from errors import GateFailure
from ha_deploy import (
    INTEGRATION,
    FileEntry,
    Options,
    compose_problems,
    load_ha_yaml,
    load_manifest,
    manifest_orphans,
    package_reloads,
    snippet_missing,
    symlink_target,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL = REPO_ROOT / INTEGRATION


# ---- the real integration tree -------------------------------------------------------------
def _tree_files(root: Path) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".pytest_cache")]
        out += [
            str((Path(dirpath) / f).relative_to(root))
            for f in filenames
            if not f.endswith(".pyc")
        ]
    return sorted(out)


def test_manifest_lists_every_integration_file_and_nothing_else() -> None:
    manifest = load_manifest((REAL / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest_orphans(manifest, _tree_files(REAL)) == []


def test_manifest_services_exist_in_the_3d_compose_file() -> None:
    manifest = load_manifest((REAL / "manifest.yaml").read_text(encoding="utf-8"))
    compose = load_ha_yaml(
        (REAL / "compose/3d.docker-compose.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(compose, dict)
    named = {s for f in manifest.files for s in f.services}
    assert named and named <= set(compose["services"])


def test_real_packages_only_need_reloads() -> None:
    reloads, restart = package_reloads(
        (REAL / "packages/3d_printers.yaml").read_text(encoding="utf-8")
    )
    assert restart == []
    assert reloads == {
        "automation.reload",
        "rest.reload",
        "rest_command.reload",
        "script.reload",
        "template.reload",
    }


def test_www_files_are_copies_and_everything_else_symlinks() -> None:
    manifest = load_manifest((REAL / "manifest.yaml").read_text(encoding="utf-8"))
    for entry in manifest.files:
        assert entry.mode == ("copy" if entry.dest.startswith("www/") else "symlink"), (
            entry
        )


def test_help_and_docs_list_the_same_flags() -> None:
    from commands.ha import USAGE

    doc = (REPO_ROOT / "docs/commands/ha.md").read_text(encoding="utf-8")
    flags = set(re.findall(r"--[a-z][a-z-]+", USAGE))
    assert flags == {"--dry-run", "--host", "--ref", "--ha-dir", "--source"}
    assert flags <= set(re.findall(r"--[a-z][a-z-]+", doc))


# ---- rules --------------------------------------------------------------------------------
def test_symlink_target_is_relative_to_the_dest_directory() -> None:
    manifest = load_manifest((REAL / "manifest.yaml").read_text(encoding="utf-8"))
    nested = FileEntry(
        "packages/3d_printers.yaml", "packages/3d_printers.yaml", "symlink"
    )
    top = FileEntry("go2rtc/go2rtc.yaml", "go2rtc.yaml", "symlink")
    assert (
        symlink_target(manifest, nested)
        == "../3d-cli/integrations/homeassistant/packages/3d_printers.yaml"
    )
    assert (
        symlink_target(manifest, top)
        == "3d-cli/integrations/homeassistant/go2rtc/go2rtc.yaml"
    )


def test_snippet_missing_reports_keys_values_and_list_items() -> None:
    expected = load_ha_yaml("a:\n  b: 1\n  c: [x, {u: 1}]\nd: !secret s\n")
    assert (
        snippet_missing(
            expected,
            load_ha_yaml("a:\n  b: 1\n  c: [{u: 1}, x, y]\n  z: 2\nd: !secret s\n"),
        )
        == []
    )
    live = load_ha_yaml("a:\n  b: 2\n  c: [x]\nd: !secret other\n")
    assert snippet_missing(expected, live) == [
        "a.b: live 2 != 1",
        "a.c: missing item {'u': 1}",
        "d: live Tagged(tag='!secret', value='other') != Tagged(tag='!secret', value='s')",
    ]
    assert snippet_missing(expected, {}) == ["a: missing", "d: missing"]


def test_compose_must_include_the_3d_file_and_not_redefine_its_services() -> None:
    inc = "3d-cli/integrations/homeassistant/compose/3d.docker-compose.yaml"
    ok = f"include:\n  - {inc}\nservices:\n  homeassistant: {{}}\n"
    assert compose_problems(ok, inc, ["go2rtc"], "c.yaml") == []
    long_form = f"include:\n  - path: [{inc}]\nservices: {{}}\n"
    assert compose_problems(long_form, inc, ["go2rtc"], "c.yaml") == []
    bad = "services:\n  go2rtc: {}\n"
    assert compose_problems(bad, inc, ["go2rtc"], "c.yaml") == [
        f"c.yaml: `include:` does not list {inc}",
        "c.yaml: service go2rtc is defined there too; it must come only from the include",
    ]
    assert compose_problems(None, inc, ["go2rtc"], "c.yaml") == [
        "c.yaml: not found on the host"
    ]


def test_package_reloads_flags_domains_that_need_a_restart() -> None:
    reloads, restart = package_reloads(
        "sensor:\n  - platform: rest\n  - platform: systemmonitor\nhomeassistant:\n  customize: {}\n"
        "input_boolean:\n  x: {}\n"
    )
    assert reloads == {"rest.reload", "input_boolean.reload"}
    assert restart == ["sensor platform 'systemmonitor'", "homeassistant"]


@pytest.mark.parametrize(
    ("snippet", "message"),
    [
        ("mode: bogus", "mode='bogus'"),
        ("mode: symlink\n  - {src: a, dest: x, mode: copy}", "listed twice"),
    ],
)
def test_load_manifest_rejects_bad_entries(snippet: str, message: str) -> None:
    text = (REAL / "manifest.yaml").read_text(encoding="utf-8").split("files:")[0]
    text += f"files:\n  - {{src: a, dest: x, {snippet.split(chr(10))[0]}}}\n"
    if "\n" in snippet:
        text += snippet.split("\n", 1)[1] + "\n"
    with pytest.raises(GateFailure, match=message):
        load_manifest(text)


def test_load_manifest_rejects_paths_that_escape() -> None:
    text = (REAL / "manifest.yaml").read_text(encoding="utf-8").split("files:")[0]
    with pytest.raises(GateFailure, match="relative"):
        load_manifest(text + "files:\n  - {src: a, dest: ../x, mode: copy}\n")


# ---- whole deploys against a local host -------------------------------------------------
@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    for var in GIT_ENV_LEAKS:
        monkeypatch.delenv(var, raising=False)
    w = build_world(tmp_path)
    for key, value in w.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ha_deploy, "POLL_SECONDS", 0)
    yield w
    w.server.shutdown()


def _deploy(w: World, *, dry_run: bool = False) -> int:
    return ha_deploy.deploy(
        Options(dry_run=dry_run, host="local", ref=w.sha, source=w.src), REPO_ROOT
    )


def _changes(out: str) -> list[str]:
    lines: list[str] = []
    for line in out.split("changes (", 1)[1].splitlines()[1:]:
        if not line.startswith("  - "):
            break
        lines.append(line[4:])
    return lines


def test_dry_run_of_the_cutover_converts_files_without_touching_them(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    before = (world.ha / "packages/3d_printers.yaml").read_bytes()
    assert _deploy(world, dry_run=True) == 0
    out = capsys.readouterr().out
    assert _changes(out) == [
        "packages/3d_printers.yaml: re-point (symlink)",
        "ui-3d-printers.yaml: re-point (symlink)",
        "scripts/cam.py: re-point (symlink)",
    ]
    assert "reload" not in out and "summary: 3 change(s)" in out
    assert not (world.ha / "packages/3d_printers.yaml").is_symlink()
    assert (world.ha / "packages/3d_printers.yaml").read_bytes() == before
    assert world.calls("curl") == [] and not [
        c for c in world.calls("docker") if " up " in c
    ]


def test_cutover_deploy_links_files_records_a_pr_and_then_reports_nothing_to_do(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _deploy(world) == 0
    out = capsys.readouterr().out
    link = world.ha / "packages/3d_printers.yaml"
    assert (
        link.is_symlink()
        and os.readlink(link)
        == "../3d-cli/integrations/homeassistant/packages/3d_printers.yaml"
    )
    assert (
        os.readlink(world.ha / "ui-3d-printers.yaml")
        == "3d-cli/integrations/homeassistant/dashboards/ui-3d-printers.yaml"
    )
    assert not (world.ha / "www/card/card.js").is_symlink()
    assert any(
        " exec fake-ha python -m homeassistant --script check_config" in c
        for c in world.calls("docker")
    )
    assert world.calls("curl") == []  # identical bytes: nothing to reload
    assert "check_config: Configuration valid" in out
    assert "HA repo PR: https://github.com/test/ha/pull/7" in out
    branch = f"3d-deploy/{world.sha[:12]}"
    pushed = git(world.origin, "rev-parse", f"refs/heads/{branch}")
    assert (
        git(world.origin, "cat-file", "-p", f"{pushed}:packages").count("120000") == 1
    )
    assert any(
        c.startswith(f"gh pr create -R test/ha --base main --head {branch}")
        for c in world.calls("gh")
    )
    assert git(world.ha, "rev-parse", "HEAD") == git(
        world.origin, "rev-parse", "main"
    )  # main untouched
    staged = git(world.ha, "diff", "--cached", "--name-only")
    assert "packages/3d_printers.yaml" in staged
    marker = (world.ha / ".3d-cli-deployed").read_text()
    assert world.sha in marker
    assert list((world.root / "backups").glob("*.tgz"))

    assert _deploy(world, dry_run=True) == 0
    out = capsys.readouterr().out
    assert "changes (0):" in out and "summary: 0 change(s)" in out
    assert "pointer not yet merged" not in out  # this deploy didn't move the submodule

    # The PR merges (merge commit keeps the deploy commit as an ancestor): main fast-forwards
    # without touching the working tree, after which there is nothing left to do.
    git(world.origin, "update-ref", "refs/heads/main", pushed)
    assert _deploy(world, dry_run=True) == 0
    assert (
        "fast-forward the HA checkout to origin/main (1 commit(s)"
        in capsys.readouterr().out
    )
    assert _deploy(world) == 0
    assert git(world.ha, "rev-parse", "HEAD") == pushed
    assert git(world.ha, "diff", "--cached", "--name-only") == ""
    assert _deploy(world, dry_run=True) == 0
    assert "changes (0):" in capsys.readouterr().out


def test_content_change_bumps_the_submodule_reloads_and_recreates(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _deploy(world) == 0
    old = world.sha
    world.commit_src(
        "packages/3d_printers.yaml", _package("v2"), "pkg v2"
    )
    world.commit_src("scripts/cam.py", "print('cam v2')\n", "cam v2")
    capsys.readouterr()
    assert _deploy(world, dry_run=True) == 0
    out = capsys.readouterr().out
    assert f"3d-cli submodule {old[:12]} -> {world.sha[:12]}" in out
    assert "reload: automation.reload, template.reload" in out
    assert "recreate (file changed): cam-proxy" in out
    assert _deploy(world) == 0
    assert git(world.ha / "3d-cli", "rev-parse", "HEAD") == world.sha
    assert "v2" in (world.ha / "packages/3d_printers.yaml").read_text()
    urls = [c.split()[-1] for c in world.calls("curl")]
    assert urls == [
        f"{world.server.url}/api/services/automation/reload",
        f"{world.server.url}/api/services/template/reload",
    ]
    assert any(
        "up -d --no-deps --force-recreate cam-proxy" in c for c in world.calls("docker")
    )
    assert (
        git(world.ha, "ls-files", "-s", "3d-cli").split()[1] == world.sha
    )  # staged pointer

    capsys.readouterr()
    assert _deploy(world, dry_run=True) == 0
    out = capsys.readouterr().out
    assert (
        "changes (0):" in out
        and f"pointer not yet merged: HA origin/main pins {old[:12]}" in out
    )


def _package(tag: str) -> str:
    return f"template:\n  - sensor:\n      - name: {tag}\n        state: '1'\nautomation: []\n"


def test_failed_check_config_rolls_back(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _deploy(world) == 0
    old = world.sha
    world.commit_src(
        "packages/3d_printers.yaml", _package("broken"), "broken"
    )
    monkeypatch.setenv("FAKE_CHECK_CONFIG_RC", "1")
    monkeypatch.setenv("FAKE_CHECK_CONFIG_OUT", "Invalid config for 'template'")
    with pytest.raises(GateFailure, match="check_config failed; rolled back") as err:
        _deploy(world)
    assert any("Invalid config for 'template'" in r for r in err.value.remediation)
    assert git(world.ha / "3d-cli", "rev-parse", "HEAD") == old
    assert "broken" not in (world.ha / "packages/3d_printers.yaml").read_text()
    assert world.calls("curl") == []
    assert world.sha not in (world.ha / ".3d-cli-deployed").read_text()


def test_post_check_regression_rolls_back(world: World) -> None:
    assert _deploy(world) == 0
    old = world.sha
    world.commit_src(
        "packages/3d_printers.yaml", _package("v3"), "v3"
    )
    gone = [{"entity_id": "sensor.printer_u1_status", "state": "unavailable"}]
    world.server.state_calls = 0
    world.server.state_sequence = [ENTITIES_OK, gone]
    with pytest.raises(GateFailure, match="post-checks failed; rolled back") as err:
        _deploy(world)
    assert any(
        "sensor.printer_u1_status: printing -> unavailable" in r
        for r in err.value.remediation
    )
    assert git(world.ha / "3d-cli", "rev-parse", "HEAD") == old
    # The rollback re-applies the old package: the reloads run once forward and once back.
    assert len([c for c in world.calls("curl") if c.endswith("template/reload")]) == 2


def test_blocking_problems_change_nothing(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    (world.ha / "packages/3d_printers.yaml").write_text(
        "automation: []\n# edited live\n"
    )
    (world.ha / "configuration.yaml").write_text("homeassistant: {}\n")
    (world.ha / "ha.docker-compose.yaml").write_text("services: {}\n")
    world.commit_src("stray.yaml", "a: 1\n", "stray")
    with pytest.raises(GateFailure, match="blocking problem"):
        _deploy(world)
    out = capsys.readouterr().out
    assert "packages/3d_printers.yaml: live file has uncommitted edits" in out
    assert "configuration.yaml: lovelace: missing" in out
    assert (
        "`include:` does not list 3d-cli/integrations/homeassistant/compose/3d.docker-compose.yaml"
        in out
    )
    assert f"{INTEGRATION}/stray.yaml is not in the manifest" in out
    assert not (world.ha / "ui-3d-printers.yaml").is_symlink()
    assert world.calls("curl") == [] and not list((world.root / "backups").glob("*"))


def test_a_change_that_needs_a_restart_is_refused(world: World) -> None:
    world.commit_src(
        "packages/3d_printers.yaml", "homeassistant:\n  customize: {}\n", "core"
    )
    with pytest.raises(GateFailure, match="needs a Home Assistant restart"):
        _deploy(world)
    assert not (world.ha / "packages/3d_printers.yaml").is_symlink()


def test_orphan_symlink_into_the_submodule_blocks(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    (world.ha / "old.yaml").symlink_to("3d-cli/integrations/homeassistant/vendor.lock")
    git(world.ha, "add", "old.yaml")
    git(world.ha, "commit", "-q", "-m", "orphan")
    with pytest.raises(GateFailure):
        _deploy(world, dry_run=True)
    assert (
        "old.yaml: tracked symlink into 3d-cli/ that the manifest doesn't list"
        in capsys.readouterr().out
    )
