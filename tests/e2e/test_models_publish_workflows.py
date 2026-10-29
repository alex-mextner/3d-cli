"""Readable e2e workflows for `3d models publish`: the real bin/3d on a throwaway project
directory — the dry run (the default), the refusals (no 3mf/scad, a remix of a source whose
licence forbids derivatives), `--yes` against a loopback Thingiverse and a fake agent-browser
on PATH. No real site is contacted and nothing is ever sent without `--yes`."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from marketplace_fixtures import THINGIVERSE_TOKEN, MarketplaceServer
from publish_fixtures import YAML, FakeThingiverse, make_project

from .workflow_helper import run_cli

SDFL_URL = "https://makerworld.com/en/models/2592818"
BLANK = {
    "THINGIVERSE_TOKEN": "",
    "THINGIVERSE_UPLOAD_TOKEN": "",
    "MAKERWORLD_TOKEN": "",
    "THREED_KEYCHAIN": "off",
}


def test_dry_run_is_the_default_and_prints_files_fields_and_steps_per_platform(
    tmp_path: Path,
) -> None:
    """A user checks a project before publishing: the same file set for every platform, the
    fields each site gets, the exact steps — and no request that could change anything."""
    project = make_project(tmp_path)
    (project / "wall_plug.step").write_bytes(b"ISO-10303")
    result = run_cli(
        tmp_path,
        "models",
        "publish",
        str(project),
        "--platform",
        "all",
        env_extra=BLANK,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert "DRY RUN, nothing is sent" in out and out.rstrip().endswith(
        "OK — rerun with --yes to publish."
    )
    for line in (
        "3mf   print/wall_plug.3mf",
        "scad  wall_plug.scad",
        "stl   wall_plug.stl",
        "cad   wall_plug.step",
        "images (1)",
        "previews/iso.png",
        "== makerworld [ready]",
        "== printables [ready]",
        "== thingiverse [ready]",
        "license: BY-NC",
        "license: CC BY-NC",
        "license: cc-nc",
        "agent-browser open https://www.printables.com/",
        "/files/0/uploadFile  (multipart: wall_plug.scad)",
    ):
        assert line in out, line
    assert "references" not in out and "gcode" not in out


def test_dry_run_sends_nothing_even_when_the_remix_source_is_looked_up(
    tmp_path: Path,
) -> None:
    """The source licence of a remix is read from the marketplace (GET only); a permissive one
    lets the project through and the dry run still sends no POST anywhere."""
    project = make_project(
        tmp_path, YAML + "remix_of: https://www.thingiverse.com/thing:5594736\n"
    )
    with MarketplaceServer() as server:
        result = run_cli(
            tmp_path,
            "models",
            "publish",
            str(project),
            "--platform",
            "thingiverse",
            "--dry-run",
            env_extra={**BLANK, **server.env(), "THINGIVERSE_TOKEN": THINGIVERSE_TOKEN},
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "marketplace lists 'Creative Commons - Attribution'" in result.stdout
    assert "=> allowed" in result.stdout
    assert "ancestors: 5594736" in result.stdout and "is_remix: True" in result.stdout
    assert server.requests and all(method == "GET" for method, _, _ in server.requests)


def test_a_remix_of_a_no_derivatives_licence_is_refused_until_permission_is_given(
    tmp_path: Path,
) -> None:
    """The wall-plug case: the original is under the Standard Digital File License. The plan is
    still shown, then the run exits 2 naming the licence; `permission_ref` lifts it."""
    declared = (
        YAML + f"remix_of: {SDFL_URL.replace('makerworld.com', 'example.invalid')}\n"
        "remix_license: Standard Digital File License\n"
    )
    project = make_project(tmp_path, declared)
    refused = run_cli(
        tmp_path,
        "models",
        "publish",
        str(project),
        "--platform",
        "all",
        "--dry-run",
        env_extra=BLANK,
    )
    assert refused.returncode == 2
    assert (
        "== makerworld [REFUSED]" in refused.stdout
        and "3mf   print/wall_plug.3mf" in refused.stdout
    )
    assert (
        "is under Standard Digital File License, which forbids derivative works"
        in refused.stdout
    )
    assert "publish refused: 1 problem(s); nothing was sent" in refused.stderr

    (project / "publish.yaml").write_text(
        declared + "permission_ref: mail from the author, 2026-11-01\n",
        encoding="utf-8",
    )
    allowed = run_cli(
        tmp_path,
        "models",
        "publish",
        str(project),
        "--platform",
        "all",
        env_extra=BLANK,
    )
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert (
        "lifted by permission_ref" in allowed.stdout and "=> allowed" in allowed.stdout
    )
    as_json = run_cli(
        tmp_path, "models", "publish", str(project), "--platform", "thingiverse", "--json", env_extra=BLANK
    )
    description = json.loads(as_json.stdout)["platforms"]["thingiverse"]["fields"]["description"]
    assert description.endswith(
        "Remix of https://example.invalid/en/models/2592818 (published with the original author's "
        "permission: mail from the author, 2026-11-01)"
    )


def test_an_unverifiable_source_licence_is_refused(tmp_path: Path) -> None:
    """A user remixes a model whose licence cannot be looked up: publishing is refused, not assumed free."""
    project = make_project(
        tmp_path, YAML + "remix_of: https://example.invalid/model/1\n"
    )
    result = run_cli(
        tmp_path,
        "models",
        "publish",
        str(project),
        "--platform",
        "printables",
        env_extra=BLANK,
    )
    assert result.returncode == 2
    assert (
        "cannot tell which licence https://example.invalid/model/1 is under"
        in result.stdout
    )


def test_a_project_without_3mf_or_scad_is_refused_before_any_platform_is_touched(
    tmp_path: Path,
) -> None:
    """A user publishes a project missing its .3mf and .scad: refused before any platform is touched."""
    project = make_project(tmp_path, mf3=False, scad=False)
    result = run_cli(
        tmp_path,
        "models",
        "publish",
        str(project),
        "--platform",
        "all",
        "--yes",
        env_extra={**BLANK, "THINGIVERSE_UPLOAD_TOKEN": "tok"},
    )
    assert result.returncode == 2
    assert "no .3mf file" in result.stdout and "no .scad file" in result.stdout
    assert "thingiverse: published" not in result.stdout


def test_json_output_feeds_a_script(tmp_path: Path) -> None:
    """A user pipes `--json` into a script and reads the plan: files, licence and platform fields."""
    project = make_project(tmp_path)
    result = run_cli(
        tmp_path,
        "models",
        "publish",
        str(project),
        "--platform",
        "thingiverse",
        "--json",
        env_extra=BLANK,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert (
        data["dry_run"] is True
        and data["refused"] is False
        and data["license"] == "cc-by-nc"
    )
    assert [f["role"] for f in data["files"]] == ["3mf", "scad", "stl"]
    assert data["platforms"]["thingiverse"]["fields"]["license"] == "cc-nc"


def test_bad_invocations_are_structured_errors(tmp_path: Path) -> None:
    """A user forgets or mistypes the platform, contradicts flags or names a missing project and gets structured errors."""
    project = make_project(tmp_path)
    missing = run_cli(tmp_path, "models", "publish", str(project), env_extra=BLANK)
    assert missing.returncode == 2
    assert "accepted: makerworld, printables, thingiverse, all" in missing.stderr
    wrong = run_cli(
        tmp_path,
        "models",
        "publish",
        str(project),
        "--platform",
        "cults",
        env_extra=BLANK,
    )
    assert wrong.returncode == 2
    assert "accepted: makerworld, printables, thingiverse, all" in wrong.stderr
    both = run_cli(
        tmp_path,
        "models",
        "publish",
        str(project),
        "--platform",
        "all",
        "--yes",
        "--dry-run",
        env_extra=BLANK,
    )
    assert both.returncode == 2
    assert "contradict" in both.stderr
    nodir = run_cli(
        tmp_path,
        "models",
        "publish",
        str(tmp_path / "nope"),
        "--platform",
        "all",
        env_extra=BLANK,
    )
    assert nodir.returncode == 2
    nospec = tmp_path / "bare"
    nospec.mkdir()
    spec = run_cli(
        tmp_path, "models", "publish", str(nospec), "--platform", "all", env_extra=BLANK
    )
    assert spec.returncode == 2
    assert "publish.yaml" in spec.stderr


def test_yes_publishes_to_thingiverse_through_its_api(tmp_path: Path) -> None:
    """`--yes` runs the plan: create, upload every file and image, finalize, publish — with the
    token taken from the environment (the keychain in daily use)."""
    project = make_project(tmp_path)
    fake = FakeThingiverse()
    try:
        result = run_cli(
            tmp_path,
            "models",
            "publish",
            str(project),
            "--platform",
            "thingiverse",
            "--yes",
            env_extra={
                **BLANK,
                "THREED_THINGIVERSE_API": fake.base,
                "THINGIVERSE_UPLOAD_TOKEN": "tok-123",
            },
        )
    finally:
        fake.close()
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "thingiverse: published https://www.thingiverse.com/thing:777" in result.stdout
    )
    assert [c[1] for c in fake.calls][0] == "/things" and fake.calls[-1][
        1
    ] == "/things/777/publish"
    assert all(c[2]["Authorization"] == "Bearer tok-123" for c in fake.calls)
    assert "tok-123" not in result.stdout + result.stderr


def test_yes_json_keeps_stdout_a_single_json_document(tmp_path: Path) -> None:
    """A script runs `--yes --json`: stdout parses as the plan, the publish line goes to stderr."""
    project = make_project(tmp_path)
    fake = FakeThingiverse()
    try:
        result = run_cli(
            tmp_path,
            "models",
            "publish",
            str(project),
            "--platform",
            "thingiverse",
            "--yes",
            "--json",
            env_extra={
                **BLANK,
                "THREED_THINGIVERSE_API": fake.base,
                "THINGIVERSE_UPLOAD_TOKEN": "tok-123",
            },
        )
    finally:
        fake.close()
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["dry_run"] is False
    assert "thingiverse: published" in result.stderr


def test_yes_without_the_upload_token_publishes_nothing(tmp_path: Path) -> None:
    """A user passes `--yes` without the Thingiverse upload token: nothing is published."""
    project = make_project(tmp_path)
    fake = FakeThingiverse()
    try:
        result = run_cli(
            tmp_path,
            "models",
            "publish",
            str(project),
            "--platform",
            "all",
            "--yes",
            env_extra={**BLANK, "THREED_THINGIVERSE_API": fake.base},
        )
    finally:
        fake.close()
    assert result.returncode == 2
    assert (
        "OAuth access token with write scope" in result.stderr
        and "auth set thingiverse-upload" in result.stderr
    )
    assert fake.calls == []


def test_yes_on_a_browser_platform_fills_the_draft_and_leaves_the_last_click_to_the_owner(
    tmp_path: Path,
) -> None:
    """A fake `agent-browser` on PATH records the steps: the form is filled, no Publish click."""
    project = make_project(tmp_path)
    log = tmp_path / "browser.log"
    fakebin = tmp_path / "browser-bin"
    fakebin.mkdir()
    script = fakebin / "agent-browser"
    script.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "print('https://makerworld.com/en' if 'url' in sys.argv else 'shot.png')\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    path = f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}"
    result = run_cli(
        tmp_path,
        "models",
        "publish",
        str(project),
        "--platform",
        "makerworld",
        "--yes",
        env_extra={**BLANK, "PATH": path},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "makerworld: the makerworld draft is filled in browser session 3d-publish-makerworld-"
        in result.stdout
    )
    steps = log.read_text(encoding="utf-8")
    assert (
        "--headed --restore open https://makerworld.com/en" in steps
        and "--session 3d-publish-makerworld-" in steps
    )
    assert (
        "upload input[type=file]" in steps
        and "find label Title fill Wall plug" in steps
    )
    assert "Publish" not in steps
