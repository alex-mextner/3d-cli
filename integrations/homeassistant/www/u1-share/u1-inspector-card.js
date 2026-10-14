/* Local read-only status and per-layer analysis through authenticated HA services. */
class U1InspectorCard extends HTMLElement {
  constructor() {
    super(); this.attachShadow({mode: 'open'}); this._generation = 0; this._inspectSequence = 0;
    this._busy = false; this._data = null; this._plan = null; this._follow = true;
    this._visibility = () => { if (!document.hidden) this._poll(); };
  }
  setConfig(config) {
    this._config = config || {};
    if (!this._built) this._build();
  }
  set hass(value) { this._hass = value; if (this.isConnected && !this._timer) this._start(); }
  getCardSize() { return 6; }
  getGridOptions() { return {columns: 12, rows: 'auto'}; }
  connectedCallback() { if (!this._built) this._build(); this._start(); }
  disconnectedCallback() {
    clearInterval(this._timer); this._timer = null; this._generation++; this._inspectSequence++;
    document.removeEventListener('visibilitychange', this._visibility);
  }
  _node(tag, text, parent, cls) {
    const e = document.createElement(tag); if (text !== null) e.textContent = text;
    if (cls) e.className = cls; if (parent) parent.append(e); return e;
  }
  _build() {
    this._built = true;
    this._node('style', `
      :host{display:block}ha-card{padding:20px;overflow:hidden}h2{margin:0 0 6px;font-size:20px;font-weight:600}
      .muted{color:var(--secondary-text-color);font-size:13px;line-height:1.5}.operation{font-size:18px;font-weight:600;margin:16px 0 8px}
      .metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:12px 0 16px}
      .metric{padding:10px 12px;border:1px solid var(--divider-color);border-radius:10px}.metric span{display:block;font-size:13px;color:var(--secondary-text-color)}
      .metric strong{display:block;font-size:16px;margin-top:4px;overflow-wrap:anywhere}.controls{display:flex;align-items:end;flex-wrap:wrap;gap:8px;margin:16px 0}
      label{font-size:13px;display:flex;flex-direction:column;gap:4px}input{box-sizing:border-box;width:100px;min-height:44px;padding:8px;font:inherit;font-size:16px;background:var(--card-background-color);color:var(--primary-text-color);border:1px solid var(--divider-color);border-radius:8px}
      button{min-height:44px;padding:8px 12px;font:inherit;font-size:14px;border-radius:8px;border:1px solid var(--divider-color);background:var(--card-background-color);color:var(--primary-text-color);cursor:pointer}
      button.primary{background:var(--primary-color);color:var(--text-primary-color,#fff);border-color:transparent}button:disabled{opacity:.5;cursor:default}
      .plan{border-top:1px solid var(--divider-color);padding-top:16px}.plan h3{font-size:16px;margin:0 0 8px}.feature{margin:10px 0}.feature strong{font-size:14px}.feature p{margin:3px 0;font-size:13px;line-height:1.45;color:var(--secondary-text-color)}
      .error{color:var(--error-color);font-size:14px;line-height:1.45;margin-top:12px}.file{overflow-wrap:anywhere;margin-top:10px}
      progress{width:100%;height:7px;accent-color:var(--primary-color)}:focus-visible{outline:2px solid var(--primary-color);outline-offset:3px}
      @media(max-width:400px){ha-card{padding:16px}.controls button{flex:1}.operation{font-size:17px}}
    `, this.shadowRoot);
    const card = this._node('ha-card', null, this.shadowRoot);
    this._node('h2', this._config?.title || 'Что делает U1', card);
    this._fresh = this._node('div', 'Подключение к анализатору…', card, 'muted');
    this._op = this._node('div', 'Состояние неизвестно', card, 'operation');
    this._op.setAttribute('role', 'status');
    this._source = this._node('div', '', card, 'muted');
    this._progress = this._node('progress', null, card); this._progress.max = 100;
    this._progress.setAttribute('aria-label', 'Прогресс печати');
    const metrics = this._node('div', null, card, 'metrics'); this._metrics = {};
    for (const [key, title] of [['layer','Слой'],['tool','Головка / материал'],['nozzle','Сопло'],['bed','Стол']]) {
      const box = this._node('div', null, metrics, 'metric'); this._node('span', title, box);
      this._metrics[key] = this._node('strong', '—', box);
    }
    const controls = this._node('div', null, card, 'controls');
    const label = this._node('label', 'Номер слоя', controls);
    this._input = this._node('input', null, label); this._input.type = 'number'; this._input.min = '1'; this._input.step = '1';
    this._input.addEventListener('keydown', e => { if (e.key === 'Enter') this._inspect(); });
    this._inspectButton = this._node('button', 'Разобрать слой', controls, 'primary');
    this._inspectButton.addEventListener('click', () => this._inspect());
    this._currentButton = this._node('button', 'Текущий слой', controls);
    this._currentButton.addEventListener('click', () => { this._inspectSequence++; this._follow = true; this._plan = this._data?.layer_plan; this._input.value = this._data?.layer || ''; this._error.textContent = ''; this._inspectButton.disabled = this._data?.index_state !== 'ready'; this._showPlan(); });
    this._error = this._node('div', '', card, 'error'); this._error.setAttribute('role', 'alert');
    this._planBox = this._node('div', null, card, 'plan');
    this._file = this._node('div', '', card, 'muted file');
    this._showPlan();
  }
  _start() {
    if (this._timer || !this._hass || !this.isConnected) return;
    document.addEventListener('visibilitychange', this._visibility);
    this._timer = setInterval(() => this._poll(), 5000); this._poll();
  }
  async _service(service, service_data = {}) {
    const result = await this._hass.callWS({type:'call_service', domain:'rest_command', service,
      service_data, return_response:true});
    const response = result?.response;
    if (!response || response.status !== 200) {
      const code = response?.content?.error;
      throw new Error(code === 'job_changed' ? 'Задание изменилось. Выбери слой нового файла.' :
        code === 'layer_not_found' ? 'Такого слоя нет в индексированном файле.' :
        code === 'index_not_ready' ? 'G-code ещё не проиндексирован.' : 'Анализатор временно недоступен.');
    }
    return response.content;
  }
  async _poll() {
    if (!this._hass || this._busy || !this.isConnected || document.hidden) return;
    this._busy = true; const generation = this._generation;
    try {
      const data = await this._service('u1_inspector_status');
      if (generation !== this._generation || !this.isConnected) return;
      const changedJob = this._data?.job_fingerprint !== data.job_fingerprint;
      if (changedJob) { this._follow = true; this._generation++; }
      this._data = data; this._error.textContent = '';
      this._op.textContent = `${data.operation?.estimated ? '≈ ' : ''}${data.operation?.label || 'Не определено'}`;
      const sources = {gcode_cursor:'По позиции G-code (оценка; возможен сдвиг из-за буфера)',firmware_action:'Сообщено прошивкой U1',firmware_state:'Сообщено прошивкой U1',printer_state:'Состояние принтера',layer_only:'Доступен только план слоя',unknown:'Операция не подтверждена',stale:'Нет свежей телеметрии'};
      this._source.textContent = [sources[data.operation?.source], data.operation?.note].filter(Boolean).join('. ');
      const stamp = data.observed_at ? new Date(data.observed_at*1000).toLocaleTimeString() : '—';
      this._fresh.textContent = data.stale ? 'Связь потеряна. Значения ниже — последние известные.' : `Данные ${stamp} · обновление каждые 5 секунд`;
      this._metrics.layer.textContent = `${data.layer ?? '—'} / ${data.total_layers ?? '—'}`;
      this._metrics.tool.textContent = [data.physical_tool == null ? null : `${data.physical_tool+1} (T${data.physical_tool})`,data.material === 'NONE' ? null : data.material].filter(Boolean).join(' · ') || '—';
      for (const key of ['nozzle','bed']) {
        const t = data.temperatures || {}; this._metrics[key].textContent = t[key] == null ? '—' : `${Math.round(t[key])} / ${t[key+'_target'] ?? '—'} °C`;
      }
      if (data.progress_percent == null || data.stale) this._progress.removeAttribute('value'); else this._progress.value = data.progress_percent;
      if (data.total_layers) this._input.max = String(data.total_layers); else this._input.removeAttribute('max');
      this._inspectButton.disabled = data.index_state !== 'ready';
      if (this._follow) { this._plan = data.layer_plan; if (document.activeElement !== this && this.shadowRoot.activeElement !== this._input) this._input.value = data.layer || ''; }
      this._file.textContent = data.file_name ? `Файл: ${data.file_name} · Индекс: ${data.index_state}` : 'Файл печати не выбран.';
      this._showPlan();
    } catch (error) {
      if (generation === this._generation && this.isConnected) {
        this._error.textContent = error?.message || 'Не удалось получить состояние.';
        this._fresh.textContent = 'Нет связи с анализатором. Значения ниже — последние известные.';
        this._op.textContent = 'Текущая операция не подтверждена'; this._source.textContent = '';
        this._progress.removeAttribute('value');
      }
    }
    finally { this._busy = false; }
  }
  async _inspect() {
    const layer = Number(this._input.value);
    if (!Number.isInteger(layer) || layer < 1 || layer > 100000) { this._error.textContent = 'Укажи целый номер слоя от 1 до 100000.'; return; }
    const job = this._data?.job_fingerprint; if (!job) return;
    const generation = this._generation; const sequence = ++this._inspectSequence; this._inspectButton.disabled = true;
    try {
      const plan = await this._service('u1_inspect_layer', {layer, job});
      if (generation !== this._generation || sequence !== this._inspectSequence || !this.isConnected || job !== this._data?.job_fingerprint) return;
      this._follow = false; this._plan = plan; this._error.textContent = ''; this._showPlan();
    } catch(error) { if (generation === this._generation && sequence === this._inspectSequence && this.isConnected) this._error.textContent = error?.message || 'Разбор слоя недоступен.'; }
    finally { if (sequence === this._inspectSequence) this._inspectButton.disabled = this._data?.index_state !== 'ready'; }
  }
  _showPlan() {
    if (!this._planBox) return; this._planBox.replaceChildren(); const p = this._plan;
    if (!p) { this._node('p','План слоя появится после загрузки размеченного G-code.',this._planBox,'muted'); return; }
    this._node('h3',`Слой ${p.layer}${p.z_mm == null ? '' : ` · Z ${p.z_mm} мм`}`,this._planBox);
    this._node('div','Что запланировано на этом слое — не утверждение о текущем движении.',this._planBox,'muted');
    for (const f of p.features || []) {
      const row = this._node('div',null,this._planBox,'feature');
      this._node('strong',f.label,row); this._node('p',f.description,row);
    }
    if (!(p.features || []).length) this._node('p','Типы траекторий не размечены слайсером.',this._planBox,'muted');
    if (p.special_operations?.length) this._node('p',`Дополнительные команды: ${p.special_operations.join(' · ')}`,this._planBox,'muted');
    this._node('div',`${p.move_count} команд движения · ${p.positive_extrusion_mm} мм положительной подачи (не масса и не нетто-расход)`,this._planBox,'muted');
    for (const warning of p.warnings || []) this._node('div',warning,this._planBox,'muted');
  }
}
if (!customElements.get('u1-inspector-card')) customElements.define('u1-inspector-card', U1InspectorCard);
window.customCards = window.customCards || [];
window.customCards.push({type:'u1-inspector-card',name:'U1 Print Inspector',description:'Read-only live operation and annotated G-code layer inspection.'});
