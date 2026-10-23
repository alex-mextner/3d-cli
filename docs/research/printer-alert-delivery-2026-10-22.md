# Why the 2026-10-22 PAUSED alert reached nobody (U1) — and what makes delivery work by itself

Companion of [printer-vision-detached-part.md](printer-vision-detached-part.md) (the same print: why the
camera did not see the part come off) and of [printer-subscribe.md](../commands/printer-subscribe.md) (the
delivery chain). Ticket [#529](https://github.com/alex-mextner/3d-cli/issues/529). Everything below was read
read-only on 2026-10-23 (`journalctl --user`, the unit files, `~/.local/state/3d-cli`, the code at
`4eb0a37a`) or reproduced under an isolated `HOME`/`XDG_*`/tmux server with a fake `omp`, `tg-ctl` and `tg`;
nothing touched a printer or the user's Telegram. What was not proven is marked **[INFERENCE]**.

## Root cause

At 20:22:59 the watcher (`3d-printer-watch@u1`, pid 150999) emitted `u1 state PAUSED layer 17/53`, warning.
The journal of that second:

```
subscriptions: sdf9f922610 (session 01a12cd6-…): no live omp session matches '01a12cd6-…' (see: omp-steer list)
subscriptions: s5bc3a19d96 (session 01a12cd6-…): no live omp session matches '01a12cd6-…' (see: omp-steer list)
printer-agent: dry-run temporary u1 agent (3d-printer-agent-u1):
[3d printer alert -> temporary u1 agent] u1 state (warning) …
```

The last line looked like a delivery. It was the `dry-run` branch of `PrinterAgentSink.deliver`
(`printer_agent.py:452-455` before the fix): it prints the alert, writes **nothing** (no `events.jsonl` line,
no tmux session, no `pane.log`) and returns the events as *reached*, so the FALLBACK stage saw nothing left
and stayed silent. The unit was a dry-run unit: `~/.config/systemd/user/3d-printer-watch@.service` ran
`3d printer watch %i --dry-run` and was described "live monitoring, delivery held" (started 2026-10-22
11:14:07; the u1 drop-in `live-observer.conf` that removed `--dry-run` is from 2026-10-23 02:01). The
vision unit carried the same flag. The repo did not put it there: `watch --install-service` writes a template
without `--dry-run` and rejects the flag, and there is no vision installer
([#531](https://github.com/alex-mextner/3d-cli/issues/531)); both units were hand-written. So the chain worked exactly as a held unit should — and nothing in it said
the delivery was held.

Contributing links, each of which would have swallowed the alert on its own:

| link | evidence | before | now |
| --- | --- | --- | --- |
| a service with `--dry-run` | the journal line above; unit file | accepted silently | refused at start when stderr is the journal |
| dead subscribers | `printer-subscriptions.json`: 89 / 41 / 36 failed deliveries of a session gone since 2026-10-17 | logged, never visible, never removed | skipped at once, marked `STALE: dead since`, counted, pruned after 3 days |
| no emergency channel | `~/.config/3d-cli/printer-devices.yaml` replaces the whole fleet entry of `u1`, so `watch.tg_fallback` (default `false`, `watch_run.py:226`) was off | even a real spawn failure went nowhere | on by default, with the frame |
| delivery not acknowledged | `OmpLive.send` treated exit 0 as delivered | no receipt check | `accepted` receipt for that request id and session, 10 s status / 30 s send bounds |
| nobody can judge | no pause path | the print ran on | opt-out automatic pause of critical verified findings, then `tg` |
| failures invisible | one `last_error` string per entry | no counters | per-printer counters in `agent list` and `subscriptions`, loud journal lines |

The agent of 2026-10-18 15:18 (state `started`) did run: alerts of 2026-10-21 15:47–16:08 reached it
(`events.jsonl`). It was lost when the user manager restarted at 2026-10-22 ~11:11 (no tmux server
`3d-printer-agent` exists now); the units that came back were the dry-run ones. **[INFERENCE]** for the
restart as the cause of the agent's end: the user manager's start time (`systemd[3661]`) and the directory
mtimes agree, no log of the end itself exists.

The earlier `restart limit` error (2026-10-18 13:50) is the 10-minute start limit working as designed after a
start that never came up; it is not the cause of this incident.

## The spawn survives the unit

The brief suspected the spawn dies with the watcher's cgroup. Reproduced with a transient user service as the
watcher and a fake `omp` (`systemd-run --user --wait --unit=… python driver.py`, tmux 3.6, isolated state dir):

| spawn | after the service ended |
| --- | --- |
| `tmux -L SOCK new-session …` inside the service (`scope=False`) | **dead**: tmux server and omp gone |
| `systemd-run --user --scope tmux -L SOCK new-session …` (the default) | **alive**: session, fake omp (`sleep`), registered host |

Why: tmux 3.6 moves each *pane process* into its own `tmux-spawn-<uuid>.scope` (`/proc/<omp>/cgroup`), but
the **server** stays in the service's cgroup; systemd kills it when the unit stops or restarts and the pty
hangup takes omp with it. The scope the code already used is what keeps the server out. The e2e smoke
(`tests/e2e/test_printer_alert_delivery_smoke.py`) pins both: it fails when the driver is mutated to
`scope=False` ("the agent died with the watcher unit") and passes with the default. Note for anyone testing
it: from a sandbox `kill(pid, 0)` on that process answers `EPERM` although it runs — use `/proc/<pid>`
(tg-cli's own liveness check has the same rule).

## What was verified, and what not

Verified here (fakes at the process boundary, real tmux and systemd): a synthetic warning alert with no live
subscriber starts a session outside the unit's cgroup that stays alive after the watcher unit ended, is
registered as a live host, received the alert text and the frame as its opening message; with no `omp` on
`PATH` the fake `tg` receives the alert with `--photo FRAME`; dead subscribers get zero sends; every branch
of the chain is a unit test with fake tmux, omp, tg, tg-ctl and clock (`tests/test_printer_alert_delivery.py`).

**Not verified on the real U1 unit** (the owner restarts it after merge): a real `omp` accepting the opening
prompt in tmux; the real `tg` delivering the photo; a real Moonraker `pause` from the watcher (the module is
tested with a fake adapter only; `printer guard`'s identical claim/pause path was proven earlier); the
`omp-live` `accepted` receipt of the live HSW observer session; that the units no longer carry `--dry-run`
(u1 watch and vision: removed by the 2026-10-23 drop-ins; the base unit files still say `--dry-run`, and with
this change a service that still did would refuse to start instead of holding alerts).
