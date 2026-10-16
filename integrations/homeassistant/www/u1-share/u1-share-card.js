import { MAX_TTL, CAMERA, publicOrigin, validateSignedPath, expiryFromPath, buildLinks } from "./share-common.js";

class U1ShareCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._busy = false;
    this._links = null;
    this._cloudAttempted = false;
  }
  setConfig(config) {
    if (config.entity && !CAMERA.test(config.entity)) throw Error("entity должна быть camera.*");
    if (config.public_url) publicOrigin(config.public_url);
    if (config.refresh !== undefined && (!Number.isInteger(config.refresh) || config.refresh < 2 || config.refresh > 60))
      throw Error("refresh должна быть от 2 до 60 секунд.");
    this._revision = (this._revision || 0) + 1;
    this._busy = false; this._links = null; this._camerasKey = null; this._cloudAttempted = false;
    this._config = { title: "Поделиться камерой U1", refresh: 5, ...config };
    this._render();
    if (this._hass) this._sync();
  }
  set hass(value) { this._hass = value; if (this._config) this._sync(); }
  getCardSize() { return 6; }
  getGridOptions() { return { columns: 12, rows: 7, min_columns: 6, min_rows: 6 }; }
  static getStubConfig() { return { title: "Поделиться камерой U1" }; }
  connectedCallback() {
    if (!this._clock) this._clock = setInterval(() => this._tick(), 1000);
  }
  disconnectedCallback() { clearInterval(this._clock); this._clock = null; }
  _el(id) { return this.shadowRoot.getElementById(id); }
  _message(text) { this._el("status").textContent = text; }
  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; color:var(--primary-text-color); }
        ha-card { display:block; overflow:hidden; }
        .body { padding:20px; }
        h2 { font-size:20px; font-weight:600; line-height:1.3; margin:0 0 6px; }
        p { font-size:14px; line-height:1.5; margin:0 0 18px; color:var(--secondary-text-color); }
        label { display:block; font-size:14px; font-weight:500; margin:14px 0 6px; }
        input,select,textarea,button { font:inherit; font-size:16px; box-sizing:border-box; }
        input,select,textarea { width:100%; color:var(--primary-text-color); background:var(--card-background-color,#fff);
          border:1px solid var(--divider-color,#ccc); border-radius:10px; padding:10px 12px; min-height:44px; }
        input:focus-visible,select:focus-visible,textarea:focus-visible,button:focus-visible { outline:2px solid var(--primary-color,#03a9f4); outline-offset:2px; }
        .row { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:10px; }
        .presets,.actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }
        button { cursor:pointer; border:1px solid var(--divider-color,#ccc); border-radius:10px; padding:10px 14px; min-height:44px;
          background:var(--card-background-color,#fff); color:var(--primary-text-color); }
        button:disabled { opacity:.5; cursor:default; }
        #generate { width:100%; margin-top:18px; background:var(--primary-color,#03a9f4); color:var(--text-primary-color,#fff); border-color:transparent; font-weight:600; }
        #status { margin:12px 0 0; font-size:14px; min-height:21px; overflow-wrap:anywhere; }
        #result { margin-top:18px; padding-top:16px; border-top:1px solid var(--divider-color,#ccc); }
        #expires { margin:0 0 12px; font-weight:500; }
        textarea { resize:vertical; min-height:86px; font-size:13px; overflow-wrap:anywhere; }
        .note { font-size:12px; line-height:1.55; margin:14px 0 0; }
        [hidden] { display:none!important; }
        @media(max-width:350px) { .body { padding:16px; } .actions button { width:100%; } }
      </style>
      <ha-card><div class="body">
        <h2 id="title"></h2><p>Временный доступ только к изображению камеры. Без входа в Home Assistant.</p>
        <label for="camera">Камера</label><select id="camera"><option value="">Выберите камеру U1</option></select>
        <label for="public">Внешний адрес Home Assistant</label><input id="public" type="url" placeholder="https://….ui.nabu.casa" autocomplete="off" spellcheck="false">
        <label for="amount">Срок действия</label><div class="row"><input id="amount" type="number" value="1" min="1" max="604800" step="1" inputmode="numeric" aria-label="Срок действия">
        <select id="unit" aria-label="Единица срока"><option value="1">Секунды</option><option value="60">Минуты</option><option value="3600" selected>Часы</option><option value="86400">Дни</option></select></div>
        <div class="presets"><button data-ttl="900" type="button">15 мин</button><button data-ttl="3600" type="button">1 час</button><button data-ttl="14400" type="button">4 часа</button><button data-ttl="86400" type="button">1 день</button></div>
        <button id="generate" type="button">Создать ссылку</button>
        <p id="status" role="status" aria-live="polite"></p>
        <section id="result" hidden><p id="shared-camera"></p><p id="expires"></p><label for="link">Ссылка на просмотр</label><textarea id="link" readonly spellcheck="false"></textarea>
        <div class="actions"><button id="copy" type="button">Копировать просмотр</button><button id="image" type="button">Копировать JPEG</button><button id="open" type="button">Открыть просмотр</button></div></section>
        <p class="note">Кадр обновляется каждые <span id="interval"></span> сек. Максимальный срок — 7 дней. Перезапуск HA или отзыв сессии завершит доступ раньше. Сохранённые изображения отозвать нельзя.</p>
      </div></ha-card>`;
    this._el("title").textContent = this._config.title;
    this._el("interval").textContent = this._config.refresh;
    this._el("public").value = this._config.public_url || "";
    for (const id of ["camera", "public", "amount", "unit"]) this._el(id).addEventListener("input", () => this._invalidate());
    for (const button of this.shadowRoot.querySelectorAll("[data-ttl]")) {
      button.addEventListener("click", () => {
        this._invalidate();
        const sec = Number(button.dataset.ttl);
        this._el("unit").value = sec % 3600 === 0 ? "3600" : "60";
        this._el("amount").value = sec / Number(this._el("unit").value);
      });
    }
    this._el("generate").addEventListener("click", () => this._generate());
    this._el("copy").addEventListener("click", () => this._copy("viewer"));
    this._el("image").addEventListener("click", () => this._copy("image"));
    this._el("open").addEventListener("click", () => {
      if (this._links && Date.now() < this._expires * 1000)
        window.open(this._links.viewer, "_blank", "noopener,noreferrer");
    });
  }
  _sync() {
    const cameras = Object.values(this._hass.states || {}).filter(s => CAMERA.test(s.entity_id));
    const key = JSON.stringify(cameras.map(s => [s.entity_id, s.attributes?.friendly_name]));
    if (key !== this._camerasKey) {
      const selected = this._el("camera").value || this._config.entity || "";
      this._el("camera").replaceChildren(new Option("Выберите камеру U1", ""));
      for (const state of cameras) {
        this._el("camera").add(new Option(`${state.attributes?.friendly_name || state.entity_id} · ${state.entity_id}`, state.entity_id));
      }
      this._el("camera").value = selected;
      this._camerasKey = key;
    }
    // Never guess the first camera: the owner must explicitly select U1.
    if (!this._el("public").value) {
      const external = this._hass.config?.external_url;
      if (external) { try { this._el("public").value = publicOrigin(external); } catch { /* Allow manual entry. */ } }
      if (!this._el("public").value && !this._cloudAttempted) {
        this._cloudAttempted = true;
        this._hass.callWS({ type: "cloud/status" }).then(data => {
          if (!this._el("public").value && data.remote_connected && data.remote_domain)
            this._el("public").value = publicOrigin(`https://${data.remote_domain}`);
        }).catch(() => {});
      }
    }
    this._el("generate").disabled = this._busy || !cameras.length;
    if (!cameras.length) this._message("В HA нет camera.*. Сначала подключите камеру U1.");
  }
  _invalidate() {
    this._links = null; this._el("result").hidden = true; this._el("link").value = ""; this._message("");
  }
  _lock(value) {
    for (const id of ["camera", "public", "amount", "unit"]) this._el(id).disabled = value;
    for (const button of this.shadowRoot.querySelectorAll("[data-ttl]")) button.disabled = value;
  }
  async _generate() {
    if (this._busy || !this._hass) return;
    const revision = this._revision;
    this._busy = true; this._el("generate").disabled = true; this._lock(true);
    this._links = null; this._el("result").hidden = true; this._el("link").value = "";
    try {
      const entity = this._el("camera").value;
      const state = this._hass.states[entity];
      if (!CAMERA.test(entity) || !state) throw Error("Выберите именно камеру U1.");
      if (["off", "unavailable", "unknown"].includes(state.state)) throw Error("Камера выключена или недоступна.");
      const base = publicOrigin(this._el("public").value.trim());
      const ttl = Number(this._el("amount").value) * Number(this._el("unit").value);
      if (!Number.isSafeInteger(ttl) || ttl < 1 || ttl > MAX_TTL) throw Error("Срок должен быть от 1 секунды до 7 дней.");
      const fallback = Math.floor(Date.now() / 1000) + ttl;
      this._message("Создаю подписанную ссылку…");
      let result;
      try { result = await this._hass.callWS({ type: "auth/sign_path", path: `/api/camera_proxy/${entity}`, expires: ttl }); }
      catch { throw Error("HA не выдал подпись. Проверьте подключение и права на камеру."); }
      if (revision !== this._revision) return;
      validateSignedPath(result.path, entity);
      this._expires = expiryFromPath(result.path, fallback);
      this._links = buildLinks(base, result.path, this._expires, this._config.refresh);
      this._el("shared-camera").textContent = `Открыт доступ: ${state.attributes?.friendly_name || entity} · ${entity}`;
      this._el("link").value = this._links.viewer;
      this._el("result").hidden = false;
      this._tick();
      this._message("Ссылка создана. Проверьте её через мобильную сеть без VPN. Любой обладатель ссылки сможет видеть камеру до истечения срока.");
    } catch (error) {
      if (revision === this._revision) this._message(error.message || "Не удалось создать ссылку.");
    } finally {
      if (revision === this._revision) { this._busy = false; this._lock(false); this._sync(); }
    }
  }
  _tick() {
    if (!this._links) return;
    const active = Date.now() < this._expires * 1000;
    this._el("expires").textContent = active ? `Действует до ${new Date(this._expires * 1000).toLocaleString()}` : "Срок действия ссылки истёк";
    for (const id of ["copy", "image", "open"]) this._el(id).disabled = !active;
  }
  async _copy(kind) {
    if (!this._links || Date.now() >= this._expires * 1000) return;
    const value = this._links[kind];
    try {
      if (!navigator.clipboard?.writeText) throw Error();
      await navigator.clipboard.writeText(value);
      this._message(kind === "viewer" ? "Ссылка на просмотр скопирована." : "Прямая JPEG-ссылка скопирована.");
    } catch {
      // Clipboard API requires HTTPS; LAN/Tailscale HTTP gets a manual fallback.
      const input = this._el("link"); input.value = value; input.focus(); input.select();
      this._message("Ссылка выделена. Нажмите ⌘C / Ctrl+C или выберите «Копировать» на телефоне.");
    }
  }
}
if (!customElements.get("u1-share-card")) customElements.define("u1-share-card", U1ShareCard);
window.customCards = window.customCards || [];
window.customCards.push({ type: "u1-share-card", name: "U1 Camera Share", description: "Временные ссылки на выбранную камеру" });
