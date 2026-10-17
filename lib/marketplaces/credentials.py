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
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from errors import MissingDependency
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
    site = next(f.site for fields in FIELDS.values() for f in fields if f.keychain == ref)
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
    if not (root / "keychainlib" / "__init__.py").is_file():
        return None  # another `keychain` (the ssh-agent manager of the same name)
    sys.path.append(str(root))
    try:
        from keychainlib import Keychain, errors
        return Keychain, errors
    except ImportError:
        sys.path.remove(str(root))
        return None


def secret_store() -> SecretStore | None:
    """The keychain store, or None when keychain-cli is not installed or THREED_KEYCHAIN=off
    (tests set it so a developer's real keychain never leaks into a run)."""
    if os.environ.get("THREED_KEYCHAIN", "").strip().lower() in ("off", "0", "no"):
        return None
    return _keychain_store()


@lru_cache(maxsize=1)
def _keychain_store() -> SecretStore | None:
    found = _import_keychainlib()
    if found is None:
        return None
    try:
        return KeychainStore(*found)
    except Exception:  # noqa: BLE001 — a broken policy/store must not break `3d models`
        return None


def _value(field: Field) -> str:
    env = os.environ.get(field.env, "").strip()
    if env:
        return env
    store = secret_store()
    if store is not None:
        stored = (store.get(field.keychain) or "").strip()
        if stored:
            return stored
    return str(read_auth_section(field.site).get(field.key) or "").strip()


def sources(field: Field) -> list[str]:
    """Which sources hold `field`, in lookup order — names only, never values."""
    found = []
    if os.environ.get(field.env, "").strip():
        found.append(f"env {field.env}")
    store = secret_store()
    if store is not None and (store.get(field.keychain) or "").strip():
        found.append(f"keychain {field.keychain}")
    if str(read_auth_section(field.site).get(field.key) or "").strip():
        found.append(f"auth.json {field.site}.{field.key}")
    return found


def require_store() -> SecretStore:
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
