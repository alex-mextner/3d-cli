# Verification record

19 September 2026. Results below distinguish tests, recorded-data UI checks and observations from the real printer.

## Automated logic checks

`PYTHONPATH=scripts pytest -q tests` — **49 passed**. Coverage includes annotated ironing, slicer aliases, byte offsets with UTF-8/CRLF, layer indexing, absolute/relative extrusion, buffered cursor provenance, firmware-action precedence, pause/completion/error/staleness, safe filenames/origins, binary/oversize rejection and artifact identity guards.

`node --check www/u1-share/u1-inspector-card.js` and Python compilation succeeded.

## Browser checks

The actual web component ran in a local browser harness with recorded responses from the real U1. **21 checks passed**, including the completed-state label, inspection of layer 186, planned-versus-current text, manual selection preservation, current-layer return, negative/fractional/missing layers, estimated/stale state, connection failure/recovery, HTML-injection resistance, out-of-order replies, job changes, disconnected-card replies, 44px button height, viewport overflow and HA grid options.

A screenshot at a 390 × 844 viewport was inspected. This verifies the component in the harness, not a claim of a full authenticated HA dashboard visual run. The real dashboard's registration/configuration and the exact service calls were separately verified through HA APIs.

## Live integration checks

The dedicated `u1_share` read-only account successfully called both read-only HA services. Status was fresh (1.4 seconds old in the recorded check), index state was ready, and the printer reported completion at layer 250/250. Layer 186 included ironing. A deliberately mismatched artifact fingerprint returned HTTP 409.

The real 3,132,992-byte file indexed in 1.076 seconds in a call including download, yielding 250 indexed layers and no parser warnings. This is one measurement, not a general performance guarantee. Ironing appeared on layers 186, 187, 210 and 244–250.

An anonymous public HTTPS request retrieved a JPEG with the baked overlay. The public viewer displayed it and cleared the image after expiry; a subsequent unauthenticated request to the expired image URL returned HTTP 403. The camera was dark after the completed print; lighting and printer controls were not changed for screenshots.

## Explicit limits

No active physical ironing operation was observed during this session. Its classification was checked against the real G-code and automated state replay. File cursor alignment with exact physical motion is not guaranteed and is labelled an estimate. Public-network tests were made from the Mac through the public endpoint, not from an independently verified off-tailnet device.

No model-generation, slicing, calibrated quoting, order database, partner routing, payment or delivery capability is claimed. Those are proposed in the separate product journey document.
