"""Per-site credentials for `3d models`, looked up in this order:

1. an environment variable (THINGIVERSE_TOKEN, CULTS_USERNAME + CULTS_API_KEY, MAKERWORLD_TOKEN);
2. the OS keychain through keychain-cli (`keychainlib`), items `3d-cli/<name>` — stored with
   `3d models auth set SITE`; skipped silently when keychain-cli is not installed or the
   store is unavailable/locked;
3. the site's object in ~/.config/3d-cli/auth.json (the file `3d auth` already keeps at 0600):

    {
      "thingiverse": {"token": "<App Token from thingiverse.com/apps/create>"},
      "cults": {"username": "<cults nick>", "api_key": "<key from cults3d.com/en/api/keys>"},
      "makerworld": {"token": "<value of the `token` cookie of a logged-in makerworld.com>"}
    }

Values never go to argv or output; `auth status` reports only which sources hold a field.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from errors import MissingDependency, ThreeDError, UsageError
from hf_auth import auth_path, read_auth_section
from marketplaces.base import MarketplaceError

THINGIVERSE_APPS_URL = "https://www.thingiverse.com/apps/create"
CULTS_KEYS_URL = "https://cults3d.com/en/api/keys"
KEYCHAIN_CALLER = "3d-cli"  # keychain-cli lets a caller read and write its own `3d-cli/*`


@dataclass(frozen=True)
class Field:
    site: str
    key: str  # auth.json key inside the site's object
    env: str
    keychain: str  # keychain-cli item ref
    prompt: str


FIELDS: dict[str, tuple[Field, ...]] = {
    "thingiverse": (Field("thingiverse", "token", "THINGIVERSE_TOKEN", "3d-cli/thingiverse-token",
                          "Thingiverse App Token"),),
    # Writing to Thingiverse needs the user's OAuth access token (authorize + code exchange,
    # see docs/commands/models.md), not the read-only App Token above. Used by `models publish`.
    "thingiverse-upload": (Field("thingiverse-upload", "token", "THINGIVERSE_UPLOAD_TOKEN",
                                 "3d-cli/thingiverse-upload-token",
                                 "Thingiverse OAuth access token (write scope)"),),
    "cults": (Field("cults", "username", "CULTS_USERNAME", "3d-cli/cults3d-user", "Cults3D username"),
              Field("cults", "api_key", "CULTS_API_KEY", "3d-cli/cults3d-key", "Cults3D API key")),
    "makerworld": (Field("makerworld", "token", "MAKERWORLD_TOKEN", "3d-cli/makerworld-token",
                         "MakerWorld `token` cookie"),),
}


class SecretStore(Protocol):
    name: str

    def get(self, ref: str) -> str | None: ...
    def set(self, ref: str, value: str, label: str) -> None: ...
    def delete(self, ref: str) -> bool: ...


class KeychainStore:
    """keychain-cli's `Keychain` behind the three calls `3d models` needs. Lookups never
    prompt (interactive=False): a locked or absent store just means "not in the keychain"."""

    name = "keychain"

    def __init__(self, keychain_cls: Any, errors: Any) -> None:
        """`keychain_cls` is keychainlib.Keychain, `errors` the keychainlib.errors module."""
        self._errors = errors
        self._reader = keychain_cls(caller=KEYCHAIN_CALLER, interactive=False)
        self._writer = keychain_cls(caller=KEYCHAIN_CALLER)

    def get(self, ref: str) -> str | None:
        try:
            return str(self._reader.get(ref).reveal())
        except self._errors.KeychainError:
            return None

    def set(self, ref: str, value: str, label: str) -> None:
        try:
            self._writer.set(ref, value, label=label)
        except self._errors.KeychainError as exc:
            raise _store_error("store", ref, exc) from exc

    def delete(self, ref: str) -> bool:
        try:
            self._writer.delete(ref)
        except self._errors.NotFound:
            return False
        except self._errors.KeychainError as exc:
            raise _store_error("delete", ref, exc) from exc
        return True


def _store_error(action: str, ref: str, exc: Exception) -> MarketplaceError:
    site = next((f.site for fields in FIELDS.values() for f in fields if f.keychain == ref), "models")
    return MarketplaceError(site, "auth", f"the keychain refused to {action} {ref}: {exc}",
                            remediation=["Run `keychain doctor` to check the secret store."])


def _import_keychainlib() -> tuple[Any, Any] | None:
    """(keychainlib.Keychain, keychainlib.errors) from the environment, else from the clone
    the installer's ~/.local/bin/keychain symlink points into (not a site-package)."""
    try:
        from keychainlib import Keychain, errors
        return Keychain, errors
    except ImportError:
        pass
    exe = shutil.which("keychain")
    if not exe:
        return None
    root = Path(exe).resolve().parent.parent
    if str(root) in sys.path or not (root / "keychainlib" / "__init__.py").is_file():
        return None  # already searched, or another `keychain` (the ssh-agent manager)
    sys.path.append(str(root))
    try:
        from keychainlib import Keychain, errors
        return Keychain, errors
    except ImportError:
        return None
    finally:
        # Both modules are in sys.modules by now; the clone must not shadow later imports.
        sys.path.pop()  # the entry appended above (imports do not reorder sys.path)


def keychain_disabled() -> bool:
    """THREED_KEYCHAIN=off: tests set it so a developer's real keychain never leaks into a run."""
    return os.environ.get("THREED_KEYCHAIN", "").strip().lower() in ("off", "0", "no")


def secret_store() -> SecretStore | None:
    """The keychain store, or None when keychain-cli is not installed or keychain_disabled()."""
    return None if keychain_disabled() else _keychain_store()


_STORE_LOCK = threading.Lock()


def _keychain_store() -> SecretStore | None:
    # One lookup per process. The lock matters: `_import_keychainlib` mutates sys.path, so a
    # concurrent second caller would see the clone already "searched" and cache a false None.
    with _STORE_LOCK:
        return _load_keychain_store()


@lru_cache(maxsize=1)
def _load_keychain_store() -> SecretStore | None:
    found = _import_keychainlib()
    if found is None:
        return None
    try:
        return KeychainStore(*found)
    except Exception:  # noqa: BLE001 — a broken policy/store must not break `3d models`
        return None


def _auth_json(site: str) -> dict[str, Any]:
    """The site's auth.json object; a broken file is this site's auth error, not the
    `3d auth hf` message the shared reader raises."""
    try:
        return read_auth_section(site)
    except ThreeDError as exc:
        raise MarketplaceError(site, "auth", f"cannot read {auth_path()}: {exc.message}",
                               remediation=[f"Fix or remove {auth_path()}, or store the credentials "
                                            f"with `3d models auth set {site}`."]) from exc


def _value(field: Field) -> str:
    env = os.environ.get(field.env, "").strip()
    if env:
        return env
    store = secret_store()
    if store is not None:
        stored = (store.get(field.keychain) or "").strip()
        if stored:
            return stored
    return str(_auth_json(field.site).get(field.key) or "").strip()


def sources(field: Field) -> list[str]:
    """Which sources hold `field`, in lookup order — names only, never values."""
    found = []
    if os.environ.get(field.env, "").strip():
        found.append(f"env {field.env}")
    store = secret_store()
    if store is not None and (store.get(field.keychain) or "").strip():
        found.append(f"keychain {field.keychain}")
    try:
        if str(_auth_json(field.site).get(field.key) or "").strip():
            found.append(f"auth.json {field.site}.{field.key}")
    except MarketplaceError:
        found.append("auth.json unreadable")
    return found


def require_store() -> SecretStore:
    if keychain_disabled():
        raise UsageError("the keychain is switched off (THREED_KEYCHAIN=off)", command="models",
                         remediation=["Unset THREED_KEYCHAIN to use the OS keychain."])
    store = secret_store()
    if store is None:
        raise MissingDependency(
            "keychain-cli (keychainlib)",
            install="curl -fsSL https://git.hyperide.ai/ultrabricks/keychain-cli/raw/branch/main/install.sh | bash",
            degrades="credentials come only from environment variables and auth.json",
            command="models")
    return store


def _where(site: str) -> str:
    fields = FIELDS[site]
    example = json.dumps({site: {f.key: "..." for f in fields}})
    return (f"Store it with `3d models auth set {site}` (OS keychain), export "
            f"{' and '.join(f.env for f in fields)}, or put it in {auth_path()} as {example}.")


def thingiverse_token() -> str:
    token = _value(FIELDS["thingiverse"][0])
    if not token:
        raise MarketplaceError(
            "thingiverse", "auth", "the Thingiverse API needs an App Token (none configured)",
            remediation=[f"Create a free app at {THINGIVERSE_APPS_URL} and copy its App Token.",
                         _where("thingiverse")],
        )
    return token


def thingiverse_upload_token() -> str:
    token = _value(FIELDS["thingiverse-upload"][0])
    if not token:
        raise MarketplaceError(
            "thingiverse", "auth", "publishing to Thingiverse needs an OAuth access token with write "
            "scope (none configured)",
            remediation=[
                f"Create an app at {THINGIVERSE_APPS_URL}, open "
                "https://www.thingiverse.com/login/oauth/authorize?client_id=<id>&redirect_uri=urn:ietf:wg:oauth:2.0:oob"
                "&response_type=code, then POST client_id, client_secret and the code to "
                "https://www.thingiverse.com/login/oauth/access_token and keep the access_token.",
                _where("thingiverse-upload"),
            ],
        )
    return token


def cults_basic_auth() -> tuple[str, str]:
    username, api_key = (_value(f) for f in FIELDS["cults"])
    if not (username and api_key):
        raise MarketplaceError(
            "cults", "auth", "the Cults3D GraphQL API needs a username and API key "
            "(none configured); the website itself is behind a Cloudflare challenge",
            remediation=[f"Log in to Cults3D and create an API key at {CULTS_KEYS_URL}.",
                         _where("cults")],
        )
    return username, api_key


def makerworld_token() -> str:
    token = _value(FIELDS["makerworld"][0])
    if not token:
        raise MarketplaceError(
            "makerworld", "auth", "MakerWorld only serves files to a logged-in account "
            "(no token configured)",
            remediation=[
                "Log in at https://makerworld.com in a browser, copy the value of the `token` "
                "cookie (DevTools -> Application -> Cookies).",
                _where("makerworld"),
            ],
        )
    return token
