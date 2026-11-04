# `3d init` — scaffold a 3d project (and, with `--env`, set up the machine)

`3d init [path]` scaffolds a PROJECT; `3d init --env` is a separate MACHINE stage that gets
Spoolman + 3d-api running and configures `3d printops` ([below](#environment-stage-3d-init---env)).

Creates or tops up a project directory with a valid `3d.yaml`, standard folders, and
optional agent-oriented support files. Re-running is intended to be idempotent: existing
project files are not clobbered.

## Usage

```
3d init [path] [options]
```

Common options:

| Option | What |
|---|---|
| `--name NAME` | Project name written to `project.name`. |
| `--printer NAME` | Default printer name for `3d.yaml`. |
| `--material NAME` | Default material name for `3d.yaml`. |
| `--units mm|cm` | Project units. |
| `--bed X,Y,Z` | Explicit build volume. |
| `--reference PATH` | Copy a reference image into `references/`. |
| `--no-git` | Skip `git init`. |
| `--no-mcp` | Skip `.mcp.json`. |
| `--no-skills` | Skip per-project `.claude/skills/`. |
| `--no-agents` | Skip `AGENTS.md` and `CLAUDE.md` symlink. |
| `--no-hooks` | Skip the pre-commit hook. |
| `--no-input`, `--yes`, `-y` | Non-interactive mode for CI and agents. |

```bash
3d init
3d init my-bracket --no-input
3d init --name pantheon --reference pantheon.jpg --printer X1C --no-input
```

## Environment stage: `3d init --env`

Per machine, not per project: makes sure Spoolman and the 3d-api that `3d printops` talks to run
somewhere, then writes `~/.config/3d-cli/printops.yaml` (`api:` + `token:`, mode 600). It
generalizes [`3d printops deploy`](printops.md#deploy) (the same plan, a different host) and adds
the token, the health check and the config.

```
3d init --env [--no-input] [--spoolman TARGET] [--script-out PATH] [--api URL] [--token TOKEN]
              [--ssh-user USER] [--bind ADDR] [--dry-run] [--allow-discouraged]
```

| Option | What |
|---|---|
| `--spoolman auto` (default) | Terminal: show the target menu. `--no-input`: print the status and the exact commands, install nothing. |
| `--spoolman skip` | Do nothing (exit 0). |
| `--spoolman local` | docker compose on this computer, built from this checkout; data in `~/.local/share/3d-cli/printops`. |
| `--spoolman printer:NAME` | The host of a registry printer (`3d printer list`: Moonraker, access `full`) over ssh, with `--ssh-user USER` (default: your ssh config). |
| `--spoolman ssh:USER@HOST` | Any server with docker compose you can `ssh` into without a password prompt; data in `~/printops` there. |
| `--spoolman script` | Write a self-contained installer (`--script-out`, default `./spoolman-setup.sh`); installs nothing here. |
| `--api URL` `--token TOKEN` | Connect to a Spoolman/3d-api that already runs (what the script prints): the URL and the token are checked, then written. |
| `--bind ADDR` | The host address Spoolman and 3d-api listen on (an IP address). Default `127.0.0.1` for `--spoolman local`, `0.0.0.0` (all interfaces) for ssh/printer hosts and for `script`. See [Network exposure](#network-exposure). |
| `--dry-run` | Install targets (`local`, `printer:NAME`, `ssh:USER@HOST`): print the steps (rsync `--dry-run`), change nothing. Refused (exit 2) with `--api` and `--spoolman script`, which would write a file. |
| `--allow-discouraged` | With `--no-input`: accept a printer marked "possible but not recommended". |
| `--no-input`, `--yes`, `-y` | Never prompt, and never install unless an explicit target is named. |

```bash
3d init --env                                            # interactive menu
3d init --env --no-input --spoolman auto                 # CI/agent: "already configured" or the commands to run
3d init --env --no-input --spoolman local
3d init --env --no-input --spoolman local --bind 192.168.0.20   # reachable from the LAN on that address only
3d init --env --no-input --spoolman script --script-out ~/spoolman-setup.sh
3d init --env --no-input --api http://nas.lan:8765 --token po_...
```

Behaviour:

- **No OpenSCAD bootstrap.** `3d init --env` skips the first-run clone of the OpenSCAD libraries (BOSL2, NopSCADlib) and
  the `.bootstrapped` marker; `3d init [path]` and the other commands keep it.
- **Idempotent.** When the configured `api:` answers `GET /v1/health` (3d-api; a configured token is
  checked too) it prints `already configured` and exits 0 without touching the file. A bare
  Spoolman (it answers `GET /api/v1/health` but has no `/v1` routes) is not a 3d-api: it is never
  counted as configured and never written as `api:`; `--api URL` for it exits 1 and names the
  3d-api URL (same host, port 8765). An install target on an already configured machine is
  ignored for the same reason; `--spoolman script` and `--api URL` still run. A configured but
  dead (or Spoolman-only) api is an error (exit 1) with the commands that fix it.
- **Config last.** `printops.yaml` is written only after the service is healthy and the token was
  accepted; a different old file is kept as `printops.yaml.bak` (mode 600). Pointing `--api` at another
  server without `--token` drops the old token: it belongs to the old server and is never sent to the new one.
- **The file, not the environment.** `PRINTOPS_API` / `PRINTOPS_TOKEN` override the file for every
  `3d printops` command, but `3d init --env` checks, keeps and replaces the *file* only (cron and
  systemd units do not inherit your shell's variables). When an exported variable differs from the
  file it prints a warning (the variable's name, never the token).
- **Nothing is chosen for you.** The menu has no default and no host is contacted before you pick
  it; then its load, cores, RAM, architecture and docker are shown, and a busy host asks first.
- **Which printers can host it.** Only registry printers that speak Moonraker and that 3d may
  drive (`access: full`). The Snapmaker U1 is *possible but not recommended* (aarch64, 1 GB RAM
  shared with Klipper and the print, Buildroot firmware without a package manager, ssh keys and
  `/etc` wiped on every firmware upgrade: [u1-paxx12-upgrade](../printers/u1-paxx12-upgrade.md)); the
  Creality K1 SE *cannot* (MIPS SoC, [k1se-root-upgrade](../printers/k1se-root-upgrade.md); the
  Spoolman image has no mipsel build). The table is `HOST_LIMITS` in `lib/printops/env_setup.py`.
  Docker is never installed for you: a host without it gets the install link and exit 1.
- **The script** (`lib/printops/standalone_template.py` + the `3d printops deploy` build context,
  ~3 MB) needs only bash, docker compose, tar/gzip/base64: it unpacks the context, runs the same
  `compose.yaml` (Spoolman `ghcr.io/donkie/spoolman:0.27.0` on `:7912`, 3d-api on `:8765`, data in
  `~/printops`), waits for health, mints a token and prints both URLs plus the
  `3d init --env --api URL --token TOKEN` line. Re-runs change nothing (`--dry-run` checks
  prerequisites). Knobs: `PRINTOPS_DIR`, `PRINTOPS_SPOOLMAN_PORT`, `PRINTOPS_API_PORT`,
  `PRINTOPS_BIND_ADDR` (or `--bind ADDR` on the script), `PRINTOPS_CONTAINER_PREFIX`,
  `PRINTOPS_SPOOLMAN_CORS_ORIGIN` (default: Spoolman's own address only,
  never `*`; add your Fluidd/Mainsail origins), `SPOOLMAN_ONLY=1`, `TOKEN_NAME` (header of the script).
  A relative `PRINTOPS_DIR` is taken from the directory you run the script in. Spoolman has no
  auth: keep its port inside the LAN/VPN.

### Network exposure

Spoolman has **no authentication** (3d-api needs a bearer token for everything but
`/v1/health`). Docker publishes ports on every interface and bypasses ufw/firewalld, so the bind
address is the control that works:

- `--spoolman local` listens on `127.0.0.1`: nothing else on the network can reach Spoolman. The
  consequence is that **no other machine and no printer can use it either** (another computer
  running `3d`, Moonraker's `[spoolman]` section, Fluidd). Give `--bind` the LAN address they
  reach it on (or `--bind 0.0.0.0` on a network you trust).
- ssh and printer hosts, and the script, listen on `0.0.0.0` so the machine running `3d` can reach
  them. On a VPS or any host without a trusted router in front, pass `--bind ADDR` with the LAN/VPN
  address (the script prints a warning when it listens on every interface).
- The same knob for the compose project: `PRINTOPS_BIND_ADDR`
  ([services/3d-api/README.md](../../services/3d-api/README.md#ports-lan--tailnet-only),
  [`3d printops deploy --bind`](printops.md#deploy)).

Exit codes: 0 configured / skipped / already configured, 1 a step or the health check failed,
2 bad options.

## Notes

- The generated `3d.yaml` has empty `parts:`, `anchors:`, `sections:`, `loads:`, and
  `gates:` sections; users add real parts and object-model metadata later.
- `3d init` can register the project so `3d web` and cross-project tools can discover it.
- Printer and material names come from the merged registries exposed by `3d printers` and
  `3d materials`.
