"""mesh_facts.py — measure mesh files with trimesh and print ONE JSON object `{path: facts}` on stdout.

The tool behind `library.analytics.measure_meshes`: the `3d` dispatcher runs on the system Python, which has no
trimesh/numpy, so the measuring runs here through `cli.pyrun` (deps `trimesh,numpy,scipy`). `facts` is exactly
`library.analytics.analyze_mesh` (never raises: an unreadable file is `{"status": "error", "reason": ...}`).

Usage: python mesh_facts.py PATH [PATH...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from library import analytics


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: mesh_facts.py PATH [PATH...]", file=sys.stderr)
        return 2
    print(json.dumps({p: analytics.analyze_mesh(Path(p)) for p in argv}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
