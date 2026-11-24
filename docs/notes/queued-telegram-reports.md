# Queued Telegram reports

Reports that could not be sent because no trusted `tg` CLI was available
(`~/.files/bin/tg` is absent; `tg` on PATH resolves to `~/.local/share/tg-cli/tg`, not the trusted path).
Recipient: owner, from `~/.config/tg-cli/.env` (`TG_CHAT_ID`). Status: QUEUED, not delivered.

## 2026-10-21 — printers show dossier (status)

Scope: `3d printers show` enrichment. Worktree/branch `printers-show-dossier`, subagent `PrintersShowDossier`.

<b>Now:</b> the subagent is implementing it in worktree `printers-show-dossier`, created via `3d worktree create`.
<b>Problem:</b> `3d printers show` prints only the model spec (bed, nozzle, firmware, material). It has no IP, web UI, status, queue, history, or fleet data.
<b>Design:</b> resolve a model or fleet name (u1, k1se). Sections: model, machine (owner, access, host:port, heads, AMS, plates), web UI (probed), live status, queue (Moonraker job_queue), history (history/list + totals). Flags: --json, --no-live, --history N, --secs. An unreachable printer degrades per section and exits 0.
<b>Done:</b> exploration, design, dispatch.
<b>Unfinished:</b> implementation, tests (loopback fakes only), docs sync, review (Codex + non-Codex), merge to main.
<b>Next:</b> wait for the subagent handoff, verify (fast gate + smoke), merge to main and push.
<b>Verification so far:</b> none, nothing is built yet.
Work continues.
