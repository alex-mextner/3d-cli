"""marketplaces — search, inspect and download models from 3D-model marketplaces.

One module per site (printables, makerworld, thingiverse, cults) exposes the same three
calls — `search`, `show`, `download_links` — plus pure `parse_*` functions over the site's
JSON so the parsers are tested on recorded responses. `base` holds the shared model shape
and errors, `transport` the HTTP lanes (stdlib urllib, and a TLS-impersonating lane for sites
behind Cloudflare), `credentials` the per-site tokens from ~/.config/3d-cli/auth.json,
`catalog` the cross-site search/show/download.
Stdlib only: the curl_cffi lane runs as a separate tool process (impersonate_fetch.py).
"""
