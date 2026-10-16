"""ha_deploy.py — `3d ha deploy`: publish integrations/homeassistant/ to the live HA config.

The HA repo carries 3d-cli as a git submodule; manifest dests are relative symlinks into it
(www/ files are byte-identical copies, see integrations/homeassistant/manifest.yaml). A deploy
pins the submodule to one 3d-cli commit and converges the dests, then proves the result:

  plan    probe the host (ha_deploy_probe.py over ssh, read-only) and compare it with the
          manifest at --ref: submodule bump, symlinks/copies to (re)write, packages to reload,
          compose services to recreate; orphans, live edits, missing configuration.yaml
          snippets, a compose file that doesn't include the 3D services, vendor drift and
          anything that would need an HA restart block the deploy.
  apply   backup tar -> submodule checkout -> converge -> check_config (rollback on failure)
          -> targeted reloads via the HA API -> recreate changed 3D services -> post-checks
          (printer entities, dashboard, go2rtc; rollback on failure) -> deployed marker ->
          HA-repo branch + PR with the new pointer (the live checkout may run ahead of main
          until that PR merges; the changes stay staged so main fast-forwards afterwards).

Never restarts Home Assistant: a change that needs one stops the deploy.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
import yaml

from errors import GateFailure, InvalidArgument, UsageError
from ha_deploy_probe import ProbeArgs, VendorPin

INTEGRATION = "integrations/homeassistant"
MANIFEST = f"{INTEGRATION}/manifest.yaml"
SNIPPET = f"{INTEGRATION}/snippets/configuration.3d.yaml"
COMPOSE = f"{INTEGRATION}/compose/3d.docker-compose.yaml"
VENDOR_LOCK = f"{INTEGRATION}/vendor.lock"
PROBE = Path(__file__).with_name("ha_deploy_probe.py")
POLL_SECONDS = 5  # between post-check polls after a reload or recreate
TOKEN_ENV = "HA_TOKEN"
BRANCH_PREFIX = "3d-deploy/"
CMD = "ha"

# HA config domains whose YAML can be re-read without a restart, and the service that does it.
RELOADABLE: dict[str, str] = {
    d: f"{d}.reload"
    for d in (
        "automation",
        "script",
        "template",
        "rest",
        "rest_command",
        "command_line",
        "scene",
        "group",
        "input_boolean",
        "input_button",
        "input_datetime",
        "input_number",
        "input_select",
        "input_text",
        "timer",
        "counter",
        "schedule",
        "zone",
        "person",
    )
}
# Domains configured as a list of `platform:` items; the platform decides the reload.
PLATFORM_DOMAINS = ("sensor", "binary_sensor")
PLATFORM_RELOADS = {
    "template": "template.reload",
    "rest": "rest.reload",
    "command_line": "command_line.reload",
}
UNAVAILABLE = ("unavailable",)
ROLLBACK_HINT = "The backup tar restores every touched path: tar -xzf BACKUP -C HA_DIR"


# ---- manifest -------------------------------------------------------------------------------
@dataclass(frozen=True)
class FileEntry:
    src: str
    dest: str
    mode: str  # "symlink" | "copy"
    services: tuple[str, ...] = ()


@dataclass(frozen=True)
class HaRepo:
    host: str
    dir: str
    github: str
    origin: str
    submodule: str
    submodule_url: str
    container: str
    compose_include: str
    compose_file: str
    configuration: str
    deployed_marker: str
    backups: str
    entity_prefixes: tuple[str, ...]
    dashboard_path: str
    ha_url: str
    go2rtc_api: str
    go2rtc_stream: str


@dataclass(frozen=True)
class Manifest:
    ha_repo: HaRepo
    files: tuple[FileEntry, ...]
    internal: tuple[str, ...]

    def is_internal(self, rel: str) -> bool:
        return any(
            rel == i or (i.endswith("/") and rel.startswith(i)) for i in self.internal
        )


def _require_str(data: dict[str, object], key: str, where: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise GateFailure(
            f"{MANIFEST}: {where}.{key} must be a non-empty string", command=CMD
        )
    return value


def load_manifest(text: str) -> Manifest:
    """Parse and validate manifest.yaml. Raises GateFailure on a malformed manifest."""
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise GateFailure(f"{MANIFEST}: expected a mapping", command=CMD)
    repo_raw = data.get("ha_repo")
    if not isinstance(repo_raw, dict):
        raise GateFailure(f"{MANIFEST}: ha_repo must be a mapping", command=CMD)
    s = {
        k: _require_str(repo_raw, k, "ha_repo")
        for k in (
            "host",
            "dir",
            "github",
            "submodule",
            "submodule_url",
            "container",
            "compose_include",
            "compose_file",
            "configuration",
            "deployed_marker",
            "backups",
            "dashboard_path",
            "ha_url",
            "go2rtc_api",
            "go2rtc_stream",
        )
    }
    prefixes = repo_raw.get("entity_prefixes")
    if not isinstance(prefixes, list) or not all(isinstance(p, str) for p in prefixes):
        raise GateFailure(
            f"{MANIFEST}: ha_repo.entity_prefixes must be a list of strings",
            command=CMD,
        )
    origin = repo_raw.get("origin") or f"https://github.com/{s['github']}.git"
    ha_repo = HaRepo(origin=str(origin), entity_prefixes=tuple(prefixes), **s)
    files: list[FileEntry] = []
    seen_dest: set[str] = set()
    for i, raw in enumerate(data.get("files") or []):
        where = f"files[{i}]"
        if not isinstance(raw, dict):
            raise GateFailure(f"{MANIFEST}: {where} must be a mapping", command=CMD)
        src, dest, mode = (_require_str(raw, k, where) for k in ("src", "dest", "mode"))
        if mode not in ("symlink", "copy"):
            raise GateFailure(
                f"{MANIFEST}: {where}.mode={mode!r}; accepted: symlink, copy",
                command=CMD,
            )
        if dest in seen_dest:
            raise GateFailure(f"{MANIFEST}: dest {dest} is listed twice", command=CMD)
        if (
            os.path.isabs(dest)
            or ".." in Path(dest).parts
            or os.path.isabs(src)
            or ".." in Path(src).parts
        ):
            raise GateFailure(
                f"{MANIFEST}: {where} paths must be relative and stay inside",
                command=CMD,
            )
        seen_dest.add(dest)
        services = raw.get("services") or []
        if not isinstance(services, list) or not all(
            isinstance(x, str) for x in services
        ):
            raise GateFailure(
                f"{MANIFEST}: {where}.services must be a list of strings", command=CMD
            )
        files.append(FileEntry(src, dest, mode, tuple(services)))
    internal = data.get("internal") or []
    if not isinstance(internal, list) or not all(isinstance(x, str) for x in internal):
        raise GateFailure(
            f"{MANIFEST}: internal must be a list of strings", command=CMD
        )
    return Manifest(ha_repo, tuple(files), tuple(internal))


def manifest_orphans(manifest: Manifest, tree: list[str]) -> list[str]:
    """Both directions: every src exists in the tree, every tree file is a src or internal."""
    present = set(tree)
    srcs = {f.src for f in manifest.files}
    problems = [
        f"manifest src {s} is not in {INTEGRATION}/" for s in sorted(srcs - present)
    ]
    problems += [
        f"{INTEGRATION}/{rel} is not in the manifest (add it to files or internal)"
        for rel in sorted(present - srcs)
        if not manifest.is_internal(rel)
    ]
    return problems


def symlink_target(manifest: Manifest, entry: FileEntry) -> str:
    """Relative link from the dest's directory to the src inside the submodule."""
    target = f"{manifest.ha_repo.submodule}/{INTEGRATION}/{entry.src}"
    return os.path.relpath(target, os.path.dirname(entry.dest) or ".")


# ---- HA YAML ------------------------------------------------------------------------------
@dataclass(frozen=True)
class Tagged:
    """A custom-tagged HA YAML node (!secret x, !include y), compared by tag and value."""

    tag: str
    value: object


class HaLoader(yaml.SafeLoader):
    """SafeLoader that keeps HA's custom tags as Tagged values instead of failing on them."""


def _tagged(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Tagged:
    if isinstance(node, yaml.ScalarNode):
        return Tagged("!" + suffix, loader.construct_scalar(node))
    if isinstance(node, yaml.SequenceNode):
        return Tagged("!" + suffix, tuple(loader.construct_sequence(node)))
    if isinstance(node, yaml.MappingNode):
        return Tagged("!" + suffix, tuple(loader.construct_mapping(node).items()))
    raise yaml.constructor.ConstructorError(None, None, f"unsupported node for !{suffix}", node.start_mark)


HaLoader.add_multi_constructor("!", _tagged)


def load_ha_yaml(text: str) -> object:
    return yaml.load(text, Loader=HaLoader)  # noqa: S506 — HaLoader is a SafeLoader subclass


def snippet_missing(expected: object, live: object, path: str = "") -> list[str]:
    """What of `expected` the live config lacks: mapping keys recursively, list items by equality."""
    where = path or "<root>"
    if isinstance(expected, dict):
        if not isinstance(live, dict):
            return [f"{where}: expected a mapping"]
        out: list[str] = []
        for key, value in expected.items():
            if key not in live:
                out.append(f"{path}{key}: missing")
            else:
                out += snippet_missing(value, live[key], f"{path}{key}.")
        return out
    if isinstance(expected, list):
        if not isinstance(live, list):
            return [f"{where}: expected a list"]
        return [
            f"{where.rstrip('.')}: missing item {item!r}"
            for item in expected
            if item not in live
        ]
    return (
        []
        if expected == live
        else [f"{where.rstrip('.')}: live {live!r} != {expected!r}"]
    )


def compose_problems(
    text: str | None, include: str, services: list[str], name: str
) -> list[str]:
    if text is None:
        return [f"{name}: not found on the host"]
    data = load_ha_yaml(text)
    if not isinstance(data, dict):
        return [f"{name}: not a mapping"]
    paths: list[str] = []
    for item in data.get("include") or []:
        if isinstance(item, str):
            paths.append(item)
        elif isinstance(item, dict):
            raw = item.get("path")
            paths += (
                [raw]
                if isinstance(raw, str)
                else [p for p in raw or [] if isinstance(p, str)]
            )
    problems = (
        [] if include in paths else [f"{name}: `include:` does not list {include}"]
    )
    own = set((data.get("services") or {}).keys())
    problems += [
        f"{name}: service {s} is defined there too; it must come only from the include"
        for s in services
        if s in own
    ]
    return problems


def package_reloads(text: str) -> tuple[set[str], list[str]]:
    """Reload services a package needs, plus the domains that would need an HA restart."""
    data = load_ha_yaml(text)
    reloads: set[str] = set()
    restart: list[str] = []
    if not isinstance(data, dict):
        return reloads, restart
    for domain, body in data.items():
        if domain in RELOADABLE:
            reloads.add(RELOADABLE[domain])
        elif domain in PLATFORM_DOMAINS and isinstance(body, list):
            for item in body:
                platform = item.get("platform") if isinstance(item, dict) else None
                if isinstance(platform, str) and platform in PLATFORM_RELOADS:
                    reloads.add(PLATFORM_RELOADS[platform])
                else:
                    restart.append(f"{domain} platform {platform!r}")
        else:
            restart.append(str(domain))
    return reloads, restart


# ---- local source (3d-cli at --ref) ---------------------------------------------------------
def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        raise GateFailure(
            f"git {' '.join(args)} failed: {proc.stderr.strip()}", command=CMD
        )
    return proc.stdout.strip()


def resolve_ref(repo: Path, ref: str) -> str:
    if ref.startswith("origin/"):
        _git(repo, "fetch", "--quiet", "origin", check=False)
    sha = _git(
        repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False
    )
    if not sha:
        raise InvalidArgument(
            "--ref",
            ref,
            ["a commit, branch or tag in the 3d-cli repo"],
            command=CMD,
            extra=f"`git -C {repo} rev-parse {ref}` found nothing; fetch first?",
        )
    return sha


class Source:
    """integrations/homeassistant/ as committed at one 3d-cli commit."""

    def __init__(self, repo: Path, sha: str) -> None:
        self.repo, self.sha = repo, sha

    def files(self) -> list[str]:
        out = _git(
            self.repo, "ls-tree", "-r", "--name-only", self.sha, "--", INTEGRATION
        )
        return [line[len(INTEGRATION) + 1 :] for line in out.splitlines() if line]

    def read(self, rel: str) -> bytes:
        proc = subprocess.run(
            ["git", "-C", str(self.repo), "cat-file", "blob", f"{self.sha}:{rel}"],
            capture_output=True,
        )
        if proc.returncode != 0:
            raise GateFailure(f"{rel} is not in 3d-cli {self.sha[:12]}", command=CMD)
        return proc.stdout

    def read_at(self, sha: str, rel: str) -> bytes | None:
        """The file at another commit, or None when this checkout doesn't have it."""
        proc = subprocess.run(
            ["git", "-C", str(self.repo), "cat-file", "blob", f"{sha}:{rel}"],
            capture_output=True,
        )
        return proc.stdout if proc.returncode == 0 else None


# ---- host -------------------------------------------------------------------------------
class Host:
    """Runs commands on the HA host over ssh, or locally for `--host local`."""

    def __init__(self, target: str) -> None:
        if not target or target.startswith("-"):
            raise InvalidArgument("--host", target, ["local", "[user@]host"], command=CMD,
                                  extra="A host may not start with '-' (ssh would read it as an option).")
        self.target = target
        self.local = target == "local"

    def _argv(self, remote_cmd: str) -> list[str]:
        if self.local:
            return ["bash", "-c", remote_cmd]
        return [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            "--",
            self.target,
            remote_cmd,
        ]

    def sh(
        self, script: str, *, check: bool = True, timeout: int = 600
    ) -> subprocess.CompletedProcess[str]:
        cmd = "bash -c " + shlex.quote("set -euo pipefail\n" + script)
        try:
            proc = subprocess.run(
                self._argv(cmd), capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise GateFailure(
                f"{self.target}: command timed out after {timeout}s", command=CMD
            ) from exc
        if check and proc.returncode != 0:
            raise GateFailure(
                f"{self.target}: command failed (rc={proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()[-2000:]}",
                command=CMD,
            )
        return proc

    def lock(self, ha_dir: str) -> str:
        """Take the host-wide deploy lock (a directory in the HA repo's .git); one deploy at a time,
        so a rollback can never restore an older backup over a newer deploy."""
        path = f"{ha_dir}/.git/3d-ha-deploy.lock"
        proc = self.sh(f"mkdir {_q(path)} && printf '%s\\n' \"$$\" > {_q(path + '/owner')}", check=False)
        if proc.returncode != 0:
            raise GateFailure(f"another `3d ha deploy` holds {self.target}:{path}", command=CMD,
                              remediation=["Wait for it to finish. If none is running (a crashed deploy), "
                                           f"remove the directory: ssh {self.target} rm -r {path}"])
        return path

    def unlock(self, path: str) -> None:
        self.sh(f"rm -rf {_q(path)}", check=False)

    def probe(self, args: ProbeArgs) -> dict[str, object]:
        payload = json.dumps(args)
        cmd = "python3 - " + shlex.quote(payload)
        proc = subprocess.run(
            self._argv(cmd),
            input=PROBE.read_text(),
            capture_output=True,
            text=True,
            timeout=600,
        )
        if proc.returncode != 0:
            raise GateFailure(
                f"{self.target}: probe failed: {proc.stderr.strip()[-2000:]}",
                command=CMD,
                remediation=[
                    "Check `ssh "
                    + self.target
                    + " true` and that python3 exists there."
                ],
            )
        result = json.loads(proc.stdout)
        if not isinstance(result, dict):
            raise GateFailure(
                f"{self.target}: probe returned {type(result).__name__}", command=CMD
            )
        if result.get("error"):
            raise GateFailure(f"{self.target}: {result['error']}", command=CMD)
        return result




# ---- plan ---------------------------------------------------------------------------------
@dataclass
class Converge:
    entry: FileEntry
    reason: str  # "content" | "link" | "missing"


@dataclass
class Plan:
    sha: str
    live_sha: str | None
    ha_dir: str
    converge: list[Converge] = field(default_factory=list)
    content_changed: list[FileEntry] = field(default_factory=list)
    reloads: list[str] = field(default_factory=list)
    recreate: list[str] = field(default_factory=list)
    compose_up: list[str] = field(default_factory=list)
    fast_forward: int = 0
    problems: list[str] = field(default_factory=list)
    restart: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    signals: dict[str, object] = field(default_factory=dict)
    compose: dict[str, object] = field(default_factory=dict)

    @property
    def bump(self) -> bool:
        return self.live_sha != self.sha

    def changes(self) -> list[str]:
        out: list[str] = []
        if self.fast_forward:
            out.append(
                f"fast-forward the HA checkout to origin/main ({self.fast_forward} commit(s), "
                "working tree unchanged)"
            )
        if self.bump:
            out.append(
                f"3d-cli submodule {(self.live_sha or 'none')[:12]} -> {self.sha[:12]}"
            )
        for c in self.converge:
            what = {
                "content": "content changes",
                "link": "re-point",
                "missing": "create",
            }[c.reason]
            out.append(f"{c.entry.dest}: {what} ({c.entry.mode})")
        changed = {c.entry.dest for c in self.converge}
        out += [
            f"{e.dest}: content changes via the submodule"
            for e in self.content_changed
            if e.dest not in changed
        ]
        if self.reloads:
            out.append("reload: " + ", ".join(self.reloads))
        if self.recreate:
            out.append("recreate (file changed): " + ", ".join(self.recreate))
        if self.compose_up:
            out.append("docker compose up -d: " + ", ".join(self.compose_up))
        return out


def compose_services(source: Source) -> list[str]:
    data = load_ha_yaml(source.read(COMPOSE).decode())
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict) or not services:
        raise GateFailure(f"{COMPOSE}: no services", command=CMD)
    return [str(s) for s in services]


def vendor_pins(source: Source) -> dict[str, dict[str, str]]:
    data = yaml.safe_load(source.read(VENDOR_LOCK))
    if not isinstance(data, dict):
        raise GateFailure(f"{VENDOR_LOCK}: expected a mapping", command=CMD)
    return {
        str(k): {kk: str(vv) for kk, vv in v.items()}
        for k, v in data.items()
        if isinstance(v, dict)
    }


def probe_args(
    manifest: Manifest,
    ha_dir: str,
    sha: str,
    services: list[str],
    vendor: dict[str, dict[str, str]],
    mode: str = "full",
) -> ProbeArgs:
    r = manifest.ha_repo
    dests = [f.dest for f in manifest.files] + [r.compose_include]
    return ProbeArgs(
        mode=mode,
        dir=ha_dir,
        submodule=r.submodule,
        sha=sha,
        dests=dests,
        copy_dests=[f.dest for f in manifest.files if f.mode == "copy"],
        marker=r.deployed_marker,
        configuration=r.configuration,
        compose_file=r.compose_file,
        container=r.container,
        services=services,
        vendor={
            k: VendorPin(domain=v["domain"], submodule=v["submodule"])
            for k, v in vendor.items()
        },
        token_env=TOKEN_ENV,
        ha_url=r.ha_url,
        entity_prefixes=list(r.entity_prefixes),
        dashboard_path=r.dashboard_path,
        go2rtc_api=r.go2rtc_api,
        go2rtc_stream=r.go2rtc_stream,
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _as_dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def build_plan(
    manifest: Manifest,
    source: Source,
    state: dict[str, object],
    ha_dir: str,
    services: list[str],
    vendor: dict[str, dict[str, str]],
) -> Plan:
    r = manifest.ha_repo
    sub = _as_dict(state.get("submodule"))
    head = sub.get("head")
    live_sha = head if isinstance(head, str) else None
    plan = Plan(sha=source.sha, live_sha=live_sha, ha_dir=ha_dir)
    plan.problems += manifest_orphans(manifest, source.files())
    if state.get("fetch_error"):
        plan.warnings.append(
            f"git fetch origin in the HA repo failed: {state['fetch_error']}"
        )

    # Submodule.
    if live_sha is None:
        plan.problems.append(
            f"no {r.submodule} submodule checkout in {ha_dir} "
            '(one-time cutover: docs/commands/ha.md, "Cutover")'
        )
    elif plan.bump and not sub.get("sha_available"):
        plan.problems.append(
            f"3d-cli {source.sha[:12]} is not fetchable from the submodule's origin "
            f"({sub.get('url')}); push it first"
        )
    if sub.get("dirty"):
        plan.problems.append(
            f"{r.submodule}/ has local edits: {sub.get('dirty')}; "
            "commit them to 3d-cli instead"
        )

    # HA checkout behind origin/main (a merged deploy PR).
    sync = _as_dict(state.get("sync"))
    behind = sync.get("behind")
    if isinstance(behind, int) and behind > 0:
        if sync.get("ff_safe"):
            plan.fast_forward = behind
        else:
            plan.notes.append(
                f"HA checkout is {behind} commit(s) behind origin/main; not fast-forwarding: "
                + "; ".join(str(b) for b in _as_list(sync.get("ff_blockers")))
            )

    # Files.
    dests = _as_dict(state.get("dests"))
    # Staged or unstaged differences from HEAD; a copy that the previous deploy wrote (it matches
    # the live submodule commit) is the deploy's own pending change, not a live edit.
    edited = set(map(str, _as_list(state.get("edited"))))
    for entry in manifest.files:
        new_hash = _sha256(source.read(f"{INTEGRATION}/{entry.src}"))
        st = _as_dict(dests.get(entry.dest))
        kind = st.get("kind")
        if kind == "other":
            plan.problems.append(
                f"{entry.dest}: exists on the host but is not a file or symlink"
            )
            continue
        content = st.get("sha256") != new_hash
        previous = source.read_at(live_sha, f"{INTEGRATION}/{entry.src}") if live_sha else None
        own_copy = previous is not None and st.get("sha256") == _sha256(previous)
        if kind == "file" and entry.dest in edited and content and not own_copy:
            plan.problems.append(
                f"{entry.dest}: live file has uncommitted edits that are not in 3d-cli "
                f"{source.sha[:12]}; move them into {INTEGRATION}/{entry.src} first"
            )
            continue
        if content:
            plan.content_changed.append(entry)
        if kind == "missing":
            plan.converge.append(Converge(entry, "missing"))
        elif entry.mode == "symlink" and (
            kind != "symlink" or st.get("target") != symlink_target(manifest, entry)
        ):
            plan.converge.append(Converge(entry, "link"))
        elif entry.mode == "copy" and (kind != "file" or content):
            plan.converge.append(Converge(entry, "content" if content else "link"))

    # Orphans on the host.
    dest_set = {f.dest for f in manifest.files}
    for raw in _as_list(state.get("tracked_links")):
        path = str(_as_list(raw)[0]) if _as_list(raw) else ""
        if path and path not in dest_set:
            plan.problems.append(
                f"{path}: tracked symlink into {r.submodule}/ that the manifest doesn't list"
            )
    for d, names in _as_dict(state.get("copy_dirs")).items():
        plan.problems += [
            f"{d}/{n}: not in the manifest (orphan next to deployed copies)"
            for n in map(str, _as_list(names))
            if f"{d}/{n}" not in dest_set
        ]

    # configuration.yaml snippets and compose include.
    conf = state.get("configuration")
    if not isinstance(conf, str):
        plan.problems.append(f"{r.configuration}: not found on the host")
    else:
        expected = load_ha_yaml(source.read(SNIPPET).decode())
        plan.problems += [
            f"{r.configuration}: {m}"
            for m in snippet_missing(expected, load_ha_yaml(conf))
        ]
    compose_text = state.get("compose_text")
    plan.problems += compose_problems(
        compose_text if isinstance(compose_text, str) else None,
        r.compose_include,
        services,
        r.compose_file,
    )

    # Vendor pins.
    live_vendor = _as_dict(state.get("vendor"))
    for name, pin in vendor.items():
        live = _as_dict(live_vendor.get(name))
        for key in ("version", "commit"):
            if str(live.get(key)) != pin.get(key):
                plan.problems.append(
                    f"vendor.lock {name}.{key}={pin.get(key)} but the host has {live.get(key)}"
                )

    # Reloads, restarts, services.
    reloads: set[str] = set()
    recreate: set[str] = set()
    for entry in plan.content_changed:
        if entry.src.startswith("packages/"):
            texts = [source.read(f"{INTEGRATION}/{entry.src}").decode()]
            old = (
                source.read_at(live_sha, f"{INTEGRATION}/{entry.src}")
                if live_sha
                else None
            )
            if old is not None:
                texts.append(old.decode())
            elif live_sha:
                plan.warnings.append(
                    f"{entry.src}: 3d-cli {live_sha[:12]} is not in this checkout; "
                    "domains removed since then are not reloaded"
                )
            for text in texts:
                found, restart = package_reloads(text)
                reloads |= found
                plan.restart += [
                    f"{entry.dest}: {d}"
                    for d in restart
                    if f"{entry.dest}: {d}" not in plan.restart
                ]
        recreate |= set(entry.services)
    plan.reloads = sorted(reloads)
    plan.recreate = sorted(recreate)

    compose = _as_dict(state.get("compose"))
    plan.compose = compose
    if compose.get("error") or compose.get("hash_error"):
        plan.problems.append(
            f"docker compose: {compose.get('error') or compose.get('hash_error')}"
        )
    compose_hash = _as_dict(dests.get(r.compose_include)).get("sha256")
    compose_changed = live_sha is not None and compose_hash != _sha256(
        source.read(COMPOSE)
    )
    running = _as_dict(compose.get("running"))
    drift = running_mismatch(compose, services)
    if compose_changed:
        plan.compose_up = [s for s in services if s not in recreate]
    elif drift:
        plan.compose_up = [s for s in drift if s not in recreate]
    for entry in plan.content_changed:
        for svc in entry.services:
            mounts = [
                str(m) for m in _as_list(_as_dict(running.get(svc)).get("mounts"))
            ]
            path = f"{ha_dir}/{entry.dest}"
            real = str(_as_dict(dests.get(entry.dest)).get("realpath") or path)
            if not any(
                p == m or p.startswith(m.rstrip("/") + "/")
                for m in mounts
                for p in (path, real)
            ):
                plan.warnings.append(
                    f"{svc} does not mount {entry.dest} (mounts: {', '.join(mounts) or 'none'}); "
                    "this change does not reach it"
                )

    # Signals: the deploy needs the HA API for reloads and post-checks.
    signals = _as_dict(state.get("signals"))
    plan.signals = signals
    if not signals.get("token"):
        plan.problems.append(
            f"no {TOKEN_ENV} in {ha_dir}/.env: reloads and post-checks need the HA API"
        )
    elif signals.get("entities_error"):
        plan.problems.append(f"HA API: {signals['entities_error']}")

    # Pointer recorded in the HA repo vs what runs.
    origin_link = sub.get("gitlink_origin")
    if live_sha and origin_link != source.sha and not plan.bump:
        plan.notes.append(
            f"pointer not yet merged: HA origin/main pins {str(origin_link or 'no submodule')[:12]}, "
            f"live runs {source.sha[:12]} (merge the open HA-repo PR)"
        )
    return plan


# ---- apply --------------------------------------------------------------------------------
def _q(s: str) -> str:
    return shlex.quote(s)


def _compose_cmd(compose: dict[str, object]) -> str:
    parts = [
        "docker",
        "compose",
        "-p",
        str(compose["project"]),
        "--project-directory",
        str(compose["workdir"]),
    ]
    for f in _as_list(compose.get("files")):
        parts += ["-f", str(f)]
    return " ".join(_q(p) for p in parts)


def _backup(host: Host, manifest: Manifest, plan: Plan) -> str:
    r = manifest.ha_repo
    stamp = time.strftime("%Y%m%dT%H%M%S")
    name = f"{stamp}-{plan.sha[:12]}-{os.getpid()}-{secrets.token_hex(3)}"
    paths = [f.dest for f in manifest.files] + [
        r.configuration,
        r.compose_file,
        ".gitmodules",
        r.deployed_marker,
    ]
    meta = json.dumps(
        {"submodule_head": plan.live_sha, "target": plan.sha, "at": stamp}
    )
    script = f"""
cd {_q(plan.ha_dir)}
dir={r.backups}
mkdir -p "$dir"
paths=()
for p in {" ".join(_q(p) for p in paths)}; do
  if [ -e "$p" ] || [ -L "$p" ]; then paths+=("$p"); fi
done
tar -czf "$dir/{name}.tgz" "${{paths[@]}}"
printf '%s\\n' {_q(meta)} > "$dir/{name}.json"
echo "$dir/{name}.tgz"
"""
    return host.sh(script).stdout.strip()


def _converge_script(manifest: Manifest, plan: Plan) -> str:
    r = manifest.ha_repo
    lines = [f"cd {_q(plan.ha_dir)}"]
    for c in plan.converge:
        dest = c.entry.dest
        tmp = f"{dest}.3d-deploy-tmp"
        lines.append(f"mkdir -p {_q(os.path.dirname(dest) or '.')}")
        if c.entry.mode == "symlink":
            lines.append(f"ln -sfn {_q(symlink_target(manifest, c.entry))} {_q(tmp)}")
        else:
            lines.append(
                f"cp {_q(f'{r.submodule}/{INTEGRATION}/{c.entry.src}')} {_q(tmp)} && chmod 644 {_q(tmp)}"
            )
        lines.append(f"mv -Tf {_q(tmp)} {_q(dest)}")
    return "\n".join(lines)


def _checkout_script(manifest: Manifest, plan: Plan, sha: str) -> str:
    sub = f"{plan.ha_dir}/{manifest.ha_repo.submodule}"
    return f"git -C {_q(sub)} checkout --quiet --detach {_q(sha)}"


def _bump_script(manifest: Manifest, plan: Plan, sha: str) -> str:
    sub = f"{plan.ha_dir}/{manifest.ha_repo.submodule}"
    return f"git -C {_q(sub)} fetch --quiet origin\n" + _checkout_script(manifest, plan, sha)


def _up_script(compose: dict[str, object], services: list[str], *, force: bool) -> str:
    flags = "up -d --no-deps --force-recreate " if force else "up -d --no-deps "
    return f"{_compose_cmd(compose)} {flags}" + " ".join(_q(s) for s in services)


def _reload_script(plan: Plan, services: list[str], ha_url: str) -> str:
    calls = "\n".join(
        f"curl -fsS -o /dev/null -X POST -H @<(printf 'Authorization: Bearer %s\\n' \"$T\") "
        f"-H 'Content-Type: application/json' -d '{{}}' {_q(f'{ha_url}/api/services/' + s.replace('.', '/'))}"
        for s in services
    )
    return f"""cd {_q(plan.ha_dir)}
T=$(sed -n 's/^{TOKEN_ENV}=//p' .env | head -n 1 | tr -d '"' | tr -d "'")
{calls}"""


def _restore(
    host: Host, manifest: Manifest, plan: Plan, backup: str, created: list[str]
) -> None:
    lines = [f"cd {_q(plan.ha_dir)}"]
    lines += [f"rm -f {_q(d)}" for d in created]
    lines.append(f"tar -xzf {_q(backup)}")
    if plan.live_sha and plan.bump:
        lines.append(_checkout_script(manifest, plan, plan.live_sha))
    host.sh("\n".join(lines))


def _check_config(host: Host, manifest: Manifest) -> tuple[bool, str]:
    proc = host.sh(
        f"docker exec {_q(manifest.ha_repo.container)} python -m homeassistant "
        "--script check_config -c /config",
        check=False,
        timeout=900,
    )
    out = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, out


def post_check_failures(
    before: dict[str, object], after: dict[str, object]
) -> list[str]:
    """Regressions only: an entity that was available and now isn't, dashboard, go2rtc stream."""
    failures: list[str] = []
    pre = _as_dict(before.get("entities"))
    post = _as_dict(after.get("entities"))
    if not post:
        failures.append(
            f"HA API returned no printer entities ({after.get('entities_error') or 'empty'})"
        )
    for entity, state in sorted(pre.items()):
        now = post.get(entity)
        if state not in UNAVAILABLE and (now is None or now in UNAVAILABLE):
            failures.append(f"{entity}: {state} -> {now or 'gone'}")
    if after.get("dashboard_status") != 200:
        failures.append(f"dashboard answered HTTP {after.get('dashboard_status')}")
    g_pre, g_post = _as_dict(before.get("go2rtc")), _as_dict(after.get("go2rtc"))
    if g_pre.get("stream") and not (g_post.get("stream") and g_post.get("producers")):
        failures.append(f"go2rtc stream missing after deploy: {g_post}")
    return failures


def running_mismatch(compose: dict[str, object], services: list[str]) -> list[str]:
    wanted = _as_dict(compose.get("wanted"))
    running = _as_dict(compose.get("running"))
    return [
        s for s in services if _as_dict(running.get(s)).get("hash") != wanted.get(s)
    ]


@dataclass
class Outcome:
    backup: str
    pr_url: str | None


def apply(
    host: Host,
    manifest: Manifest,
    plan: Plan,
    vendor: dict[str, dict[str, str]],
    services: list[str],
    log: list[str],
) -> Outcome:
    r = manifest.ha_repo
    backup = _backup(host, manifest, plan)
    log.append(f"backup: {backup}")
    created = [c.entry.dest for c in plan.converge if c.reason == "missing"]

    def rollback(reason: str, detail: str, *, runtime_touched: bool) -> GateFailure:
        """Restore the backup; when reloads/recreates may have run, run them again on the old files."""
        _restore(host, manifest, plan, backup, created)
        if runtime_touched and plan.reloads:
            host.sh(_reload_script(plan, plan.reloads, r.ha_url), check=False)
        if runtime_touched and plan.recreate:
            host.sh(_up_script(plan.compose, plan.recreate, force=True), check=False)
        if runtime_touched and plan.compose_up:
            host.sh(_up_script(plan.compose, plan.compose_up, force=False), check=False)
        return GateFailure(
            f"{reason}; rolled back to the backup",
            command=CMD,
            remediation=[detail[-3000:], f"backup: {backup}"],
        )

    try:
        if plan.fast_forward:
            host.sh(f"git -C {_q(plan.ha_dir)} merge --ff-only --quiet origin/main")
            log.append("HA checkout fast-forwarded to origin/main")
        if plan.bump:
            host.sh(_bump_script(manifest, plan, plan.sha))
            log.append(f"submodule -> {plan.sha[:12]}")
        if plan.converge:
            host.sh(_converge_script(manifest, plan))
            log.append(f"converged {len(plan.converge)} path(s)")
    except GateFailure as exc:
        raise rollback("applying the files failed", exc.message, runtime_touched=False) from exc
    ok, out = _check_config(host, manifest)
    if not ok:
        raise rollback("check_config failed", out, runtime_touched=False)
    log.append("check_config: " + (out.splitlines()[-1] if out else "OK"))
    try:
        if plan.reloads:
            host.sh(_reload_script(plan, plan.reloads, r.ha_url))
            log.append("reloaded: " + ", ".join(plan.reloads))
        if plan.recreate:
            host.sh(_up_script(plan.compose, plan.recreate, force=True))
            log.append("recreated: " + ", ".join(plan.recreate))
        if plan.compose_up:
            host.sh(_up_script(plan.compose, plan.compose_up, force=False))
            log.append("compose up -d: " + ", ".join(plan.compose_up))
    except GateFailure as exc:
        raise rollback("reload or service restart failed", exc.message, runtime_touched=True) from exc

    signals_args = probe_args(manifest, plan.ha_dir, plan.sha, services, vendor, mode="signals")
    after: dict[str, object] = {}
    failures: list[str] = ["post-checks did not run"]
    for _ in range(6):  # entities settle within a few polls after a reload or recreate
        time.sleep(POLL_SECONDS if plan.reloads or plan.recreate or plan.compose_up else 0)
        result = host.probe(signals_args)
        after = _as_dict(result.get("signals"))
        failures = post_check_failures(plan.signals, after)
        failures += [
            f"{s}: running container differs from the compose config"
            for s in running_mismatch(_as_dict(result.get("compose")), services)
        ]
        if not failures:
            break
    if failures:
        raise rollback("post-checks failed", "\n".join(failures), runtime_touched=True)
    log.append(
        f"post-checks: {len(_as_dict(after.get('entities')))} printer entities OK, "
        f"dashboard {after.get('dashboard_status')}, go2rtc {_as_dict(after.get('go2rtc'))}"
    )

    marker = json.dumps(
        {
            "sha": plan.sha,
            "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "backup": backup,
        }
    )
    host.sh(
        f"printf '%s\\n' {_q(marker)} > {_q(plan.ha_dir + '/' + r.deployed_marker)}"
    )
    return Outcome(backup, record(host, manifest, plan, log))


def record(host: Host, manifest: Manifest, plan: Plan, log: list[str]) -> str | None:
    """Stage the deployed paths, commit them on a branch (not main) and open the HA-repo PR."""
    r = manifest.ha_repo
    paths = [r.submodule, ".gitmodules", *(f.dest for f in manifest.files)]
    branch = f"{BRANCH_PREFIX}{plan.sha[:12]}"
    msg = (
        f"3d ha deploy: 3d-cli {plan.sha[:12]}\n\n"
        f"Pins the {r.submodule} submodule to alex-mextner/3d-cli@{plan.sha} and records the files\n"
        "`3d ha deploy` converged from integrations/homeassistant/manifest.yaml. Already live on the\n"
        "host (check_config OK, post-checks OK); merging records it in main.\n\n"
        + "\n".join(f"- {c}" for c in plan.changes())
    )
    quoted = " ".join(_q(p) for p in paths)
    script = f"""
cd {_q(plan.ha_dir)}
export PATH="$HOME/.local/bin:$PATH"
# The real index carries the deployed paths so main fast-forwards once the PR is merged.
git add -A -- {quoted}
tmp=$(mktemp)
rm -f "$tmp"
trap 'rm -f "$tmp"' EXIT
export GIT_INDEX_FILE="$tmp"
git read-tree HEAD
git add -A -- {quoted}
tree=$(git write-tree)
if [ "$tree" = "$(git rev-parse 'HEAD^{{tree}}')" ]; then echo NOCHANGE; exit 0; fi
hooks=$(git config core.hooksPath || echo .git/hooks)
if [ -x "$hooks/pre-commit" ]; then "$hooks/pre-commit" >&2; fi
commit=$(printf '%s\\n' {_q(msg)} | git commit-tree "$tree" -p HEAD)
git update-ref {_q("refs/heads/" + branch)} "$commit"
echo "$commit"
"""
    out = host.sh(script).stdout.strip().splitlines()
    if not out or out[-1] == "NOCHANGE":
        log.append("HA repo: nothing to record (HEAD already has this state)")
        return None
    commit = out[-1]
    url = publish(host, manifest, plan, branch, commit, msg)
    log.append(f"HA repo: {branch} {commit[:12]} -> {url}")
    return url


def _cache_repo() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    repo = base / "3d-cli" / "ha-deploy.git"
    if not (repo / "HEAD").exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--quiet", "--bare", str(repo)], check=True)
    return repo


def _run(argv: list[str], what: str) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GateFailure(
            f"{what} failed: {(proc.stderr or proc.stdout).strip()[-1500:]}",
            command=CMD,
        )
    return proc.stdout.strip()


def publish(
    host: Host, manifest: Manifest, plan: Plan, branch: str, commit: str, msg: str
) -> str:
    """Push the deploy branch from the host (no GitHub credentials there) via this machine."""
    r = manifest.ha_repo
    cache = _cache_repo()
    src = plan.ha_dir if host.local else f"ssh://{host.target}{plan.ha_dir}"
    ref = f"refs/heads/{branch}"
    _run(
        ["git", "-C", str(cache), "fetch", "--quiet", src, f"+{ref}:{ref}"],
        "fetch of the deploy commit",
    )
    _run(
        ["git", "-C", str(cache), "push", "--quiet", r.origin, f"+{ref}:{ref}"],
        f"push of {branch}",
    )
    host.sh(
        f"git -C {_q(plan.ha_dir)} update-ref -d {_q(ref)} {_q(commit)}", check=False
    )
    existing = _run(
        [
            "gh",
            "pr",
            "list",
            "-R",
            r.github,
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "url",
            "-q",
            ".[].url",
        ],
        "gh pr list",
    )
    title, _, body = msg.partition("\n\n")
    url = (existing.splitlines() or [""])[0] or _run(
        [
            "gh",
            "pr",
            "create",
            "-R",
            r.github,
            "--base",
            "main",
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body + "\n\nMerge with a merge commit so the live checkout fast-forwards.",
        ],
        "gh pr create",
    )
    others = _run(
        [
            "gh",
            "pr",
            "list",
            "-R",
            r.github,
            "--state",
            "open",
            "--json",
            "number,headRefName",
            "-q",
            f'.[] | select(.headRefName | startswith("{BRANCH_PREFIX}")) | '
            f'select(.headRefName != "{branch}") | .number',
        ],
        "gh pr list",
    )
    for number in others.split():
        _run(
            [
                "gh",
                "pr",
                "close",
                number,
                "-R",
                r.github,
                "--delete-branch",
                "--comment",
                f"Superseded by {url} (a later `3d ha deploy`).",
            ],
            f"closing PR #{number}",
        )
    return url


# ---- entry -------------------------------------------------------------------------------
@dataclass
class Options:
    dry_run: bool = False
    host: str | None = None
    ha_dir: str | None = None
    ref: str = "origin/main"
    source: Path | None = None


def render_plan(plan: Plan, target: str) -> str:
    lines = [
        f"3d ha deploy{' (dry run)' if target.startswith('DRY ') else ''} -> {target.removeprefix('DRY ')}",
        f"  3d-cli live {(plan.live_sha or 'none')[:12]}, target {plan.sha[:12]}",
    ]
    changes = plan.changes()
    lines.append(f"changes ({len(changes)}):")
    lines += [f"  - {c}" for c in changes] or ["  (none)"]
    for title, items in (
        ("notes", plan.notes),
        ("warnings", plan.warnings),
        ("needs an HA restart (not done by deploy)", plan.restart),
        ("problems (blocking)", plan.problems),
    ):
        if items:
            lines.append(f"{title}:")
            lines += [f"  - {i}" for i in items]
    return "\n".join(lines)


def deploy(opts: Options, repo_root: Path) -> int:
    source_repo = opts.source or repo_root
    if not (source_repo / ".git").exists():
        raise UsageError(
            f"--source {source_repo} is not a git checkout of 3d-cli", command=CMD
        )
    sha = resolve_ref(source_repo, opts.ref)
    source = Source(source_repo, sha)
    manifest = load_manifest(source.read(MANIFEST).decode())
    host = Host(opts.host or manifest.ha_repo.host)
    ha_dir = opts.ha_dir or manifest.ha_repo.dir
    services = compose_services(source)
    vendor = vendor_pins(source)
    target = f"{host.target}:{ha_dir}"
    lock = None if opts.dry_run else host.lock(ha_dir)
    try:
        state = host.probe(probe_args(manifest, ha_dir, sha, services, vendor))
        plan = build_plan(manifest, source, state, ha_dir, services, vendor)
        print(render_plan(plan, ("DRY " if opts.dry_run else "") + target), flush=True)
        if plan.problems:
            raise GateFailure(
                f"{len(plan.problems)} blocking problem(s); nothing was changed",
                command=CMD,
            )
        if plan.restart:
            raise GateFailure(
                "this change needs a Home Assistant restart; deploy never restarts HA",
                command=CMD,
                remediation=[
                    "Ask the HA owner for a restart window, or move the change to a "
                    "reloadable domain."
                ],
            )
        if opts.dry_run:
            print(f"summary: {len(plan.changes())} change(s)")
            return 0
        if not plan.changes():
            print("summary: 0 changes; nothing to deploy")
            return 0
        log: list[str] = []
        try:
            outcome = apply(host, manifest, plan, vendor, services, log)
        finally:
            for line in log:
                print(f"  {line}", flush=True)
    finally:
        if lock:
            host.unlock(lock)
    print(f"deployed 3d-cli {sha[:12]} (backup {outcome.backup})")
    if outcome.pr_url:
        print(f"HA repo PR: {outcome.pr_url} — merge it to record the pointer in main")
    return 0
