# Isolated frontend recovery fixture

This is a CI-only browser test for the printer cards' delayed and once-failed module loads. It uses the actual Home Assistant 20260826.7 card creation and error rendering source, the scoped custom element registry polyfill 0.0.10, the four current dashboard configurations, and the candidate bootstrap and bundle from the repository.

The fixture does not use a Home Assistant account, a saved browser profile, real camera streams, or printer APIs. Its synthetic `hass` object rejects API and service requests locally. All non-local browser network requests are blocked. A camera's ordinary unavailable-stream message is expected in this isolated state; that is separate from the generic `hui-error-card` regression being checked.

## Assertions

Each Chromium and WebKit project runs the same before/after sequence in fresh documents:

1. Load the pinned pre-shell bootstrap and hold the bundle response. After 2.3 seconds, assert that all four native HA `hui-error-card` titles are visible. Their rendering and red background come from the pinned HA source, not a fixture imitation.
2. Load the candidate bootstrap and hold its bundle response. After 2.3 seconds, assert zero `hui-error-card` instances and four visible loading messages from the actual candidate shell.
3. Release the candidate bundle, then wait for two camera, one inspector, and one share implementation element to be connected and have nonzero geometry. Check that the cameras received synthetic `hass`, no generic error card remains, and no uncaught page exception occurred.

A second test uses a fresh document, returns HTTP 503 for exactly the first real module request, and serves the candidate on retry. It asserts distinct attempt URLs `1` and `2`, exactly two attempts ending in `ready`, no `hui-error-card` ever added to the fixture DOM, and all four actual implementation elements mounted. The first test's held responses and the second test's failure injection are independent of test execution order.

The baseline is deliberately limited to the period before its bundle arrives. It does not claim to reproduce the complete old dashboard or its historical dependency graph. Exhausted retries, late rejections and circuit-breaker timing remain covered by the separate Node VM tests; the browser fixture covers the delayed renderer transition and recovery from one actual failed module fetch.

## Source and output

`ha-frontend-fixture.json` records the upstream version, source URL, complete selected source files, their SHA-256 digests, and the upstream license. The builder checks every digest before compiling. The extensionless `lit/decorators` import is resolved to the package's `lit/decorators.js` export without changing the upstream files. Only unused badge lazy imports are left external; the card creation, error card, card frame, icon, and context implementations are the original source.

`before-printer-cards-bootstrap.js` is the exact pre-shell snapshot, checked against SHA-256 `65c1ccc50c7f3e016877d9bf72d8b9e3c59a628a3ea50c5f2910874b7fc2c85a`.

The tests emit four screenshots per browser: native errors before, actual loading shells after, actual implementation elements after release, and actual implementation elements after the transient failure. Traces are retained on failure. Every screenshot includes an **Isolated frontend recovery fixture** heading. These artifacts do not establish production or phone visual acceptance.

## Integrating into CI

Place these files together at `integrations/homeassistant/tests/printer_cards_browser/`. Reuse the existing approved workflow's Node 22 setup and artifact upload mechanism. No deployment, live HA credentials, new browser login, or local browser run is needed.

From that directory, in the approved hosted CI runner:

```sh
npm ci --ignore-scripts --no-audit --no-fund
npm run build:fixture
npx --no-install playwright install --with-deps chromium webkit
PRINTER_CARD_INTEGRATION="$GITHUB_WORKSPACE/integrations/homeassistant" npm test
```

CI sets `CI=true`; the browser spec refuses to run without it. `npm run build:fixture` is a separate build-only check and never starts a browser. Package versions and transitive dependencies are pinned by `package-lock.json`.

Upload `integrations/homeassistant/tests/printer_cards_browser/test-results/` through the existing CI artifact step, including on failure. Each script prints its private temporary log path at startup and completion.
