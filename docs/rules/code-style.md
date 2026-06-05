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

## Help text & docs: motivation + example, always
Every command and EVERY option/flag documents (a) **why** it exists — the problem it solves or when
to reach for it — and (b) a **concrete example** invocation. Never ship a bare noun-phrase ("draw
axes", "set color"). A flag's `--help`, the README, and the ROADMAP all hold to this. Explain domain
terms at first mention with a `GLOSSARY.md` link. A help entry with no why + no example is a review
reject, same as a missing test.

## No comparisons against our own past / unstated baseline
State what the tool DOES, affirmatively and absolutely. Never frame a capability as an improvement
over the tool's OWN earlier state, an internal refactor, or any baseline the reader cannot see — the
reader does not know the old version and the comparison is noise (worse, it leaks throwaway history).
Banned shapes: "now works on any project, not just X", "instead of bare tracebacks", "no longer
hardcoded", "unlike the previous version", "used to require …". Write the positive form instead:
"works on any FDM project", "structured, actionable errors". The ONLY admissible comparison is
against a **named, externally-known tool/standard** (e.g. "ffmpeg-style filter graph", "jq-like
selectors") where the reference genuinely helps the reader place the feature. Applies to README,
ROADMAP, `--help`, commit messages, and docs alike.

## Show examples, don't narrate about them
Don't write meta-commentary ABOUT an example ("this is one example pipeline, not the headline",
"here is a sample of how you might…"). Just give the example. Concrete, illustrative examples go in a
**quote block** (`>`) for a scenario, or a fenced **code block** for runnable commands — set off from
the prose, not buried in a sentence that talks around them. A section that positions itself ("this is
just one of many…") instead of demonstrating is a reject; lead with what it does, then show it.
(In design/spec docs, discussing a feature's *positioning* is fine — this rule is about user-facing
copy, where the reader wants the thing, not commentary on the thing.)
