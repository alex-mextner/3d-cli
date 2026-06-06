"""3d init — scaffold a new 3d project (the `3d.yaml` + directory skeleton).

WHAT: creates a valid 3d.yaml, directory skeleton (parts/, references/, previews/),
  .gitignore, git repo, and agent environment (MCP, skills, AGENTS.md) in one command.

WHY: this is the first command a user runs; everything else (pack/slice/check/strength/
  projects/AI tools) reads the `3d.yaml` it writes. Like `git init` for git, `3d init`
  is the entry point that makes every subsequent command work. Re-running is safe — it
  tops up missing pieces without clobbering existing files.

Examples:
  3d init                                   # scaffold current directory, prompting
  3d init my-bracket --no-input             # CI-safe: name from dir, all defaults
  3d init --name pantheon --reference pantheon.jpg --printer X1C --no-input

ROADMAP §28: "3d init [path] — fully sets up a new 3d project in one command:
  git init, 3d.yaml (§5), directory skeleton, MCP, skills, git hooks, agents docs,
  and register the project so 3d web (§9) lists it. Idempotent — re-running tops up
  missing pieces without clobbering."

ACCESSED VIA: `3d init [path] [flags]` from the CLI. This is the first command a user runs;
everything else (pack/slice/check/strength/projects/AI tools) reads the `3d.yaml` it writes,
so the file it emits MUST round-trip through `project.load_project()` with file-checking ON.

INVARIANTS:
  - Idempotent / top-up only: re-running NEVER clobbers an existing 3d.yaml, .gitignore, or a
    reference already copied in. It creates only what is missing, so `3d init` is safe to re-run
    to add the skeleton to a half-set-up directory.
  - The emitted 3d.yaml has NO parts (the parts: map is empty), so load_project(check_files=True)
    never trips over a dangling part file the scaffolder invented. A part is added by the user via
    a later `3d add`, not here.
  - Three input modes share ONE code path: interactive (TTY prompts), --no-input/--yes (flags +
    defaults, REQUIRED for CI/agents), and combined (flags pre-fill, prompt for the rest). Prompts
    are skipped entirely when --no-input or when stdin is not a TTY.
  - Heavy/optional pieces degrade, never fail: `git init` is best-effort; the projects registry is
    imported guarded (it is authored by a parallel task and may be absent).
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

from cli import env
from cli.registry import Command
from errors import InputNotFound, InvalidArgument

USAGE = """3d init [path] [options]
  Scaffold a PRODUCTIVE agent project: write a 3d.yaml (project, parts, anchors, sections,
  loads, gates), create parts/ references/ previews/,
  add a .gitignore, init a git repo, register the project, AND install the agent environment —
  the OpenSCAD MCP server (.mcp.json), the openscad + fdm-printability skills (.claude/skills/),
  an AGENTS.md (with CLAUDE.md symlink), and a `3d check` pre-commit hook.
  WHY: every other command (pack/slice/check/strength, the AI tools) reads 3d.yaml; this is
  the one command that creates a valid one, so you start with `3d init` like you start with
  `git init`. The agent assets make an LLM productive on day one. Re-running is safe: it tops up
  whatever is missing and never clobbers your edits.

  path                  directory to scaffold into (default: current directory). Created if
                        absent. WHY: lets you spin up a project without cd-ing first.
                        Example: 3d init my-bracket

Options:
  --name NAME           project name written to project.name. WHY: names the part set in
                        reports/exports; defaults to the directory name when omitted.
                        Example: 3d init --name bracket --no-input
  --printer NAME        default printer preset (a name into printers.yaml, §2a). WHY: slice/
                        check resolve bed size + speeds from it instead of you re-passing it.
                        Example: 3d init --printer X1C --no-input
  --material NAME       default material (a name into materials.yaml). WHY: strength/slice use
                        it as the per-part default so you set it once.
                        Example: 3d init --material PETG --no-input
  --units U             project units: mm or cm (default mm). WHY: drives export scaling.
                        Example: 3d init --units cm --no-input
  --bed X,Y,Z           explicit build volume in project units. WHY: pack/check use it to flag
                        parts that won't fit when no --printer preset is set.
                        Example: 3d init --bed 256,256,256 --no-input
  --reference PATH      copy an image into references/ and record it as project.reference. WHY:
                        the reference-match workflow (`3d match`/`3d score`) needs a target
                        photo; this wires it in at creation time.
                        Example: 3d init pantheon --reference pantheon.jpg --no-input
  --no-git              do NOT run `git init`. WHY: when scaffolding inside an existing repo or
                        a throwaway dir you don't want a nested repo.
                        Example: 3d init --no-git --no-input
  --no-mcp              do NOT write .mcp.json (the OpenSCAD MCP server). WHY: skip it if your
                        agent host configures MCP elsewhere or you don't run an MCP client.
                        Example: 3d init --no-mcp --no-input
  --no-skills           do NOT install the .claude/skills (openscad + fdm-printability). WHY:
                        skip if you keep skills centrally instead of per-project.
                        Example: 3d init --no-skills --no-input
  --no-agents           do NOT write AGENTS.md + the CLAUDE.md symlink. WHY: skip if you already
                        maintain your own agent doc in this directory.
                        Example: 3d init --no-agents --no-input
  --no-hooks            do NOT install the pre-commit hook. WHY: skip in a repo that already has a
                        hooks setup or a custom core.hooksPath you don't want overwritten.
                        Example: 3d init --no-hooks --no-input
  --no-input, --yes     non-interactive: take everything from flags + defaults, ask nothing.
                        WHY: REQUIRED for CI and agents; -y is the alias.
                        Example: 3d init --name gear --no-input

Examples:
  3d init                                   # scaffold the current directory, prompting
  3d init my-bracket --no-input             # CI-safe: name from dir, all defaults
  3d init --name pantheon --reference pantheon.jpg --printer X1C --no-input"""

_VALID_UNITS = ("mm", "cm")


def _parse_args(argv: list[str]) -> dict[str, object]:
    """Parse argv into an options dict. Raises InvalidArgument on a flag with a missing value
    or a bad enum/format. Unknown flags are surfaced rather than silently ignored."""
    opts: dict[str, object] = {
        "path": None,
        "name": None,
        "printer": None,
        "material": None,
        "units": None,
        "bed": None,
        "reference": None,
        "no_git": False,
        "no_input": False,
        # Agent-environment pieces — all install by default; flags turn them OFF.
        "mcp": True,
        "skills": True,
        "agents": True,
        "hooks": True,
    }
    needs_value = {
        "--name": "name",
        "--printer": "printer",
        "--material": "material",
        "--units": "units",
        "--bed": "bed",
        "--reference": "reference",
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in needs_value:
            if i + 1 >= len(argv):
                raise InvalidArgument(a, "", ["<value>"], command="init",
                                      extra=f"{a} requires a value, e.g. {a} foo")
            opts[needs_value[a]] = argv[i + 1]
            i += 2
            continue
        if a in ("--no-git",):
            opts["no_git"] = True
        elif a == "--no-mcp":
            opts["mcp"] = False
        elif a == "--no-skills":
            opts["skills"] = False
        elif a == "--no-agents":
            opts["agents"] = False
        elif a == "--no-hooks":
            opts["hooks"] = False
        elif a in ("--no-input", "--yes", "-y"):
            opts["no_input"] = True
        elif a.startswith("-"):
            raise InvalidArgument(
                "argument", a, ["see `3d init --help`"], command="init",
                extra="Unknown flag. Run `3d init --help` for the accepted flags.",
            )
        elif opts["path"] is None:
            opts["path"] = a
        else:
            raise InvalidArgument(
                "argument", a, ["a single [path]"], command="init",
                extra="`3d init` takes at most one positional path.",
            )
        i += 1
    return opts


def _parse_bed(raw: str) -> list[float]:
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 3:
        raise InvalidArgument("--bed", raw, ["X,Y,Z (three numbers)"], command="init",
                              extra="Example: --bed 256,256,256")
    try:
        return [float(p) for p in parts]
    except ValueError:
        raise InvalidArgument("--bed", raw, ["X,Y,Z (three numbers)"], command="init",
                              extra="Each of X,Y,Z must be a number, e.g. --bed 256,256,256") from None


def _prompt(label: str, default: str | None) -> str | None:
    """Ask one question, showing the default in brackets. Empty answer keeps the default."""
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"{label}{suffix}: ").strip()
    except EOFError:
        return default
    return ans or default


def _inside_work_tree(root: pathlib.Path) -> bool:
    """True if `root` is already inside a git work tree (this dir OR any ancestor's repo).

    A bare `(root / ".git").exists()` only catches a repo rooted exactly at `root`; scaffolding
    into a SUBDIR of an existing repo would otherwise fall through to `git init` and create a
    nested repository that hides the new files from the parent. `rev-parse` walks up, so we skip
    git init whenever any ancestor is already a work tree."""
    try:
        r = subprocess.run(
            ["git", "-C", os.fspath(root), "rev-parse", "--is-inside-work-tree"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except OSError:
        return False
    return r.returncode == 0 and r.stdout.strip() == "true"


def _gitignore_body() -> str:
    return (
        "# 3d-cli project\n"
        "libs/\n"
        ".venv/\n"
        "previews/\n"
        "*.stl\n"
        "*.gcode\n"
        "*.3mf\n"
        "__pycache__/\n"
        ".DS_Store\n"
    )


def _write_yaml(target: pathlib.Path, opts: dict[str, object]) -> None:
    """Write 3d.yaml from `opts` using yaml.safe_dump (lazy import). Empty `parts:` map so
    load_project(check_files=True) is satisfied. Never called when the file already exists."""
    import yaml  # lazy: pyyaml is a project-handling dep, not an import-time one (see project.py)

    project_block: dict[str, object] = {"name": opts["name"], "units": opts["units"]}
    if opts["printer"]:
        project_block["printer"] = opts["printer"]
    if opts["material"]:
        project_block["material"] = opts["material"]
    if opts["bed"] is not None:
        project_block["bed"] = opts["bed"]
    if opts["reference"]:
        project_block["reference"] = opts["reference"]
    doc = {
        "project": project_block,
        "parts": {},
        "anchors": {},
        "sections": {},
        "loads": {},
        "gates": [],
    }
    target.write_text(
        yaml.safe_dump(doc, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def _register(root: pathlib.Path) -> str:
    """Append the project root to the projects registry, if that module exists yet.

    The registry (lib/projects_registry.py) is authored by a parallel task and may be absent;
    we import it guarded and skip registration gracefully rather than fail the scaffold."""
    try:
        import projects_registry  # type: ignore[import-not-found]
    except ImportError:
        return "projects registry not available yet — skipped registration"
    try:
        projects_registry.add(str(root))  # type: ignore[attr-defined]
        return f"registered {root} in the projects registry"
    except Exception as exc:  # registry present but its API differs / fails: degrade, don't crash
        return f"could not register project ({exc})"


def _assets_dir() -> pathlib.Path:
    """The vendored agent assets shipped with the 3d-cli repo (skills/templates)."""
    return pathlib.Path(env.repo_root()) / "assets"


def _copy_tree_missing_only(src: pathlib.Path, dst: pathlib.Path) -> int:
    """Copy every file under `src` into `dst`, creating dirs, but NEVER overwriting a file
    that already exists at the destination (so a user's edits to a skill survive a re-run).
    Returns the number of files written. shutil.copytree(dirs_exist_ok=True) would clobber,
    so we walk by hand."""
    written = 0
    for s in src.rglob("*"):
        rel = s.relative_to(src)
        d = dst / rel
        if s.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        if d.exists():
            continue
        shutil.copy2(s, d)
        written += 1
    return written


def _install_mcp(root: pathlib.Path, notes: list[str]) -> None:
    """Install .mcp.json (the OpenSCAD MCP server). Idempotent: skip if one already exists."""
    dest = root / ".mcp.json"
    if dest.exists():
        notes.append(".mcp.json already exists — left untouched")
        return
    src = _assets_dir() / "templates" / "mcp.json"
    if not src.is_file():
        notes.append("mcp.json template missing from assets — skipped .mcp.json")
        return
    shutil.copy2(src, dest)
    notes.append("wrote .mcp.json")


def _install_skills(root: pathlib.Path, notes: list[str]) -> None:
    """Install .claude/skills/{openscad,fdm-printability}. Idempotent: tops up missing files
    only, never overwrites an existing one."""
    skills_src = _assets_dir() / "skills"
    if not skills_src.is_dir():
        notes.append("skills missing from assets — skipped skill install")
        return
    installed: list[str] = []
    for name in ("openscad", "fdm-printability"):
        s = skills_src / name
        if not s.is_dir():
            continue
        _copy_tree_missing_only(s, root / ".claude" / "skills" / name)
        installed.append(name)
    if installed:
        notes.append("installed skills: " + ", ".join(installed))


def _install_agents(root: pathlib.Path, opts: dict[str, object], notes: list[str]) -> None:
    """Write AGENTS.md (with {{PROJECT_NAME}}/{{PRINTER}}/{{MATERIAL}} substituted) and create
    CLAUDE.md as a RELATIVE symlink -> AGENTS.md. Both halves are independent + idempotent:
    AGENTS.md is written only if missing; the symlink is created if missing (even when AGENTS.md
    already exists), so a deleted symlink is topped up without clobbering the doc."""
    src = _assets_dir() / "templates" / "AGENTS.project.md"
    agents = root / "AGENTS.md"
    if agents.exists():
        notes.append("AGENTS.md already exists — left untouched")
    elif not src.is_file():
        notes.append("AGENTS.project.md template missing from assets — skipped AGENTS.md")
    else:
        body = src.read_text(encoding="utf-8")
        body = body.replace("{{PROJECT_NAME}}", str(opts["name"] or root.name))
        body = body.replace("{{PRINTER}}", str(opts["printer"] or "see 3d.yaml (default Bambu A1)"))
        body = body.replace("{{MATERIAL}}", str(opts["material"] or "see 3d.yaml (default PETG)"))
        agents.write_text(body, encoding="utf-8")
        notes.append("wrote AGENTS.md")

    # CLAUDE.md symlink — independent of AGENTS.md above. Use os.path.lexists so a broken/dangling
    # symlink counts as present (plain .exists() follows the link and would re-create it).
    claude = root / "CLAUDE.md"
    if os.path.lexists(claude):
        notes.append("CLAUDE.md already exists — left untouched")
    else:
        try:
            os.symlink("AGENTS.md", claude)
            notes.append("wrote CLAUDE.md symlink -> AGENTS.md")
        except OSError as exc:  # e.g. filesystem without symlink support — degrade, don't crash
            notes.append(f"could not create CLAUDE.md symlink ({exc})")


def _install_hooks(root: pathlib.Path, opts: dict[str, object], notes: list[str]) -> None:
    """Install hooks/pre-commit (chmod +x) and point git at hooks/. Idempotent: skip the copy if
    the hook already exists; `git config core.hooksPath` is best-effort (needs a git repo)."""
    dest = root / "hooks" / "pre-commit"
    if dest.exists():
        notes.append("pre-commit hook already exists — left untouched")
        return
    src = _assets_dir() / "templates" / "pre-commit"
    if not src.is_file():
        notes.append("pre-commit template missing from assets — skipped hook install")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    os.chmod(dest, 0o755)  # don't trust copy2 to carry the exec bit; the hook must be executable
    notes.append("installed pre-commit hook")
    # Point git at hooks/ (best-effort: only if git is available AND this is a repo).
    if shutil.which("git") is not None and (root / ".git").exists():
        try:
            subprocess.run(
                ["git", "config", "core.hooksPath", "hooks"], cwd=root, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            notes.append("set git core.hooksPath = hooks")
        except (OSError, subprocess.CalledProcessError):
            notes.append("could not set git core.hooksPath (run: git config core.hooksPath hooks)")
    elif shutil.which("git") is not None:
        # Scaffolded inside an existing parent repo (no own .git): do NOT touch the parent's
        # git config. The hook is written to hooks/ but not wired — tell the user how.
        notes.append(
            "pre-commit hook written to hooks/ but NOT wired (this dir is inside another git "
            "repo) — to enable: git config core.hooksPath hooks"
        )


def run(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    opts = _parse_args(argv)

    # Resolve the target directory (positional path or cwd), create it if needed.
    raw_path = opts["path"]
    root = (pathlib.Path(str(raw_path)) if raw_path else pathlib.Path.cwd()).resolve()
    root.mkdir(parents=True, exist_ok=True)

    interactive = (not opts["no_input"]) and sys.stdin.isatty()

    # Fill name/printer/material/units: flags first, then prompts (interactive only), then defaults.
    if opts["name"] is None:
        opts["name"] = _prompt("Project name", root.name) if interactive else root.name
    if opts["units"] is None:
        opts["units"] = _prompt("Units (mm/cm)", "mm") if interactive else "mm"
    if interactive and opts["printer"] is None:
        opts["printer"] = _prompt("Default printer (blank = none)", None)
    if interactive and opts["material"] is None:
        opts["material"] = _prompt("Default material (blank = none)", None)

    units = str(opts["units"])
    if units not in _VALID_UNITS:
        raise InvalidArgument("--units", units, list(_VALID_UNITS), command="init")

    if opts["bed"] is not None:
        opts["bed"] = _parse_bed(str(opts["bed"]))

    notes: list[str] = []

    # Directory skeleton (idempotent).
    for d in ("parts", "references", "previews"):
        (root / d).mkdir(exist_ok=True)

    # --reference: copy the image in and record it. Resolve BEFORE writing yaml so a missing
    # source fails fast without leaving a half-written project.
    if opts["reference"]:
        src = pathlib.Path(str(opts["reference"]))
        if not src.is_file():
            raise InputNotFound(str(src), command="init")
        dest = root / "references" / src.name
        if dest.resolve() != src.resolve() and not dest.exists():
            shutil.copy2(src, dest)
        opts["reference"] = f"references/{src.name}"

    # 3d.yaml — never clobber an existing one (it may carry user edits / parts).
    yaml_path = root / "3d.yaml"
    if yaml_path.exists():
        notes.append(f"{yaml_path.name} already exists — left untouched")
    else:
        _write_yaml(yaml_path, opts)
        notes.append(f"wrote {yaml_path.name}")

    # .gitignore — never clobber.
    gi = root / ".gitignore"
    if gi.exists():
        notes.append(".gitignore already exists — left untouched")
    else:
        gi.write_text(_gitignore_body(), encoding="utf-8")
        notes.append("wrote .gitignore")

    # git init — best-effort, only when not --no-git and not ALREADY under a worktree.
    # Use `rev-parse --is-inside-work-tree` (not just a local .git) so scaffolding into a
    # subdir of an existing repo does NOT create a nested repository that hides the files
    # from the parent.
    if not opts["no_git"]:
        if shutil.which("git") is None:
            notes.append("git not on PATH — skipped git init")
        elif _inside_work_tree(root):
            notes.append("already inside a git work tree — skipped git init")
        else:
            try:
                subprocess.run(
                    ["git", "init", "-q"], cwd=root, check=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                notes.append("ran git init")
            except (OSError, subprocess.CalledProcessError) as exc:
                notes.append(f"git init failed ({exc}) — continuing")

    # Agent environment (all default ON; each piece idempotent + independently skippable).
    # Installed AFTER git init so the pre-commit hook can wire core.hooksPath on a fresh repo.
    if opts["mcp"]:
        _install_mcp(root, notes)
    if opts["skills"]:
        _install_skills(root, notes)
    if opts["agents"]:
        _install_agents(root, opts, notes)
    if opts["hooks"]:
        _install_hooks(root, opts, notes)

    notes.append(_register(root))

    print(f"Initialized 3d project '{opts['name']}' at {root}")
    for n in notes:
        print(f"  - {n}")
    print("\nNext: add a part .scad under parts/, then `3d render` / `3d check`.")
    return 0


COMMAND = Command(
    name="init",
    group="ENVIRONMENT",
    summary="scaffold a new 3d project (3d.yaml + skeleton; --no-input for CI)",
    usage=USAGE,
    run=run,
)
