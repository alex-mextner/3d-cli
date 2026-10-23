"""publish.py — publish the tier (b) frame set and trained artifacts to PUBLIC Hugging Face repos.

    publish.py whoami                                     account name + whether the token can write
    publish.py dataset DIR [--repo ID] [--publish]        the sanitised own frames of dataset.py build
    publish.py model RUN_DIR --dataset-repo ID --dataset-revision SHA [--repo ID] [--weights FILE]
                                      [--publish]         head + metrics (+ weights) of a training run

DRY RUN BY DEFAULT: without --publish nothing is created or uploaded; the command prints what
would go, runs every sanitation check and exits non-zero when one fails. With --publish it creates
the repo (public), commits everything in ONE commit, reads every file back at that commit and
compares sha256 (and size) with the local bytes, and prints the repo URL and the commit SHA.

TOKEN: HF_TOKEN from the environment, else the single variable HF_API_KEY of the env file
THREED_ENV_FILE (default /home/ultra/xp/.env); no other line of that file is read. The token is
passed to huggingface_hub only; it is never printed, logged, written to a card or sent to the
training machine (weights are pulled to this machine with `remote.py pull --weights` and pushed
from here).

Proposed repos (the account must be the one that owns the GitHub repo):
    dataset  alex-mextner/3d-cli-print-failure-frames     (CC-BY-4.0; own frames only)
    model    alex-mextner/3d-cli-print-failure-tier-b     (MIT: CLIP ViT-B/16 base licence)
Cards link both ways with github.com/alex-mextner/3d-cli and pin the code commit.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
GITHUB = "https://github.com/alex-mextner/3d-cli"
DATASET_REPO = "alex-mextner/3d-cli-print-failure-frames"
MODEL_REPO = "alex-mextner/3d-cli-print-failure-tier-b"
ENV_FILE = Path(os.environ.get("THREED_ENV_FILE", "/home/ultra/xp/.env"))
TOKEN_VAR = "HF_API_KEY"


def load_token() -> str:
    """HF_TOKEN, else HF_API_KEY from ENV_FILE (only that line is looked at)."""
    token = os.environ.get("HF_TOKEN", "").strip()
    if token:
        return token
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith(f"{TOKEN_VAR}="):
                return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit(
        f"no Hugging Face token: set HF_TOKEN, or {TOKEN_VAR}= in {ENV_FILE}"
    )


def redact(text: str, token: str) -> str:
    return text.replace(token, "<token>") if token else text


def api() -> tuple[Any, str]:
    from huggingface_hub import HfApi

    token = load_token()
    return HfApi(token=token), token


def can_write(info: dict[str, Any]) -> bool:
    """Whether the whoami answer shows a token that may create repos and commit."""
    auth = (info.get("auth") or {}).get("accessToken") or {}
    role = auth.get("role")
    if role in ("write", "admin"):
        return True
    fine = auth.get("fineGrained") or {}
    perms = {
        p for entry in fine.get("global", []) for p in [entry] if isinstance(entry, str)
    }
    perms |= {
        p for scoped in fine.get("scoped", []) for p in scoped.get("permissions", [])
    }
    return bool(perms & {"repo.write", "repo.content.write", "repo.create"})


def cmd_whoami() -> int:
    hf, token = api()
    try:
        info = hf.whoami()
    except Exception as exc:  # noqa: BLE001 — network/auth errors differ per hub version
        print(redact(f"whoami failed: {type(exc).__name__}: {exc}", token))
        return 1
    orgs = [o.get("name") for o in info.get("orgs", [])]
    print(
        json.dumps(
            {"account": info.get("name"), "orgs": orgs, "can_write": can_write(info)}
        )
    )
    return 0


def _module(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def git_commit() -> str:
    out = subprocess.run(
        ["git", "-C", str(HERE), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip() or "unknown"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dataset_operations(folder: Path) -> list[tuple[str, bytes]]:
    """(path in repo, bytes) of everything that is published: own frames, labels of own rows
    only, sanitation report without third-party details, card and dataset hash."""
    rows = [
        json.loads(line)
        for line in (folder / "labels.jsonl").read_text().splitlines()
        if line.strip()
    ]
    own = [r for r in rows if r["provenance"] == "own"]
    files: list[tuple[str, bytes]] = [
        (r["file"], (folder / r["file"]).read_bytes()) for r in own
    ]
    files.append(
        (
            "labels.jsonl",
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in own).encode(),
        )
    )
    report = json.loads((folder / "sanitation.json").read_text())
    report["third_party"] = "not published"
    report["frames"] = {"own": len(own)}
    files.append(
        (
            "sanitation.json",
            (json.dumps(report, indent=2, sort_keys=True) + "\n").encode(),
        )
    )
    files.append(("DATASET_SHA256", (folder / "DATASET_SHA256").read_bytes()))
    files.append(("README.md", (folder / "README.md").read_bytes()))
    return files


def verify_upload(
    hf: Any,
    repo: str,
    repo_type: str,
    revision: str,
    files: list[tuple[str, bytes]],
    token: str,
) -> list[str]:
    """Read every file back from the commit and compare sha256 + size with what was sent."""
    from huggingface_hub import hf_hub_download

    bad = []
    with tempfile.TemporaryDirectory(
        prefix="hfverify-", dir=os.environ.get("TMPDIR")
    ) as tmp:
        for name, data in files:
            path = hf_hub_download(
                repo,
                name,
                repo_type=repo_type,
                revision=revision,
                token=token,
                local_dir=tmp,
            )
            got = Path(path).read_bytes()
            if len(got) != len(data) or sha256(got) != sha256(data):
                bad.append(
                    f"{name}: {len(got)} bytes, sha256 {sha256(got)[:12]} != sent {len(data)} bytes, {sha256(data)[:12]}"
                )
    return bad


def commit_and_verify(
    hf: Any,
    token: str,
    repo: str,
    repo_type: str,
    files: list[tuple[str, bytes]],
    message: str,
) -> int:
    from huggingface_hub import CommitOperationAdd

    hf.create_repo(repo, repo_type=repo_type, private=False, exist_ok=True)
    info = hf.create_commit(
        repo,
        operations=[CommitOperationAdd(n, d) for n, d in files],
        commit_message=message,
        repo_type=repo_type,
    )
    revision = info.oid
    bad = verify_upload(hf, repo, repo_type, revision, files, token)
    base = "datasets/" if repo_type == "dataset" else ""
    print(
        json.dumps(
            {
                "repo": f"https://huggingface.co/{base}{repo}",
                "revision": revision,
                "files": len(files),
                "verified": not bad,
            }
        )
    )
    for line in bad:
        print("VERIFY FAILED " + redact(line, token), file=sys.stderr)
    return 1 if bad else 0


def cmd_dataset(folder: Path, repo: str, publish: bool) -> int:
    ds = _module("dataset")
    ds.cmd_card(folder, git_commit())  # the card pins the CURRENT code commit
    problems = ds.check(folder)
    if problems:
        print("REFUSING TO PUBLISH:\n" + "\n".join(problems), file=sys.stderr)
        return 1
    files = dataset_operations(folder)
    total = sum(len(d) for _, d in files)
    print(
        f"dataset {repo}: {len(files)} files, {total / 1e6:.1f} MB, third-party frames excluded; sanitation clean"
    )
    if not publish:
        print("dry run: add --publish to create the repo and upload")
        return 0
    hf, token = api()
    info = hf.whoami()
    if not can_write(info):
        print("the token cannot write; refusing", file=sys.stderr)
        return 1
    if info.get("name") != repo.split("/")[0] and repo.split("/")[0] not in [
        o.get("name") for o in info.get("orgs", [])
    ]:
        print(
            f"token account {info.get('name')!r} cannot publish under {repo.split('/')[0]!r}",
            file=sys.stderr,
        )
        return 1
    return commit_and_verify(
        hf,
        token,
        repo,
        "dataset",
        files,
        f"Add frames (dataset sha256 {(folder / 'DATASET_SHA256').read_text().strip()[:16]})",
    )


MODEL_CARD = """---
license: mit
base_model: {base}
library_name: onnx
tags:
- 3d-printing
- print-failure
- clip
---

# 3d-cli tier (b) print-failure head

Fitted head(s) and metrics for the tier (b) whole-frame vision model of
[3d-cli]({github}) (`3d printer vision`), trained with `tools/vision_train` at 3d-cli commit
`{commit}`.

- Base model: [{base_id}](https://huggingface.co/{base_id}) at revision `{base_rev}` (MIT). The
  runtime embeds frames with the fp16 ONNX export of its vision tower; the head files here are
  linear layers on that embedding (`model: {tag}`).
- Training data: [{dataset}](https://huggingface.co/datasets/{dataset}) at revision
  `{dataset_rev}` (own frames, CC-BY-4.0) plus third-party public frames that are not redistributed
  (provenance: docs/research/printer-vision-tuning.md in the GitHub repo).
- Files: `tier-b-head.json` (drop into `~/.cache/3d-cli/vision/models/`), `metrics.json`
  (per class AUROC and precision/recall at thresholds on the test split, the same tables as
  `3d printer vision eval --model`){weights_line}.

## Intended use and limits

A second opinion next to the tier (a) detectors; warnings only, never an automatic pause. The
labelled data contains very few failure frames of the owner's printers (see the dataset card):
read the per-class numbers in `metrics.json` with their frame counts before trusting any class.
"""


def cmd_model(
    run_dir: Path,
    repo: str,
    dataset_repo: str,
    dataset_rev: str,
    weights: Path | None,
    publish: bool,
) -> int:
    sys.path.insert(0, str(HERE.parent.parent / "lib"))
    import vision_model as vm

    head, metrics = run_dir / "tier-b-head.json", run_dir / "metrics.json"
    for f in (head, metrics):
        if not f.is_file():
            print(f"{f} missing: `remote.py pull RUN` first", file=sys.stderr)
            return 1
    doc = json.loads(head.read_text())
    if doc.get("model") != vm.MODEL_TAG:
        print(
            f"head is for {doc.get('model')!r}, this build embeds {vm.MODEL_TAG!r}",
            file=sys.stderr,
        )
        return 1
    card = MODEL_CARD.format(
        base=vm.BASE_MODEL_ID,
        base_id=vm.BASE_MODEL_ID,
        base_rev=vm.BASE_REVISION,
        github=GITHUB,
        commit=git_commit(),
        tag=vm.MODEL_TAG,
        dataset=dataset_repo,
        dataset_rev=dataset_rev,
        weights_line=", `weights/` (the fine-tuned vision tower)" if weights else "",
    )
    files = [
        ("tier-b-head.json", head.read_bytes()),
        ("metrics.json", metrics.read_bytes()),
        ("README.md", card.encode()),
    ]
    if weights is not None:
        files.append((f"weights/{weights.name}", weights.read_bytes()))
    for name, data in files:
        text = data.decode("utf-8", "ignore") if name.endswith((".json", ".md")) else ""
        for pat in _module("dataset").SECRET_PATTERNS:
            if m := pat.search(text):
                print(
                    f"REFUSING: {name} matches {pat.pattern!r} ({m.group(0)[:20]!r})",
                    file=sys.stderr,
                )
                return 1
    print(f"model {repo}: {[(n, len(d)) for n, d in files]}")
    if not publish:
        print("dry run: add --publish to create the repo and upload")
        return 0
    hf, token = api()
    if not can_write(hf.whoami()):
        print("the token cannot write; refusing", file=sys.stderr)
        return 1
    return commit_and_verify(
        hf, token, repo, "model", files, f"Add tier (b) head for {vm.MODEL_TAG}"
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("whoami")
    d = sub.add_parser("dataset")
    d.add_argument("dir", type=Path)
    d.add_argument("--repo", default=DATASET_REPO)
    d.add_argument("--publish", action="store_true")
    m = sub.add_parser("model")
    m.add_argument("run_dir", type=Path)
    m.add_argument("--repo", default=MODEL_REPO)
    m.add_argument("--dataset-repo", default=DATASET_REPO)
    m.add_argument("--dataset-revision", required=True)
    m.add_argument("--weights", type=Path)
    m.add_argument("--publish", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "whoami":
        return cmd_whoami()
    if args.cmd == "dataset":
        return cmd_dataset(args.dir, args.repo, args.publish)
    return cmd_model(
        args.run_dir,
        args.repo,
        args.dataset_repo,
        args.dataset_revision,
        args.weights,
        args.publish,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
