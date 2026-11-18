/* Independent of Lovelace's one-shot resource-list loader. No card classes before HA is ready. */
(() => {
  if (window.__printerCardResources) return;

  const BUNDLE_VERSION = "e6cda029f36e0fc4";
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
  const shells = new Set();
  let haReady = false;
  let active = false;
  let retryTimer = null;
  let sequence = 0;

  function missingTags() {
    // The scoped-registry polyfill replaces window.customElements during HA startup.
    return TAGS.filter(tag => !window.customElements.get(`${tag}-implementation`));
  }

  function registerCatalog() {
    // HA retains this array by reference; preserve it and every unrelated entry.
    window.customCards = window.customCards || [];
    const cards = [
      { type: "printer-webrtc-camera", name: "Printer Camera", preview: false,
        description: "WebRTC camera allows you to view the stream of almost any camera without delay" },
      { type: "u1-inspector-card", name: "U1 Print Inspector",
        description: "Read-only live operation and annotated G-code layer inspection." },
      { type: "u1-share-card", name: "U1 Camera Share",
        description: "Временные ссылки на выбранную камеру" },
    ];
    for (const card of cards) {
      if (!window.customCards.some(entry => entry.type === card.type)) window.customCards.push(card);
    }
  }

  function defineShells() {
    // Called only after HA/polyfill readiness. A pending bundle is a loading state,
    // not a missing custom element that HA replaces with a configuration error.
    class PrinterCardShell extends HTMLElement {
      constructor() {
        super();
        this.attachShadow({ mode: "open" });
        this._style = document.createElement("style");
        this._style.textContent = ":host{display:block}.printer-resource-state{padding:20px}" +
          "h2{font-size:20px;margin:0 0 12px}p{color:var(--secondary-text-color);line-height:1.5}" +
          "button{font:inherit;min-height:44px;padding:8px 16px;border-radius:8px;cursor:pointer;" +
          "color:var(--primary-text-color);background:var(--card-background-color);border:1px solid var(--divider-color)}";
        this._placeholder = document.createElement("ha-card");
        this._placeholder.className = "printer-resource-state";
        this._title = document.createElement("h2");
        this._message = document.createElement("p");
        this._message.setAttribute("role", "status");
        this._retry = document.createElement("button");
        this._retry.textContent = "Повторить";
        this._retry.addEventListener("click", () => recover(true));
        this._placeholder.append(this._title, this._message, this._retry);
        this.shadowRoot.append(this._style, this._placeholder);
      }
      setConfig(config) {
        this._config = config;
        this._failure = null;
        if (this._card) {
          this._card.setConfig(config);
          if (this._hass !== undefined) this._card.hass = this._hass;
        } else {
          this._mount();
          this._loading();
        }
      }
      set hass(value) {
        this._hass = value;
        if (this._card) this._card.hass = value;
      }
      get hass() { return this._hass; }
      getCardSize() { return this._card?.getCardSize() ?? this.constructor.cardSize; }
      connectedCallback() {
        shells.add(this);
        this._refresh();
        recover();
      }
      disconnectedCallback() { shells.delete(this); }
      _mount() {
        if (this._card || !this._config) return;
        const Constructor = window.customElements.get(`${this.constructor.tag}-implementation`);
        if (!Constructor) return;
        const card = new Constructor();
        // The camera's hass setter relies on setConfig having initialized onhass.
        card.setConfig(this._config);
        if (this._hass !== undefined) card.hass = this._hass;
        card.style.display = "block";
        this._card = card;
        this.shadowRoot.replaceChildren(this._style, card);
        // Browser DOM lifecycle connects/disconnects the inner card; never call it twice.
      }
      _refresh() {
        try { this._mount(); } catch (error) { this._failure = error.message || String(error); }
        this._loading();
      }
      _loading() {
        if (this._card) return;
        this._title.textContent = this._config?.title || this.constructor.title;
        const failed = status.phase === "failed";
        let message = "Загрузка карточки…";
        if (status.phase === "retrying") message = "Связь прервалась. Повторная загрузка…";
        if (navigator.onLine === false) message = "Нет подключения. Загрузка продолжится после восстановления связи.";
        if (failed) message = "Не удалось загрузить карточку. Проверьте соединение и повторите попытку.";
        if (this._failure) message = `Не удалось открыть карточку: ${this._failure}`;
        this._message.textContent = message;
        this._retry.hidden = !failed || Boolean(this._failure);
        this._retry.disabled = navigator.onLine === false;
      }
    }
    class PrinterCameraShell extends PrinterCardShell {
      static tag = "printer-webrtc-camera";
      static title = "Камера принтера";
      static cardSize = 5;
      static getStubConfig() { return { url: "" }; }
    }
    class InspectorShell extends PrinterCardShell {
      static tag = "u1-inspector-card";
      static title = "Что делает U1";
      static cardSize = 6;
      static gridOptions = { columns: 12, rows: "auto" };
      getGridOptions() { return this._card?.getGridOptions() ?? InspectorShell.gridOptions; }
    }
    class ShareShell extends PrinterCardShell {
      static tag = "u1-share-card";
      static title = "Поделиться камерой U1";
      static cardSize = 6;
      static gridOptions = { columns: 12, rows: 7, min_columns: 6, min_rows: 6 };
      getGridOptions() { return this._card?.getGridOptions() ?? ShareShell.gridOptions; }
      static getStubConfig() { return { title: "Поделиться камерой U1" }; }
    }
    const customElements = window.customElements;
    // Literal tags also let HA's static Lovelace resource validator inspect this entry.
    if (!customElements.get("printer-webrtc-camera")) customElements.define("printer-webrtc-camera", PrinterCameraShell);
    if (!customElements.get("u1-inspector-card")) customElements.define("u1-inspector-card", InspectorShell);
    if (!customElements.get("u1-share-card")) customElements.define("u1-share-card", ShareShell);
    registerCatalog();
  }

  function eligible() {
    return haReady && window.customElements.get("home-assistant") &&
      (/^\/3d-printers(?:\/|$)/.test(window.location.pathname) || shells.size > 0) &&
      !document.hidden && navigator.onLine !== false;
  }

  function phase(value, failure = null) {
    status.phase = value;
    status.missing = missingTags();
    status.lastFailure = failure;
    for (const shell of shells) shell._refresh();
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

  function recover(manual = false) {
    for (const shell of shells) shell._loading();
    if (!eligible() || active || retryTimer !== null) return;
    if (missingTags().length === 0) {
      markReady();
      return;
    }
    if (status.phase === "failed") {
      if (manual !== true && performance.now() - status.cycleStartedAt < CYCLE_COOLDOWN) return;
      status.attempts = 0;
    }
    if (status.attempts === 0) status.cycleStartedAt = performance.now();
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
    defineShells();
    phase("waiting");
    recover();
  });
})();
