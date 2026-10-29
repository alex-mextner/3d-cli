"""library/launch.py — the configured launchers behind the model browser's slicer, agent and browser keys (#578).

WHAT: resolves WHICH program a key starts and builds the exact argv, with no hard-coded path: the slicer
(`resolve_slicer`: OrcaSlicer, Snapmaker Orca, Creality Print, Bambu Studio, or whatever the user configured), the
URL opener (`browser_argv`) and the coding agent (`agent_argv`: omp by default, also claude, codex, opencode), plus
the brief an agent is seeded with (`write_brief`). It also starts them (`spawn_detached`, `run_attached`,
`start_in_tmux`). `library.actions.open_slicer / launch_agent / open_browser` are the thin, `3d.action/1`-shaped
callers; the TUI and `3d models act` reach this module only through them.

CONFIGURATION (first hit wins, per key): the action's own argument, an environment variable, then the JSON file
`~/.config/3d-cli/browser.json` (`$THREED_BROWSER_CONFIG` names another file):

    {"slicer": "snapmaker-orca",                      # a kind (orca, snapmaker-orca, creality-print, bambu-studio)
                                                      # or a command: "/opt/orca/AppRun {paths}"
     "browser": "firefox {url}",                      # default: xdg-open (Linux) / open (macOS)
     "agent": {"backend": "omp",                      # omp | claude | codex | opencode
               "command": ["omp", "@{brief}", "{prompt}"],   # placeholders {prompt} {brief} {cwd}
               "prompt": "Read {brief} ..."},         # the seed prompt; same placeholders
     "download_dir": "~/models"}                      # parent of the folders `d` creates

Environment: `THREED_SLICER`, `THREED_BROWSER`, `THREED_AGENT` (backend name). A command without a placeholder gets
the file paths (slicer) or the prompt (agent) appended.

INVARIANTS: nothing here prints, prompts or exits; a missing program is a `MissingDependency` (exit 127) that names
the setting to change; the bundle of an agent is written under `<state dir>/agent-briefs/`, never into the
user's working directory; stdlib-only at module top (checked by tests/test_imports.py).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cli import paths
from errors import InvalidArgument, MissingDependency

CONFIG_ENV = "THREED_BROWSER_CONFIG"
SLICER_ENV = "THREED_SLICER"
BROWSER_ENV = "THREED_BROWSER"
AGENT_ENV = "THREED_AGENT"
AGENT_MODES = ("auto", "attach", "tmux", "detached")
BACKENDS = ("omp", "claude", "codex", "opencode")
SLICE_FILE_ORDER = (".3mf", ".stl", ".obj", ".step", ".stp", ".amf", ".ply")

# kind -> (names looked up on PATH, absolute locations tried after PATH). Order = preference of the auto-detect.
SLICERS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "orca": (
        ("orca-slicer", "OrcaSlicer"),
        ("/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer",),
    ),
    "snapmaker-orca": (
        ("snapmaker-orca", "snapmaker_orca"),
        (
            "~/Applications/snapmaker_orca/AppRun",
            "~/.local/bin/snapmaker-orca",
            "/Applications/Snapmaker Orca.app/Contents/MacOS/Snapmaker Orca",
        ),
    ),
    "creality-print": (
        ("CrealityPrint", "creality-print", "Creality_Print"),
        ("/Applications/Creality Print.app/Contents/MacOS/CrealityPrint",),
    ),
    "bambu-studio": (
        ("bambu-studio", "BambuStudio", "Bambu_Studio"),
        (
            "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio",
            "/Applications/Bambu Studio.app/Contents/MacOS/BambuStudio",
        ),
    ),
}

DEFAULT_AGENT_TEMPLATES: dict[str, tuple[str, ...]] = {
    "omp": ("omp", "@{brief}", "{prompt}"),
    "claude": ("claude", "{prompt}"),
    "codex": ("codex", "{prompt}"),
    "opencode": ("opencode", "--prompt", "{prompt}"),
}
DEFAULT_PROMPT = (
    "Use the search brief in {brief}: the user searched 3D-model sites for the query it names and picked the "
    "models it lists. Check each one against that need (licence, size, files, provenance in model.json), say "
    "which fits and why, and propose the next step. Do not print or upload anything."
)


# --------------------------------------------------------------------------- configuration
def config_path(env: Mapping[str, str] | None = None) -> Path:
    e = os.environ if env is None else env
    forced = e.get(CONFIG_ENV)
    return Path(forced).expanduser() if forced else paths.config_dir() / "browser.json"


def load_config(path: Path | None = None) -> dict[str, Any]:
    """The browser config (an absent file is `{}`); a file that is not a JSON object is an InvalidArgument."""
    target = path or config_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise InvalidArgument(
            str(target), str(exc), ["a readable JSON file"], command="models"
        ) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InvalidArgument(
            str(target), "invalid JSON", [str(exc)], command="models"
        ) from exc
    if not isinstance(data, dict):
        raise InvalidArgument(
            str(target), type(data).__name__, ["a JSON object"], command="models"
        )
    return data


def _split(command: object, where: str) -> list[str]:
    """A configured command (a string, split like a shell would, or a list of strings) as an argv template."""
    if isinstance(command, str) and command.strip():
        return shlex.split(command)
    if (
        isinstance(command, list)
        and command
        and all(isinstance(c, str) for c in command)
    ):
        return list(command)
    raise InvalidArgument(
        where,
        repr(command),
        ["a command string or a list of strings"],
        command="models",
    )


def _executable(candidate: str) -> str | None:
    path = Path(candidate).expanduser()
    if path.is_file() and os.access(path, os.X_OK):
        return str(path)
    return None


def _which(program: str) -> str | None:
    if os.sep in program:
        return _executable(program)
    return shutil.which(program)


# --------------------------------------------------------------------------- slicer
@dataclass(frozen=True)
class Slicer:
    kind: str  # orca | snapmaker-orca | creality-print | bambu-studio | custom
    argv: tuple[str, ...]  # program + fixed arguments; may hold `{path}` / `{paths}`


def detect_slicer(kind: str | None = None) -> Slicer | None:
    """The first installed slicer in preference order (or the named `kind`); None when there is none."""
    for name, (commands, bundles) in SLICERS.items():
        if kind is not None and name != kind:
            continue
        for command in commands:
            found = shutil.which(command)
            if found:
                return Slicer(name, (found,))
        for bundle in bundles:
            found = _executable(bundle)
            if found:
                return Slicer(name, (found,))
    return None


def resolve_slicer(
    explicit: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    config: Mapping[str, Any] | None = None,
) -> Slicer:
    """The slicer to start: `explicit`, `$THREED_SLICER`, the config's `slicer`, then auto-detect. A value that is a
    kind name picks that installed slicer; anything else is a command (`{path}` / `{paths}` placeholders)."""
    e = os.environ if env is None else env
    cfg = load_config() if config is None else config
    choice = explicit or e.get(SLICER_ENV) or cfg.get("slicer")
    if choice is None:
        found = detect_slicer()
        if found is None:
            raise _no_slicer()
        return found
    if isinstance(choice, str) and choice in SLICERS:
        found = detect_slicer(choice)
        if found is None:
            raise _no_slicer(choice)
        return found
    template = _split(choice, "slicer")
    program = _which(template[0])
    if program is None:
        raise _no_slicer(template[0])
    return Slicer("custom", (program, *template[1:]))


def _no_slicer(wanted: str | None = None) -> MissingDependency:
    what = f"slicer {wanted!r}" if wanted else "a desktop slicer"
    return MissingDependency(
        what,
        install="install OrcaSlicer, Snapmaker Orca, Creality Print or Bambu Studio, "
        f"or name the program: export {SLICER_ENV}='/path/to/slicer {{paths}}' "
        f'(or "slicer" in {config_path()})',
        degrades="the slicer key cannot open the model",
        command="models",
    )


def slicer_argv(slicer: Slicer, files: Sequence[str]) -> list[str]:
    """`slicer.argv` with `{paths}` replaced by every file, `{path}` by the first; no placeholder = files appended."""
    out: list[str] = []
    placed = False
    for arg in slicer.argv:
        if arg == "{paths}":
            out.extend(files)
            placed = True
        elif "{path}" in arg:
            out.append(arg.replace("{path}", files[0]))
            placed = True
        else:
            out.append(arg)
    if not placed:
        out.extend(files)
    return out


def best_model_file(names: Sequence[str]) -> str | None:
    """The file a slicer should open from a download: a project .3mf first, then a mesh, then CAD."""
    for suffix in SLICE_FILE_ORDER:
        for name in names:
            if name.lower().endswith(suffix):
                return name
    return None


# --------------------------------------------------------------------------- browser
def browser_argv(
    url: str,
    *,
    explicit: str | None = None,
    env: Mapping[str, str] | None = None,
    config: Mapping[str, Any] | None = None,
) -> list[str]:
    """The command that opens `url`: `explicit`, `$THREED_BROWSER`, the config's `browser`, `$BROWSER`, then the
    platform opener (`xdg-open`, `open`, `cmd /c start`)."""
    e = os.environ if env is None else env
    cfg = load_config() if config is None else config
    choice = explicit or e.get(BROWSER_ENV) or cfg.get("browser") or e.get("BROWSER")
    if choice:
        template = _split(choice, "browser")
    elif sys.platform == "darwin":
        template = ["open"]
    elif sys.platform.startswith("win"):
        template = ["cmd", "/c", "start", ""]
    else:
        template = ["xdg-open"]
    program = _which(template[0])
    if program is None:
        raise MissingDependency(
            f"a browser opener ({template[0]})",
            install=f"set a command: export {BROWSER_ENV}='firefox {{url}}' (or \"browser\" in {config_path()})",
            degrades="the browser key cannot open the model page",
            command="models",
        )
    argv = [program, *template[1:]]
    if any("{url}" in a for a in argv):
        return [a.replace("{url}", url) for a in argv]
    return [*argv, url]


# --------------------------------------------------------------------------- agent
@dataclass(frozen=True)
class AgentPlan:
    backend: str
    argv: list[str]
    cwd: str
    brief: Path
    prompt: str


def _agent_section(cfg: Mapping[str, Any]) -> Mapping[str, Any]:
    section = cfg.get("agent")
    return section if isinstance(section, dict) else {}


def agent_backend(
    explicit: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    config: Mapping[str, Any] | None = None,
) -> str:
    e = os.environ if env is None else env
    cfg = load_config() if config is None else config
    agent_cfg = _agent_section(cfg)
    backend = explicit or e.get(AGENT_ENV) or agent_cfg.get("backend") or "omp"
    if backend not in BACKENDS:
        raise InvalidArgument("backend", str(backend), list(BACKENDS), command="models")
    return str(backend)


def brief_dir(key: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key) or "adhoc"
    return paths.state_dir() / "agent-briefs" / safe


def plan_agent(
    prompt: str,
    *,
    cwd: str,
    backend: str,
    bundle_key: str,
    config: Mapping[str, Any] | None = None,
) -> AgentPlan:
    """The argv an agent is started with. `prompt` empty = the configured seed prompt; the brief path is where
    `write_brief` puts the bundle. The program must exist (MissingDependency otherwise)."""
    cfg = load_config() if config is None else config
    agent_cfg = _agent_section(cfg)
    brief = brief_dir(bundle_key) / "brief.md"
    values = {"brief": str(brief), "cwd": cwd}
    seed = prompt or str(agent_cfg.get("prompt") or DEFAULT_PROMPT)
    text = _fill(seed, values)
    values["prompt"] = text
    configured = agent_cfg.get("command")
    if configured is not None:
        template = _split(configured, "agent.command")
    else:
        template = list(DEFAULT_AGENT_TEMPLATES[backend])
    argv = [_fill(a, values) for a in template]
    if not any("{prompt}" in a for a in template):
        argv.append(text)
    program = _which(argv[0])
    if program is None:
        raise MissingDependency(
            f"the {backend} agent ({argv[0]})",
            install=f'install {argv[0]}, or set "agent.command" in {config_path()} / export {AGENT_ENV}=<backend>',
            degrades="the agent key cannot start a session",
            command="models",
        )
    argv[0] = program
    return AgentPlan(backend, argv, cwd, brief, text)


def _fill(template: str, values: Mapping[str, str]) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


def brief_markdown(prompt: str, context: Mapping[str, Any]) -> str:
    """The text of the brief: the original query, then one block per selected model with its URL, verdict, licence,
    downloaded folder and the provenance from its `model.json`."""
    lines = ["# Model search brief", ""]
    if context.get("query"):
        lines.append(f"Original query: {context['query']}")
    if context.get("session"):
        lines.append(f"Search session (library DB): {context['session']}")
    if prompt:
        lines += ["", f"Task: {prompt}"]
    lines += ["", "## Selected models", ""]
    for n, model in enumerate(context.get("models", []), start=1):
        lines.append(f"{n}. {model.get('title') or model.get('url')}")
        for key, label in (
            ("site", "site"),
            ("url", "url"),
            ("verdict", "verdict"),
            ("rationale", "why"),
            ("license", "licence"),
            ("price", "price"),
            ("size_mm", "size (mm)"),
            ("dir", "downloaded to"),
        ):
            value = model.get(key)
            if value not in (None, "", []):
                shown = (
                    "x".join(f"{v:g}" for v in value)
                    if key == "size_mm"
                    else str(value)
                )
                lines.append(f"   - {label}: {shown}")
        if model.get("files"):
            lines.append(f"   - files: {', '.join(map(str, model['files']))}")
        provenance = model.get("provenance")
        if provenance:
            lines.append(
                "   - provenance (model.json): "
                + json.dumps(provenance, ensure_ascii=False)
            )
    lines.append("")
    return "\n".join(lines)


def write_brief(plan: AgentPlan, prompt: str, context: Mapping[str, Any]) -> Path:
    """Write `brief.md` and `context.json` next to each other under the plan's bundle dir; returns the brief path."""
    plan.brief.parent.mkdir(parents=True, exist_ok=True)
    plan.brief.write_text(brief_markdown(prompt, context), encoding="utf-8")
    (plan.brief.parent / "context.json").write_text(
        json.dumps(context, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return plan.brief


def read_provenance(folder: Path) -> dict[str, Any] | None:
    """The parsed `model.json` of a downloaded model folder, or None when absent or unreadable."""
    try:
        data = json.loads((folder / "model.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------- starting programs
_DETACHED: list[
    subprocess.Popen[bytes]
] = []  # held until they exit: a dropped Popen of a live child warns on GC


def spawn_detached(
    argv: Sequence[str], *, cwd: str | None = None, log: Path | None = None
) -> int:
    """Start `argv` in its own session with no terminal; output goes to `log` (or nowhere). Returns the pid."""
    sink: Any
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        sink = log.open("ab")
    else:
        sink = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(  # noqa: S603 — the user's own configured program
            list(argv),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.STDOUT if log is not None else subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        if log is not None:
            sink.close()
    _DETACHED[:] = [p for p in _DETACHED if p.poll() is None]
    _DETACHED.append(proc)
    return proc.pid


def run_attached(argv: Sequence[str], *, cwd: str | None = None) -> int:
    """Run `argv` on the current terminal and wait; returns its exit code."""
    return subprocess.run(list(argv), cwd=cwd, check=False).returncode  # noqa: S603


def tmux_session_name(key: str) -> str:
    safe = "".join(c if c.isalnum() else "-" for c in key)[:24].strip("-") or "adhoc"
    return f"3d-search-{safe}"


def start_in_tmux(argv: Sequence[str], *, cwd: str, name: str) -> str:
    """Start `argv` in a new detached tmux session `name` (made unique with a numeric suffix when taken).
    Returns the session name; raises MissingDependency without tmux, OSError when tmux refuses."""
    tmux = shutil.which("tmux")
    if tmux is None:
        raise MissingDependency(
            "tmux",
            install="install tmux, or start the agent attached with --agent-mode attach",
            degrades="a detached agent session cannot be created",
            command="models",
        )
    session = name
    for n in range(2, 20):
        taken = subprocess.run(  # noqa: S603
            [tmux, "has-session", "-t", session],
            capture_output=True,
            check=False,
        ).returncode
        if taken != 0:
            break
        session = f"{name}-{n}"
    proc = subprocess.run(  # noqa: S603
        [tmux, "new-session", "-d", "-s", session, "-c", cwd, shlex.join(argv)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise OSError(
            f"tmux new-session failed: {proc.stderr.strip() or proc.returncode}"
        )
    return session
