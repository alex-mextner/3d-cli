# `3d ha` — Home Assistant integration

`integrations/homeassistant/` is the source of truth for every 3D artifact of the Home
Assistant config on the Odroid (`/home/ultra/homeassistant`, repo
[alex-mextner/ha-configs-rs-bg-stanka_vraza](https://github.com/alex-mextner/ha-configs-rs-bg-stanka_vraza)):
printer packages, the «3D Printers» dashboard, the U1 camera/inspector/share scripts, the
Lovelace cards, the printer ingress proxy and go2rtc config. Their history came along from the
HA repo (`git log -- integrations/homeassistant`; each imported commit carries a
`Source-Commit:` trailer with the original SHA).

The HA repo includes 3d-cli as a git submodule (`3d-cli/`). The files HA and the containers
read are relative symlinks into it; `www/` files are byte-identical tracked copies, because HA
serves `/local/*` only from files whose resolved path stays inside `/config/www` (a symlink
out of `www` answers 404). `3d ha deploy` moves the submodule and keeps all of that
consistent.

Issue: [#156](https://github.com/alex-mextner/3d-cli/issues/156).

## Usage

```
3d ha deploy [--dry-run] [--host H] [--ref REF] [--ha-dir DIR] [--source DIR]
```

- `--dry-run` — print the plan and stop. Exit 0 when nothing blocks, with or without changes.
  `3d ha deploy --dry-run` right after a deploy prints `changes (0):`.
- `--host H` — ssh target of the HA host (default `ha_repo.host` from the manifest,
  `ultra@192.168.0.39`). `local` runs everything on this machine (tests use it). A value
  starting with `-` is refused (ssh would read it as an option).
- `--ref REF` — the 3d-cli commit to deploy. Default `origin/main` (fetched first). The HA
  host fetches it from GitHub, so it has to be pushed.
  `3d ha deploy --ref 96c0165 --dry-run`
- `--ha-dir DIR` — the HA repo checkout on the host (default `ha_repo.dir`).
- `--source DIR` — another 3d-cli checkout that resolves `--ref` (default: this one).

## The files

| In 3d-cli `integrations/homeassistant/` | In the HA repo | How |
|---|---|---|
| `packages/3d_printers.yaml`, `packages/u1_print_inspector.yaml` | `packages/` | symlink; reloaded per domain |
| `dashboards/ui-3d-printers.yaml` | `ui-3d-printers.yaml` | symlink (YAML dashboards are re-read when the file changes) |
| `scripts/u1_*.py`, `requirements/u1-share.txt` | `scripts/`, `requirements/` | symlink; the camera proxy is recreated when its scripts change |
| `proxy/printer-proxy.nginx.conf` | `printer-proxy.nginx.conf` | symlink; `printer-proxy` recreated on change |
| `go2rtc/go2rtc.yaml` | `go2rtc.yaml` | symlink; `go2rtc` recreated on change |
| `docs/*.md` | `docs/` | symlink |
| `www/u1-share/*` | `www/u1-share/` | tracked copy |
| `compose/3d.docker-compose.yaml` | — | `ha.docker-compose.yaml` lists it under `include:` |
| `snippets/configuration.3d.yaml` | — | the 3D keys of `configuration.yaml` (ingress panels, Lovelace dashboard and resources); checked, never copied |
| `vendor.lock` | — | pins of moonraker-home-assistant 1.13.4, ha_creality_ws 0.9.9, WebRTC v3.6.1; checked against the live `manifest.json` versions and submodule commits |
| `manifest.yaml` | — | the table above, plus the HA-repo settings `3d ha deploy` uses |

`tests/u1_print_inspector/` runs in the 3d-cli gate (`dev run test`), and `yamllint` checks
the whole directory (`.yamllint`).

On the Odroid the compose overlay `/home/ultra/odroid-overlay/compose.odroid.yaml` mounts its
own copy of `go2rtc.yaml` (different Tailscale WebRTC candidate), so a change to
`go2rtc/go2rtc.yaml` does not reach the running go2rtc there; `3d ha deploy` prints a warning
when a changed file is not mounted by the service it lists.

## What a deploy does

1. **Plan** from a read-only probe of the host (`lib/ha_deploy_probe.py`, run with the host's
   `python3` over ssh). It blocks — nothing changes — on: files under
   `integrations/homeassistant/` missing from the manifest or vice versa; tracked symlinks into
   the submodule or files next to the `www` copies that the manifest doesn't list; a live dest
   with staged or unstaged edits that 3d-cli doesn't have (a copy the previous deploy wrote
   doesn't count); local edits inside the submodule; a
   `configuration.yaml` without the snippet keys; a compose file that doesn't include the 3D
   file or redefines its services; `vendor.lock` drift; a `--ref` the host can't fetch; no
   `HA_TOKEN` in the HA repo's `.env`. A package change in a domain without a reload service
   (for example `homeassistant:` or a `sensor` platform other than rest/template/command_line)
   stops the deploy as well: it would need an HA restart, and the deploy never restarts HA.
   A real deploy holds a lock (`.git/3d-ha-deploy.lock` in the HA repo) from the probe to the
   end, so a rollback can never restore an older backup over a newer deploy. A crashed deploy
   leaves it behind; the error names the directory to remove.
2. **Backup**: a tar of every touched path to `~/.cache/ha-deploy-backups/` on the host, plus
   a JSON note with the previous submodule commit.
3. **Apply**: fast-forward the HA checkout when a merged deploy PR is the only difference to
   `origin/main`; check out `--ref` in the submodule; write the symlinks and copies.
4. **`check_config`**: `docker exec homeassistant-homeassistant-1 python -m homeassistant
   --script check_config -c /config`. Failure restores the backup and the old submodule
   commit.
5. **Reloads** through the HA API, only for the domains of changed packages
   (`template.reload`, `rest.reload`, `automation.reload`, …). Identical bytes reload nothing.
6. **Services**: `docker compose up -d --no-deps --force-recreate` for the services of changed
   files (single-file bind mounts keep the old inode), `up -d` when the 3D compose file changed
   or a running container differs from the compose config. A failing reload or `up` restores
   the backup and runs the same reloads/recreates on the old files.
7. **Post-checks**, repeated for up to 30 s: every `sensor.printer_*`/`binary_sensor.printer_*`
   that was available before still is, the dashboard answers 200, go2rtc still has the
   `u1_camera` stream with producers, and the 3D containers match the compose config. A
   regression restores the backup and runs the same reloads/recreates on the old files.
8. **Record**: `.3d-cli-deployed` in the HA repo gets the SHA, time and backup path. The
   deployed paths are staged in the HA checkout and committed on a branch
   `3d-deploy/<sha12>` built on `origin/main`, never on the live HEAD, so local commits nobody
   pushed are not published with it. `.gitmodules` is never taken from the working tree: at the
   cutover the branch gets origin/main's file plus only the 3d-cli stanza. The HA repo's
   pre-commit hook runs on the commit, which is pushed through
   this machine and opened as a PR in the HA repo; older open `3d-deploy/*` PRs are closed as
   superseded. `main` on the host is not moved: the live checkout runs ahead of `main` until
   the PR merges, and `--dry-run` reports `pointer not yet merged` meanwhile instead of
   failing. Merge it with a merge commit; the next deploy then fast-forwards the checkout
   without touching the working tree.

## Cutover (once)

The HA repo got the submodule, the `include:` and the symlinks in one reviewed PR; contents
stayed byte-identical (same compose config hashes, same files behind the links), so the
effective HA config did not change. A fresh clone of the HA repo needs
`git submodule update --init 3d-cli` before HA starts.

## Exit codes

`0` deployed, or a dry run with nothing blocking; `1` blocked, or a failed check that was
rolled back; `2` usage.
