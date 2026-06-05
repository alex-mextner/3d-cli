# Decision requests (escalation)

Ported from hyper-canvas-draft's CTO-decision protocol.

## Escalate ONLY product/architectural decisions not derivable from code.
Self-check, mandatory before asking anything:
- Everything verifiable — verify it yourself. If the answer is one shell command, run it; don't ask.
- Before escalating: (1) consult `advisor()`, (2) run a code review, (3) only if the question is
  still genuinely open and is a product/roadmap call, escalate.

## Format a decision request like this
1. **Context** — where the code is (`file:line`), what the function does.
2. **Problem** — what exactly needs a decision.
3. **Options** — only those with real merit; each with pros / cons.
4. **Recommendation** — which option is right and why.
5. **Where to look** — specific files/lines.
6. **Visual** — for visual changes, BEFORE image + description of intended AFTER.

Don't escalate "should I proceed?" or anything answerable from the code, the ROADMAP, or a quick
experiment.
