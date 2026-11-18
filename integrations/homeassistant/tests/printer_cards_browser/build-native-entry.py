#!/usr/bin/env python3
"""Build the pinned HA browser fixture without starting a browser."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def entry_source(source_root: Path) -> str:
    template = (HERE / "native-entry.js").read_text(encoding="utf-8")
    rendered, count = re.subn(
        r'"/__HA_FIXTURE_ROOT__/([^"\n]+)"',
        lambda match: json.dumps(str(source_root / match[1])),
        template,
    )
    if count != 4 or "/__HA_FIXTURE_ROOT__" in rendered:
        raise ValueError("Expected the four pinned HA imports in native-entry.js")
    return rendered


def build_native_entry(work: Path) -> tuple[bytes, dict[str, Any]]:
    pinned = json.loads((HERE / "ha-frontend-fixture.json").read_text(encoding="utf-8"))
    for relative, contents in pinned["files"].items():
        data = contents.encode("utf-8")
        if hashlib.sha256(data).hexdigest() != pinned["sha256"][relative]:
            raise ValueError(f"Upstream fixture hash mismatch: {relative}")
        target = work / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    entry = work / "fixture-entry.ts"
    entry.write_text(entry_source(work), encoding="utf-8", newline="")
    tsconfig = {
        "compilerOptions": {"experimentalDecorators": True, "useDefineForClassFields": False}
    }
    command = [
        str(HERE / "node_modules/.bin/esbuild"),
        str(entry),
        "--bundle",
        "--format=esm",
        "--target=es2022",
        "--alias:lit/decorators=lit/decorators.js",
        "--tsconfig-raw=" + json.dumps(tsconfig),
        # Exact pre-resolution exclusions preserve the unused badge branches.
        "--external:../badges/hui-error-badge",
        "--external:../heading-badges/hui-error-heading-badge",
    ]
    result = subprocess.run(command, cwd=HERE, check=True, capture_output=True, timeout=30)
    return result.stdout, pinned


def write_build(work: Path) -> None:
    contents, pinned = build_native_entry(work)
    (work / "ha-entry.js").write_bytes(contents)
    logging.info(
        "PASS: exact HA %s renderer graph builds (%s bytes); no browser started",
        pinned["frontend_version"],
        len(contents),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-dir", type=Path, help="existing isolated fixture directory; caller owns cleanup"
    )
    args = parser.parse_args()
    descriptor, log_name = tempfile.mkstemp(
        prefix="printer-fixture-build-", suffix=".log", dir="/tmp"
    )
    os.close(descriptor)
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[logging.FileHandler(log_name, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )
    logging.info("Build-only verification; private log %s", log_name)
    try:
        if args.work_dir is not None:
            write_build(args.work_dir.resolve())
        else:
            with tempfile.TemporaryDirectory(prefix=".isolated-build-", dir=HERE) as work:
                write_build(Path(work))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        logging.error("FAIL: %s", error)
        if isinstance(error, subprocess.CalledProcessError):
            logging.error("%s", error.stderr.decode("utf-8", errors="replace"))
        return 1
    finally:
        logging.info("Private log %s", log_name)


if __name__ == "__main__":
    raise SystemExit(main())
