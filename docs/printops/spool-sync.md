# Spoolman ↔ PrintOps: one authority, one write path, a detector that can fail

Ticket: [#704](https://github.com/alex-mextner/3d-cli/issues/704). Code: `lib/printops/spool_mirror.py`
(the write path and the bookmark), `lib/printops/spool_sync.py` (detect / heal), `lib/printops/api/routes_spool_sync.py`
(the schedule and the API), `3d printops spool drift` / `3d printops health` (CLI), `spool_in_sync` in
[print-preflight](../commands/print-preflight.md). Tests: `tests/test_printops_spool_sync*.py`,
`tests/e2e/test_printops_spool_sync_workflows.py`.

## The model in ten lines

1. **Spoolman holds the current value of every spool field** (weights, location, archived, extra fields).
   It is where the owner's physical inventory and weighing happen (the web UI) and where Moonraker's
   `[spoolman]` books usage, so it cannot be made read-only and a PrintOps copy of its values can never be
   trusted to stay equal.
2. **PrintOps keeps no copy of a Spoolman value.** It keeps what Spoolman cannot: the append-only **ledger**
   (why the number is what it is, with its ± uncertainty), **events**, **anomalies**, drying sessions, the tare
   DB — and a **bookmark** per spool (`spool_sync`, migration 0105): the Spoolman state PrintOps last processed.
3. A divergence is therefore never "which of two copies is right": it is **"something changed in Spoolman that
   PrintOps has not processed yet"**. The bookmark is what tells that from PrintOps' own writes.
4. **Invariant** (checked by `spool_sync.detect`, restored by `heal`): ledger mid (floored at 0) = Spoolman
   `remaining_weight` within **0.5 g**; every Spoolman spool is known to PrintOps and every spool PrintOps knows
   exists in Spoolman; the `location` text names a PrintOps location and is the one place a spool is (a slot is
   `slot:<printer>/<head>`), one spool per slot, no archived spool in a slot; every move/archive/delete is
   recorded; derived caches (`remaining_confidence_g`, a native tare) match what PrintOps knows.
5. **Healing appends, it never overwrites Spoolman with an old copy.** A weight typed or measured in the UI
   becomes an **anchor**: a `weigh` one (± scale and tare, like `spool measure`) when Spoolman's own `/measure`
   or `/use` made it (they move `last_used`), a `manual` one (an estimate, ± 10 %, at least 20 g, like
   `receive --remaining`) when it was typed into the edit form, seen for the first time, or the ledger moved
   alone — a typed number is not a weighing, so it never satisfies the `spools/not-weighed` lint; a drop of a spool loaded in a Moonraker
   `[spoolman]` printer becomes a `moonraker_live` **delta**. The label is a best guess, the resulting mid is
   Spoolman's either way, so a wrong label can never leave the two apart. The weight is never written back.
6. **One write path.** 3d-api hands every domain function a `SyncedSpoolman` (`ApiState.synced(conn)`,
   `deps.get_spoolman`) that moves the bookmark **only for the fields a write sets**, in the caller's
   transaction; a usage PrintOps books moves the bookmark by *its* grams, so a Moonraker `/use` that landed just
   before stays a pending difference. `tests/test_printops_spool_sync_architecture.py` pins who may touch the raw client.
7. **Idempotent and loop-free.** A heal is one transaction per spool (Spoolman reads and writes inside the write
   lock every PrintOps writer takes), the bookmark stores what was *read*, and nothing it writes is read back as a
   change: a second run finds nothing. A change that lands during a heal is the next pass's, never lost.
8. **Scheduled in 3d-api**, not the CLI: every `PRINTOPS_SPOOL_SYNC_S` s (default 60, floor 10, 0 = off) one
   pass = one Spoolman listing + one GET per spool with something to do. What cannot self-heal (a location
   PrintOps does not know, two spools in one slot, a Spoolman restored from a backup, a refused write) opens an
   **anomaly** of scope `spools` (`3d printops anomalies --printer spools`) and clears by itself.
9. **Loud**: `3d printops spool drift` (read-only, `--json`, exit 1 on drift, `--heal` applies), `3d printops
   health` (drift count, last pass, stuck anomalies, exit 1), and the print gates: `spool_in_sync` (policy
   `block`) FAILs a loaded spool whose two numbers really compete (a typed weight, a ledger that moved alone, a clash, an
   unknown location) and `filament_sufficient` goes UNVERIFIED instead of picking a number; where the ledger is only
   late (usage just booked, first sight) `spool_in_sync` warns and `filament_sufficient` is judged against Spoolman's
   own number, so a short spool still FAILs.
10. **Guards against the dangerous case**: a spool is "deleted" only after a 404 for that very id, and never
    when most of the known spools vanish at once (`spoolman_empty`: a wiped Spoolman or a wrong `SPOOLMAN_URL`);
    while that holds the pass heals **no** spool (its ids may belong to another inventory) and the way out is
    explicit: `3d printops spool drift --confirm-deleted` (`POST /v1/spool-sync/confirm-deleted`) records every
    spool Spoolman answers 404 for as deleted, and names the known ids Spoolman still lists as "may now belong to
    different spools: check them" (a wiped and re-entered Spoolman numbers its new spools from 1 again, so a
    surviving id can be a different spool). It cannot be combined with `--spool` or `--heal`;
    the bookmark never moves past a placement nobody healed (`location_unknown`, `slot_conflict`): the move,
    the unload and the load are booked in full once the cause is fixed; an archived spool's free-text location is
    not drift;
    a Spoolman whose `last_used` went backwards was restored from a backup (`spoolman_rolled_back`): nothing is
    healed on that spool until someone weighs it.

## What was wrong before (investigation, `main` at f3d8aa3f)

Writers of a spool field, per side:

| Side | Writer | Where |
|---|---|---|
| Spoolman | web UI: weight (`remaining_weight`/`used_weight`/`initial_weight`), `/measure`, location, archive, delete, create, extra fields, tare | — |
| Spoolman | Moonraker `[spoolman]`: `PUT /spool/{id}/use` every `sync_rate` (5 s) for its one active spool | `docs/printops/oss.md` §9.6, `docs/commands/printops-spool.md` "Who books usage" |
| both | PrintOps: `spools.use` (`ledger.book` → `/use`), `measure` (`/measure` or PATCH), `empty`, `move`/`load`/`unload` (`locations.place_spool`), `receive`, `set_slicer_preset`, `ledger.sync_confidence`, drying (`_mark_fresh`, `dry_state`), `locations.resync` | `spools.py:470,643,996,1081`, `locations.py:471,518`, `ledger.py:315,385`, `drying.py:488,838` |
| PrintOps DB | ledger rows (append-only), events, `spool_import`, `spool_tare_choice`, drying/incident rows | `migrations/0020…0100` |

Authoritative where (before): identity, lot, price, location, `printops_slot` → Spoolman; the remaining *interval*
→ the ledger; Spoolman's `remaining_weight` → whoever wrote last. Nothing compared them.

How they diverged (all reproduced on fixtures, see the ticket):

* **UI weight edit / `/measure`**: Spoolman changes, the ledger never hears; `spools.view` (`spools.py:143`) shows the
  ledger number as "remaining" and Spoolman's as a side field; `filament_sufficient` grades the stale one.
* **Moonraker usage**: booked straight into Spoolman by design ("PrintOps never books those jobs"), so the ledger
  was permanently ahead of Spoolman on a K1 SE.
* **Create / archive / delete in the UI**: a new spool has no anchor and no event; an archived one is silently
  dropped from `reconcile.bindings` (`reconcile.py:171`) and stays "loaded"; a deleted one keeps its ledger, import
  key (`spools.py:525`) and slot event stream.
* **Location / slot**: `printops_slot` (extra) and `location` (native) both said where a spool is — two fields for
  one fact (`location_conflict`, `reconcile.py:21`); `locations.resync` trusted the stale `printops_location_id`
  over the text and **reverted a move made in the UI** (`locations.py:437`).
* **Extra fields**: the `remaining_confidence_g` cache goes stale after `empty`/ingest; the native tare is empty for
  spools made in the UI although `spool measure` (and the UI's own measure dialog) need it.
* **Concurrency**: a PrintOps booking that commits while Moonraker's report is in flight is silently merged into
  the usage figure; a crash between Spoolman accepting `/use` and the DB commit (the documented non-atomic window)
  left Spoolman ahead forever.

## Per spool, per pass

```
GET /spool?allow_archived=true               one request for the whole pass
for each spool with a difference:            weight / archived / location / identity / derived
    BEGIN IMMEDIATE                          the lock every PrintOps writer takes
      GET /spool/{id}                        re-read inside the lock
      plan = what differs, how to heal it    one function: the detector and the healer cannot disagree
      ledger rows + events + notes
      bookmark := what was read
      PATCH /spool/{id}                      only derived fields (confidence ±, native tare), never the weight
    COMMIT
anomalies of scope `spools` := what is left that cannot self-heal
```

| Drift kind | Cause | Heal |
|---|---|---|
| `remaining` | UI edit / `/measure` (anchor), Moonraker usage (delta), first sight, ledger moved alone | append anchor / delta; never write Spoolman |
| `unknown_spool` / `spool_returned` | created in the UI / back after a restore | event + anchor from its weight (sealed → `receive`, used → `manual`) |
| `spool_deleted` | deleted in the UI (404 confirmed) | ledger note, event, slot released |
| `archived` | archived / un-archived in the UI | ledger note + event |
| `location` | moved in the UI; former path left by a failed PrintOps move; archived spool still in a slot | `spool.moved` / `slot.loaded` / `slot.unloaded` events; text healed only for a former path; archived → out of the slot |
| `tare`, `confidence` | no native tare for a spool the tare DB knows; `remaining_confidence_g` ≠ ledger ± | write the derived value |
| `location_unknown`, `slot_conflict`, `spoolman_rolled_back`, `spoolman_empty` | needs a person | anomaly |

## Operating it

* After restoring Spoolman **or** the PrintOps DB from a backup: `3d printops spool drift`, then `--heal`
  (a restored Spoolman is reported as `spoolman_rolled_back`; weigh the affected spools).
* First pass on an existing system: every spool is seen for the first time; where the ledger and Spoolman already
  disagree the ledger is anchored at Spoolman's weight (a `manual` row noting "first sync"), the spool's history stays.
* The print-queue's ledger-only booking for Moonraker printers (`docs/specs/2026-10-30-print-queue.md` §4.2) is
  compatible: the value comparison is what prevents counting a job twice, whichever side books first.
* Not covered: Moonraker's active spool is not cleared when the UI moves a loaded spool
  ([#518](https://github.com/alex-mextner/3d-cli/issues/518)).
