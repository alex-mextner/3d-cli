"""The HA backup package: where its 3d-api URL comes from, the watchdog, and what install_package does when
something goes wrong half way (printops.ha_wire against a fake HA and the local shell)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from printops_ha_fakes import TOKEN, FakeHa

from errors import ThreeDError
from printops import backup_setup, ha_wire
from printops.ha_rest import HaRest, HaRestError

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / ha_wire.PACKAGE_REL
VALUES: dict[str, object] = {
    "api_url": "http://10.0.0.9:8765",
    "target": "default",
    "subdir": "3d-printops",
    "keep_daily": 7,
    "keep_weekly": 4,
    "keep_monthly": 6,
}


@pytest.fixture
def fake_ha() -> Iterator[FakeHa]:
    ha = FakeHa()
    yield ha
    ha.close()


def make_ha_dir(
    root: Path, name: str = "ha-config", packages: str = "packages"
) -> Path:
    d = root / name
    d.mkdir()
    (d / "configuration.yaml").write_text(
        f"homeassistant:\n  packages: !include_dir_named {packages}\n", encoding="utf-8"
    )
    (d / "secrets.yaml").write_text("existing: 1\n", encoding="utf-8")
    return d


def install(ha: HaRest, ha_dir: Path, bearer=lambda: "po_t"):  # type: ignore[no-untyped-def]
    return ha_wire.install_package(
        backup_setup.local_shell(), ha, str(ha_dir), REPO, VALUES, bearer
    )


def load_package(text: str) -> dict[str, object]:
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor("!secret", lambda loader, node: f"<secret {node.value}>")
    return yaml.load(text, Loader)  # noqa: S506 — SafeLoader subclass


# ------------------------------------------------------------ the URL the bearer goes to


def test_the_api_url_is_written_into_the_rest_command_and_never_read_from_service_data() -> (
    None
):
    rendered = ha_wire.render_package(PACKAGE.read_text(encoding="utf-8"), VALUES)
    doc = load_package(rendered)
    command = doc["rest_command"]["printops_backup_run"]  # type: ignore[index]
    assert command["url"] == "http://10.0.0.9:8765/v1/backup/run"
    assert "{{" not in command["url"]
    sequence = json.dumps(doc["script"]["printops_backup_run"])  # type: ignore[index]
    assert "api_url" not in sequence and "api_url" not in json.dumps(command)
    assert "api_url" not in rendered.replace(
        "(url)", ""
    )  # no template reads a caller-supplied address either


@pytest.mark.parametrize(
    "bad",
    [
        'http://evil/"\n  headers: {}',
        "http://x:1/{{ api_url }}",
        "ftp://h",
        "http://h/path",
        "h:8123",
        "http://",
        "http://a b",
    ],
)
def test_an_api_url_that_could_alter_the_package_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="3d-api URL"):
        ha_wire.render_package(PACKAGE.read_text(encoding="utf-8"), {"api_url": bad})


def test_an_ipv6_api_url_renders() -> None:
    out = ha_wire.render_package(
        PACKAGE.read_text(encoding="utf-8"), {"api_url": "http://[fe80::1]:8765"}
    )
    assert 'url: "http://[fe80::1]:8765/v1/backup/run"' in out


# ------------------------------------------------------------ the overdue watchdog


def test_the_watchdog_treats_a_backup_that_never_ran_explicitly_and_not_through_epoch_zero() -> (
    None
):
    doc = load_package(PACKAGE.read_text(encoding="utf-8"))
    stale = next(a for a in doc["automation"] if a["id"] == "printops_backup_stale")  # type: ignore[attr-defined]
    template = stale["conditions"][0]["value_template"]
    assert (
        "1970-01-01 00:00:00" in template
        and "unknown" in template
        and "never" in template
    )
    assert (
        "last > 0" not in template
    )  # the old rule depended on the timezone of the epoch default


# ------------------------------------------------------------ the package must really load


def test_done_means_script_exists_after_the_reload(
    fake_ha: FakeHa, tmp_path: Path
) -> None:
    out = install(HaRest(fake_ha.url, TOKEN), make_ha_dir(tmp_path))
    assert out.status == "done"
    assert ("GET", "/api/states/script.printops_backup_run", None) in fake_ha.requests


def test_reload_that_leaves_no_script_is_not_done(tmp_path: Path) -> None:
    ha = FakeHa(package_loaded=False)
    try:
        out = install(HaRest(ha.url, TOKEN), make_ha_dir(tmp_path))
    finally:
        ha.close()
    assert (
        out.status == "failed"
        and "script.printops_backup_run does not exist" in out.detail
    )
    assert "!include_dir_named packages" in out.detail


def test_check_config_gets_a_long_timeout(fake_ha: FakeHa, tmp_path: Path) -> None:
    seen: dict[str, float | None] = {}

    class Spy(HaRest):
        def post(
            self, path: str, body: object = None, *, timeout: float | None = None
        ) -> object:
            seen[path] = timeout
            return super().post(path, body, timeout=timeout)

    install(Spy(fake_ha.url, TOKEN), make_ha_dir(tmp_path))
    timeout = seen["/api/config/core/check_config"]
    assert timeout is not None and timeout >= 120
    assert (
        seen["/api/services/script/reload"] is None
    )  # reloads keep the normal short timeout


@pytest.mark.parametrize(
    ("line", "loads"),
    [
        ("!include_dir_named packages", True),
        ("!include_dir_named ./packages/", True),
        ("!include_dir_named 'packages'", True),
        ("!include_dir_named {ha}/packages", True),
        ("!include_dir_named pkgs", False),
        ("!include_dir_named /srv/other/packages", False),
        ("!include_dir_merge_named packages", False),
        ("!include packages.yaml", False),
        ("", False),
    ],
)
def test_only_a_packages_line_that_loads_the_directory_we_write_to_counts(
    tmp_path: Path, line: str, loads: bool
) -> None:
    d = tmp_path / "cfg"
    d.mkdir()
    (d / "configuration.yaml").write_text(
        f"homeassistant:\n  packages: {line.format(ha=d)}\n".replace(
            "packages: \n", "packages:\n  inline_pkg:\n    sensor: []\n"
        ),
        encoding="utf-8",
    )
    reason = ha_wire._refuse_reason(backup_setup.local_shell(), str(d))
    assert (reason == "") is loads, reason
    if not loads:
        assert "packages" in reason


# ------------------------------------------------------------ failures half way


def test_a_failed_token_mint_writes_nothing_to_home_assistant(
    fake_ha: FakeHa, tmp_path: Path
) -> None:
    ha_dir = make_ha_dir(tmp_path)

    def broken() -> str:
        raise ThreeDError("container printops-api is not running", command="init --env")

    with pytest.raises(ThreeDError):
        install(HaRest(fake_ha.url, TOKEN), ha_dir, broken)
    assert not (
        ha_dir / "packages"
    ).exists()  # the package is never written before its secret exists
    assert (ha_dir / "secrets.yaml").read_text(encoding="utf-8") == "existing: 1\n"
    assert fake_ha.writes() == []


def test_any_exception_after_the_write_puts_the_previous_package_back(
    fake_ha: FakeHa, tmp_path: Path
) -> None:
    ha_dir = make_ha_dir(tmp_path)
    pkg = ha_dir / "packages" / "printops_backup.yaml"
    pkg.parent.mkdir()
    pkg.write_text("# previous\n", encoding="utf-8")

    class Boom(HaRest):
        def post(
            self, path: str, body: object = None, *, timeout: float | None = None
        ) -> object:
            raise KeyError("not an HaRestError")

    with pytest.raises(KeyError):
        install(Boom(fake_ha.url, TOKEN), ha_dir)
    assert pkg.read_text(encoding="utf-8") == "# previous\n"
    assert not (pkg.parent / "printops_backup.yaml.3d-bak").exists()


def test_a_stale_backup_file_from_an_earlier_run_is_not_resurrected(
    fake_ha: FakeHa, tmp_path: Path
) -> None:
    ha_dir = make_ha_dir(tmp_path)
    packages = ha_dir / "packages"
    packages.mkdir()
    (packages / "printops_backup.yaml.3d-bak").write_text(
        "# a package the user deleted\n", encoding="utf-8"
    )
    fake_ha.check_valid = False
    out = install(HaRest(fake_ha.url, TOKEN), ha_dir)
    assert out.status == "failed"
    assert not (
        packages / "printops_backup.yaml"
    ).exists()  # put back = removed, not the stale copy


def test_a_ha_directory_with_a_quote_in_its_name_is_handled(
    fake_ha: FakeHa, tmp_path: Path
) -> None:
    ha_dir = make_ha_dir(tmp_path, name="ha'; touch pwned; '")
    out = install(HaRest(fake_ha.url, TOKEN), ha_dir)
    assert out.status == "done", out.detail
    assert (ha_dir / "packages" / "printops_backup.yaml").is_file()
    assert not list(tmp_path.glob("**/pwned"))


def test_ha_rest_error_from_the_api_keeps_the_package_for_a_retry(
    tmp_path: Path,
) -> None:
    class Down(HaRest):
        def post(
            self, path: str, body: object = None, *, timeout: float | None = None
        ) -> object:
            raise HaRestError("Home Assistant is unreachable")

    ha_dir = make_ha_dir(tmp_path)
    out = install(Down("http://127.0.0.1:9", TOKEN), ha_dir)
    assert out.status == "failed" and "retry" in out.detail
    assert (ha_dir / "packages" / "printops_backup.yaml").is_file()
