"""Entry script of `3d prep`: run through `cli.pyrun` with the prep dependencies (see commands/prep.py).

The dispatcher's error rendering does not reach a child process, so structured errors are rendered here
with the same exit codes (2 bad invocation, 127 missing dependency, 1 gate failure).
"""

from __future__ import annotations

import sys

from errors import ThreeDError
from prep.cli import main

if __name__ == "__main__":
    try:
        code = main(sys.argv[1:])
    except ThreeDError as exc:
        print(exc.render(), file=sys.stderr)
        code = exc.exit_code
    sys.exit(code)
