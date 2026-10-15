# `3d printops spool` / `3d printops tare` — spools, ledger, weighing

Part of [`3d printops`](printops.md) (configuration, tokens, service). Issues:
[#160](https://github.com/alex-mextner/3d-cli/issues/160) (spool lifecycle + ledger),
[#163](https://github.com/alex-mextner/3d-cli/issues/163) (weighing + tare DB). Plan §5.2–5.3, F6.

A spool's identity, lot, price, remaining weight and location live in Spoolman; 3d-api is
the only writer. On top of that 3d-api keeps a **ledger**: every change of remaining filament
with its own uncertainty, so remaining is always shown as an **interval**.

## Remaining as an interval

- An **anchor** sets an absolute value: a weighing (`measure`), a sealed spool received with a
  known label net (`receive --net`, ± max(10 g, 2 %)), a part-used spool received with an
  estimate (`receive --remaining`, ± max(20 g, 10 %) unless `--confidence`), or `empty` (0 g).
- Every **usage** booked after it (`use`, later the job history) adds `-grams` with its own ±.
  Default ± per source when the caller gives none: `gcode_estimate` 10 %, `moonraker_live` 3 %,
  `printer_reported` 5 %, `voice` 20 % (≥ 5 g), `manual` 10 % (≥ 2 g), `purge` 30 % (these are
  assumptions until reconcile measures each source's drift).
- The interval is `anchor + Σ usage ± √(σ_anchor² + Σσ²)`. A weighing is a new anchor, so it
  resets the accumulated error; its ± is √(scale² + tare²) (scale ±1 g unless
  `--scale-sigma`). The jump between the old estimate and the weighing is stored on the
  anchor row (`drift`), printed by `measure`, and is what reconcile learns from.
- A spool never anchored shows `≤N g (never weighed)` where N is Spoolman's remaining.
  With no Spoolman remaining either (no label net, e.g. an `import-owned` spool) it shows
  `unknown (never weighed)`.
- The ledger is append-only (DB triggers): a wrong booking is fixed by booking a correction
  (`use ID -12`).

Spoolman's own `remaining_weight` follows the mid value (`/use`, `/measure`); its extra field
`remaining_confidence_g` caches the ±.

## Tares (empty-spool weights)

A weighing needs the spool's tare. The chain, first hit wins:

1. the tare 3d-api wrote into the spool's Spoolman `spool_weight` (with its recorded source);
2. a `spool_weight` someone typed into Spoolman (unsourced, ±15 g);
3. Spoolman `filament.spool_weight`, vendor extra `spool_tare_g`, vendor `empty_spool_weight` (±15 g);
4. the **tare DB**: rows for the spool's vendor, narrowed by spool kind and label net. Seeded
   from [SpoolmanDB](https://github.com/Donkie/SpoolmanDB) — every distinct
   manufacturer / spool type / net / spool weight (151 rows, ±10 g) — plus your own entries
   (`tare add`, ±2 g by default, which beat catalogue rows). Disagreeing rows → ambiguous, no tare.

No tare → `measure` refuses (409) and says why; pass `--tare G` (recorded for the spool).
`receive` writes the resolved tare into Spoolman `spool_weight` so Spoolman's own UI agrees.

## `3d printops spool`

```
3d printops spool ls [--material M] [--color C] [--vendor V] [--location PREFIX] [--loaded|--unloaded] [--archived]
3d printops spool show ID [--ledger N]
3d printops spool receive (--filament-id N | --material M --name NAME [--vendor V] [--color #HEX] [--density D] [--diameter MM])
                          [--net G] [--remaining G [--confidence G]] [--tare G [--tare-source TEXT]]
                          [--spool-kind cardboard|plastic|refill] [--lot LOT] [--price P] [--location LOC] [--opened] [--comment TEXT]
3d printops spool move ID LOCATION
3d printops spool load ID PRINTER/SLOT [--replace-to LOCATION]
3d printops spool unload PRINTER/SLOT [--to LOCATION] [--gross G]
3d printops spool use ID GRAMS [--source S] [--confidence G] [--job JOB] [--note TEXT]
3d printops spool measure ID GROSS_G [--tare G [--tare-source TEXT]] [--scale-sigma G] [--note TEXT]
3d printops spool empty ID [--note TEXT]
3d printops spool import-owned FILE [--location LOC] [--project NAME] [--apply]
```

Every action takes `--json` (the API answer), `--api URL`, `--token TOKEN`.

- `ls` — one line per spool: `#7    Elegoo PLA+ RAPID PLA+ Green #4FE44D  in u1/T3  900 ± 22 g (878–922)`.
  `--color` is a hex (`#4FE44D`) or a word of the filament name; `--location` a path prefix.
  `3d printops spool ls --material PLA --unloaded`
- `show` — identity, location/slot, remaining with its anchor, last weighing, tare and where it
  comes from, lot/price/opened/import key, and the ledger (newest `--ledger N`, default 50).
  `3d printops spool show 7`
- `receive` — a new spool. The vendor is matched case/punctuation-insensitively, the filament
  by vendor + material + name + colour (+ net), else created (density defaults per material:
  PLA 1.24, PETG 1.27, ABS 1.04, …; diameter 1.75). The location must be storage, not a
  printer slot. `--opened` sets `opened_at` now (drying policy reads it).
  `3d printops spool receive --vendor Elegoo --material PLA+ --name "RAPID PLA+ Green" --color #4FE44D --net 1000 --price 1499 --location WH:default`
- `move` — store or move to a location (`WH:A/S2/B3/04`, `cell:…`, a tag uid — see `3d printops loc`).
  A loaded spool is unloaded on the way. `3d printops spool move 7 WH:A/S1`
- `load` — bind to a printer head (`u1/T3`) or AMS slot (`p1s/ams1/2`): Spoolman extra
  `printops_slot` = `u1/T3`, location `slot:u1/T3`, `opened_at` if unset. Refused: an unknown
  printer/slot, a head [`fleet/printers.yaml`](../../fleet/printers.yaml) marks unusable (with
  its reason, e.g. U1 T2), an occupied slot unless `--replace-to LOCATION` says where the
  current spool goes, an archived spool. Offline printers (other people's, D7) are recorded
  by hand like any other. `3d printops spool load 7 u1/T3`
- `unload` — take the spool out (to `--to LOCATION`, else no location); the slot is spelled as
  for `load` (`u1/t3` = `u1/T3`, `ams1/02` = `ams1/2`), and a head marked unusable can still be
  emptied. `--gross G` weighs it first: a weighing that fails leaves it loaded; an unload that
  fails after a good weighing leaves it weighed and loaded (run `unload` again).
  `3d printops spool unload u1/T3 --to WH:A/S1 --gross 974`
- `use` — book usage: Spoolman `/use` + a ledger row, atomically (a Spoolman error books
  nothing). `3d printops spool use 7 12.4 --source gcode_estimate --job u1-2026-10-14-01`
- `measure` — weigh the spool **with** its empty spool on it. Prints the new interval and how
  far the ledger was off. `3d printops spool measure 7 974`
- `empty` — used up: anchor 0, unloaded, location cleared, archived in Spoolman.
  `3d printops spool empty 7 --note "ran out on plate 7"`
- `import-owned` — spools from a project's `gen/plates.json`: one per `owned` entry
  (colour name → hex), PLA unless `bought` names the product (vendor, PLA+/PETG/…, price in
  дин). The vendor is the longest vendor of the tare DB (`3d printops tare ls`) the product
  starts with (`Bambu Lab PLA Basic` → `Bambu Lab`), else its first word. No guessed vendor
  or net: such spools show "never weighed". Prints the plan — `+`
  create (marking NEW vendor/filament), `=` already imported (with fields that drifted from the
  source, never overwritten) — and writes nothing without `--apply`. Keyed by
  `<project>:owned:<name>` in the spool extra field `external_key`, so a re-run changes nothing.
  `3d printops spool import-owned ~/xp/garage-band/projects/babushkin-dom/gen/plates.json --location WH:default`

## `3d printops tare`

```
3d printops tare ls [VENDOR]
3d printops tare add VENDOR GRAMS --source TEXT [--kind cardboard|plastic|refill] [--net G] [--sigma G] [--note TEXT]
3d printops tare show SPOOL_ID
```

- `ls` — the tare DB, all vendors or one (`Bambu Lab` = `bambulab`). `3d printops tare ls elegoo`
- `add` — a known empty-spool weight. `--source` is required (a URL, or how it was weighed).
  `3d printops tare add Elegoo 154 --kind cardboard --net 1000 --source "weighed empty RAPID PLA+ spool 2026-10-14"`
- `show` — the tare a weighing of this spool would subtract, and where it comes from; for an
  ambiguous vendor, the candidate rows. `3d printops tare show 7`

## REST (3d-api)

| Method | Path | Scope | Body / query |
|---|---|---|---|
| GET | `/v1/spools` | read | `material, color, vendor, location, loaded, archived` |
| POST | `/v1/spools` | write | receive fields (`vendor, material, name, color_hex, net_g, remaining_g, tare_g, spool_kind, lot_nr, price, location, opened, …`) |
| POST | `/v1/spools/import` | write | `{items: [receive fields + external_key], dry_run}` |
| GET | `/v1/spools/{id}` | read | `ledger=N` |
| GET | `/v1/spools/{id}/ledger`, `/v1/spools/{id}/tare` | read | |
| POST | `/v1/spools/{id}/move` | write | `{location}` |
| POST | `/v1/spools/{id}/use` | write | `{grams, source, confidence_g?, job_id?, note?}` |
| POST | `/v1/spools/{id}/measure` | write | `{gross_g, tare_g?, tare_source?, scale_sigma_g?, note?}` |
| POST | `/v1/spools/{id}/empty` | write | `{note?}` |
| POST | `/v1/printers/{p}/slots/{s}/load` | write | `{spool_id, replace_to?}` (`s` = `T3` or `ams1/2`) |
| POST | `/v1/printers/{p}/slots/{s}/unload` | write | `{to?, gross_g?, spool_id?}` |
| GET / POST | `/v1/tares` | read / write | `vendor` / `{vendor, tare_g, source, spool_kind?, net_g?, sigma_g?, note?}` |
| GET | `/v1/lint` | read | `weigh_days` — the data rules below |

Errors: `{"detail": message, "info": {...}}` — 404 unknown spool/printer/slot/location, 409
unusable head / occupied slot / archived spool / no tare / duplicate import key, 422 invalid
input, 502 Spoolman failed.

Events: `spool.received`, `spool.moved`, `slot.loaded` / `slot.unloaded` (subject
`slot:u1/T3`, payload `printer`, `head`, `spool_id`), `spool.used`, `spool.measured` (gross,
tare and its source, net, ±, previous interval), `spool.empty`.

Python API for other slices: `printops.ledger.book(conn, spoolman, spool_id, grams, source,
confidence_g, job_id=None, note=None, *, actor)`, `ledger.remaining(conn, spoolman, spool_id)
-> Interval`, `ledger.note(conn, spool_id, text, *, actor)` (inside the caller's transaction).

## Lint (`3d lint --printops`)

3d-api runs these over every non-archived spool (`GET /v1/lint`, read-only):

| Rule | Level | Checks |
|---|---|---|
| `spools/remaining-negative` | error | the ledger books more than the spool had — weigh it |
| `spools/no-tare` | warn | no tare anywhere in the chain, so it cannot be weighed |
| `spools/not-weighed` | warn | never weighed, or last weighed more than `--weigh-days` (30) days ago; a sealed spool received with a known net and not used yet is exempt |
