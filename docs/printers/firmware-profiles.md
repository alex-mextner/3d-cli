# Firmware profiles — the data behind `3d printers fw`

`3d printers fw` ([command reference](../commands/printers-fw.md)) knows nothing about any
particular printer. Everything it knows about a printer *family* lives in one YAML file under
`lib/data/firmware/`: which fleet `model` names it covers, how to tell which firmware state a printer
is in, the transitions between states and the ordered steps of each, what to back up first, and the
pitfalls that must never be done. This page is the schema reference, the "how do I add a printer"
guide and the table of the shipped profiles with their sources.

Issue: <https://github.com/alex-mextner/3d-cli/issues/519>. The runbooks the K1 and U1 profiles encode
line by line: [`k1se-root-upgrade.md`](k1se-root-upgrade.md), [`u1-paxx12-upgrade.md`](u1-paxx12-upgrade.md).

## How a printer finds its profile

`fleet/printers.yaml` (and `~/.config/3d-cli/printer-devices.yaml`) give every printer a `model:`.
`3d printers fw` runs each profile's `models:` matchers against that string (case-insensitive
`contains`, or an unanchored `regex` — anchor it yourself). The printer's **name** is never matched.
A model that matches no profile prints `no firmware profile for model <model>`; a model matching two
profiles is a load error (narrow the matchers). The tests (`tests/test_printer_fw_parity.py`) run this
for every printer of the real fleet registry and scan the generic code (`lib/printer_fw/**`,
`lib/printers_cli/fw.py`) to prove no fleet printer name, model or host is a string literal there.

## Schema

```yaml
id: creality-k1                 # lowercase letters, digits, . _ -
vendor: Creality
family: K1 series
models:                         # one of `contains` / `regex` per entry
  - contains: "snapmaker u1"
  - regex: '^creality\s+k1(\s?(se|c|max))?$'
sources: [https://...]          # REQUIRED http(s) URLs; every vendor fact must be traceable to one
capabilities:
  klipper_possible: true        # honest: false for closed firmware (Bambu)
  notes: "what is and is not possible, with the source"
services: {web: 80, moonraker: 7125}     # named ports probes/steps refer to
adapter_services: {moonraker: moonraker} # registry adapter -> the service its `port:` is
ssh:                                     # only when ssh probes/steps are used
  user: root
  credentials: "where the secret comes from (documentation only; 3d stores none)"
  connect_timeout_s: 90
idle_probe: {...}               # a probe that holds ONLY for a readable, idle printer
backups: [...]                  # what a runbook says to save first
guards: [...]                   # machine-checked pitfalls
states: [...]
transitions: [...]
```

Unknown keys are errors (typos must not pass), every problem of a file is reported at once with the
path of the key (`snapmaker-u1.yaml: states[1].detection.all[0].kind: unknown probe kind 'x'; accepted: ...`).

### Services and ports

A probe or step never contains a port number; it names a `service`. Ports resolve as: the profile's
`services:` default, then the registry's own `port:` for the service the printer's adapter speaks
(`adapter_services:` — e.g. a `moonraker` adapter's port is the `moonraker` service), then the
`THREED_FW_SERVICE_PORTS=name=PORT[,name=PORT]` environment override (port-forwards, loopback test
fakes). The `ssh` service defaults to 22 when the profile has an `ssh:` block.

### Probes (read-only)

Used in `detection`, `preconditions`, `skip_if`, `verify`, `idle_probe` and invariant guards. Every
probe may set `id`, `title`, `negate` (flip ok/fail), `absent_ok` and `timeout` (seconds).

| kind | fields | holds when |
|---|---|---|
| `http_get` | `service`, `path`, `expect_status` (default 200, int or list), `json_path` (`a.b.0.c`), `equals`, `regex` | the GET answers an expected status and, if given, the JSON value equals / matches (or the body matches `regex` when there is no `json_path`) |
| `adapter_kind` | `equals` | the registry's adapter name equals it (static, no contact) |
| `tcp_open` | `service` | a TCP connection opens |
| `tcp_banner` | `service`, `regex`, `timeout` | the service sends a banner matching `regex` (an sshd announcing itself) |
| `ssh_command` | `command`, optional `regex` | the remote command exits 0 (and stdout matches). Validated at load so a probe cannot change a printer: the first word must be one of `cat test ls grep head stat uname readlink wc df true` and every character must be a letter, digit, space or one of `_ . / : = @ % + , ' " ^ -` — no newline, `$`, `;`, `&`, `\|`, `<`, `>`, backtick, backslash or braces |
| `ssh_file_exists` | `path` | `test -e PATH` succeeds |
| `confirm` | `prompt` | the user answers yes (interactive only; never usable for detection) |

A probe has a tri-state outcome: `ok`, `fail` (definitely not there — a refused connection, or an HTTP status other
than the expected one, is a fail) and `error` (could not tell: timeout, ssh auth failure, an answer longer than
256 KiB; `skipped` for `--no-ssh` and non-interactive `confirm`). `negate` flips ok/fail and leaves error/skipped
alone, so a rebooting printer is never read as "firmware not present". A **negated** `http_get` with a `json_path`
also fails closed on an answer that is not JSON or lacks the path (`error`, not "absent"), so a hardening check
cannot pass vacuously against a page of an unexpected shape; add `absent_ok: true` for an option that is
legitimately missing when it is unset (for example an auth switch left at its default) or to read a stock
server's UI fallback page as "that API is not here". Pair a negated probe with a positive "readable" probe first
(an `all` group stops at the first probe that does not hold).

### Reuse: probe library and snippets

```yaml
probes:                         # named probes, used as {ref: NAME} anywhere a probe is expected
  moonraker-up: {kind: http_get, service: moonraker, path: /server/info, title: Moonraker answers}
snippets:                       # named text, used as {{snippet.NAME}} inside any string (a shared script header)
  k1drive: |
    cat > /usr/data/k1drive.sh <<'DRIVER'
    ...
states:
  - id: up
    detection: {all: [{ref: moonraker-up}, {ref: moonraker-up, negate: true, id: not-up}]}   # fields override
```

Both expand BEFORE validation, so a `ref`/snippet problem is reported like any other (`unknown probe ref 'x'`);
a ref keeps the library name as its probe `id` unless it sets one.

### States

```yaml
states:
  - id: rooted
    title: Root enabled and key-based ssh works
    requires: [stock]          # probed only after these matched (no ssh probe against a dead port)
    detection:                 # `all:` or `any:`; omit `detection` = not machine-detectable
      all:
        - {kind: tcp_banner, service: ssh, regex: '^SSH-2\.0-'}
    notes: ...
    sources: [...]
```

List states from least to most modified. `fw status` evaluates them in order; the **detected state is
the last one that matched** (later states include the evidence of earlier ones). An empty `all:` is a
baseline that always matches. Detection is `partial` when some probe could not tell. A profile whose
states have no `detection` at all (closed firmware that needs credentials to read) reports `unknown`
and is planned with `--from STATE`.

### Transitions and steps

```yaml
transitions:
  - id: install-moonraker
    from: helper-installed
    to: moonraker-installed
    title: ...
    sources: [...]
    steps: [...]               # required, in order
    optional_steps: [...]      # selectable with --also STEP_ID
```

A **target** is a state id. `plan` = the shortest path over the transitions (breadth-first, ties go to
the transition listed first) from the detected state to the target. A transition with `from == to`
is a **maintenance** transition: no `steps`, only `optional_steps` acting on that state (an in-place
firmware upgrade); an `--also` step is valid only when its transition lies on the path.

```yaml
- id: fetch-helper-script
  title: ...
  kind: manual | http_upload | http_post | ssh_script | ssh_command
  automatable: true            # only for non-manual steps with a verify probe
  risk: low | medium | high    # high = can brick; medium/high REQUIRE a rollback
  backup: [printer-cfg]        # backup ids that must be in the backup before this step
  preconditions: [probe, ...]  # all must hold, else the run stops before the step
  skip_if: [probe, ...]        # all hold -> the step is already satisfied and skipped
  command: |                   # ssh_script / ssh_command: the exact text that runs (sh -s on stdin)
  instruction: |               # manual: what the user does
  upload: {service, path, field: file, accept_suffix, min_bytes, known_sha256: [...], expect_status, success_regex}
  post: {service, path, expect_status, success_regex}
  verify: [probe, ...]         # or {wait_s: 900, interval_s: 10, down_probes: [...], down_wait_s: 300, probes: [...]}  REQUIRED
  rollback: {instruction: "...", to: stock}
  sources: [...]
  notes: ...
  timeout_s: 300
```

Rules enforced at load: `automatable: true` is only valid where the runbook gives a precise command or
endpoint **and** a `verify` probe exists (3d never runs a step it cannot check); a manual step cannot be
automatable and every non-manual step MUST be automatable (a step 3d cannot run is a `manual` step); a step
may only require `backup:` ids that 3d can take itself (not `manual` items); `upload.field` must be `file`
(the part name the shared uploader sends). **Only automate what the runbook documents completely**: a menu
function that reads answers from stdin is automatable only when the runbook gives those answers (the K1
Remove menus: `printf 'y\ny\n'`); the Install menus are manual steps whose instruction carries the commands.

`verify.wait_s` polls for the minutes a reboot takes. A step that reboots the printer lists `down_probes`
(with `down_wait_s`): those must first STOP holding — the printer is seen going down — before `probes` are polled,
because a verification that already holds before the action says nothing about it. A dropped connection during an
upload or POST is believed only for such a step (then the verification decides); anywhere else it is a failed
step, and a refused connection (nothing was sent) always is.

A manual step is checked by its `verify` probe: already holding means "done earlier" and the step is skipped;
otherwise the instruction is shown and (interactive) the tool waits for Enter and re-checks — once per answer, so a
`confirm` probe asks its question once — or (non-interactive) the run stops with
`NEXT: do the step, then re-run the same command`.

### Backups

```yaml
backups:
  - id: printer-cfg
    description: ...
    via: http_get | ssh_file | ssh_tar | manual
    service: moonraker  path: /server/files/config/printer.cfg    # http_get
    path: /usr/data/printer_data/config/printer.cfg               # ssh_file (cat)
    base: /usr/data  paths: [printer_data/config]                 # ssh_tar (tar czf -, archive verified)
    optional: true               # absent = `missing` (404 / no such file) or `skipped` (connection refused); never a failure
    states: [rooted]             # taken only in these states (ssh items need root)
    expect_regex: '(?m)^#\*# \['  # the content must match (a SAVE_CONFIG block must exist)
    expect_tar_members: [printer_data/config/printer.cfg]
```

`fw backup` writes `DIR/<printer>-<profile>-<UTC>/` (0700, files 0600) with the files, `SHA256SUMS` and
`manifest.json` (profile id, printer, model, state, time, per item source/file/size/sha256/status).
`fw apply` accepts only the NEWEST manifest of that printer and profile that is younger than the age
limit (24 h, `--backup-max-age`), has no failed item, still matches its files' SHA-256 and holds the
backup ids the first step requires; later steps are checked right before they run (the printer's state,
and so the items that can be taken, changes on the way — a refusal there asks for a new `fw backup`).

### Guards (machine-checked pitfalls)

A guard is data — `id`, `kind`, `applies_when: {from: [states], to: [states]}` (empty = always),
`reason`, `sources` — evaluated by generic code (`lib/printer_fw/guards.py`). `--yes` never skips a
guard, and the refusal quotes `reason` and the source URLs.

| kind | fields | enforced |
|---|---|---|
| `forbidden_option` | `options: [ids]` | `--also ID` is refused (exit 1) and a plan that contains a step with that id is refused |
| `forbidden_command_pattern` | `pattern` (regex) | no command of the plan may match |
| `step_requires_command` | `step`, `pattern` | the step's command (a manual step: its instruction) must contain the pattern — checked at profile load and on every plan |
| `step_requires_step` | `step`, `requires_step` | a plan containing `step` must contain `requires_step` |
| `require_idle` | — | the printer must be idle before every step; the guard's reason is quoted in the refusal |
| `invariant_probe` | `probe` (one probe or a list; all must hold) | before and after every step; an unreadable invariant counts as violated |

`scope: referenced` limits an invariant to steps that list `{guard: ID}` in `preconditions`.

### The idle check

`apply` refuses while the printer is printing, paused, busy or unreadable — at the start AND before every
step (a step runs minutes after the first check). It asks the registry adapter's status (`moonraker` /
`creality-stock`); if that cannot be read, the profile's `idle_probe` decides, and it holds only for a
*readable, idle* printer (an HTTP error, a body of another shape or an unreachable printer all count as busy).
No adapter and no `idle_probe` = busy. A `require_idle` guard supplies the `reason` and sources quoted in
the refusal.

### SSH and secrets

ssh runs through an injected `CommandRunner`; the real one is `ssh -o BatchMode=yes` (key or agent
only). 3d stores and sends no password. The profile's `ssh.credentials` only documents the vendor
default (for example the public root password a vendor prints on the screen) so you can log in once by
hand, install your key and change the password. A missing `ssh` binary is exit 127. ssh
host-key policy is ssh's own: connect once by hand so the key is in `known_hosts`.

## Adding a printer

1. Find the primary sources (vendor wiki, the upstream project README/source). Do not invent URLs,
   versions, checksums or exploits; mark what no source states `[INFERENCE]` or leave it out.
2. Copy the closest file in `lib/data/firmware/` and set `id`, `vendor`, `family`, `models`
   (matchers on the fleet `model` string), `sources`, `capabilities` (`klipper_possible` honestly).
3. Write the states (least to most modified) with read-only probes, then each transition's steps.
   Mark a step `automatable` only when a runbook gives the exact command/endpoint and a probe can verify it;
   everything else is `manual` with the instruction and a pointer to the primary doc.
4. Put every runbook "do not" into `guards:` and every "back up X first" into `backups:`.
5. Run `3d printers fw profiles` (the loader validates the file) and
   `pytest tests/test_printer_fw_profiles.py tests/test_printer_fw_parity.py` (sources are URLs, probes
   read-only, steps follow the rules, no fleet literal in code). No code change is needed.

## The shipped profiles

Maintained by hand from the YAML (the YAML is the source of truth); `tests/test_printer_fw_docs.py` fails when a
profile, state, transition, step, guard or source URL of a shipped file is missing from this page. "Detected by"
lists the probes (`not` = negated). Every URL below is cited by the profile.

### `bambu-lab-p1-a1` — Bambu Lab P1 and A1 series (P1P, P1S, A1, A1 mini)

- **Models matched** (against the fleet entry's `model`, never its name): `regex /^bambu\s*lab\s+(p1p|p1s|a1|a1\s*mini)$/`
- **Klipper possible:** **no** — Klipper cannot be installed on Bambu P1/A1 printers: they run Bambu's closed firmware and none of the sources of this profile describes a Klipper port. What is possible: LAN Only mode (printer works on the local network without the cloud), Developer mode (opens the MQTT channel, live stream and FTP so third-party software works without Bambu's authorization checks; only together with LAN Only mode, never with the cloud) and installing an older firmware through Bambu Handy. The Authorization Control System arrived in firmware 01.08.02.00 for P1 and 01.05.00.00 for A1 (SimplyPrint helpdesk; the Bambu wiki gives no version numbers for P/A series).
- **Profile file:** `lib/data/firmware/bambu-lab-p1-a1.yaml`

| State | Detected by | Meaning |
|---|---|---|
| `stock` | not machine-detectable (use `--from`) | Stock firmware, cloud mode (Bambu Handy / Bambu Studio cloud) |
| `lan-mode` | not machine-detectable (use `--from`) | LAN Only mode on (local network, access code, no cloud) |
| `developer-mode` | not machine-detectable (use `--from`) | LAN Only mode + Developer mode (third-party software works without authorization) |
| `pre-authorization-firmware` | not machine-detectable (use `--from`) | Older firmware without the Authorization Control System (downgraded) |

| Transition | Steps (kind, risk) | Sources |
|---|---|---|
| `stock` → `lan-mode` (enable-lan-mode) | `enable-lan-only-mode` (manual, low) | <https://wiki.bambulab.com/en/knowledge-sharing/enable-lan-mode> |
| `lan-mode` → `developer-mode` (enable-developer-mode) | `enable-developer-mode-on-screen` (manual, medium) | <https://wiki.bambulab.com/en/knowledge-sharing/enable-developer-mode><br><https://wiki.bambulab.com/en/software/third-party-integration> |
| `stock` → `pre-authorization-firmware` (downgrade-firmware) | `downgrade-through-bambu-handy` (manual, medium) | <https://wiki.bambulab.com/en/knowledge-sharing/firmware-downgrade> |

Sources: <https://wiki.bambulab.com/en/knowledge-sharing/enable-lan-mode>; <https://wiki.bambulab.com/en/knowledge-sharing/enable-developer-mode>; <https://wiki.bambulab.com/en/software/third-party-integration>; <https://blog.bambulab.com/firmware-update-introducing-new-authorization-control-system-2/>; <https://wiki.bambulab.com/en/knowledge-sharing/firmware-downgrade>; <https://help.simplyprint.io/en/article/bambu-lab-security-firmware-authorization-control-system-update-will-i-still-be-able-to-use-simplyprint-y2uoor/>

### `bambu-lab-x1` — Bambu Lab X1 series (X1, X1 Carbon, X1E)

- **Models matched** (against the fleet entry's `model`, never its name): `regex /^bambu\s*lab\s+x1(c|e|\s*carbon)?$/`
- **Klipper possible:** **no** — Klipper cannot be installed on Bambu X1 printers on stock firmware. Possible: LAN Only mode, Developer mode (third-party software without authorization; the Authorization Control System arrived with X1 firmware 01.08.03.00) and the community X1Plus firmware, an overlay on Bambu's firmware (https://github.com/X1Plus/X1Plus README: "custom firmware for Bambu Lab X1 and X1 Carbon"), which is not Klipper either.
- **Profile file:** `lib/data/firmware/bambu-lab-x1.yaml`

| State | Detected by | Meaning |
|---|---|---|
| `stock` | not machine-detectable (use `--from`) | Stock firmware, cloud mode |
| `lan-mode` | not machine-detectable (use `--from`) | LAN Only mode on (local network, access code, no cloud) |
| `developer-mode` | not machine-detectable (use `--from`) | LAN Only mode + Developer mode |
| `x1plus` | not machine-detectable (use `--from`) | X1Plus custom firmware installed |

| Transition | Steps (kind, risk) | Sources |
|---|---|---|
| `stock` → `lan-mode` (enable-lan-mode) | `enable-lan-only-mode` (manual, low) | <https://wiki.bambulab.com/en/knowledge-sharing/enable-lan-mode> |
| `lan-mode` → `developer-mode` (enable-developer-mode) | `enable-developer-mode-on-screen` (manual, medium) | <https://wiki.bambulab.com/en/knowledge-sharing/enable-developer-mode> |
| `stock` → `x1plus` (install-x1plus) | `install-x1plus-from-its-wiki` (manual, high) | <https://github.com/X1Plus/X1Plus><br><https://github.com/X1Plus/X1Plus/wiki> |

Sources: <https://wiki.bambulab.com/en/knowledge-sharing/enable-lan-mode>; <https://wiki.bambulab.com/en/knowledge-sharing/enable-developer-mode>; <https://wiki.bambulab.com/en/software/third-party-integration>; <https://blog.bambulab.com/firmware-update-introducing-new-authorization-control-system-2/>; <https://github.com/X1Plus/X1Plus>; <https://github.com/X1Plus/X1Plus/wiki>

### `creality-k1` — Creality K1 series (K1, K1 SE, K1C, K1 Max)

- **Models matched** (against the fleet entry's `model`, never its name): `regex /^creality\s+k1(\s?(se|c|max))?$/`
- **Klipper possible:** yes — The stock K1 firmware runs Klipper but ships no Moonraker (the stock web UI on :80 and the ws :9999 API are Creality's own `web-server`). Creality's official root access plus Guilouz's Creality Helper Script install Moonraker + Fluidd and keep the stock stack, so `3d printer` can use the moonraker adapter. The Helper Script supports K1-series firmware 1.3.3.5 and later; some 2025 K1C units ship without the root menu item (stop there, do not try exploits).
- **Profile file:** `lib/data/firmware/creality-k1.yaml`

| State | Detected by | Meaning |
|---|---|---|
| `stock` | all of: stock web server answers GET /info | Creality stock firmware (web-server on :80 and ws :9999; no Moonraker, no key-authenticated ssh) |
| `rooted` | all of: dropbear sends an SSH banner; key-based ssh login works | Root enabled on the screen and key-based ssh works |
| `helper-installed` | all of: /usr/data/helper-script/helper.sh exists (probed only after rooted) | Creality Helper Script present in /usr/data/helper-script |
| `moonraker-installed` | all of: Moonraker answers /server/info | Moonraker answers, but its LAN access is NOT narrowed yet (open to the whole LAN) |
| `klipper-ready` | all of: Moonraker reports klippy_state ready; Moonraker reports its trusted_clients; not trusted_clients lists single hosts, no CIDR range; use_xheaders is off; nginx.conf denies other hosts (`deny all`) (probed only after moonraker-installed) | Moonraker reports Klipper ready AND is locked down (trusted_clients narrowed, xheaders off, nginx denies other hosts) |

| Transition | Steps (kind, risk) | Sources |
|---|---|---|
| `stock` → `rooted` (enable-root) | `enable-root-on-screen` (manual, medium)<br>`install-ssh-key` (manual, low) | <https://guilouz.github.io/Creality-Helper-Script-Wiki/firmwares/install-and-update-rooted-firmware-k1/><br><https://github.com/alex-mextner/3d-cli/blob/main/docs/printers/k1se-root-upgrade.md> |
| `rooted` → `helper-installed` (install-helper-script) | `fetch-helper-script` (ssh_script, automatable, medium) | <https://guilouz.github.io/Creality-Helper-Script-Wiki/helper-script/helper-script-installation/><br><https://github.com/alex-mextner/3d-cli/blob/main/docs/printers/k1se-root-upgrade.md> |
| `helper-installed` → `moonraker-installed` (install-moonraker) | `remove-preinstalled-moonraker-fluidd` (ssh_script, automatable, medium)<br>`install-moonraker-nginx` (manual, medium) | <https://github.com/alex-mextner/3d-cli/blob/main/docs/printers/k1se-root-upgrade.md><br><https://guilouz.github.io/Creality-Helper-Script-Wiki/helper-script/moonraker-k1/> |
| `moonraker-installed` → `klipper-ready` (finish-moonraker) | `install-fluidd` (manual, medium)<br>`narrow-trusted-clients` (manual, medium)<br>`install-gcode-shell-command` (manual, low) *optional, `--also`*<br>`install-save-zoffset-macros` (manual, low) *optional, `--also`*<br>`install-usb-camera` (manual, medium) *optional, `--also`* | <https://github.com/alex-mextner/3d-cli/blob/main/docs/printers/k1se-root-upgrade.md> |

| Guard | Kind | Why (quoted in the refusal) | Sources |
|---|---|---|---|
| `never-flash-while-printing` | printer must be idle | Do not start while the K1 prints (runbook): rooting, the Helper Script installs and the reboots they need interrupt a job. | <https://github.com/alex-mextner/3d-cli/blob/main/docs/printers/k1se-root-upgrade.md> |
| `keep-creality-web-server-up` | must hold after every step | The 3d creality-stock adapter, HA ha_creality_ws and Creality Print uploads use the stock `web-server` (:80 and ws :9999); nothing may stop it. After every step the stock GET /info must still answer. | <https://github.com/alex-mextner/3d-cli/blob/main/docs/printers/k1se-root-upgrade.md> |
| `no-remove-creality-web-interface` | forbidden option: `remove-creality-web-interface`, `customize-remove-creality-web-interface` | Do NOT use Helper Script Customize > "Remove Creality Web Interface": it renames /usr/bin/web-server and /usr/bin/Monitor to *.disabled, kills them and puts Fluidd on :80. :80 and :9999 are both served by that web-server, so removing it breaks the 3d creality-stock adapter, HA ha_creality_ws and Creality Print uploads. | <https://github.com/alex-mextner/3d-cli/blob/main/docs/printers/k1se-root-upgrade.md><br><https://guilouz.github.io/Creality-Helper-Script-Wiki/helper-script/remove-and-restore-creality-web-interface/> |
| `no-guppy-screen` | forbidden option: `install-guppy-screen`, `guppy-screen`, `customize-guppy-screen` | Do not install Guppy Screen: it stops the same stock stack (web-server) that the creality-stock adapter and ws :9999 depend on. | <https://github.com/alex-mextner/3d-cli/blob/main/docs/printers/k1se-root-upgrade.md><br><https://guilouz.github.io/Creality-Helper-Script-Wiki/helper-script/guppy-screen/> |
| `fluidd-needs-world-readable` | step `install-fluidd` must run `chmod -R a\+rX /usr/data/fluidd` | The Fluidd v1.37.6 zip unpacks as 0600/0700 and nginx (user www-data) answers 403 until `chmod -R a+rX /usr/data/fluidd` is run. | <https://github.com/alex-mextner/3d-cli/blob/main/docs/printers/k1se-root-upgrade.md> |
| `narrow-trusted-clients-after-moonraker` | step `install-moonraker-nginx` needs `narrow-trusted-clients` | The Helper Script's Moonraker trusts 192.168.0.0/16 and listens on 0.0.0.0:7125, so the whole LAN would control the printer (G-code, reboot) without an API key; narrow trusted_clients to the hosts that need control and turn off use_xheaders right after the install. | <https://github.com/alex-mextner/3d-cli/blob/main/docs/printers/k1se-root-upgrade.md> |
| `no-ssl-verify-off` | forbidden command `sslVerify\s+false` | Never `git config --global http.sslVerify false` on the printer (the wiki suggests it on an SSL error): the script runs as root and its Moonraker step git-pulls again, so anyone on the path could swap the code. Check `date` first; if TLS still fails use the offline archive. | <https://github.com/alex-mextner/3d-cli/blob/main/docs/printers/k1se-root-upgrade.md><br><https://guilouz.github.io/Creality-Helper-Script-Wiki/helper-script/helper-script-installation/> |

Sources: <https://guilouz.github.io/Creality-Helper-Script-Wiki/>; <https://guilouz.github.io/Creality-Helper-Script-Wiki/firmwares/install-and-update-rooted-firmware-k1/>; <https://guilouz.github.io/Creality-Helper-Script-Wiki/helper-script/helper-script-installation/>; <https://github.com/Guilouz/Creality-Helper-Script>; <https://github.com/CrealityOfficial/K1_Series_Klipper>; <https://github.com/alex-mextner/3d-cli/blob/main/docs/printers/k1se-root-upgrade.md>

### `snapmaker-u1` — Snapmaker U1

- **Models matched** (against the fleet entry's `model`, never its name): `contains "snapmaker u1"`
- **Klipper possible:** yes — The U1 runs Klipper + Moonraker on stock firmware already (Moonraker answers on :7125 with no login). The paxx12 Extended Firmware keeps that and adds SSH, a firmware-config web page, native camera streaming, SpoolLink and more. Source: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/blob/main/README.md
- **Profile file:** `lib/data/firmware/snapmaker-u1.yaml`

| State | Detected by | Meaning |
|---|---|---|
| `stock` | all of: Moonraker answers /machine/system_info; not firmware-config does not answer | Snapmaker stock firmware (Klipper + Moonraker, no firmware-config page) |
| `extended` | all of: firmware-config /api/status lists System Information; Moonraker answers /machine/system_info | paxx12 Extended Firmware with Advanced Mode on (firmware-config answers) |

| Transition | Steps (kind, risk) | Sources |
|---|---|---|
| `stock` → `extended` (install-extended) | `local-update-extended-firmware` (manual, high) | <https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/blob/main/docs/install.md><br><https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/releases/tag/v1.6.0-paxx12-22> |
| `extended` → `stock` (revert-to-stock) | `switch-to-backup-firmware` (http_post, automatable, high) | <https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/blob/main/overlays/firmware-extended/02-firmware-config/root/usr/local/share/firmware-config/functions/20_actions_troubleshooting.yaml> |
| `extended` → `extended` (maintain-extended) | `upgrade-extended-firmware` (http_upload, automatable, high) *optional, `--also`*<br>`reset-extended-to-defaults` (http_post, automatable, medium) *optional, `--also`* | <https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/blob/main/docs/firmware_config.md> |

| Guard | Kind | Why (quoted in the refusal) | Sources |
|---|---|---|---|
| `never-flash-while-printing` | printer must be idle | Do not start while the U1 prints: the install reboots the printer and flashes the toolhead MCUs. | <https://github.com/alex-mextner/3d-cli/blob/main/docs/printers/u1-paxx12-upgrade.md> |
| `keep-force-logins-off` | must hold after every step | firmware-config "Require Login (Fluidd only)" writes force_logins: true, which overrides trusted_clients; the 3d moonraker adapter, the HA moonraker integration, u1_print_inspector and the camera proxy all call Moonraker without a key, so it must stay off (paxx12 issue 660: it also breaks OpenRFID's webhook). | <https://github.com/alex-mextner/3d-cli/blob/main/docs/printers/u1-paxx12-upgrade.md><br><https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/issues/660> |
| `no-require-login` | forbidden option: `enable-require-login`, `require-login` | Turning on firmware-config "Require Login" sets force_logins: true and breaks every key-less Moonraker consumer (3d moonraker adapter, HA integration, camera proxy). Keep it off; the adapter has no API-key support. | <https://github.com/alex-mextner/3d-cli/blob/main/docs/printers/u1-paxx12-upgrade.md> |
| `no-touchscreen-factory-reset` | forbidden option: `touchscreen-factory-reset`, `factory-reset` | Never use the touchscreen "factory reset" while the extended firmware is installed (paxx12 issue 778: splash-screen hang); use the recovery files (extended-recover.txt / full-recover.txt) or the firmware-config recovery actions instead. | <https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/issues/778><br><https://github.com/alex-mextner/3d-cli/blob/main/docs/printers/u1-paxx12-upgrade.md> |

Sources: <https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware>; <https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/blob/main/docs/install.md>; <https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/blob/main/docs/firmware_config.md>; <https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/blob/main/docs/ssh_access.md>; <https://wiki.snapmaker.com/en/snapmaker_u1/firmware_update_procedure>; <https://github.com/alex-mextner/3d-cli/blob/main/docs/printers/u1-paxx12-upgrade.md>


## What is not verified

- Nothing here was run against a real printer by the author of the profiles: the tests use loopback fakes and an
  injected command runner. The detection probes and the verification probes of the ssh steps are the places a
  real printer can disagree; each assumption that no source states is marked `[INFERENCE]` in the profile.
- Snapmaker U1: the firmware-config API (`/firmware-config/api/status`, `.../upgrade/upload`,
  `.../action/<id>`) is read from the paxx12 source tree (tag `v1.6.0-paxx12-22`), not exercised. An extended
  build with Advanced Mode off looks like stock to read-only probes. The reboot steps rely on Moonraker being
  seen unreachable for at least one probe (poll every 2 s); a reboot shorter than that reads as "the printer never
  went down" and fails closed with the rollback printed.
- Creality K1: a banner-only dropbear needs ~28 s; `fw status --no-ssh` skips those probes. The Helper Script is
  pinned to the runbook's commit `b46787a` (that it is reachable from `origin/main` is an `[INFERENCE]`; the step
  fails loudly if it is not). Only the Remove menus are automated (the runbook gives their stdin answers); the Install
  menus are manual because no source documents their prompts. `klipper-ready` needs an ssh read of `nginx.conf`
  (`deny all` present; that both server blocks have it is not proven), so a locked-down K1 read with `--no-ssh` shows as
  `moonraker-installed`. The verification of `install-save-zoffset-macros` assumes the include lands in `printer.cfg`.
- Bambu: no state is detectable without the access code; every step is manual and confirmed by you. Klipper is
  not possible on these printers.
