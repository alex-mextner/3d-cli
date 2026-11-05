"""`3d init --env` (printops.env_setup): the machine stage that sets up Spoolman + 3d-api.

Behaviour tests with injected fakes: a local HTTP stand-in for the service, a recording command
runner instead of ssh/docker. No test can reach a real host, docker or printer.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from errors import InvalidArgument, MissingDependency, ThreeDError, UsageError
from printops_env_fakes import GOOD_TOKEN, FakeService

from printops import deploy, env_setup
from printops.client import config_path, load_config

REPO = Path(__file__).resolve().parents[1]

FLEET = """\
u1:
  model: Snapmaker U1
  owner: own
  access: full
  adapter: moonraker
  host: 10.9.9.1
  port: 7125
  heads: [{label: T1, index: 0}]
k1se:
  model: Creality K1 SE
  owner: own
  access: full
  adapter: moonraker
  host: 10.9.9.2
  port: 7125
  heads: [{label: T1, index: 0}]
voron:
  model: Voron 2.4
  owner: own
  access: full
  adapter: moonraker
  host: 10.9.9.3
  port: 7125
  heads: [{label: T1, index: 0}]
p1s:
  model: Bambu Lab P1S
  owner: neighbour
  access: offline
  heads: [{label: T1, index: 0}]
"""

PROBE_OK = (
    "home=/home/me\narch=aarch64\ncores=4\nload=0.30\nmem_mb=8000\n"
    "docker=Docker version 27.1.1, build abc\ncompose=2.29.1\ndocker_ok=yes\nrsync=/usr/bin/rsync\n"
    "os=Debian GNU/Linux 12\n"
)
PROBE_BUSY = PROBE_OK.replace("load=0.30", "load=7.50")
PROBE_NO_DOCKER = PROBE_OK.replace("docker=Docker version 27.1.1, build abc", "docker=")
PROBE_NO_DAEMON_ACCESS = PROBE_OK.replace("docker_ok=yes", "docker_ok=")
PROBE_NO_RSYNC = PROBE_OK.replace("rsync=/usr/bin/rsync", "rsync=")


@pytest.fixture(autouse=True)
def cfg_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Own config + data dirs, no inherited PrintOps env, the fleet file of this test module."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.delenv("PRINTOPS_API", raising=False)
    monkeypatch.delenv("PRINTOPS_TOKEN", raising=False)
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(FLEET, encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(fleet))
    return tmp_path


@pytest.fixture(autouse=True)
def stage_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The backup + Home Assistant stage (printops.env_stage) has its own tests (test_printops_ha_stage.py) and
    probes the network (mDNS, homeassistant.local), so these install-flow tests replace it with a recorder."""
    calls: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(env_setup.env_stage_install, "stage_after_install", fake)
    return calls


def test_stage_flags_reach_the_backup_stage_options() -> None:
    o = env_setup.parse_options(
        [
            "--spoolman",
            "local",
            "--backup",
            "path:/mnt/b",
            "--ha",
            "skip",
            "--keep",
            "daily=3",
        ]
    )
    assert (
        o.stage.backup == "path:/mnt/b"
        and o.stage.ha == "skip"
        and o.stage.keep == "daily=3"
    )
    with pytest.raises(InvalidArgument):
        env_setup.parse_options(["--backup", "bogus"])


@pytest.fixture
def service() -> Iterator[FakeService]:
    svc = FakeService()
    yield svc
    svc.close()


class Recorder:
    """Stands in for subprocess.run: records argv, answers probes/token mints, never executes."""

    def __init__(
        self, probe_out: str = PROBE_OK, ssh_hostnames: dict[str, str] | None = None
    ) -> None:
        self.calls: list[list[str]] = []
        self.ssh_queries: list[
            list[str]
        ] = []  # `ssh -G HOST` lookups, kept out of `calls`
        self.probe_out = probe_out
        self.ssh_hostnames = (
            ssh_hostnames or {}
        )  # ssh target -> HostName of the ssh config

    def __call__(self, argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["ssh", "-G"]:
            self.ssh_queries.append(list(argv))
            name = self.ssh_hostnames.get(argv[2], argv[2].rsplit("@", 1)[-1])
            return subprocess.CompletedProcess(
                argv, 0, f"user me\nhostname {name}\nport 22\n", ""
            )
        self.calls.append(list(argv))
        script = argv[-1]
        if "token create" in script:
            return subprocess.CompletedProcess(
                argv, 0, f"{GOOD_TOKEN}\n", "stored as sha256\n"
            )
        if script.startswith("printf"):  # the probe
            return subprocess.CompletedProcess(argv, 0, self.probe_out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")


class Prompts:
    """Scripted answers for `ask`; a prompt past the script fails the test."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.asked.append(prompt)
        assert self.answers, f"unexpected prompt: {prompt!r}"
        return self.answers.pop(0)


def make_env(
    root: Path,
    *,
    tty: bool = False,
    ask: Any = None,
    runner: Any = None,
    deploy_runner: Any = None,
) -> tuple[env_setup.Env, list[str], Recorder, Recorder]:
    out: list[str] = []
    rec = runner or Recorder()
    drec = deploy_runner or Recorder()
    env = env_setup.Env(
        ask=ask or Prompts(),
        say=out.append,
        runner=rec,
        deploy_runner=drec,
        is_tty=tty,
        repo_root=REPO,
    )
    return env, out, rec, drec


def write_cfg(api: str, token: str | None = GOOD_TOKEN) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"api: {api}\n" + (f"token: {token}\n" if token else ""), encoding="utf-8"
    )
    path.chmod(0o600)
    return path


# ------------------------------------------------------------------ options


def test_target_grammar() -> None:
    assert env_setup.parse_target("printer:u1") == env_setup.Target("printer", "u1")
    assert env_setup.parse_target("ssh:me@nas") == env_setup.Target("ssh", "me@nas")
    for bad in ("printer:", "ssh:", "docker", "ssh:-oProxyCommand=x", "ssh:a b"):
        with pytest.raises(InvalidArgument):
            env_setup.parse_target(bad)


def test_option_conflicts_are_usage_errors() -> None:
    for argv in (
        ["--api", "http://x", "--spoolman", "local"],
        ["--token", "po_x"],
        ["--script-out", "x.sh"],
        ["--spoolman", "nonsense"],
    ):
        with pytest.raises((UsageError, InvalidArgument)):
            env_setup.parse_options(argv)
    assert env_setup.parse_options(["-y"]).no_input


# ------------------------------------------------------------------ already configured / no-input


def test_already_configured_changes_nothing_and_asks_nothing(
    cfg_home: Path, service: FakeService
) -> None:
    cfg = write_cfg(service.url)
    before = (cfg.read_bytes(), cfg.stat().st_mtime_ns)
    env, out, rec, drec = make_env(cfg_home, tty=True)  # a TTY, but --no-input wins
    assert env_setup.run(["--no-input", "--spoolman", "auto"], env) == 0
    assert any("already configured" in line for line in out)
    assert (cfg.read_bytes(), cfg.stat().st_mtime_ns) == before
    assert not rec.calls and not drec.calls
    assert all(m == "GET" for m, _ in service.requests) and service.requests


def test_already_configured_wins_over_an_install_target(
    cfg_home: Path, service: FakeService
) -> None:
    write_cfg(service.url)
    env, out, rec, drec = make_env(cfg_home)
    assert env_setup.run(["--no-input", "--spoolman", "ssh:me@nas.lan"], env) == 0
    assert any("already configured" in line for line in out)
    assert not rec.calls and not drec.calls  # nothing probed, nothing installed


def test_no_input_auto_unconfigured_reports_commands_and_installs_nothing(
    cfg_home: Path,
) -> None:
    env, out, rec, drec = make_env(cfg_home, tty=True)
    assert env_setup.run(["--no-input"], env) == 0
    text = "\n".join(out)
    assert "installs nothing" in text
    assert (
        "--spoolman local" in text
        and "--spoolman script" in text
        and "--spoolman ssh:USER@HOST" in text
    )
    assert "--spoolman printer:voron" in text and "--spoolman printer:u1" in text
    assert "--allow-discouraged" in text  # the U1 line says so
    assert "printer:k1se" not in text  # cannot host it: never suggested as a command
    assert not rec.calls and not drec.calls and not config_path().exists()


def test_non_tty_never_prompts_even_without_no_input(cfg_home: Path) -> None:
    env, out, rec, _ = make_env(
        cfg_home, tty=False
    )  # Prompts() fails the test on any ask
    assert env_setup.run([], env) == 0
    assert not rec.calls and not config_path().exists()


def test_configured_but_unreachable_is_a_failure_with_the_fix(cfg_home: Path) -> None:
    dead = FakeService()
    url = dead.url
    dead.close()
    write_cfg(url)
    env, _, rec, _ = make_env(cfg_home)
    with pytest.raises(ThreeDError, match="unreachable"):
        env_setup.run(["--no-input", "--spoolman", "auto"], env)
    assert not rec.calls


def test_skip_is_exit_zero_and_touches_nothing(cfg_home: Path) -> None:
    env, out, rec, drec = make_env(cfg_home)
    assert env_setup.run(["--no-input", "--spoolman", "skip"], env) == 0
    assert any("skipped" in line for line in out) and not config_path().exists()
    assert not rec.calls and not drec.calls


# ------------------------------------------------------------------ connect (--api): write only after health


def test_api_connect_writes_config_0600_only_after_the_checks(
    cfg_home: Path, service: FakeService
) -> None:
    env, out, _, _ = make_env(cfg_home)
    assert (
        env_setup.run(
            ["--no-input", "--api", service.url + "/", "--token", GOOD_TOKEN], env
        )
        == 0
    )
    cfg = config_path()
    assert (cfg.stat().st_mode & 0o777) == 0o600
    assert f"api: {service.url}" in cfg.read_text() and GOOD_TOKEN in cfg.read_text()
    assert ("GET", "/v1/health") in service.requests and any(
        p.startswith("/v1/events") for _, p in service.requests
    )
    again, out2, _, _ = make_env(cfg_home)
    mtime = cfg.stat().st_mtime_ns
    assert (
        env_setup.run(
            ["--no-input", "--api", service.url, "--token", GOOD_TOKEN], again
        )
        == 0
    )
    assert (
        any("already configured" in line for line in out2)
        and cfg.stat().st_mtime_ns == mtime
    )


def test_config_is_never_written_for_an_unhealthy_target(
    cfg_home: Path, service: FakeService
) -> None:
    dead = FakeService()
    dead_url = dead.url
    dead.close()
    for url, token in ((dead_url, GOOD_TOKEN), (service.url, "po_wrong")):
        env, _, _, _ = make_env(cfg_home)
        with pytest.raises(ThreeDError, match="Nothing was written|not healthy"):
            env_setup.run(["--no-input", "--api", url, "--token", token], env)
        assert not config_path().exists()
    service.status = "degraded"
    env, _, _, _ = make_env(cfg_home)
    with pytest.raises(ThreeDError, match="degraded"):
        env_setup.run(["--no-input", "--api", service.url, "--token", GOOD_TOKEN], env)
    assert not config_path().exists()


def test_a_different_old_config_is_kept_as_bak(
    cfg_home: Path, service: FakeService
) -> None:
    old = write_cfg("http://10.0.0.99:8765", "po_old")
    env, out, _, _ = make_env(cfg_home)
    assert (
        env_setup.run(["--no-input", "--api", service.url, "--token", GOOD_TOKEN], env)
        == 0
    )
    bak = old.with_name("printops.yaml.bak")
    assert "10.0.0.99" in bak.read_text() and (bak.stat().st_mode & 0o777) == 0o600
    assert service.url in old.read_text()


def test_moving_to_another_api_drops_the_old_token(
    cfg_home: Path, service: FakeService
) -> None:
    old = write_cfg("http://10.0.0.99:8765", "po_old")
    env, _, _, _ = make_env(cfg_home)
    assert env_setup.run(["--no-input", "--api", service.url], env) == 0  # no --token
    assert "po_old" not in old.read_text()  # never sent to the new host
    assert load_config() == (service.url, None)
    assert (
        "po_old" in old.with_name("printops.yaml.bak").read_text()
    )  # still recoverable


def test_the_same_api_without_a_token_keeps_the_token(
    cfg_home: Path, service: FakeService
) -> None:
    old = write_cfg(service.url, GOOD_TOKEN)
    env_setup.write_config(
        service.url + "/", None, lambda _m: None
    )  # same endpoint, spelled with a slash
    assert (
        load_config() == (service.url + "/", GOOD_TOKEN)
        and GOOD_TOKEN in old.read_text()
    )


def test_config_and_bak_are_private_from_the_moment_they_exist(
    cfg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = write_cfg("http://10.0.0.99:8765", "po_old")
    seen: list[tuple[str, int]] = []
    real_replace = os.replace

    def replace(src: str | Path, dst: str | Path) -> None:
        seen.append(
            (Path(dst).name, os.stat(src).st_mode & 0o777)
        )  # before it has its final name
        real_replace(src, dst)

    monkeypatch.setattr(env_setup.os, "replace", replace)
    previous = os.umask(0o022)
    try:
        env_setup.write_config("http://10.0.0.5:8765", GOOD_TOKEN, lambda _m: None)
    finally:
        os.umask(previous)
    assert sorted(seen) == [("printops.yaml", 0o600), ("printops.yaml.bak", 0o600)]
    assert (old.with_name("printops.yaml.bak").stat().st_mode & 0o777) == 0o600


def test_a_bare_spoolman_is_reported_but_never_counted_as_the_3d_api(
    cfg_home: Path,
) -> None:
    svc = FakeService(api=False)
    try:
        h = env_setup.check_health(svc.url, None)
        assert (h.state, h.kind) == ("spoolman-only", "spoolman")
        assert (
            "bare Spoolman" in h.detail and "http://127.0.0.1:8765" in h.hint
        )  # the 3d-api: same host, port 8765
        dead = FakeService(api=False, spoolman=False)
        dead_url = dead.url
        dead.close()
        assert env_setup.check_health(dead_url, None).state == "unreachable"
    finally:
        svc.close()


def test_api_pointing_at_a_bare_spoolman_fails_and_names_the_3d_api_url(
    cfg_home: Path,
) -> None:
    """The URL the script prints for Spoolman (:7912) pasted as --api: no config, no unverified token."""
    svc = FakeService(api=False)
    try:
        for argv in (["--api", svc.url, "--token", GOOD_TOKEN], ["--api", svc.url]):
            env, _, _, _ = make_env(cfg_home)
            with pytest.raises(ThreeDError, match="bare Spoolman") as err:
                env_setup.run(["--no-input", *argv], env)
            assert err.value.exit_code == 1
            assert "http://127.0.0.1:8765" in err.value.render()
            assert not config_path().exists()
        old = write_cfg("http://10.0.0.99:8765", "po_old")
        before = old.read_bytes()
        with pytest.raises(ThreeDError, match="bare Spoolman"):
            env_setup.run(
                ["--no-input", "--api", svc.url, "--token", GOOD_TOKEN],
                make_env(cfg_home)[0],
            )
        assert (
            old.read_bytes() == before
            and not old.with_name("printops.yaml.bak").exists()
        )
    finally:
        svc.close()


def test_a_configured_bare_spoolman_is_not_already_configured(
    cfg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = FakeService(api=False)
    try:
        write_cfg(svc.url, None)
        env, out, rec, _ = make_env(cfg_home)
        with pytest.raises(
            ThreeDError, match="spoolman-only"
        ) as err:  # status-only run: a failure with the fix
            env_setup.run(["--no-input", "--spoolman", "auto"], env)
        assert "http://127.0.0.1:8765" in err.value.render() and not rec.calls
        assert not any("already configured" in line for line in out)

        real = (
            env_setup.check_health
        )  # the configured Spoolman is judged for real, the new install is faked ok
        monkeypatch.setattr(
            env_setup,
            "check_health",
            lambda api, token: (
                real(api, token)
                if api == svc.url
                else env_setup.Health("ok", "3d-api", "fake")
            ),
        )
        monkeypatch.setattr(deploy, "wait_healthy", lambda url, **_kw: {"status": "ok"})
        env2, out2, rec2, _ = make_env(cfg_home)
        assert (
            env_setup.run(["--no-input", "--spoolman", "ssh:me@nas.lan"], env2) == 0
        )  # an install target is not skipped
        assert rec2.calls and any("spoolman-only" in line for line in out2)
        assert "api: http://nas.lan:8765" in config_path().read_text()
        assert svc.url in config_path().with_name("printops.yaml.bak").read_text()
    finally:
        svc.close()


# ------------------------------------------------------------------ the file, not the environment


def test_status_reads_the_config_file_not_an_exported_dead_url(
    cfg_home: Path, service: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_cfg(service.url)
    monkeypatch.setenv("PRINTOPS_API", "http://127.0.0.1:9")  # exported and dead
    env, out, _, _ = make_env(cfg_home)
    assert env_setup.run(["--no-input", "--spoolman", "auto"], env) == 0
    assert any(f"already configured: {service.url}" in line for line in out)
    assert any(
        "PRINTOPS_API is set in the environment and overrides" in line for line in out
    )


def test_a_healthy_exported_api_does_not_make_an_unwritten_file_configured(
    cfg_home: Path, service: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dotfiles installer exports PRINTOPS_API and runs `3d init --env --no-input`: cron and
    systemd units without that variable still have no config, so nothing is "already configured"."""
    monkeypatch.setenv("PRINTOPS_API", service.url)
    monkeypatch.setenv("PRINTOPS_TOKEN", GOOD_TOKEN)
    env, out, _, _ = make_env(cfg_home)
    assert env_setup.run(["--no-input"], env) == 0
    assert not any("already configured" in line for line in out)
    assert any("not configured" in line for line in out) and not config_path().exists()
    assert (
        sum("is set in the environment" in line for line in out) == 2
    )  # PRINTOPS_API and PRINTOPS_TOKEN
    assert GOOD_TOKEN not in "\n".join(
        out
    )  # the warning names the variable, never its value


def test_api_is_compared_with_the_file_not_with_the_environment(
    cfg_home: Path, service: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """env -> A with a token, file -> B, `--api A`: the file must end up at A, not stay at B as
    "already configured"; the exported token is not the file's, so it is not written unverified."""
    write_cfg("http://10.0.0.99:8765", "po_old")
    monkeypatch.setenv("PRINTOPS_API", service.url)
    monkeypatch.setenv("PRINTOPS_TOKEN", GOOD_TOKEN)
    env, out, _, _ = make_env(cfg_home)
    assert env_setup.run(["--no-input", "--api", service.url], env) == 0
    assert not any("already configured" in line for line in out)
    assert load_config(env={}) == (service.url, None)
    assert "10.0.0.99" in config_path().with_name("printops.yaml.bak").read_text()


# ------------------------------------------------------------------ failures are structured errors, not tracebacks


def test_an_html_catch_all_answering_200_is_unreachable_not_a_traceback(
    cfg_home: Path,
) -> None:
    """An nginx `try_files … /index.html` or a Mainsail port answers every path with 200 HTML."""
    from printops.client import ApiClient, ApiError

    web = FakeService(html=True)
    try:
        h = env_setup.check_health(web.url, GOOD_TOKEN)
        assert h.state == "unreachable" and "not JSON" in h.detail
        with pytest.raises(ApiError, match="not JSON") as err:
            ApiClient(web.url).get("/anything")
        assert err.value.status == 200
        env, _, _, _ = make_env(cfg_home)
        with pytest.raises(ThreeDError, match="not healthy") as exc:
            env_setup.run(["--no-input", "--api", web.url], env)
        assert exc.value.exit_code == 1 and not config_path().exists()
    finally:
        web.close()


class Missing:
    """A deploy runner on a machine without the program (subprocess.run raises FileNotFoundError)."""

    def __call__(self, argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "No such file or directory", argv[0])


def test_a_missing_local_rsync_is_a_missing_dependency_even_in_a_dry_run(
    cfg_home: Path,
) -> None:
    env, _, _, _ = make_env(cfg_home, deploy_runner=Missing())
    with pytest.raises(
        MissingDependency, match="rsync"
    ) as err:  # the dry run really executes rsync
        env_setup.run(["--no-input", "--spoolman", "ssh:me@nas.lan", "--dry-run"], env)
    assert err.value.exit_code == 127 and "apt install rsync" in err.value.render()
    with pytest.raises(deploy.ToolMissing) as plain:
        deploy.run_steps(
            deploy.plan(
                deploy.DeployConfig(repo_root=REPO, host="me@nas.lan"), dry_run=True
            ),
            cwd=REPO,
            dry_run=True,
            log=lambda _m: None,
            runner=Missing(),
        )
    assert plain.value.tool == "rsync"


class MintFails(Recorder):
    def __init__(self, exc: BaseException) -> None:
        super().__init__()
        self.exc = exc

    def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        if "token create" in argv[-1]:
            raise self.exc
        return super().__call__(argv, **kw)


def test_a_hung_or_unstartable_token_mint_is_a_structured_error_with_the_hand_command(
    cfg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_health(monkeypatch, [])
    for exc in (
        subprocess.TimeoutExpired("ssh", 120),
        FileNotFoundError(2, "No such file", "ssh"),
    ):
        env, _, _, _ = make_env(cfg_home, runner=MintFails(exc))
        with pytest.raises(ThreeDError, match="could not mint") as err:
            env_setup.run(["--no-input", "--spoolman", "ssh:me@nas.lan"], env)
        # the hand-run command names the host: without `ssh HOST` it would run on the wrong machine
        assert (
            "ssh me@nas.lan " in err.value.render()
            and "docker exec printops-api" in err.value.render()
        )
        assert not config_path().exists()


def test_a_corrupt_config_file_is_a_usage_error_everywhere(
    cfg_home: Path, service: FakeService
) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("api: [unclosed\n", encoding="utf-8")
    with pytest.raises(UsageError, match="not valid YAML"):
        env_setup.write_config(service.url, GOOD_TOKEN, lambda _m: None)
    env, _, _, _ = make_env(cfg_home)
    with pytest.raises(UsageError, match="not valid YAML"):
        env_setup.run(["--no-input", "--api", service.url, "--token", GOOD_TOKEN], env)
    assert (
        path.read_text(encoding="utf-8") == "api: [unclosed\n"
    )  # kept as it was, and no .bak of garbage
    assert not path.with_name("printops.yaml.bak").exists()


def test_end_of_input_at_a_prompt_is_a_usage_error(
    cfg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    def eof(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr(builtins, "input", eof)
    out: list[str] = []
    env = env_setup.Env(
        say=out.append, is_tty=True, repo_root=REPO
    )  # the real `ask`, a closed stdin
    with pytest.raises(UsageError, match="stdin was closed") as err:
        env_setup.run([], env)
    assert err.value.exit_code == 2 and "--no-input" in err.value.render()


def test_an_api_without_a_scheme_or_host_is_refused_before_any_request(
    cfg_home: Path, service: FakeService
) -> None:
    for bad in (
        "192.168.0.5",
        "nas.lan:8765",
        "file:///etc/passwd",
        "ftp://nas.lan",
        "http://",
        "http://nas.lan:99999",
    ):
        with pytest.raises(InvalidArgument, match="--api") as err:
            env_setup.run(["--no-input", "--api", bad], make_env(cfg_home)[0])
        assert err.value.exit_code == 2 and "http://nas.lan:8765" in err.value.render()
    assert not config_path().exists() and service.requests == []


def test_a_configured_api_without_a_scheme_is_reported_not_raised(
    cfg_home: Path,
) -> None:
    write_cfg("nas.lan:8765")
    env, _, rec, _ = make_env(cfg_home)
    with pytest.raises(ThreeDError, match="must be an http") as err:
        env_setup.run(["--no-input"], env)
    assert (
        err.value.exit_code == 1 and "--api URL" in err.value.render() and not rec.calls
    )


def test_help_and_docs_show_a_concrete_example_for_every_option_and_target() -> None:
    """AGENTS.md help/docs sync: each flag and each --spoolman target has an example line in the
    `3d init --env` help and in docs/commands/init.md."""
    docs = (REPO / "docs" / "commands" / "init.md").read_text(encoding="utf-8")
    needles = (
        "--spoolman skip",
        "--spoolman local",
        "--spoolman printer:voron",
        "--spoolman ssh:me@nas.lan",
        "--spoolman script",
        "--ssh-user pi",
        "--allow-discouraged",
        "--dry-run",
        "--bind 192.168.0.",
        "--api http",
    )
    for name, text in (("help", env_setup.USAGE), ("init.md", docs)):
        for needle in needles:
            assert any(
                needle in line and line.lstrip().startswith("3d init --env")
                for line in text.splitlines()
            ), (name, needle)
    assert (
        "~/.config/3d-cli/printops.yaml" in env_setup.USAGE
    )  # not resolved from XDG at import time


# ------------------------------------------------------------------ candidates and the menu


def test_candidates_are_the_registry_printers_we_may_use() -> None:
    got = {c.name: c for c in env_setup.candidates()}
    assert set(got) == {
        "u1",
        "k1se",
        "voron",
    }  # the offline neighbour printer is never a host
    assert got["voron"].level == "ok"
    assert got["u1"].level == "discouraged" and "1 GB RAM" in got["u1"].reason
    assert got["k1se"].level == "unsupported" and "MIPS" in got["k1se"].reason
    assert got["u1"].host == "10.9.9.1"


def test_menu_lists_registry_printers_and_preselects_nothing(cfg_home: Path) -> None:
    # junk re-asks; printer menu (k1se=1, u1=2, voron=3): the K1 SE is refused and the menu restarts
    ask = Prompts("", "7", "1", "1", "1", "3", "me")
    env, out, _, _ = make_env(cfg_home, tty=True, ask=ask)
    opts = env_setup.Options()
    target = env_setup.menu(env_setup.candidates(), env, opts)
    text = "\n".join(out)
    assert target == env_setup.Target("printer", "voron") and opts.ssh_user == "me"
    for needle in (
        "1) a printer of the registry",
        "2) this computer",
        "3) a server over ssh",
        "4) generate a standalone",
        "0) skip",
        "u1  Snapmaker U1  10.9.9.1  [possible but NOT RECOMMENDED]",
        "k1se  Creality K1 SE  10.9.9.2  [not possible]",
        "voron  Voron 2.4  10.9.9.3",
    ):
        assert needle in text
    assert "Enter 1, 2, 3, 4 or 0." in text  # junk was rejected, not defaulted
    assert "cannot host it" in text  # picking the K1 SE was refused


def test_menu_u1_needs_an_explicit_yes(cfg_home: Path) -> None:
    env, _, _, _ = make_env(cfg_home, tty=True, ask=Prompts("1", "2", "n", "0"))
    assert env_setup.menu(
        env_setup.candidates(), env, env_setup.Options()
    ) == env_setup.Target("skip")
    opts = env_setup.Options()
    env2, _, _, _ = make_env(cfg_home, tty=True, ask=Prompts("1", "2", "y", ""))
    assert env_setup.menu(env_setup.candidates(), env2, opts) == env_setup.Target(
        "printer", "u1"
    )
    assert opts.allow_discouraged


def test_host_load_is_shown_only_for_the_picked_host(cfg_home: Path) -> None:
    env, out, rec, _ = make_env(cfg_home, tty=True, ask=Prompts("2"))
    env_setup.menu(env_setup.candidates(), env, env_setup.Options())
    assert not rec.calls  # building the menu contacted nothing
    assert not any("load" in line for line in out)


# ------------------------------------------------------------------ printer / ssh / local install


def test_printer_target_guards(cfg_home: Path) -> None:
    env, _, rec, drec = make_env(cfg_home)
    with pytest.raises(ThreeDError, match="cannot host Spoolman"):
        env_setup.run(["--no-input", "--spoolman", "printer:k1se"], env)
    with pytest.raises(ThreeDError, match="not recommended"):
        env_setup.run(["--no-input", "--spoolman", "printer:u1"], env)
    with pytest.raises(InvalidArgument, match="nosuch"):
        env_setup.run(["--no-input", "--spoolman", "printer:nosuch"], env)
    assert not rec.calls and not drec.calls  # every refusal came before any contact


def test_an_option_like_printer_host_or_ssh_user_never_reaches_ssh(
    cfg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cloned project's ./printer-devices.yaml can name a host `-oProxyCommand=…`; `--ssh-user` and
    the menu prompt are the same hole: ssh reads such an argument as an option and runs it locally."""
    evil = env_setup.Candidate("evil", "Voron 2.4", "-oProxyCommand=touch-pwned", "ok")
    registry = env_setup.candidates()
    monkeypatch.setattr(env_setup, "candidates", lambda: [evil, *registry])
    env, _, rec, drec = make_env(cfg_home)
    for argv in (["printer:evil"], ["printer:evil", "--ssh-user", "pi"]):
        with pytest.raises(InvalidArgument, match="host of printer evil"):
            env_setup.run(["--no-input", "--spoolman", *argv], env)
    for user in ("-oProxyCommand=x", "a b"):
        with pytest.raises(InvalidArgument, match="--ssh-user"):
            env_setup.run(
                ["--no-input", "--spoolman", "printer:voron", "--ssh-user", user], env
            )
    assert not rec.calls and not drec.calls  # not even the read-only probe was started


def test_the_menu_asks_again_for_an_option_like_ssh_target_or_user(
    cfg_home: Path,
) -> None:
    env, out, _, _ = make_env(
        cfg_home,
        tty=True,
        ask=Prompts("3", "-oProxyCommand=x", "3", "a b", "3", "me@nas"),
    )
    assert env_setup.menu(
        env_setup.candidates(), env, env_setup.Options()
    ) == env_setup.Target("ssh", "me@nas")
    assert (
        sum("starts with '-'" in line or "contains whitespace" in line for line in out)
        == 2
    )
    opts = env_setup.Options()
    env2, out2, _, _ = make_env(
        cfg_home, tty=True, ask=Prompts("1", "3", "-oX", "1", "3", "pi")
    )  # voron is #3
    assert env_setup.menu(env_setup.candidates(), env2, opts) == env_setup.Target(
        "printer", "voron"
    )
    assert opts.ssh_user == "pi" and any("starts with '-'" in line for line in out2)


def _patch_health(monkeypatch: pytest.MonkeyPatch, log: list[str]) -> None:
    def wait_healthy(url: str, **_kw: Any) -> dict[str, Any]:
        log.append(f"wait {url}")
        assert not config_path().exists()
        return {"status": "ok"}

    def check_health(api: str, token: str | None) -> env_setup.Health:
        log.append(f"check {api} {token}")
        assert not config_path().exists()  # the config is written AFTER this
        return env_setup.Health("ok", "3d-api", "fake")

    monkeypatch.setattr(deploy, "wait_healthy", wait_healthy)
    monkeypatch.setattr(env_setup, "check_health", check_health)


def test_ssh_install_runs_the_shared_deploy_plan_then_checks_then_writes(
    cfg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[str] = []
    _patch_health(monkeypatch, log)
    env, out, rec, drec = make_env(cfg_home)
    assert env_setup.run(["--no-input", "--spoolman", "ssh:me@nas.lan"], env) == 0
    probe, mint = rec.calls
    assert (
        probe[:2] == ["ssh", "-o"]
        and "me@nas.lan" in probe
        and "token create" in mint[-1]
    )
    assert "docker exec printops-api 3d printops token create cli-" in mint[-1]
    titles = [c[-1] for c in drec.calls]
    assert any(
        c[0] == "rsync" and c[-1] == "me@nas.lan:/home/me/printops/src/"
        for c in drec.calls
    )
    assert any(
        "docker compose -p printops" in t
        and "PRINTOPS_DATA_DIR=/home/me/printops/data" in t
        for t in titles
    )
    assert log == [
        "wait http://nas.lan:8765",
        f"check http://nas.lan:8765 {GOOD_TOKEN}",
    ]
    assert "api: http://nas.lan:8765" in config_path().read_text()
    assert (config_path().stat().st_mode & 0o777) == 0o600
    assert any(
        "Debian GNU/Linux 12 aarch64, 4 cores, 8000 MB RAM, load 0.30" in line
        for line in out
    )


def test_install_failure_leaves_no_config(
    cfg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def wait_healthy(url: str, **_kw: Any) -> dict[str, Any]:
        raise env_setup.ApiError(f"3d-api at {url} not healthy after 300s: down")

    monkeypatch.setattr(deploy, "wait_healthy", wait_healthy)
    env, _, rec, _ = make_env(cfg_home)
    with pytest.raises(ThreeDError, match="not healthy"):
        env_setup.run(["--no-input", "--spoolman", "ssh:me@nas.lan"], env)
    assert (
        not config_path().exists() and len(rec.calls) == 1
    )  # probe only: no token minted


def test_a_host_whose_docker_daemon_is_not_reachable_is_not_touched(
    cfg_home: Path,
) -> None:
    """`docker --version` and `docker compose version` work without the daemon (and for a user outside
    the docker group): the old check passed, the deploy failed halfway, and the hint blamed a container."""
    env, out, rec, drec = make_env(cfg_home, runner=Recorder(PROBE_NO_DAEMON_ACCESS))
    with pytest.raises(ThreeDError, match="cannot talk to its daemon") as err:
        env_setup.run(["--no-input", "--spoolman", "ssh:me@nas.lan"], env)
    assert (
        "usermod -aG docker" in err.value.render()
        and "docker logs" not in err.value.render()
    )
    assert any("daemon NOT reachable" in line for line in out)
    assert not drec.calls and not config_path().exists()


def test_an_ssh_host_without_rsync_is_refused_before_any_step_runs(
    cfg_home: Path,
) -> None:
    env, _, _, drec = make_env(cfg_home, runner=Recorder(PROBE_NO_RSYNC))
    with pytest.raises(ThreeDError, match="no rsync") as err:
        env_setup.run(["--no-input", "--spoolman", "ssh:me@nas.lan"], env)
    assert (
        "apt install rsync" in err.value.render()
        and "--spoolman script" in err.value.render()
    )
    assert not drec.calls  # not even the mkdir step: no half-done install


def test_this_machine_needs_no_rsync(
    cfg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_health(monkeypatch, [])
    env, _, _, drec = make_env(cfg_home, runner=Recorder(PROBE_NO_RSYNC))
    assert (
        env_setup.run(["--no-input", "--spoolman", "local"], env) == 0
    )  # compose builds from the checkout
    assert drec.calls


def test_the_probe_script_reports_docker_daemon_access_and_rsync(
    tmp_path: Path,
) -> None:
    """The probe really runs on the host: shims stand in for docker and rsync."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (
        (
            "docker",
            'case "$1" in info) exit "${FAKE_DOCKER_INFO:-0}" ;; --version) echo "Docker version 27.1.1, build x" ;; '
            "compose) echo 2.29.1 ;; esac",
        ),
        ("rsync", "exit 0"),
    ):
        (bindir / name).write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        (bindir / name).chmod(0o755)
    base = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    up = env_setup.parse_probe(
        subprocess.run(
            ["bash", "-c", env_setup.PROBE_SCRIPT],
            env=base,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    assert up.docker == "27.1.1" and up.docker_access and up.rsync
    down = env_setup.parse_probe(
        subprocess.run(
            ["bash", "-c", env_setup.PROBE_SCRIPT],
            env={**base, "FAKE_DOCKER_INFO": "1"},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    assert down.docker == "27.1.1" and not down.docker_access


def test_a_host_without_docker_is_not_touched(cfg_home: Path) -> None:
    env, _, rec, drec = make_env(cfg_home, runner=Recorder(PROBE_NO_DOCKER))
    with pytest.raises(ThreeDError, match="no docker") as err:
        env_setup.run(["--no-input", "--spoolman", "ssh:me@nas.lan"], env)
    assert "docs.docker.com" in err.value.render()
    assert not drec.calls and not config_path().exists()


def test_busy_host_warns_and_interactive_can_decline(cfg_home: Path) -> None:
    env, out, _, drec = make_env(
        cfg_home, runner=Recorder(PROBE_BUSY), tty=True, ask=Prompts("n")
    )
    with pytest.raises(ThreeDError, match="cancelled"):
        env_setup.run(["--spoolman", "ssh:me@nas.lan"], env)
    assert any("BUSY" in line for line in out) and not drec.calls


def test_dry_run_with_api_or_script_is_refused_and_writes_nothing(
    cfg_home: Path, service: FakeService
) -> None:
    """`--api URL --dry-run` used to rewrite printops.yaml (keeping the old one only as .bak) and
    `--spoolman script --dry-run` wrote the script: a preview flag must never write."""
    old = write_cfg("http://10.0.0.99:8765", "po_old")
    before = old.read_bytes()
    out_file = cfg_home / "preview.sh"
    for argv in (
        ["--api", service.url, "--token", GOOD_TOKEN, "--dry-run"],
        ["--spoolman", "script", "--script-out", str(out_file), "--dry-run"],
    ):
        env, _, rec, drec = make_env(cfg_home)
        with pytest.raises(UsageError, match="--dry-run") as err:
            env_setup.run(["--no-input", *argv], env)
        assert err.value.exit_code == 2
        assert not rec.calls and not drec.calls
    assert (
        old.read_bytes() == before and not old.with_name("printops.yaml.bak").exists()
    )
    assert (
        not out_file.exists() and service.requests == []
    )  # refused before any request, too


def test_dry_run_chosen_from_the_menu_cannot_write_the_script(
    cfg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(cfg_home)
    env, _, _, _ = make_env(cfg_home, tty=True, ask=Prompts("4", "preview.sh"))
    with pytest.raises(UsageError, match="--dry-run"):
        env_setup.run(["--dry-run"], env)
    assert not (cfg_home / "preview.sh").exists()


def test_dry_run_prints_the_plan_and_changes_nothing(cfg_home: Path) -> None:
    env, out, rec, drec = make_env(cfg_home)
    assert (
        env_setup.run(["--no-input", "--spoolman", "ssh:me@nas.lan", "--dry-run"], env)
        == 0
    )
    assert len(rec.calls) == 1  # the read-only probe
    # only the read-only rsync --dry-run executes; every script step is merely printed
    assert [c[0] for c in drec.calls] == ["rsync"] and "--dry-run" in drec.calls[0]
    assert not config_path().exists()
    assert any("[dry run]" in line for line in out)


def test_printer_install_uses_registry_host_and_ssh_user(
    cfg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_health(monkeypatch, [])
    env, _, rec, drec = make_env(cfg_home)
    assert (
        env_setup.run(
            ["--no-input", "--spoolman", "printer:voron", "--ssh-user", "pi"], env
        )
        == 0
    )
    assert "pi@10.9.9.3" in rec.calls[0]
    assert any(
        c[-1] == "pi@10.9.9.3:/home/me/printops/src/"
        for c in drec.calls
        if c[0] == "rsync"
    )
    assert "api: http://10.9.9.3:8765" in config_path().read_text()


def test_an_ssh_only_alias_is_resolved_for_the_health_check_and_the_config(
    cfg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[str] = []
    _patch_health(monkeypatch, log)
    rec = Recorder(
        ssh_hostnames={"me@printops-server": "192.0.2.10"}
    )  # the alias is not in DNS
    env, out, _, drec = make_env(cfg_home, runner=rec)
    assert (
        env_setup.run(["--no-input", "--spoolman", "ssh:me@printops-server"], env) == 0
    )
    assert rec.ssh_queries == [["ssh", "-G", "me@printops-server"]]
    assert log == [
        "wait http://192.0.2.10:8765",
        f"check http://192.0.2.10:8765 {GOOD_TOKEN}",
    ]
    assert "api: http://192.0.2.10:8765" in config_path().read_text()
    assert "printops-server" not in config_path().read_text()
    assert any(
        c[0] == "rsync" and c[-1].startswith("me@printops-server:") for c in drec.calls
    )  # ssh keeps the alias


def test_http_host_falls_back_to_the_ssh_target_name_and_keeps_an_ipv6_literal_bare(
    cfg_home: Path,
) -> None:
    class Failing:
        def __call__(
            self, argv: list[str], **_kw: Any
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 255, "", "ssh: bad config")

    env, _, _, _ = make_env(cfg_home, runner=Failing())
    assert env_setup._http_host("me@nas.lan", env) == "nas.lan"
    env6, _, _, _ = make_env(
        cfg_home, runner=Recorder(ssh_hostnames={"v6": "2001:db8::7"})
    )
    assert env_setup._http_host("v6", env6) == "2001:db8::7"


def test_an_ipv6_literal_target_is_bracketed_where_rsync_and_http_need_it(
    cfg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rsync splits `me@2001:db8::7:/dir` at the first colon; ssh wants the address bare."""
    log: list[str] = []
    _patch_health(monkeypatch, log)
    env, _, rec, drec = make_env(cfg_home)
    assert env_setup.run(["--no-input", "--spoolman", "ssh:me@2001:db8::7"], env) == 0
    assert any(
        c[0] == "rsync" and c[-1] == "me@[2001:db8::7]:/home/me/printops/src/"
        for c in drec.calls
    )
    ssh_steps = [c for c in drec.calls if c[0] == "ssh"]
    assert ssh_steps and all(
        "me@2001:db8::7" in c for c in ssh_steps
    )  # the ssh steps take it bare
    assert log[0] == "wait http://[2001:db8::7]:8765"
    assert "api: http://[2001:db8::7]:8765" in config_path().read_text()
    assert "me@2001:db8::7" in rec.calls[0]


def test_token_names_stay_within_the_token_name_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    from printops.tokens import NAME_RE

    for length in (1, 45, 46, 63):  # 46 chars used to give a 65-char name
        monkeypatch.setattr(socket, "gethostname", lambda n=length: "a" * n)
        name = env_setup._token_name()
        assert NAME_RE.match(name) and len(name) <= 64, (length, name)
        assert name.startswith("cli-a") and name[-14:].isdigit()


def test_u1_is_possible_but_only_with_the_explicit_flag(
    cfg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_health(monkeypatch, [])
    env, _, rec, _ = make_env(cfg_home)
    assert (
        env_setup.run(
            ["--no-input", "--spoolman", "printer:u1", "--allow-discouraged"], env
        )
        == 0
    )
    assert "10.9.9.1" in rec.calls[0]


def test_local_install_builds_from_this_checkout_without_rsync(
    cfg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_health(monkeypatch, [])
    env, _, rec, drec = make_env(cfg_home)
    assert env_setup.run(["--no-input", "--spoolman", "local"], env) == 0
    assert rec.calls[0][:2] == ["bash", "-c"]  # the probe runs locally, no ssh
    assert all(c[0] == "bash" for c in drec.calls)  # no ssh, no rsync
    compose = next(c[-1] for c in drec.calls if "docker compose" in c[-1])
    assert (
        f"cd {REPO} &&" in compose
        and str(cfg_home / "xdg-data" / "3d-cli" / "printops" / "data") in compose
    )
    # this host's own settings, not the Odroid's compose defaults nor the caller's environment
    assert (
        "PRINTOPS_CONTAINER_PREFIX=printops" in compose
        and 'PRINTOPS_TZ="${tz:-UTC}"' in compose
    )
    assert (
        "PRINTOPS_SPOOLMAN_CORS_ORIGIN=http://127.0.0.1:7912,http://localhost:7912"
        in compose
    )
    assert "api: http://127.0.0.1:8765" in config_path().read_text()


def _compose_of(drec: Recorder) -> str:
    return next(c[-1] for c in drec.calls if "docker compose" in c[-1])


def test_local_install_listens_on_loopback_and_says_what_that_costs(
    cfg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spoolman has no authentication: `--spoolman local` on a laptop must not open it to the Wi-Fi."""
    _patch_health(monkeypatch, [])
    env, out, _, drec = make_env(cfg_home)
    assert env_setup.run(["--no-input", "--spoolman", "local"], env) == 0
    assert "PRINTOPS_BIND_ADDR=127.0.0.1" in _compose_of(drec)
    assert any(
        "listen on 127.0.0.1 only" in line and "--bind 0.0.0.0" in line for line in out
    )
    assert "api: http://127.0.0.1:8765" in config_path().read_text()


def test_bind_opens_a_local_install_to_the_lan_and_a_remote_one_to_one_address(
    cfg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_health(monkeypatch, [])
    env, out, _, drec = make_env(cfg_home)
    assert (
        env_setup.run(["--no-input", "--spoolman", "local", "--bind", "0.0.0.0"], env)
        == 0
    )
    assert "PRINTOPS_BIND_ADDR=0.0.0.0" in _compose_of(
        drec
    )  # pinned: the caller's environment cannot narrow it
    assert not any("listen on" in line for line in out)
    config_path().unlink()  # the first install configured the machine; the next ones start unconfigured
    env2, _, _, drec2 = make_env(cfg_home)
    assert (
        env_setup.run(
            ["--no-input", "--spoolman", "ssh:me@nas.lan", "--bind", "192.168.0.9"],
            env2,
        )
        == 0
    )
    assert "PRINTOPS_BIND_ADDR=192.168.0.9" in _compose_of(drec2)
    config_path().unlink()
    env3, _, _, drec3 = make_env(cfg_home)
    assert env_setup.run(["--no-input", "--spoolman", "ssh:me@nas.lan"], env3) == 0
    assert "PRINTOPS_BIND_ADDR" not in _compose_of(
        drec3
    )  # a remote host keeps compose's default


def test_bind_is_checked_before_anything_runs(cfg_home: Path) -> None:
    for argv in (["--bind", "nas.lan"], ["--bind", "999.1.1.1"]):
        with pytest.raises(InvalidArgument, match="--bind"):
            env_setup.parse_options(argv)
    with pytest.raises(UsageError, match="--bind"):
        env_setup.parse_options(["--api", "http://x:8765", "--bind", "127.0.0.1"])
    assert env_setup.parse_options(["--bind", "::1"]).bind == "[::1]"


def test_the_script_target_bakes_the_bind_address_in_as_its_default(
    cfg_home: Path,
) -> None:
    out = cfg_home / "bound.sh"
    env, _, _, _ = make_env(cfg_home)
    assert (
        env_setup.run(
            [
                "--no-input",
                "--spoolman",
                "script",
                "--script-out",
                str(out),
                "--bind",
                "127.0.0.1",
            ],
            env,
        )
        == 0
    )
    assert "${PRINTOPS_BIND_ADDR:-127.0.0.1}" in out.read_text(encoding="utf-8")


# ------------------------------------------------------------------ deploy generalization


def test_odroid_deploy_plan_keeps_its_shape_and_defaults(tmp_path: Path) -> None:
    cfg = deploy.DeployConfig(repo_root=tmp_path, version="v1")
    steps = deploy.plan(cfg)
    assert [s.argv[0] for s in steps] == [
        "ssh",
        "rsync",
        "ssh",
        "ssh",
    ] and cfg.health_url == "http://192.168.0.39:8765"
    assert not any(
        "enable" in s.argv[-1] for s in steps
    )  # HA schedules the backup; only the legacy timer goes
    compose = steps[2].argv[-1]
    assert (
        "PRINTOPS_API_PORT=8765" in compose and "PRINTOPS_SPOOLMAN_PORT=7912" in compose
    )
    assert (
        "PRINTOPS_GCODE_HOST_DIR=/home/ultra/printops/gcodes" in compose
    )  # = the compose default


def test_local_deploy_plan_and_nondefault_ports(tmp_path: Path) -> None:
    cfg = deploy.DeployConfig(
        repo_root=tmp_path,
        host="",
        local=True,
        remote_dir=str(tmp_path / "p"),
        backup_dir=str(tmp_path / "p" / "backup"),
        api_port=18765,
        spoolman_port=17912,
    )
    steps = deploy.plan(cfg)
    assert [s.argv[:2] for s in steps] == [
        ["bash", "-c"]
    ] * 3  # mkdirs, compose, legacy-timer retirement
    assert (
        "PRINTOPS_API_PORT=18765" in steps[1].argv[-1]
        and "PRINTOPS_SPOOLMAN_PORT=17912" in steps[1].argv[-1]
    )
    assert cfg.health_url == "http://127.0.0.1:18765"


def test_compose_publishes_the_port_the_health_check_polls(tmp_path: Path) -> None:
    """A caller's own PRINTOPS_API_PORT must not move the published port away from health_url."""
    from printops_env_fakes import fake_docker_bin

    cfg = deploy.DeployConfig(
        repo_root=tmp_path,
        host="",
        local=True,
        remote_dir=str(tmp_path / "p"),
        backup_dir=str(tmp_path / "p" / "backup"),
    )
    step = next(s for s in deploy.plan(cfg) if "docker compose" in s.argv[-1])
    env = {
        **os.environ,
        "PATH": f"{fake_docker_bin(tmp_path)}{os.pathsep}{os.environ['PATH']}",
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "PRINTOPS_API_PORT": "18765",
        "PRINTOPS_SPOOLMAN_PORT": "17912",
    }
    done = subprocess.run(
        step.argv, env=env, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    calls = (tmp_path / "calls.log").read_text()
    assert "PRINTOPS_API_PORT=8765" in calls and "PRINTOPS_SPOOLMAN_PORT=7912" in calls
    assert "18765" not in calls and cfg.health_url == "http://127.0.0.1:8765"


def _fake_systemctl(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """A HOME holding the units an OLD deploy installed + a recording fake `systemctl`."""
    home = tmp_path / "home"
    units = home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    (units / "printops-backup.timer").write_text("[Timer]\n", encoding="utf-8")
    (units / "printops-backup.service").write_text(
        "[Service]\nExecStart=3d printops backup\n", encoding="utf-8"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "systemctl.log"
    fake = bin_dir / "systemctl"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {log}\n', encoding="utf-8")
    fake.chmod(0o755)
    env = {"HOME": str(home), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return env, units


def test_redeploy_retires_a_backup_timer_an_older_deploy_installed(
    tmp_path: Path,
) -> None:
    """The old timer ran `3d printops backup` (no subcommand): it now exits 1 every night while health
    stays green. A redeploy must disable + delete it, and a second redeploy must be a quiet no-op."""
    env, units = _fake_systemctl(tmp_path)
    cfg = deploy.DeployConfig(
        repo_root=tmp_path,
        host="",
        local=True,
        remote_dir=str(tmp_path / "p"),
        backup_dir=str(tmp_path / "p" / "backup"),
    )
    retire = deploy.plan(cfg)[-1]
    assert retire.title.startswith("retire the legacy backup timer")
    first = subprocess.run(
        retire.argv, env=env, capture_output=True, text=True, check=False
    )
    assert first.returncode == 0, first.stderr
    assert (
        not (units / "printops-backup.timer").exists()
        and not (units / "printops-backup.service").exists()
    )
    assert (
        "disable --now printops-backup.timer"
        in (tmp_path / "systemctl.log").read_text()
    )
    assert "daemon-reload" in (tmp_path / "systemctl.log").read_text()
    assert "removed the legacy printops-backup.timer" in first.stdout
    (tmp_path / "systemctl.log").unlink()
    again = subprocess.run(
        retire.argv, env=env, capture_output=True, text=True, check=False
    )
    assert (
        again.returncode == 0
        and again.stdout == ""
        and not (tmp_path / "systemctl.log").exists()
    )


def test_redeploy_retire_step_survives_a_host_without_systemd(tmp_path: Path) -> None:
    env, units = _fake_systemctl(tmp_path)
    (tmp_path / "bin" / "systemctl").unlink()
    cfg = deploy.DeployConfig(
        repo_root=tmp_path,
        host="",
        local=True,
        remote_dir=str(tmp_path / "p"),
        backup_dir=str(tmp_path / "p" / "backup"),
    )
    bare = tmp_path / "bare"
    bare.mkdir()
    for tool in ("bash", "rm"):  # a PATH with no systemctl on it
        (bare / tool).symlink_to(shutil.which(tool) or tool)
    run = subprocess.run(
        deploy.plan(cfg)[-1].argv,
        env={**env, "PATH": str(bare)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert not (units / "printops-backup.timer").exists()


STAGE_ONLY = [
    "--stage-only",
    "--no-input",
    "--spoolman",
    "local",
    "--backup",
    "path:/mnt/backup",
    "--ha",
    "skip",
]


def test_stage_only_dry_run_runs_nothing_at_all(
    cfg_home: Path, stage_calls: list[dict[str, Any]]
) -> None:
    """`--stage-only --dry-run` used to skip the install path's dry-run check and register the target, recreate
    the 3d-api container and run the selftest."""
    env, out, rec, drec = make_env(cfg_home)
    assert env_setup.run([*STAGE_ONLY, "--dry-run"], env) == 0
    assert (
        stage_calls == []
        and not rec.calls
        and not drec.calls
        and not config_path().exists()
    )
    assert any(
        "dry run" in line and "nothing was probed, written or changed" in line
        for line in out
    )

    env, out, rec, _ = make_env(
        cfg_home
    )  # the same command without --dry-run does reach the stage
    assert env_setup.run(STAGE_ONLY, env) == 0
    assert len(stage_calls) == 1


def test_local_deploy_hands_compose_absolute_dirs_for_relative_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--dir ./state` is prepared where it was typed; compose resolves bind sources against
    services/3d-api/, so both must see the same absolute directory."""
    from printops.cli import deploy as deploy_cli

    monkeypatch.chdir(tmp_path)
    assert (
        deploy_cli.run(
            ["--local", "--dry-run", "--dir", "./state", "--backup-dir", "bk"]
        )
        == 0
    )
    shown = capsys.readouterr().out
    state = str(tmp_path.resolve() / "state")
    assert f"PRINTOPS_DATA_DIR={state}/data" in shown
    assert f"mkdir -p {state}/data/api" in shown
    assert f"{tmp_path.resolve()}/bk" in shown and 'PRINTOPS_BACKUP_DIR="$bd"' in shown
    assert " ./state" not in shown and "=./state" not in shown


# ------------------------------------------------------------------ interactive flows


def test_interactive_menu_to_the_script_writes_where_asked(
    cfg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(cfg_home)
    target = cfg_home / "chosen.sh"
    env, out, rec, drec = make_env(cfg_home, tty=True, ask=Prompts("4", str(target)))
    assert env_setup.run([], env) == 0
    assert target.is_file() and os.access(target, os.X_OK)
    assert any(f"wrote {target}" in line for line in out)
    assert (
        not rec.calls and not drec.calls and not config_path().exists()
    )  # a script installs nothing here


def test_interactive_menu_to_an_ssh_server_installs_and_configures(
    cfg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_health(monkeypatch, [])
    env, out, rec, _ = make_env(cfg_home, tty=True, ask=Prompts("3", "me@nas.lan"))
    assert env_setup.run([], env) == 0
    assert (
        "me@nas.lan" in rec.calls[0]
        and "api: http://nas.lan:8765" in config_path().read_text()
    )
    assert any("Where should Spoolman" in line for line in out)


def test_interactive_menu_skip_changes_nothing(cfg_home: Path) -> None:
    env, out, rec, drec = make_env(cfg_home, tty=True, ask=Prompts("0"))
    assert env_setup.run([], env) == 0
    assert not rec.calls and not drec.calls and not config_path().exists()


def test_interactive_with_a_dead_config_asks_before_replacing_it(
    cfg_home: Path,
) -> None:
    dead = FakeService()
    url = dead.url
    dead.close()
    write_cfg(url)
    env, out, rec, _ = make_env(cfg_home, tty=True, ask=Prompts("n"))
    assert env_setup.run([], env) == 1
    assert any("unreachable" in line for line in out) and not rec.calls
