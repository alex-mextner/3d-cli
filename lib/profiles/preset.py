"""preset.py — the Orca-family user preset formats, and what of them belongs in the repo.

JSON (ConfigBase::save_to_json): one flat object, keys sorted (nlohmann's std::map), string or
list-of-string values, dumped with an indent then std::endl. Snapmaker Orca 2.x and Bambu Studio
indent 4 spaces, upstream OrcaSlicer main a tab — so a rewrite keeps the file's own indent, and
Python's json.dumps(indent=..., ensure_ascii=False) reproduces those bytes exactly. Files are
otherwise copied byte for byte. `version` is required by the loader, so it is never stripped.

SLICER-LOCAL JSON KEYS: the printer connection (host, API key, password, CA file) — the set Orca
itself refuses to carry over from a project (Preset.cpp `load_full_config` filter). They stay
only in the slicer dir: the repo copy omits them and a push puts the slicer's values back.

`.info` (Preset::save_info): `sync_info`, `user_id`, `setting_id`, `base_id`, `updated_time`
lines. Only `base_id` (the parent system preset's id, e.g. GP006) is portable; the rest is cloud
sync state for this account/machine. load_info reads whatever keys are present, so the repo
`.info` holds `base_id` alone, and a push writes the full five lines as a GUI save would.
"""

from __future__ import annotations

import json
from typing import Any

from errors import ThreeDError

LOCAL_KEYS = frozenset(
    {
        "print_host",
        "print_host_webui",
        "printhost_apikey",
        "printhost_cafile",
        "printhost_user",
        "printhost_password",
        "printhost_port",
    }
)
INFO_KEYS = ("sync_info", "user_id", "setting_id", "base_id", "updated_time")


def load(raw: bytes, *, where: str) -> dict[str, Any]:
    fix = ["Fix or remove the file; the slicer writes one JSON object per preset."]
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ThreeDError(
            f"{where} is not a readable preset: {exc}",
            command="profiles",
            remediation=fix,
        ) from exc
    if not isinstance(obj, dict):
        raise ThreeDError(
            f"{where} is not a preset: the top level is not a JSON object",
            command="profiles",
            remediation=fix,
        )
    return obj


def indent_of(raw: bytes) -> str:
    """The indent unit the file was written with (default 4 spaces)."""
    lines = raw.decode("utf-8").splitlines()
    if len(lines) > 1:
        second = lines[1]
        unit = second[: len(second) - len(second.lstrip(" \t"))]
        if unit:
            return unit
    return "    "


def dump(obj: dict[str, Any], indent: str) -> bytes:
    return (json.dumps(obj, indent=indent, ensure_ascii=False) + "\n").encode("utf-8")


def with_keys(obj: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """`obj` with `extra` set; new keys go where the slicer would put them (sorted order) when
    the file is sorted, else at the end."""
    merged = {**obj, **extra}
    return dict(sorted(merged.items())) if list(obj) == sorted(obj) else merged


def portable_json(raw: bytes, *, where: str) -> bytes:
    """The repo form of a slicer preset: the same bytes, minus slicer-local keys."""
    obj = load(raw, where=where)
    if not LOCAL_KEYS & obj.keys():
        return raw
    return dump({k: v for k, v in obj.items() if k not in LOCAL_KEYS}, indent_of(raw))


def local_keys_in(raw: bytes, *, where: str) -> list[str]:
    return sorted(LOCAL_KEYS & load(raw, where=where).keys())


def merge_local(repo_raw: bytes, slicer_raw: bytes | None, *, where: str) -> bytes:
    """What a push writes: the repo preset plus the slicer's own local keys, if it has any."""
    if slicer_raw is None:
        return repo_raw
    local = {k: v for k, v in load(slicer_raw, where=where).items() if k in LOCAL_KEYS}
    if not local:
        return repo_raw
    return dump(with_keys(load(repo_raw, where=where), local), indent_of(repo_raw))


def parse_info(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def portable_info(text: str) -> bytes | None:
    """The repo form of a `.info`: `base_id` alone, or None when it is empty."""
    base_id = parse_info(text).get("base_id", "")
    return f"base_id = {base_id}\n".encode() if base_id and base_id != "null" else None


def pushed_info(existing: str | None, repo_info: bytes | None, now: int) -> str:
    """The `.info` a push writes, as a GUI save would: keep this account's sync state, take
    `base_id` from the repo, stamp `updated_time` (so a cloud copy does not win over the push),
    and mark an already cloud-synced preset for upload."""
    fields = dict.fromkeys(INFO_KEYS, "")
    if existing is not None:
        fields.update({k: v for k, v in parse_info(existing).items() if k in fields})
    fields["base_id"] = (
        parse_info(repo_info.decode("utf-8")).get("base_id", "") if repo_info else ""
    )
    fields["updated_time"] = str(now)
    if fields["setting_id"] and not fields["sync_info"]:
        fields["sync_info"] = "update"
    return "".join(f"{k} = {fields[k]}\n" for k in INFO_KEYS)


def deleted_info(existing: str) -> str | None:
    """Preset::remove_files: a cloud-synced preset keeps its `.info` marked `sync_info = delete`
    until the cloud confirms; a local-only one loses it (None)."""
    fields = parse_info(existing)
    if not fields.get("setting_id"):
        return None
    fields = {
        **dict.fromkeys(INFO_KEYS, ""),
        **{k: v for k, v in fields.items() if k in INFO_KEYS},
    }
    fields["sync_info"] = "delete"
    return "".join(f"{k} = {fields[k]}\n" for k in INFO_KEYS)
