# `3d printers fw` — firmware, root and Klipper-unlock plans per vendor profile

Detect which firmware state a printer is in, print the ordered plan to another state, back up what the
runbook says to save, and — only behind every safety gate — carry the plan out. Driven by vendor
**profiles** (`lib/data/firmware/*.yaml`, schema and shipped table in
[../printers/firmware-profiles.md](../printers/firmware-profiles.md)) matched on each fleet printer's `model:`;
it works for every printer of `fleet/printers.yaml` + `printer-devices.yaml` and nothing in the code is keyed on a
printer name. Alias: `3d printers firmware`. Issue: <https://github.com/alex-mextner/3d-cli/issues/519>.

The runbooks it encodes: [K1 SE root + Helper Script](../printers/k1se-root-upgrade.md),
[U1 paxx12 Extended Firmware](../printers/u1-paxx12-upgrade.md); Bambu printers (closed firmware, **no Klipper
possible**) get the documented LAN Only / Developer mode / downgrade paths as manual steps.

## Usage

```
3d printers fw status [PRINTER|--all] [--json] [--no-live] [--no-ssh] [--verbose]
3d printers fw plan PRINTER --to STATE [--from STATE] [--also STEP_ID ...] [--no-ssh] [--json]
3d printers fw backup PRINTER [--dest DIR] [--no-ssh] [--json]
3d printers fw apply PRINTER --to STATE [--yes] [--confirm NAME] [--backup-dir DIR] [--backup-max-age AGE]
                     [--also STEP_ID ...] [--file [STEP_ID=]PATH ...] [--no-ssh] [--json]
3d printers fw profiles [--json]
```

(`myprinter` below stands for a name from `3d printer list`.)

## `status`

Detects the state with READ-ONLY probes (HTTP GET, TCP connect/banner, read-only ssh commands). The detected
state is the last state of the profile whose evidence is present; the evidence per probe is available with
`--verbose`. A printer whose model has no profile prints `no firmware profile for model <model>` (exit 0: it is
information). An offline fleet printer is never contacted.

| Flag | Meaning | Example |
|---|---|---|
| `PRINTER` / `--all` | one printer, or every printer of the registry | `3d printers fw status myprinter` · `3d printers fw status --all` |
| `--json` | machine-readable (`printers[]` with `profile`, `state`, `detection.evidence[].probes[]`) | `3d printers fw status myprinter --json` |
| `--no-live` | profile resolution only, no network | `3d printers fw status --all --no-live` |
| `--no-ssh` | skip ssh and ssh-banner probes (a slow dropbear takes ~30 s to answer); detection becomes `partial` | `3d printers fw status myprinter --no-ssh` |
| `--verbose` | show every probe and what it saw | `3d printers fw status myprinter --verbose` |

## `plan`

Prints the ordered plan — steps with kind, risk, the exact command / endpoint / instruction, preconditions, the
backups required, the verification, the rollback and the sources — from the detected state (or `--from`) to the
target. **It never executes anything**; with `--from` it does not even contact the printer.

| Flag | Meaning | Example |
|---|---|---|
| `--to STATE` | target state (`3d printers fw profiles` lists them) | `3d printers fw plan myprinter --to extended` |
| `--from STATE` | assume this state instead of detecting; the only way to plan for an offline printer | `3d printers fw plan myprinter --from stock --to developer-mode` |
| `--also ID` | add an optional step of a transition on the path (repeatable); a guard-forbidden id is refused with its reason | `3d printers fw plan myprinter --to klipper-ready --also install-gcode-shell-command` |
| `--no-ssh` | skip ssh probes while detecting the state | `3d printers fw plan myprinter --to extended --no-ssh` |
| `--json` | machine-readable (`steps[]`, `violations[]`, `detected`) | `3d printers fw plan myprinter --to extended --json` |

Exit codes: 0 plan printed (an empty plan = already there); 1 the target is unreachable from the state or a
guard refuses an `--also`; 2 unknown state / bad flag / offline printer without `--from`.

## `backup`

Read-only pull of the profile's backup items that apply in the printer's state into
`DIR/<printer>-<profile>-<UTC>/` (directory 0700, files 0600): the files, a `SHA256SUMS` and a
`manifest.json` — profile id, printer name, model, state, creation time and per item the source, file, size,
SHA-256 and status (`ok`, `failed`, `missing` for an absent optional item (HTTP 404 / no such file), `skipped` for an item that
does not apply in the printer's state or an optional HTTP item whose port refuses the connection, `manual`). Exit 1 when an item
failed: `apply` will not accept that backup.

| Flag | Meaning | Example |
|---|---|---|
| `--dest DIR` | where to write (default `~/.local/share/3d-cli/firmware-backups`) | `3d printers fw backup myprinter --dest ~/fw-backups` |
| `--no-ssh` | take only the HTTP items (ssh items then fail the backup: it would not be accepted) | `3d printers fw backup myprinter --no-ssh` |
| `--json` | the manifest as JSON | `3d printers fw backup myprinter --json` |

## `apply`

**Dry run by default**: prints what would run and the state of each gate, and sends nothing but reads. With
`--yes` it runs the plan — and executes **only** steps flagged `automatable` (the HTTP upload of the firmware file
*you* supply, the ssh scripts and endpoints the runbooks give); a manual step is shown, then verified by its probe.

The gates, all machine-enforced (each has a failing and a passing test):

| Gate | Refusal | Exit |
|---|---|---|
| `--yes` | without it nothing executes | 0 (dry run) |
| fleet access | an offline fleet printer is never contacted (`apply` and `backup` refuse it); a `read-only` printer is never changed | 1 |
| typed name | `--confirm NAME` must equal the printer's fleet name (interactive prompt otherwise; a script without `--confirm` is refused) | 2 |
| state | the printer's state must be detected, and the target must be reachable from it | 1 |
| guards | a profile guard violation (forbidden `--also`, missing hardening step, forbidden command) quotes its reason and source | 1 |
| backup | a FRESH backup of this printer: manifest younger than 24 h (`--backup-max-age`), no failed item, files intact, holding the first step's backup ids | 1 |
| idle | adapter status, else the profile's idle probe; printing / paused / busy / unreadable is refused | 1 |
| per step | before: idle again, backup ids present, `skip_if`, preconditions, invariants; after: the verification probe (polled for reboots; a reboot step must first be SEEN going down, see `down_probes`) and the invariants — a failure STOPS, prints the step's rollback. A connection dropped during an upload/POST counts only for a step that expects the reboot | 1 |

Manual steps: interactive runs wait for Enter and re-check the probe; scripts stop at the first pending manual
step with `NEXT: do the step, then re-run the same command` (exit 1). The state is re-detected on every run and a
step whose verification already holds is skipped, so `apply` is resumable.

Resuming never skips a security step: a profile makes the hardening part of the target state's DETECTION (the K1's
`klipper-ready` needs Moonraker locked down: `trusted_clients` narrowed, proxy headers off, nginx denying other
hosts), so a K1 whose Moonraker came up but still trusts the whole LAN reads as `moonraker-installed` and a re-run
of the same command stops at the lock-down step instead of reporting "nothing to do".

| Flag | Meaning | Example |
|---|---|---|
| `--to STATE` | target state | `3d printers fw apply myprinter --to extended` |
| `--yes` | execute (without it: dry run) | `3d printers fw apply myprinter --to extended --yes --confirm myprinter` |
| `--confirm NAME` | the printer's fleet name, typed explicitly | `3d printers fw apply myprinter --to extended --yes --confirm myprinter` |
| `--backup-dir DIR` | where `fw backup` wrote (default as for `backup`) | `3d printers fw apply myprinter --to klipper-ready --backup-dir ~/fw-backups` |
| `--backup-max-age AGE` | newest allowed backup age: `24h` (default), `90m`, `2d`; a bare number is hours | `3d printers fw apply myprinter --to klipper-ready --backup-max-age 6h` |
| `--also ID` | optional step (repeatable) | `3d printers fw apply myprinter --to klipper-ready --also install-usb-camera` |
| `--file [ID=]PATH` | the firmware file an upload step sends; a bare `PATH` when the plan has one upload step | `3d printers fw apply myprinter --to extended --file ~/fw/update.bin` |
| `--no-ssh` | never use ssh: ssh probes are skipped, and a plan containing an `ssh_script` / `ssh_command` step is "not ready" in the dry run and REFUSED (exit 1, before anything runs) with `--yes` | `3d printers fw apply myprinter --to extended --no-ssh` |
| `--json` | machine-readable event list (`events[]` with `kind`, `message` and data); every error is one `{"result": "error", "exit_code": N, "error": "...", "events": [...]}` document, exit code kept | `3d printers fw apply myprinter --to extended --json` |

An upload file is checked before anything runs (suffix, minimum size, SHA-256 printed and compared with the
profile's known list; an unknown hash is a warning that tells you to compare it with the release notes).

## `profiles`

Lists every profile: vendor, models matched, states, transitions and sources.

| Flag | Meaning | Example |
|---|---|---|
| `--json` | machine-readable | `3d printers fw profiles --json` |

## Exit codes

`0` ok / dry run / nothing to do · `1` refused for safety, verification failed or a manual step is pending ·
`2` usage / invalid argument (bad flag, unknown state, wrong `--confirm`, missing `--file`) ·
`127` the `ssh` binary is missing.

## Credentials and ssh

ssh runs through a command runner (`ssh -o BatchMode=yes`: key or agent only). 3d stores and sends no passwords;
a profile's `ssh.credentials` only documents the vendor default (for example the public root password a vendor
shows on the screen) so you can log in once by hand, install your key and change it. Connect once by hand so the
printer's host key is in `known_hosts`. `THREED_FW_SERVICE_PORTS=name=PORT[,name=PORT]` redirects a profile's
named services (for a port-forward); a name that is a service of no profile, or a port that is not 1-65535, is a
usage error.

## Safety notes

- Rooting and flashing void or limit vendor support; the profile steps carry the vendor's own warnings.
- Never run `apply` while a job runs; the gates enforce it, but the printer you do not see (a neighbour's) is
  not in this tool's reach: offline fleet printers are never contacted.
- What no source verifies is marked `[INFERENCE]` in the profile and listed in
  [firmware-profiles.md](../printers/firmware-profiles.md#what-is-not-verified).
