"""`models status|pull`: the registry of weights the vision stages need (offline-first, explicit fetch)."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .. import models

NAME = "vision-models"
SUMMARY = "list (status) or explicitly fetch (pull) the vision model weights; nothing is ever downloaded implicitly"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="action", metavar="status|pull", required=True)
    st = sub.add_parser("status", help="per-model install state, sizes, licences")
    st.add_argument(
        "--verify",
        action="store_true",
        help="sha256-check every file now (cached afterwards)",
    )
    st.add_argument("--json", action="store_true", help="print one JSON object")
    pl = sub.add_parser(
        "pull",
        help="make models present: import a local copy when one exists, else download + verify",
    )
    pl.add_argument(
        "names",
        nargs="*",
        help=f"model names (default all): {', '.join(models.MODELS)}",
    )
    pl.add_argument("--json", action="store_true", help="print one JSON object")


def _mb(n: int) -> str:
    return f"{n / (1 << 20):.0f} MB"


def _state_label(m: dict[str, Any]) -> str:
    if m["verified"]:
        return "ok"
    return "present (unverified)" if m["installed"] else "MISSING"


def _status(args: argparse.Namespace) -> int:
    st = models.status(verify=args.verify)
    if args.json:
        print(json.dumps(st))
        return 0
    print(f"models dir: {st['models_dir']}")
    for name, m in st["models"].items():
        print(
            f"  {name:11s} {_state_label(m):21s} {_mb(m['size_bytes']):>7s}  {m['licence']}"
        )
        for f in m["files"]:
            if f["state"] not in ("ok", "unverified"):
                print(f"      {f['rel']}: {f['state']}")
    missing = [n for n, m in st["models"].items() if not m["installed"]]
    if missing:
        print(f"fix: 3d vision-models pull {' '.join(missing)}")
    return 0


def _pull(args: argparse.Namespace) -> int:
    def report_download(name: str, rel: str, done: int, total: int) -> None:
        if not args.json and done == total:
            print(f"  downloaded {name}/{rel} ({_mb(total)})", file=sys.stderr)

    res = models.fetch(args.names or None, progress=report_download)
    if args.json:
        print(json.dumps(res))
        return 0
    for name, files in res["results"].items():
        print(f"{name}: " + ", ".join(f"{rel} {how}" for rel, how in files.items()))
    return 0


def run(args: argparse.Namespace) -> int:
    return _status(args) if args.action == "status" else _pull(args)
