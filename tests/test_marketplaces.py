"""Unit tests for lib/marketplaces: the per-site parsers on recorded (Printables, MakerWorld)
and documented (Thingiverse, Cults) responses, URL routing, error mapping, credentials and
the TLS-impersonating transport lane (#401)."""
from __future__ import annotations

import email.message
import http.client
import io
import json
import subprocess
import sys
import types
import urllib.error
from pathlib import Path

import pytest
from marketplace_fixtures import FILES, THINGIVERSE_TOKEN, MarketplaceServer, load, write_fake_curl

from errors import InvalidArgument, MissingDependency, UsageError
from marketplaces import catalog, credentials, cults, makerworld, printables, thingiverse, transport
from marketplaces.base import MarketplaceError, ModelFile, as_str, safe_name

SPOON_URL = "https://www.printables.com/model/311253-spoon-drawer-pull"


@pytest.fixture(autouse=True)
def _isolated_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for var in ("THINGIVERSE_TOKEN", "CULTS_USERNAME", "CULTS_API_KEY", "MAKERWORLD_TOKEN",
                "THREED_CURL_IMPERSONATE", "AXE_CURL_IMPERSONATE_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("THREED_KEYCHAIN", "off")  # never the developer's real keychain


# -- parsers --------------------------------------------------------------------------

def test_printables_search_parses_the_recorded_answer() -> None:
    models = printables.parse_search(load("printables_search")["data"])
    first = models[0]
    assert (first.id, first.title, first.author, first.license) == (
        "311253", "Spoon Drawer Pull", "cluckOmatic", "CC0")
    assert first.url == SPOON_URL
    assert first.likes == 58 and first.downloads == 180
    assert not first.paid and first.price == ""


def test_printables_model_lists_every_file_with_its_download_type() -> None:
    model = printables.parse_model(load("printables_model")["data"])
    assert model.files == (ModelFile("spoon_handle.stl", 3306984, "1395244", "stl"),)
    assert model.tags == ("pull", "spoon", "drawer")
    assert model.author_url == "https://www.printables.com/@cluckOmatic"


def test_printables_paid_and_premium_models_are_flagged() -> None:
    item = load("printables_model")["data"]["print"]
    assert printables.parse_model({"print": {**item, "price": "4.99"}}).paid
    club = printables.parse_model({"print": {**item, "premium": True}})
    assert club.paid and club.price == "Printables Club"


def test_printables_download_link_and_its_failure() -> None:
    file = ModelFile("spoon_handle.stl", ref="1395244", kind="stl")
    link = printables.parse_download_link(load("printables_download_link")["data"], file)
    assert link.startswith("https://files.printables.com/") and link.endswith("spoon_handle.stl")
    with pytest.raises(MarketplaceError, match="no download link for spoon_handle.stl"):
        printables.parse_download_link({"getDownloadLink": {"ok": False, "output": None}}, file)


def test_printables_missing_model_is_not_found() -> None:
    with pytest.raises(MarketplaceError) as err:
        printables.parse_model({"print": None})
    assert err.value.kind == "not-found"


def test_makerworld_search_and_design_parse_the_recorded_answers() -> None:
    hit = makerworld.parse_search(load("makerworld_search"))[0]
    assert hit.id == "1871683" and hit.author == "zentenro"
    assert hit.license == "Standard Digital File License"
    assert hit.url == "https://makerworld.com/en/models/1871683-modern-angular-front-mounted-drawer-pull-or-handle"
    design = makerworld.parse_model(load("makerworld_design"))
    assert [f.name for f in design.files] == ["Handler-120.stl", "Handler-DG.stl"]
    assert not design.paid and "<p>" not in design.summary


def test_makerworld_paid_and_point_redeemable_designs_are_flagged() -> None:
    design = load("makerworld_design")
    assert makerworld.parse_model({**design, "paidSetting": {"isPaid": True}}).paid
    points = makerworld.parse_model({**design, "isPointRedeemable": True})
    assert points.paid and points.price == "points"
    for unreadable in (None, {}, {"price": 0}):  # no isPaid: the detail answer fails closed
        assert makerworld.parse_model({**design, "paidSetting": unreadable}).paid


def test_token_goes_only_to_the_same_scheme_and_host() -> None:
    assert transport.same_host("https://api.thingiverse.com/files/1", "https://api.thingiverse.com")
    assert not transport.same_host("http://api.thingiverse.com/files/1", "https://api.thingiverse.com")


def test_a_non_utf8_body_is_a_parse_error() -> None:
    with pytest.raises(MarketplaceError) as err:
        transport.decode_json("cults", transport.Response(200, "caf\xe9".encode("latin-1"), "https://x"))
    assert err.value.kind == "parse"


def test_odd_printables_link_errors_and_quoted_cults_names() -> None:
    file = ModelFile(name="a.stl", ref="1", kind="stl")
    with pytest.raises(MarketplaceError, match="no download link for a.stl: denied"):
        printables.parse_download_link({"getDownloadLink": {"ok": False, "errors": [
            {"messages": [None, {"x": 1}, "denied"]}, {"messages": None}]}}, file)
    creation = {"creation": {"blueprints": [{"fileUrl": "https://cdn/b/spoon%20pull.stl?x=1"}]}}
    assert [f.name for f in cults.parse_model(creation).files] == ["spoon pull.stl"]


def test_a_redirect_off_http_is_refused(tmp_path: Path) -> None:
    """Without the guard urllib follows to ftp:// (and fails as a network error there)."""
    with MarketplaceServer() as server, pytest.raises(MarketplaceError) as err:
        transport.download("cults", f"{server.base}/ftp-redirect", tmp_path / "f.stl")
    assert err.value.kind != "network"
    assert not (tmp_path / "f.stl").exists()


def test_lone_surrogates_in_site_text_become_replacement_chars() -> None:
    assert as_str("bad \ud800 title ") == "bad \ufffd title"
    assert safe_name(as_str("x\udc00.stl")).encode("utf-8")


def test_makerworld_download_answer() -> None:
    assert makerworld.parse_download({"name": "a.zip", "url": "https://cdn/x"}) == ("a.zip", "https://cdn/x")
    with pytest.raises(MarketplaceError, match="no download URL"):
        makerworld.parse_download({"code": 1})


def test_thingiverse_search_files_and_thing() -> None:
    models = thingiverse.parse_search(load("thingiverse_search"))
    assert [(m.id, m.author, m.likes) for m in models] == [("5594736", "spoonmaker", 42), ("4000001", "forker", 3)]
    assert thingiverse.parse_search(load("thingiverse_search")["hits"])[0].id == "5594736"  # bare-list form
    model = thingiverse.parse_model(load("thingiverse_thing"), load("thingiverse_files"))
    assert model.license == "Creative Commons - Attribution" and model.downloads == 900
    assert [(f.name, f.ref) for f in model.files] == [
        ("spoon_pull.stl", "https://cdn.thingiverse.com/assets/spoon_pull.stl"),
        ("spoon_pull_v2.stl", "https://api.thingiverse.com/files/12/download"),  # no direct_url
    ]


def test_cults_search_reports_price_and_paid() -> None:
    free, paid = cults.parse_search(load("cults_search")["data"])
    assert (free.price, free.paid, free.author, free.license) == ("free", False, "kitchenfab", "CC BY")
    assert (paid.price, paid.paid) == ("4.99 EUR", True)


def test_cults_creation_names_blueprints_from_their_url() -> None:
    model = cults.parse_model(load("cults_creation")["data"])
    assert [f.name for f in model.files] == ["spoon-pull.stl"]
    assert model.tags == ("spoon", "drawer")


@pytest.mark.parametrize("price", [None, {}, {"cents": None}, {"cents": "n/a"}])
def test_cults_unreadable_price_counts_as_paid(price: object) -> None:
    """Only an explicit 0 cents is free: a null or unreadable price must never turn a priced
    creation into a download."""
    item = load("cults_creation")["data"]["creation"]
    assert not cults.parse_model({"creation": item}).paid
    model = cults.parse_model({"creation": {**item, "price": price}})
    assert model.paid and model.price == "price unknown"


# -- URL routing ----------------------------------------------------------------------

@pytest.mark.parametrize(("url", "site", "ref"), [
    (SPOON_URL, "printables", "311253"),
    ("https://printables.com/de/model/311253", "printables", "311253"),
    ("https://makerworld.com/en/models/1871683-modern-angular", "makerworld", "1871683"),
    ("https://makerworld.com/models/1871683#profileId-1", "makerworld", "1871683"),
    ("https://www.thingiverse.com/thing:5594736/files", "thingiverse", "5594736"),
    ("https://cults3d.com/en/3d-model/home/spoon-drawer-pull?x=1", "cults", "spoon-drawer-pull"),
])
def test_resolve_routes_each_site_url(url: str, site: str, ref: str) -> None:
    client, got = catalog.resolve(url)
    assert (client.site, got) == (site, ref)


def test_resolve_rejects_other_urls_with_the_accepted_forms() -> None:
    with pytest.raises(UsageError) as err:
        catalog.resolve("https://example.com/model/1")
    assert "thing:<id>" in err.value.remediation[0]


# -- helpers and transport ------------------------------------------------------------

def test_safe_name_strips_paths_and_dots() -> None:
    name = safe_name("../../etc/passwd")
    assert "/" not in name and not name.startswith(".") and name.endswith("etc_passwd")
    assert safe_name("...") == "file"


def test_cloudflare_challenge_is_reported_as_blocked() -> None:
    page = transport.Response(403, b"<title>Just a moment...</title>", "https://cults3d.com/en")
    with pytest.raises(MarketplaceError) as err:
        transport.raise_for_status("cults", page)
    assert err.value.kind == "blocked" and err.value.exit_code == 1


def test_http_401_with_hint_is_an_auth_error_exit_2() -> None:
    reply = transport.Response(401, b'{"type":"NO_TOKEN_PROVIDED"}', "https://api.thingiverse.com/x")
    with pytest.raises(MarketplaceError) as err:
        transport.raise_for_status("thingiverse", reply, auth_hint="set a token")
    assert (err.value.kind, err.value.exit_code, err.value.remediation) == ("auth", 2, ["set a token"])


def test_credentials_env_wins_over_auth_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    auth = tmp_path / "xdg" / "3d-cli" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"thingiverse": {"token": "from-file"},
                                "cults": {"username": "u", "api_key": "k"}}), encoding="utf-8")
    assert credentials.thingiverse_token() == "from-file"
    assert credentials.cults_basic_auth() == ("u", "k")
    monkeypatch.setenv("THINGIVERSE_TOKEN", "from-env")
    assert credentials.thingiverse_token() == "from-env"
    assert auth.stat().st_mode & 0o077 == 0  # the shared auth file is kept private


def test_missing_credentials_name_the_fix() -> None:
    with pytest.raises(MarketplaceError) as err:
        credentials.makerworld_token()
    assert err.value.kind == "auth" and "MAKERWORLD_TOKEN" in err.value.remediation[1]


# -- OS keychain (keychain-cli) ---------------------------------------------------------

class FakeStore:
    """Stands in for credentials.KeychainStore: a dict, so nothing reaches a real keyring."""

    name = "keychain"

    def __init__(self, items: dict[str, str] | None = None) -> None:
        self.items = dict(items or {})

    def get(self, ref: str) -> str | None:
        return self.items.get(ref)

    def set(self, ref: str, value: str, label: str) -> None:
        self.items[ref] = value

    def delete(self, ref: str) -> bool:
        return self.items.pop(ref, None) is not None


def test_lookup_order_is_env_then_keychain_then_auth_json(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    auth = tmp_path / "xdg" / "3d-cli" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"thingiverse": {"token": "from-file"},
                                "makerworld": {"token": "mw-file"}}), encoding="utf-8")
    store = FakeStore({"3d-cli/thingiverse-token": "from-keychain",
                       "3d-cli/cults3d-user": "nick", "3d-cli/cults3d-key": "key"})
    monkeypatch.setattr(credentials, "secret_store", lambda: store)
    assert credentials.thingiverse_token() == "from-keychain"
    assert credentials.cults_basic_auth() == ("nick", "key")
    assert credentials.makerworld_token() == "mw-file"  # not in the keychain: auth.json
    monkeypatch.setenv("THINGIVERSE_TOKEN", "from-env")
    assert credentials.thingiverse_token() == "from-env"
    field = credentials.FIELDS["thingiverse"][0]
    assert credentials.sources(field) == ["env THINGIVERSE_TOKEN", "keychain 3d-cli/thingiverse-token",
                                          "auth.json thingiverse.token"]


def _fake_keychainlib(items: dict[str, str], *, locked: bool = False) -> tuple[type, types.ModuleType]:
    """A (Keychain, keychainlib.errors) look-alike with the API credentials.KeychainStore uses."""
    errors = types.ModuleType("keychainlib.errors")

    class KeychainError(Exception):
        pass

    class NotFound(KeychainError):
        pass

    class Secret:
        def __init__(self, value: str) -> None:
            self._value = value

        def reveal(self) -> str:
            return self._value

    class Keychain:
        def __init__(self, caller: str, interactive: bool = True) -> None:
            assert caller == "3d-cli"

        def get(self, ref: str) -> Secret:
            if locked:
                raise KeychainError("locked")
            if ref not in items:
                raise NotFound(ref)
            return Secret(items[ref])

        def set(self, ref: str, value: str, *, label: str | None = None) -> None:
            if locked:
                raise KeychainError("locked")
            items[ref] = value

        def delete(self, ref: str) -> None:
            if ref not in items:
                raise NotFound(ref)
            del items[ref]

    errors.KeychainError, errors.NotFound = KeychainError, NotFound  # type: ignore[attr-defined]
    return Keychain, errors


def test_keychain_store_skips_a_locked_or_missing_item_silently() -> None:
    items = {"3d-cli/thingiverse-token": "t"}
    store = credentials.KeychainStore(*_fake_keychainlib(items))
    assert store.get("3d-cli/thingiverse-token") == "t" and store.get("3d-cli/cults3d-key") is None
    store.set("3d-cli/cults3d-key", "k", label="x")
    assert store.delete("3d-cli/cults3d-key") and not store.delete("3d-cli/cults3d-key")
    assert credentials.KeychainStore(*_fake_keychainlib(items, locked=True)).get(
        "3d-cli/thingiverse-token") is None
    with pytest.raises(MarketplaceError) as err:
        credentials.KeychainStore(*_fake_keychainlib(items, locked=True)).set("3d-cli/cults3d-key", "k", "x")
    assert (err.value.site, err.value.kind) == ("cults", "auth")  # the site, not the `3d-cli` prefix


def test_keychain_off_or_not_installed_means_no_store(monkeypatch: pytest.MonkeyPatch) -> None:
    assert credentials.secret_store() is None  # THREED_KEYCHAIN=off from the fixture
    monkeypatch.delenv("THREED_KEYCHAIN")
    monkeypatch.setattr(credentials, "_keychain_store", lambda: None)
    assert credentials.secret_store() is None
    with pytest.raises(MissingDependency, match="keychain-cli"):
        credentials.require_store()


def test_keychainlib_is_found_through_the_installer_symlink_only_in_a_real_clone(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """install.sh links ~/.local/bin/keychain into the clone; an unrelated `keychain` (the
    ssh-agent manager) on PATH must not be imported from."""
    monkeypatch.setitem(sys.modules, "keychainlib", None)  # not importable from the env
    bindir = tmp_path / "bin"
    bindir.mkdir()
    other = tmp_path / "ssh-keychain" / "bin" / "keychain"
    other.parent.mkdir(parents=True)
    other.write_text("#!/bin/sh\n", encoding="utf-8")
    other.chmod(0o755)
    (bindir / "keychain").symlink_to(other)
    monkeypatch.setenv("PATH", str(bindir))
    assert credentials._import_keychainlib() is None
    clone = tmp_path / "keychain-cli"
    (clone / "bin").mkdir(parents=True)
    (clone / "bin" / "keychain").write_text("#!/bin/sh\n", encoding="utf-8")
    (clone / "bin" / "keychain").chmod(0o755)
    (clone / "keychainlib").mkdir()
    (clone / "keychainlib" / "__init__.py").write_text("Keychain = 'clone'\n", encoding="utf-8")
    (clone / "keychainlib" / "errors.py").write_text("", encoding="utf-8")
    (bindir / "keychain").unlink()
    (bindir / "keychain").symlink_to(clone / "bin" / "keychain")
    monkeypatch.delitem(sys.modules, "keychainlib")
    # Record keychainlib.errors as absent so the undo drops the stub this import leaves there.
    monkeypatch.setitem(sys.modules, "keychainlib.errors", types.ModuleType("placeholder"))
    monkeypatch.delitem(sys.modules, "keychainlib.errors")
    monkeypatch.setattr(sys, "path", list(sys.path))
    found = credentials._import_keychainlib()
    assert found is not None and found[0] == "clone"


def _run_models(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str],
                stdin: str = "") -> tuple[int, str]:
    from commands import models

    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = models.run(argv)
    return code, capsys.readouterr().out


def test_auth_set_status_delete_never_print_values(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    store = FakeStore()
    monkeypatch.delenv("THREED_KEYCHAIN")
    monkeypatch.setattr(credentials, "_keychain_store", lambda: store)
    code, out = _run_models(monkeypatch, capsys, ["auth", "set", "cults"], "nick-x\nkey-secret-x\n")
    assert code == 0 and store.items == {"3d-cli/cults3d-user": "nick-x", "3d-cli/cults3d-key": "key-secret-x"}
    assert "stored 3d-cli/cults3d-key" in out and "secret-x" not in out and "nick-x" not in out
    code, out = _run_models(monkeypatch, capsys, ["auth", "status", "--json"])
    status = json.loads(out)
    assert status["keychain"] is True
    assert status["sites"]["cults"]["api_key"] == ["keychain 3d-cli/cults3d-key"]
    assert status["sites"]["thingiverse"]["token"] == [] and "secret-x" not in out
    code, out = _run_models(monkeypatch, capsys, ["auth", "delete", "cults"])
    assert code == 0 and store.items == {} and "deleted 3d-cli/cults3d-key" in out


def test_auth_set_refuses_an_empty_value_and_unknown_sites(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    store = FakeStore()
    monkeypatch.delenv("THREED_KEYCHAIN")
    monkeypatch.setattr(credentials, "_keychain_store", lambda: store)
    with pytest.raises(UsageError, match="empty"):
        _run_models(monkeypatch, capsys, ["auth", "set", "thingiverse"], "\n")
    with pytest.raises(InvalidArgument):
        _run_models(monkeypatch, capsys, ["auth", "set", "printables"])
    assert store.items == {}


def test_ctrl_d_at_the_auth_set_prompt_is_the_empty_value_error(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    store = FakeStore()
    monkeypatch.delenv("THREED_KEYCHAIN")
    monkeypatch.setattr(credentials, "_keychain_store", lambda: store)

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    def eof(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("getpass.getpass", eof)
    monkeypatch.setattr(sys, "stdin", Tty())
    from commands import models
    with pytest.raises(UsageError, match="empty"):
        models.run(["auth", "set", "thingiverse"])
    assert store.items == {}


def test_concurrent_first_keychain_lookups_all_see_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lookup mutates sys.path; racing callers must not cache a false 'no keychain'."""
    import threading
    import time

    calls: list[int] = []

    def slow_import() -> tuple[type, types.ModuleType] | None:
        if calls:
            return None  # what a second, unserialized caller would see mid-import
        calls.append(1)
        time.sleep(0.05)
        return _fake_keychainlib({})

    monkeypatch.setattr(credentials, "_import_keychainlib", slow_import)
    credentials._load_keychain_store.cache_clear()
    seen: list[object] = []
    threads = [threading.Thread(target=lambda: seen.append(credentials._keychain_store())) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    credentials._load_keychain_store.cache_clear()
    assert len(calls) == 1 and all(s is not None for s in seen) and len({id(s) for s in seen}) == 1


def test_auth_set_without_keychain_cli_is_a_missing_dependency(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("THREED_KEYCHAIN")
    monkeypatch.setattr(credentials, "_keychain_store", lambda: None)
    with pytest.raises(MissingDependency):
        _run_models(monkeypatch, capsys, ["auth", "set", "thingiverse"], "tok\n")
    code, out = _run_models(monkeypatch, capsys, ["auth", "status"])
    assert code == 0 and "keychain: not available (keychain-cli not installed)" in out


def test_switched_off_keychain_is_reported_as_off_not_missing(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(credentials, "_keychain_store", lambda: FakeStore())
    with pytest.raises(UsageError, match="switched off"):
        _run_models(monkeypatch, capsys, ["auth", "set", "thingiverse"], "tok\n")
    code, out = _run_models(monkeypatch, capsys, ["auth", "status"])
    assert code == 0 and "keychain: switched off (THREED_KEYCHAIN=off)" in out


def test_a_broken_auth_json_is_the_sites_auth_error(tmp_path: Path) -> None:
    auth = tmp_path / "xdg" / "3d-cli" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("{not json", encoding="utf-8")
    with pytest.raises(MarketplaceError) as err:
        credentials.thingiverse_token()
    assert (err.value.site, err.value.kind) == ("thingiverse", "auth")
    assert "3d models auth set thingiverse" in err.value.remediation[0]
    assert credentials.sources(credentials.FIELDS["thingiverse"][0]) == ["auth.json unreadable"]


def test_a_refused_file_token_is_an_auth_error(tmp_path: Path) -> None:
    """A file fetched with the user's token that answers 401 is kind auth (exit 2), not http."""
    with MarketplaceServer() as server:
        with pytest.raises(MarketplaceError) as err:
            transport.download("thingiverse", f"{server.base}/status/401", tmp_path / "f.stl",
                               headers={"Authorization": "Bearer wrong"})
        with pytest.raises(MarketplaceError) as anon:
            transport.download("thingiverse", f"{server.base}/status/401", tmp_path / "f.stl")
    assert (err.value.kind, err.value.exit_code) == ("auth", 2)
    assert anon.value.kind == "http"


def test_impersonated_fetch_uses_axe_curl_build_with_headers_off_argv(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary, log = write_fake_curl(tmp_path / "axe-bin")
    monkeypatch.setenv("AXE_CURL_IMPERSONATE_DIR", str(binary.parent))
    assert transport.curl_impersonate_binary() == str(binary)
    with MarketplaceServer() as server:
        monkeypatch.setenv("THREED_MAKERWORLD_URL", f"{server.base}/makerworld")
        design = makerworld.show("1871683")
        auth_seen = [hdrs.get("Authorization") for _, _, hdrs in server.requests]
        monkeypatch.setenv("MAKERWORLD_TOKEN", "secret-token")
        with pytest.raises(MarketplaceError) as err:
            makerworld.download_links(design)
    assert design.title.startswith("Modern Angular")
    assert log.read_text().count("/makerworld/") == 2
    assert auth_seen == [None]
    assert err.value.kind == "auth"  # loopback wants MAKERWORLD_TOKEN=mw-test-token
    assert "secret-token" not in log.read_text()


def test_impersonated_fetch_without_any_lane_is_a_missing_dependency(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # no curl_chrome*, no uv, no python3
    monkeypatch.setenv("PY3D_NO_UV", "1")
    monkeypatch.setattr("cli.pyrun._venv_has_deps", lambda *_: False)
    with pytest.raises(MissingDependency):
        transport.fetch_impersonated("makerworld", "https://makerworld.com/api")


def test_download_writes_files_and_model_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with MarketplaceServer() as server:
        monkeypatch.setenv("THREED_PRINTABLES_API", f"{server.base}/printables/graphql/")
        model, target, files = catalog.download(SPOON_URL, tmp_path / "out")
    meta = json.loads((target / "model.json").read_text(encoding="utf-8"))
    assert (target / "spoon_handle.stl").read_bytes() == FILES["spoon_handle.stl"]
    assert meta["source"] == {"site": "printables", "id": "311253", "url": SPOON_URL}
    assert meta["author"]["name"] == "cluckOmatic" and meta["license"] == "CC0"
    assert files[0].name == "spoon_handle.stl" and len(files[0].sha256) == 64
    assert model.title == "Spoon Drawer Pull"


def test_unique_names_never_collide_with_model_json() -> None:
    used = {"model.json"}
    assert [catalog._unique(n, used) for n in ("a.stl", "a.stl", "model.json")] == [
        "a.stl", "a-2.stl", "model-2.json"]


def test_unique_names_ignore_case_for_macos_and_windows() -> None:
    used = {"model.json"}
    assert [catalog._unique(n, used) for n in ("Part.stl", "part.stl", "Model.json")] == [
        "Part.stl", "part-2.stl", "Model-2.json"]


def test_safe_name_clips_by_utf8_bytes_and_keeps_the_extension() -> None:
    name = safe_name("模型" * 100 + ".3mf")
    assert len(name.encode("utf-8")) <= 200 and name.endswith(".3mf") and name.startswith("模型")
    assert safe_name("a" * 50 + ".stl") == "a" * 50 + ".stl"


@pytest.mark.parametrize(("headers", "follows"), [
    ({}, True), ({"Cookie": "token=t"}, False), ({"authorization": "Bearer t"}, False)])
def test_impersonated_fetch_never_follows_redirects_with_a_credential(
        monkeypatch: pytest.MonkeyPatch, headers: dict[str, str], follows: bool) -> None:
    """curl cannot pin headers to the first host the way the urllib lane does, so with a
    token aboard a redirect is not followed at all."""
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "200 https://makerworld.com/x", "")

    monkeypatch.setattr(transport, "curl_impersonate_binary", lambda: "/opt/curl_chrome146")
    monkeypatch.setattr(transport.subprocess, "run", fake_run)
    transport.fetch_impersonated("makerworld", "https://makerworld.com/x", headers=headers)
    assert ("-L" in seen[0]) is follows


def test_busy_dir_is_refused_before_any_request_even_without_credentials(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A non-empty --dir is a usage error (exit 2) even when the site would answer auth/paid."""
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "keep.txt").write_text("mine", encoding="utf-8")
    with MarketplaceServer() as server:
        monkeypatch.setenv("THREED_THINGIVERSE_API", f"{server.base}/thingiverse")
        with pytest.raises(UsageError, match="not empty"):
            catalog.download("https://www.thingiverse.com/thing:5594736", busy)
        assert server.requests == []


def test_existing_empty_dir_is_filled_in_place_keeping_its_mode(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The user's empty 0700 dir (here also the current directory, `--dir .`) is filled, not
    replaced: same inode, same mode, no staging dir left behind."""
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    inode = private.stat().st_ino
    monkeypatch.chdir(private)
    with MarketplaceServer() as server:
        monkeypatch.setenv("THREED_PRINTABLES_API", f"{server.base}/printables/graphql/")
        catalog.download(SPOON_URL, Path("."))
    assert private.stat().st_ino == inode and private.stat().st_mode & 0o777 == 0o700
    assert sorted(p.name for p in private.iterdir()) == ["model.json", "spoon_handle.stl"]


def test_download_into_a_symlinked_empty_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with MarketplaceServer() as server:
        monkeypatch.setenv("THREED_PRINTABLES_API", f"{server.base}/printables/graphql/")
        catalog.download(SPOON_URL, link)
    assert link.is_symlink() and (real / "spoon_handle.stl").is_file()


def test_a_file_that_appears_meanwhile_is_never_overwritten(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Another writer drops spoon_handle.stl into the empty --dir mid-download: publishing
    refuses to replace it, and this run's own files are rolled back."""
    out = tmp_path / "out"
    out.mkdir()
    real_download = transport.download

    def racing_download(site: str, url: str, dest: Path, **kw: object) -> int:
        size = real_download(site, url, dest, **kw)  # type: ignore[arg-type]
        (out / "spoon_handle.stl").write_text("theirs", encoding="utf-8")
        return size

    monkeypatch.setattr(transport, "download", racing_download)
    with MarketplaceServer() as server:
        monkeypatch.setenv("THREED_PRINTABLES_API", f"{server.base}/printables/graphql/")
        with pytest.raises(UsageError, match="could not move spoon_handle.stl"):
            catalog.download(SPOON_URL, out)
    assert [p.name for p in out.iterdir()] == ["spoon_handle.stl"]
    assert (out / "spoon_handle.stl").read_text(encoding="utf-8") == "theirs"


def test_dir_under_a_file_is_a_usage_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    with MarketplaceServer() as server:
        monkeypatch.setenv("THREED_PRINTABLES_API", f"{server.base}/printables/graphql/")
        with pytest.raises(UsageError):
            catalog.download(SPOON_URL, blocker / "sub")
    assert blocker.read_text(encoding="utf-8") == "x"


def test_credential_headers_stay_on_the_first_host_across_a_redirect(tmp_path: Path) -> None:
    """A file link that hands off to another host (a CDN) must not carry the user's token."""
    dest = tmp_path / "f.stl"
    with MarketplaceServer() as server:
        size = transport.download("makerworld", f"{server.base}/redirect/spoon_handle.stl", dest,
                                  headers={"Authorization": "Bearer secret", "Cookie": "token=secret"})
    sent = [(path, {k.lower() for k in hdrs}) for _, path, hdrs in server.requests]
    assert [path for path, _ in sent] == ["/redirect/spoon_handle.stl", "/files/spoon_handle.stl"]
    assert {"authorization", "cookie"} <= sent[0][1]
    assert not {"authorization", "cookie"} & sent[1][1]
    assert dest.read_bytes() == FILES["spoon_handle.stl"] and size == len(FILES["spoon_handle.stl"])


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x.stl", "/etc/passwd"])
def test_non_http_links_from_a_site_are_refused(tmp_path: Path, url: str) -> None:
    dest = tmp_path / "x.stl"
    with pytest.raises(MarketplaceError) as err:
        transport.download("thingiverse", url, dest)
    assert err.value.kind == "parse" and not dest.exists()
    with pytest.raises(MarketplaceError, match="non-HTTP URL"):
        transport.fetch("thingiverse", url)


def test_dropped_connection_mid_download_leaves_nothing_behind(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The second file of a Thingiverse model arrives short of its Content-Length: the
    download fails as a network error and neither the target nor the staging dir remains."""
    out = tmp_path / "out"
    with MarketplaceServer() as server:
        server.truncated.add("spoon_pull_v2.stl")
        monkeypatch.setenv("THREED_THINGIVERSE_API", f"{server.base}/thingiverse")
        monkeypatch.setenv("THINGIVERSE_TOKEN", THINGIVERSE_TOKEN)
        with pytest.raises(MarketplaceError) as err:
            catalog.download("https://www.thingiverse.com/thing:5594736", out)
    assert err.value.kind == "network" and "truncated" in err.value.reason
    assert not out.exists() and not list(tmp_path.rglob(".part-*"))


def test_publish_without_hard_links_copies_and_cleans_up_a_failed_copy(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """exFAT/SMB have no hard links: the exclusive-create copy is used, and a copy that dies
    part-way (disk full) leaves no half-written file in the target."""
    src, dst = tmp_path / "src.stl", tmp_path / "dst.stl"
    src.write_bytes(b"solid")

    def no_links(*_: object) -> None:
        raise PermissionError("no hard links here")

    monkeypatch.setattr(catalog.os, "link", no_links)
    catalog._publish(src, dst)
    assert dst.read_bytes() == b"solid" and not src.exists()
    src.write_bytes(b"solid")
    (tmp_path / "dst2.stl").unlink(missing_ok=True)

    def disk_full(*_: object, **__: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(catalog.shutil, "copyfileobj", disk_full)
    with pytest.raises(OSError):
        catalog._publish(src, tmp_path / "dst2.stl")
    assert not (tmp_path / "dst2.stl").exists() and src.exists()


def test_an_error_answer_whose_body_breaks_is_still_structured() -> None:
    """A 500 whose body read itself fails maps by status, never escapes as a traceback."""

    class BrokenBody(urllib.error.HTTPError):
        def read(self, *_: object) -> bytes:
            raise http.client.IncompleteRead(b"")

    exc = BrokenBody("https://x/y", 500, "boom", email.message.Message(), None)
    assert transport._error_body(exc) == b""


def test_download_refuses_a_file_or_non_empty_dir_before_any_request(tmp_path: Path) -> None:
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "keep.txt").write_text("mine", encoding="utf-8")
    a_file = tmp_path / "a-file"
    a_file.write_text("x", encoding="utf-8")
    for target, reason in ((busy, "not empty"), (a_file, "not a directory")):
        with pytest.raises(UsageError, match=reason):
            catalog._check_target(target)
    assert (busy / "keep.txt").read_text(encoding="utf-8") == "mine"
    catalog._check_target(tmp_path / "new")  # absent: fine
