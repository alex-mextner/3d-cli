"""Entry script of `3d colour-regions`: run through `cli.pyrun` with the mesh dependencies (see commands/colour_regions.py).

The dispatcher's error rendering does not reach a child process, so structured errors are rendered here with the
same exit codes (2 bad invocation, 127 missing dependency, 1 gate failure).
"""

from __future__ import annotations

import sys

from colour_regions.cli import main
from errors import ThreeDError

if __name__ == "__main__":
    try:
        code = main(sys.argv[1:])
    except ThreeDError as exc:
        text = exc.render()
        if text:
            print(text, file=sys.stderr)
        code = exc.exit_code
    sys.exit(code)
