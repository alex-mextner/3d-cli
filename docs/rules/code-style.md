# Code style & hygiene

Ported from hyper-canvas-draft, adapted to Python.

## Typing
- `from __future__ import annotations`; full type hints everywhere; **mypy-clean** (aim for strict).
- async where it genuinely helps (parallel renders, subprocess fan-out, SSE) — not as decoration.

## Naming
- Describe WHAT, not HOW. `Validator` not `ZodValidator`; `RemoteTool` not `MCPToolWrapper`.
- No temporal names: no `new_`, `legacy_`, `improved_`, `enhanced_`. If you write one, find a name for
  the thing's actual purpose.
- No pattern suffixes unless they add clarity: `Registry` not `RegistryManager`, `Tool` not
  `ToolFactory`.

## File headers (every non-trivial module)
Start with a module docstring covering: purpose; **accessed via** (where the user/command hits this);
**assumptions** (deep invariants relied on, e.g. "graph is acyclic"); **past bugs** (non-obvious ones
fixed here); **architecture** (link to the spec/ADR). Skip for trivial files (constants, re-exports).

## Comments
- Evergreen and English-only. No temporal context ("improved", "used to be", "refactored from").
- No instructional comments ("copy this pattern", "prefer X over Y").
- Never silently drop comments in a refactor — especially docstrings on public functions. After a
  refactor, verify with `git diff | grep '^-.*#'` that no useful comment was lost.

## Single source of truth
- A shared utility exists to be THE implementation. When one exists (path validation, error shapes,
  parsing, formatting, camera math), every call site uses it — never hand-roll an inline equivalent.
  This matters most for safety primitives (e.g. root-confined path validation must guard every route
  that takes a client-supplied path).
- Constants/types used by more than one module live in a shared module. If duplication is truly
  unavoidable (cross-language/process boundary), every copy carries a `# SYNC:` comment listing the
  other locations.

## Dead code — investigate, don't delete on a grep miss
A symbol that looks unused demands an investigation that ends in a concrete resolution:
1. `git log --all --oneline -S '<symbol>'` — when added, last touched.
2. `git show <commit>` on the adding commit — read the author's intent.
3. `git log -S '<symbol>('` — were there callsites, were they removed?
4. Check dynamic/string/cross-package usage.

End in exactly one of: **DELETE** (truly dead) · **FIX/RECONNECT** (forgotten-but-useful concept) ·
**SALVAGE+RELOCATE** (only part is useful) · **ESCALATE** (keep-vs-wire-vs-drop is a product call).
Never delete silently because `grep` found nothing.

## Errors
Use the structured `lib/errors.py` types — never ad-hoc `sys.exit("...")`. Every error states WHAT
failed + WHY, concrete remediation (exact command/file), accepted values when input was invalid, and
the precise install command + which tier degrades for a missing dependency.
