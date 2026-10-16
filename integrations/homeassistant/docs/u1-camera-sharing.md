# U1 Camera Sharing — live deployment

Installed on 2026-09-19. No manual Home Assistant login was required for setup.

## Generate on this Mac

```sh
cd ~/xp/u1-camera-share
.venv/bin/python u1_share.py --ttl 2h --format viewer
.venv/bin/python u1_share.py --ttl 30m
.venv/bin/python u1_share.py --ttl 1h30m --format json
```

Configuration and credentials are already installed in `~/.config/u1-share/`, with private permissions. Do not run setup again or publish that directory. Python uses the dedicated read-only `u1_share` account. Its private API token expires after one year; generated links are limited to 1 second–7 days.

The existing 3D Printers dashboard now has **Поделиться камерой U1**, below the U1 video card. Reload an already open dashboard to load the new resource. Choose duration, generate, then copy the viewer or JPEG link. The card uses the current signed-in HA session, not a token embedded in its configuration.

The generator connects to HA over the existing Tailscale connection. Guests use the existing Dataplicity HTTPS endpoint without Tailscale or an HA account. No new DNS records, router ports, public IP, tunnel, or background Python process were added.

`camera.u1_camera` is a Generic Camera backed by the existing U1 JPEG proxy. The guest viewer requests a snapshot every 5 seconds, not a continuous video stream. HA restart or revocation of the issuing session can invalidate links early. Downloaded images cannot be revoked.

Verified: real public JPEG HTTP 200; fresh request after a 20-second TTL HTTP 403; all five static assets HTTP 200; anonymous browser displayed a 1920-pixel-wide frame, then removed it at expiry. Public requests were tested from this Mac. An independent container test was blocked by DNS/network availability, so independent off-tailnet verification is not claimed. The dashboard resource and card configuration were verified through HA APIs; no full authenticated-dashboard visual test is claimed.

Automated checks: 62 Python + 48 JavaScript tests passed. YAML and all Lovelace dashboards validated. HA was not restarted. Config backups: `/home/ultra/.local/share/u1-share/u1-share-20260919T171759Z/`.

A full disk initially prevented installation. Only an inactive generated Bazel/Fuchsia build cache was removed, leaving 13.63 GiB available. Original source files, HA databases, existing backups and printer controls were not changed. The cache can be rebuilt/redownloaded when next needed.
