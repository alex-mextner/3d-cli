"""ha_deploy_probe.py — read-only state probe that `3d ha deploy` runs ON the HA host.

Sent over ssh as `python3 - <json>` (stdlib only: the host has a bare python3) and prints one
JSON object: git state of the HA repo and its 3d-cli submodule, every manifest dest (kind,
symlink target, sha256 of the resolved content), text of configuration.yaml and the compose
file, the 3D compose services (running config hash vs `docker compose config --hash`,
bind-mount sources), vendored integration versions, and the post-check signals (printer
entity states via the HA API, dashboard HTTP status, go2rtc stream). It never writes to the
HA repo; `git fetch origin` only updates remote-tracking refs.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from typing import TypedDict


class VendorPin(TypedDict):
    domain: str
    submodule: str


class ProbeArgs(TypedDict):
    """The JSON argument `3d ha deploy` sends (built by ha_deploy.probe_args)."""

    mode: str  # "full" | "signals"
    dir: str
    submodule: str
    sha: str
    dests: list[str]
    copy_dests: list[str]
    marker: str
    configuration: str
    compose_file: str
    container: str
    services: list[str]
    vendor: dict[str, VendorPin]
    token_env: str
    ha_url: str
    entity_prefixes: list[str]
    dashboard_path: str
    go2rtc_api: str
    go2rtc_stream: str


def _run(args: list[str], cwd: str | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=120
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return (
        proc.returncode,
        proc.stdout.strip() if proc.returncode == 0 else proc.stderr.strip(),
    )


def _git(repo: str, *args: str) -> str | None:
    rc, out = _run(["git", "-C", repo, *args])
    return out if rc == 0 else None


def _sha256(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _gitlink(repo: str, rev: str, path: str) -> str | None:
    out = _git(repo, "ls-tree", rev, "--", path)
    if not out:
        return None
    meta = out.split("\t", 1)[0].split()
    return meta[2] if len(meta) == 3 and meta[0] == "160000" else None


def _index_gitlink(repo: str, path: str) -> str | None:
    out = _git(repo, "ls-files", "-s", "--", path) or ""
    meta = out.split("\t", 1)[0].split()
    return meta[1] if len(meta) == 3 and meta[0] == "160000" else None


def _dest_state(repo: str, dest: str) -> dict[str, object]:
    path = os.path.join(repo, dest)
    if os.path.islink(path):
        return {
            "kind": "symlink",
            "target": os.readlink(path),
            "sha256": _sha256(path),
            "realpath": os.path.realpath(path),
        }
    if os.path.isfile(path):
        return {
            "kind": "file",
            "target": None,
            "sha256": _sha256(path),
            "realpath": path,
        }
    if os.path.exists(path):
        return {"kind": "other", "target": None, "sha256": None, "realpath": path}
    return {"kind": "missing", "target": None, "sha256": None, "realpath": None}


def _tracked_links(repo: str, submodule: str) -> list[list[str]]:
    """Tracked symlinks whose target points into the submodule: [path, target]."""
    out = _git(repo, "ls-files", "-s") or ""
    links: list[list[str]] = []
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        if not meta.startswith("120000 "):
            continue
        full = os.path.join(repo, path)
        if not os.path.islink(full):
            continue
        target = os.readlink(full)
        resolved = os.path.normpath(os.path.join(os.path.dirname(full), target))
        if resolved == os.path.join(repo, submodule) or resolved.startswith(
            os.path.join(repo, submodule) + os.sep
        ):
            links.append([path, target])
    return links


def _sync_state(repo: str, deploy_paths: list[str]) -> dict[str, object]:
    """Whether HEAD can fast-forward to origin/main without touching the working tree."""
    head = _git(repo, "rev-parse", "HEAD")
    origin = _git(repo, "rev-parse", "origin/main")
    state: dict[str, object] = {
        "head": head,
        "origin_main": origin,
        "behind": 0,
        "ff_safe": False,
        "ff_blockers": [],
    }
    if not head or not origin or head == origin:
        return state
    behind = _git(repo, "rev-list", "--count", f"{head}..{origin}")
    state["behind"] = int(behind) if behind and behind.isdigit() else 0
    if not state["behind"]:
        return state  # HEAD is ahead of origin/main (an unmerged deploy or local commits)
    if _run(["git", "-C", repo, "merge-base", "--is-ancestor", head, origin])[0] != 0:
        state["ff_blockers"] = ["HEAD is not an ancestor of origin/main"]
        return state
    changed = (_git(repo, "diff", "--name-only", head, origin) or "").splitlines()
    allowed = set(deploy_paths)
    outside = [p for p in changed if p not in allowed]
    # The fast-forward keeps the working tree only where the index already holds origin/main.
    differs = (
        (
            _git(repo, "diff", "--cached", "--name-only", origin, "--", *changed) or ""
        ).splitlines()
        if changed
        else []
    )
    state["ff_blockers"] = [
        f"{p}: changed on origin/main outside the deploy paths" for p in outside
    ] + [f"{p}: index differs from origin/main" for p in differs]
    state["ff_safe"] = not state["ff_blockers"]
    return state


def _compose(args: ProbeArgs) -> dict[str, object]:
    container = args["container"]
    services = args["services"]
    rc, out = _run(
        ["docker", "inspect", container, "--format", "{{json .Config.Labels}}"]
    )
    if rc != 0:
        return {"error": f"docker inspect {container}: {out}"}
    labels = json.loads(out)
    project = labels.get("com.docker.compose.project", "")
    files = [
        f
        for f in labels.get("com.docker.compose.project.config_files", "").split(",")
        if f
    ]
    workdir = labels.get("com.docker.compose.project.working_dir", "")
    base = ["docker", "compose", "-p", project, "--project-directory", workdir]
    for f in files:
        base += ["-f", f]
    rc, out = _run([*base, "config", "--hash", ",".join(services)])
    wanted: dict[str, str] = {}
    hash_error = None
    if rc == 0:
        for line in out.splitlines():
            name, _, digest = line.partition(" ")
            wanted[name] = digest.strip()
    else:
        hash_error = out
    running: dict[str, dict[str, object]] = {}
    for svc in services:
        rc, out = _run(
            [
                "docker",
                "inspect",
                f"{project}-{svc}-1",
                "--format",
                "{{json .Config.Labels}}\t{{json .Mounts}}",
            ]
        )
        if rc != 0:
            running[svc] = {"hash": None, "mounts": []}
            continue
        lab, _, mounts = out.partition("\t")
        running[svc] = {
            "hash": json.loads(lab).get("com.docker.compose.config-hash"),
            "mounts": [
                m.get("Source", "")
                for m in json.loads(mounts)
                if m.get("Type") == "bind"
            ],
        }
    return {
        "project": project,
        "files": files,
        "workdir": workdir,
        "wanted": wanted,
        "hash_error": hash_error,
        "running": running,
    }


def _token(repo: str, token_env: str) -> str | None:
    text = _read_text(os.path.join(repo, ".env")) or ""
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == token_env:
            return value.strip().strip('"').strip("'") or None
    return None


def _http(url: str, token: str | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 — fixed local URLs
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc).encode()


def _signals(args: ProbeArgs) -> dict[str, object]:
    repo = args["dir"]
    token = _token(repo, args["token_env"])
    ha = args["ha_url"]
    out: dict[str, object] = {"token": bool(token)}
    if token:
        status, body = _http(f"{ha}/api/states", token)
        if status == 200:
            prefixes = tuple(args["entity_prefixes"])
            out["entities"] = {
                s["entity_id"]: s["state"]
                for s in json.loads(body)
                if s["entity_id"].startswith(prefixes)
            }
        else:
            out["entities_error"] = (
                f"GET /api/states -> {status} {body[:200].decode(errors='replace')}"
            )
    out["dashboard_status"] = _http(f"{ha}{args['dashboard_path']}")[0]
    status, body = _http(f"{args['go2rtc_api']}/api/streams")
    stream = args["go2rtc_stream"]
    try:
        streams = json.loads(body) if status == 200 else {}
    except ValueError:
        streams = {}
    producers = (streams.get(stream) or {}).get("producers") or []
    out["go2rtc"] = {
        "status": status,
        "stream": stream in streams,
        "producers": len(producers),
    }
    return out


def probe(args: ProbeArgs) -> dict[str, object]:
    repo = args["dir"]
    sub = args["submodule"]
    dests = args["dests"]
    if not os.path.isdir(os.path.join(repo, ".git")) and not os.path.isfile(
        os.path.join(repo, ".git")
    ):
        return {"error": f"{repo} is not a git checkout"}
    fetch_rc, fetch_out = _run(["git", "-C", repo, "fetch", "--quiet", "origin"])
    sub_dir = os.path.join(repo, sub)
    sub_head = (
        _git(sub_dir, "rev-parse", "HEAD")
        if os.path.exists(os.path.join(sub_dir, ".git"))
        else None
    )
    sha_available = False
    if sub_head:
        has = ["git", "-C", sub_dir, "cat-file", "-e", f"{args['sha']}^{{commit}}"]
        if _run(has)[0] != 0:
            # Only remote-tracking refs and objects change; the checkout stays as it is.
            _run(["git", "-C", sub_dir, "fetch", "--quiet", "origin"])
        sha_available = _run(has)[0] == 0
    sub_dirty = (_git(sub_dir, "status", "--porcelain") or "") if sub_head else ""
    own_dests = [d for d in dests if not d.startswith(sub + "/")]
    deploy_paths = [*own_dests, sub, ".gitmodules"]
    copy_dirs: dict[str, list[str]] = {}
    for d in sorted({os.path.dirname(x) for x in args["copy_dests"]}):
        full = os.path.join(repo, d)
        copy_dirs[d] = sorted(os.listdir(full)) if os.path.isdir(full) else []
    vendor: dict[str, dict[str, object]] = {}
    for name, pin in args["vendor"].items():
        manifest = _read_text(
            os.path.join(repo, "custom_components", pin["domain"], "manifest.json")
        )
        vendor[name] = {
            "version": json.loads(manifest).get("version") if manifest else None,
            "commit": _git(os.path.join(repo, pin["submodule"]), "rev-parse", "HEAD"),
        }
    marker = _read_text(os.path.join(repo, args["marker"]))
    return {
        "fetch_error": None if fetch_rc == 0 else fetch_out,
        "submodule": {
            "head": sub_head,
            "gitlink_head": _gitlink(repo, "HEAD", sub),
            "gitlink_index": _index_gitlink(repo, sub),
            "gitlink_origin": _gitlink(repo, "origin/main", sub),
            "url": _git(repo, "config", "-f", ".gitmodules", f"submodule.{sub}.url"),
            "sha_available": sha_available,
            "dirty": sub_dirty.splitlines(),
        },
        "sync": _sync_state(repo, deploy_paths),
        "marker": json.loads(marker)
        if marker and marker.strip().startswith("{")
        else None,
        "dests": {d: _dest_state(repo, d) for d in dests},
        "unstaged": (
            _git(repo, "diff", "--name-only", "--", *own_dests) or ""
        ).splitlines(),
        "tracked_links": _tracked_links(repo, sub),
        "copy_dirs": copy_dirs,
        "configuration": _read_text(os.path.join(repo, args["configuration"])),
        "compose_text": _read_text(os.path.join(repo, args["compose_file"])),
        "compose": _compose(args),
        "vendor": vendor,
        "signals": _signals(args),
    }


def signals_only(args: ProbeArgs) -> dict[str, object]:
    return {"signals": _signals(args), "compose": _compose(args)}


def main(argv: list[str]) -> int:
    args: ProbeArgs = json.loads(argv[0])
    result = signals_only(args) if args.get("mode") == "signals" else probe(args)
    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
