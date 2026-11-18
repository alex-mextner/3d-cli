/* Independent of Lovelace's one-shot resource-list loader. No card classes before HA is ready. */
(() => {
  if (window.__printerCardResources) return;

  const BUNDLE_VERSION = "7cf7f5d69cefc558";
  const TAGS = ["printer-webrtc-camera", "u1-inspector-card", "u1-share-card"];
  const RETRY_DELAYS = [1000, 4000, 10000];
  const MAX_ATTEMPTS = 4;
  const LOAD_TIMEOUT = 15000;
  const CYCLE_COOLDOWN = 60000;
  const status = window.__printerCardResources = {
    version: BUNDLE_VERSION, phase: "waiting_ha", attempts: 0, totalAttempts: 0,
    missing: [...TAGS], lastFailure: null, cycleStartedAt: 0,
  };
  const timeouts = new Map();
  let haReady = false;
  let active = false;
  let retryTimer = null;
  let sequence = 0;

  function missingTags() {
    // The scoped-registry polyfill replaces window.customElements during HA startup.
    return TAGS.filter(tag => !window.customElements.get(tag));
  }

  function eligible() {
    return haReady && window.customElements.get("home-assistant") &&
      /^\/3d-printers(?:\/|$)/.test(window.location.pathname) &&
      !document.hidden && navigator.onLine !== false;
  }

  function phase(value, failure = null) {
    status.phase = value;
    status.missing = missingTags();
    status.lastFailure = failure;
  }

  function markReady() {
    sequence++;
    active = false;
    clearTimeout(retryTimer);
    retryTimer = null;
    for (const timer of timeouts.values()) clearTimeout(timer);
    timeouts.clear();
    phase("ready");
  }

  function settled(token, failure = null) {
    const pending = timeouts.has(token);
    clearTimeout(timeouts.get(token));
    timeouts.delete(token);
    // A timed-out import can still finish. Guarded bundle definitions make that safe.
    if (missingTags().length === 0) {
      markReady();
      return;
    }
    if (!pending || token !== sequence) return;
    active = false;
    const reason = failure || "MissingRegistration";
    if (status.attempts >= MAX_ATTEMPTS) {
      phase("failed", reason);
      console.warn("[3d-printers] Card loading stopped after bounded retries", {
        attempts: status.attempts, missing: status.missing, failure: reason,
      });
      return;
    }
    phase("retrying", reason);
    retryTimer = setTimeout(() => {
      retryTimer = null;
      attempt();
    }, RETRY_DELAYS[status.attempts - 1]);
  }

  function attempt() {
    if (missingTags().length === 0) {
      markReady();
      return;
    }
    if (!eligible()) {
      phase("waiting");
      return;
    }
    if (active || status.attempts >= MAX_ATTEMPTS) return;
    active = true;
    status.attempts++;
    status.totalAttempts++;
    const token = ++sequence;
    const url = new URL("./printer-cards.bundle.js", import.meta.url);
    url.searchParams.set("v", BUNDLE_VERSION);
    url.searchParams.set("attempt", String(status.totalAttempts));
    phase("loading");
    timeouts.set(token, setTimeout(() => settled(token, "Timeout"), LOAD_TIMEOUT));
    // One self-contained retry unit: no failed child-module URL can poison the retry.
    import(url.href).then(() => settled(token), error => settled(token, error?.name || "Error"));
  }

  function recover() {
    if (!eligible() || active || retryTimer !== null) return;
    if (missingTags().length === 0) {
      markReady();
      return;
    }
    if (status.phase === "failed") {
      if (Date.now() - status.cycleStartedAt < CYCLE_COOLDOWN) return;
      status.attempts = 0;
    }
    if (status.attempts === 0) status.cycleStartedAt = Date.now();
    attempt();
  }

  for (const name of ["online", "location-changed", "popstate"]) {
    window.addEventListener(name, recover);
  }
  document.addEventListener("visibilitychange", recover);
  // A native promise captured before the polyfill is valid as a readiness barrier.
  // Do not use its resolved class/registry: reacquire the current registry above.
  window.customElements.whenDefined("home-assistant").then(() => {
    haReady = true;
    phase("waiting");
    recover();
  });
})();
