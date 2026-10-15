"""_common.py — shared plumbing for `3d printops <sub>` modules: option parsing, API client, errors."""
from __future__ import annotations

from collections.abc import Iterator, Set
from contextlib import contextmanager
from dataclasses import dataclass, field

from errors import ThreeDError, UsageError
from printops.client import ApiClient, ApiConfigError, ApiError


@dataclass
class Parsed:
    pos: list[str] = field(default_factory=list)
    opts: dict[str, str] = field(default_factory=dict)
    flags: set[str] = field(default_factory=set)

    def int_opt(self, name: str, sub: str) -> int | None:
        raw = self.opts.get(name)
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError as e:
            raise UsageError(f"{name} needs an integer, got {raw!r}", command=f"printops {sub}") from e


def parse(argv: list[str], sub: str, *, values: Set[str] = frozenset(), flags: Set[str] = frozenset(),
          n_pos: int | tuple[int, int] = 0) -> Parsed:
    """Split argv into positionals, `--opt VALUE` and `--flag`; unknown options are errors."""
    out = Parsed()
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in values:
            if i + 1 >= len(argv):
                raise UsageError(f"{a} needs a value", command=f"printops {sub}")
            out.opts[a] = argv[i + 1]
            i += 2
            continue
        if a in flags:
            out.flags.add(a)
        elif a.startswith("--"):
            raise UsageError(f"unknown option {a} for `printops {sub}`", command=f"printops {sub}",
                             remediation=[f"See `3d printops {sub} --help`."])
        else:
            out.pos.append(a)
        i += 1
    lo, hi = (n_pos, n_pos) if isinstance(n_pos, int) else n_pos
    if not lo <= len(out.pos) <= hi:
        want = str(lo) if lo == hi else f"{lo}-{hi}"
        raise UsageError(f"`printops {sub}` takes {want} positional argument(s), got {len(out.pos)}: {out.pos}",
                         command=f"printops {sub}", remediation=[f"See `3d printops {sub} --help`."])
    return out


def client(p: Parsed, sub: str) -> ApiClient:
    try:
        return ApiClient.from_config(api=p.opts.get("--api"), token=p.opts.get("--token"))
    except ApiConfigError as e:
        raise UsageError(str(e), command=f"printops {sub}") from e


@contextmanager
def api_errors(sub: str) -> Iterator[None]:
    """Render ApiError as a structured CLI error (exit 1) with the likely fix."""
    try:
        yield
    except ApiError as e:
        fix: list[str] = []
        if e.status == 401:
            fix.append("Set PRINTOPS_TOKEN (or `token:` in ~/.config/3d-cli/printops.yaml); mint one on the "
                       "Odroid: see `3d printops token --help`.")
        elif e.status == 403:
            fix.append("This token lacks the scope; mint one with the needed --scopes.")
        elif e.status == 0:
            fix.append("Check PRINTOPS_API and that 3d-api is running (`3d printops deploy --dry-run` shows where).")
        raise ThreeDError(e.message, command=f"printops {sub}", remediation=fix) from e
