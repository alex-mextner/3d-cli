// Actual bootstrap/bundle modules, isolated DOM and deterministic network/timer fixtures.
// This is a unit regression, not a browser or a production screenshot.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = process.argv[2];
const configs = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const bootstrapPath = path.join(root, 'www/printer-cards/printer-cards-bootstrap.js');
const bundlePath = path.join(root, 'www/printer-cards/printer-cards.bundle.js');
const bootstrap = fs.existsSync(bootstrapPath) ? fs.readFileSync(bootstrapPath, 'utf8') : '';
const bundle = fs.existsSync(bundlePath) ? fs.readFileSync(bundlePath, 'utf8') : '';
const tags = ['printer-webrtc-camera', 'u1-inspector-card', 'u1-share-card'];
const flush = () => new Promise(resolve => setImmediate(resolve));

class UnitNode extends EventTarget {
  constructor(tag = 'element') {
    super(); this.tagName = tag; this.children = []; this.attributes = {};
    this.dataset = {}; this.isConnected = false;
    this.style = {};
  }
  append(...children) { this.children.push(...children); }
  add(child) { this.append(child); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  removeAttribute(key) { delete this.attributes[key]; }
  attachShadow() { this.shadowRoot = new UnitNode('shadow'); return this.shadowRoot; }
  set innerHTML(html) {
    this._html = html; this._ids = new Map(); this._ttl = [];
    for (const item of html.matchAll(/id="([^"]+)"/g)) this._ids.set(item[1], new UnitNode());
    for (const item of html.matchAll(/data-ttl="([^"]+)"/g)) {
      const node = new UnitNode('button'); node.dataset.ttl = item[1]; this._ttl.push(node);
    }
  }
  getElementById(id) { return this._ids?.get(id) || null; }
  querySelectorAll(selector) { return selector === '[data-ttl]' ? this._ttl || [] : []; }
}

function registry(ready) {
  const values = new Map();
  return {
    values, get: name => values.get(name), whenDefined: () => ready,
    define(name, value) {
      assert.equal(values.has(name), false, `duplicate definition of ${name}`);
      values.set(name, value);
    },
  };
}

async function fixture(options = {}) {
  let now = 0, nextTimer = 1, resolveReady;
  const ready = new Promise(resolve => { resolveReady = resolve; });
  const timers = new Map(), imports = [], warnings = [];
  const window = new EventTarget(), document = new EventTarget();
  window.customElements = registry(ready);
  window.location = { pathname: options.pathname || '/3d-printers/overview' };
  document.hidden = false;
  document.createElement = name => new UnitNode(name);
  const navigator = { onLine: true };
  const setTimeout = (fn, ms) => {
    const id = nextTimer++; timers.set(id, { fn, at: now + ms }); return id;
  };
  const clearTimeout = id => timers.delete(id);
  Object.assign(window, { document, navigator, setTimeout, clearTimeout });
  class Clock extends Date { static now() { return now; } }
  const context = vm.createContext({
    window, document, navigator, HTMLElement: UnitNode, WebSocket: { OPEN: 1, CLOSED: 3 },
    Option: class extends UnitNode {
      constructor(text, value) { super('option'); this.textContent = text; this.value = value; }
    },
    customElements: window.customElements, URL, Date: Clock, Symbol, Promise, performance: { now: () => now },
    setTimeout, clearTimeout,
    console: { warn: (...args) => warnings.push(args), error: (...args) => warnings.push(args) },
    setInterval() { throw Error('The bootstrap must not poll or start card networking'); },
    clearInterval() {},
  });
  let env;
  async function evaluateBundle(identifier) {
    const module = new vm.SourceTextModule(bundle, { context, identifier });
    await module.link(() => { throw Error('The bundle must have no external imports'); });
    await module.evaluate();
    return module;
  }
  async function evaluateBootstrap(suffix = '') {
    const module = new vm.SourceTextModule(bootstrap, {
      context, identifier: `https://isolated.invalid/local/printer-cards/printer-cards-bootstrap.js?v=test${suffix}`,
      initializeImportMeta(meta) { meta.url = module.identifier; },
      importModuleDynamically: async url => {
        imports.push(url);
        return options.importer ? options.importer(url, env) : evaluateBundle(url);
      },
    });
    await module.link(() => { throw Error('Bootstrap must not eagerly import card classes'); });
    await module.evaluate();
    await flush();
  }
  env = {
    window, document, navigator, imports, warnings, evaluateBundle, evaluateBootstrap,
    get status() { return window.__printerCardResources; },
    get missing() { return tags.filter(tag => !window.customElements.get(`${tag}-implementation`)); },
    async start(swap = false) {
      if (swap) {
        window.customElements = registry(ready);
        context.customElements = window.customElements;
      }
      window.customElements.define('home-assistant', class {});
      resolveReady(); await flush(); await flush();
    },
    async advance(ms) {
      const end = now + ms;
      for (let steps = 0; steps < 100; steps++) {
        const due = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) { now = end; await flush(); return; }
        const [id, timer] = due; timers.delete(id); now = timer.at; timer.fn(); await flush(); await flush();
      }
      throw Error('Unexpected retry loop');
    },
    async event(target, name) { target.dispatchEvent(new Event(name)); await flush(); await flush(); },
  };
  await evaluateBootstrap();
  return env;
}

async function cameraPriorityRegression() {
  const env = await fixture(); await env.start();
  const Camera = env.window.customElements.get('printer-webrtc-camera-implementation');
  const cases = [
    [true, false, 'avc1.4d002a', true], [true, false, 'hvc1.1.6', false],
    [false, true, 'mp4a.40.2', true], [true, true, 'avc1.4d002a,mp4a.40.2', true],
    [true, true, 'hvc1.1.6,mp4a.40.2', false], [false, false, '', true],
  ];
  for (const [video, audio, codecs, preferRtc] of cases) {
    const card = new Camera(); let plays = 0, wsClosed = 0, pcClosed = 0;
    card.video = { srcObject: null }; card.play = () => { plays++; };
    card.ws = { close: () => { wsClosed++; } }; card.pc = { close: () => { pcClosed++; } };
    card.mseCodecs = codecs;
    const stream = { getVideoTracks: () => video ? [{}] : [], getAudioTracks: () => audio ? [{}] : [] };
    const temporary = { srcObject: stream };
    Object.getPrototypeOf(Camera.prototype).onpcvideo.call(card, temporary);
    assert.equal(temporary.srcObject, null);
    assert.equal(plays, preferRtc ? 1 : 0); assert.equal(wsClosed, preferRtc ? 1 : 0);
    assert.equal(pcClosed, preferRtc ? 0 : 1);
    assert.equal(card.video.srcObject, preferRtc ? stream : null);
  }
}

async function inspectorStatusRegression() {
  const env = await fixture(); await env.start();
  const Inspector = env.window.customElements.get('u1-inspector-card-implementation');
  const card = new Inspector(); card.setConfig({}); card.isConnected = true; card._hass = {};
  const removed = []; card._progress = { removeAttribute: name => removed.push(name) };
  const initial = {
    job_fingerprint: 'job-a', operation: { label: 'Глажка', estimated: true, source: 'gcode_cursor' },
    observed_at: 1, stale: false, layer: 14, total_layers: 104, head: 'T0', material: 'PLA',
    temperatures: { nozzle: 219.9, nozzle_target: 220, bed: 54.1, bed_target: 55 },
    progress_percent: 20, index_state: 'ready', file_name: 'sample.gcode',
    layer_plan: { layer: 14, features: [], warnings: [] },
  };
  card._service = async () => initial; await card._poll();
  assert.equal(card._op.textContent, '≈ Глажка'); assert.equal(card._metrics.layer.textContent, '14 / 104');
  assert.equal(card._metrics.nozzle.textContent, '220 / 220 °C'); assert.equal(card._progress.value, 20);
  const manual = { layer: 8, features: [], warnings: [] }; card._plan = manual; card._follow = false;
  card._service = async () => ({ ...initial, stale: true }); await card._poll();
  assert.equal(card._plan, manual); assert.equal(removed.at(-1), 'value');
  assert.match(card._fresh.textContent, /последние известные/);
  card._service = async () => ({ ...initial, job_fingerprint: 'job-b' }); await card._poll();
  assert.equal(card._follow, true); assert.equal(card._plan, initial.layer_plan);
  card._service = async () => { throw Error('offline fixture'); }; await card._poll();
  assert.equal(card._busy, false); assert.equal(card._op.textContent, 'Текущая операция не подтверждена');
  let resolveOld; card._service = () => new Promise(resolve => { resolveOld = resolve; });
  const pending = card._poll(); card._generation++; resolveOld(initial); await pending;
  assert.equal(card._op.textContent, 'Текущая операция не подтверждена');
}

async function shareSelectionRegression() {
  const env = await fixture(); await env.start();
  const Share = env.window.customElements.get('u1-share-card-implementation');
  const card = new Share(); card.setConfig({ entity: 'camera.u1_camera' });
  let cloudCalls = 0;
  card._hass = {
    states: { 'camera.u1_camera': { entity_id: 'camera.u1_camera', attributes: { friendly_name: 'U1' } } },
    config: { external_url: 'https://example.invalid' },
    callWS: async () => { cloudCalls++; return { remote_connected: true, remote_domain: 'cloud.example.invalid' }; },
  };
  card._sync(); assert.equal(card._el('camera').value, 'camera.u1_camera');
  assert.equal(card._el('public').value, 'https://example.invalid'); assert.equal(cloudCalls, 0);
  card._el('public').value = ''; card._hass.config.external_url = 'http://invalid.test';
  card._sync(); await flush();
  assert.equal(card._el('public').value, 'https://cloud.example.invalid'); assert.equal(cloudCalls, 1);
  card._el('public').value = ''; card._sync(); await flush(); assert.equal(cloudCalls, 1);
  card._hass.states = {}; card._sync(); assert.equal(card._el('generate').disabled, true);
}

async function loadingShellPreservesState() {
  let complete;
  const env = await fixture({ importer: (url, state) => new Promise(resolve => {
    complete = () => state.evaluateBundle(url).then(resolve);
  }) });
  await env.start(); await env.advance(2100);
  for (const tag of tags) {
    assert.ok(env.window.customElements.get(tag), `Loading shell must exist before HA's 2-second error timer: ${tag}`);
  }
  const Camera = env.window.customElements.get('printer-webrtc-camera');
  const card = new Camera();
  card.setConfig(configs[0]); card.hass = { marker: 'old' };
  card.setConfig({ ...configs[0], title: 'Latest title' });
  const latest = { marker: 'latest' }; card.hass = latest;
  card.connectedCallback();
  assert.match(card._message.textContent, /Загрузка/);
  assert.equal(card.getCardSize(), 5);
  await complete(); await flush();
  assert.equal(card._card.config.title, 'Latest title');
  assert.equal(card._card.hass, latest);
  const inner = card._card; inner.streamID = 12;
  card.hass = { marker: 'next' };
  assert.equal(card._card, inner); assert.equal(inner.streamID, 12, 'hass updates must not reset the stream/config');
  assert.ok(card.shadowRoot.children.includes(inner));
  card.disconnectedCallback();
}

async function registryBarrierUsesCurrentRegistry() {
  const env = await fixture();
  const initial = env.window.customElements;
  assert.equal(env.imports.length, 0, 'No card class is loaded before the HA/polyfill barrier');
  await env.start(true);
  assert.deepEqual(env.missing, [], 'Independent bootstrap must register all three tags after HA is ready');
  assert.ok(tags.every(tag => !initial.get(tag)), 'Definitions must use the new registry');
  assert.equal(env.status.phase, 'ready');
}

async function transientFailureRecovers() {
  const env = await fixture({ importer: (url, state) => state.imports.length === 1
    ? Promise.reject(Error('injected transport failure')) : state.evaluateBundle(url) });
  await env.start();
  await env.event(env.window, 'online');
  assert.equal(env.imports.length, 1, 'An event must not bypass an already scheduled retry');
  await env.advance(1000);
  assert.deepEqual(env.missing, []); assert.equal(env.imports.length, 2);
  assert.notEqual(env.imports[0], env.imports[1], 'Retry must use a fresh, self-contained module URL');
  await env.advance(120000); await env.event(env.window, 'online');
  assert.equal(env.imports.length, 2, 'Successful recovery must stop requests');
}

async function lateImportKeepsReadiness() {
  let finishOld;
  const env = await fixture({ importer: (url, state) => state.imports.length === 1
    ? new Promise(resolve => { finishOld = () => state.evaluateBundle(url).then(resolve); })
    : state.evaluateBundle(url) });
  await env.start(); await env.advance(16000);
  assert.equal(env.imports.length, 2); assert.deepEqual(env.missing, []);
  await finishOld(); await flush();
  assert.equal(env.status.phase, 'ready'); assert.deepEqual(env.missing, []);
  assert.equal(env.window.customCards.length, 3, 'Late module must not duplicate card catalog entries');
}

async function permanentFailureIsBounded() {
  const env = await fixture({ importer: () => Promise.reject(Error('permanent failure')) });
  await env.start(); await env.advance(15000);
  await env.event(env.window, 'online');
  assert.equal(env.imports.length, 4, 'Automatic recovery must respect the cycle-start cooldown');
  await env.advance(105000);
  assert.equal(env.imports.length, 4); assert.equal(env.status.phase, 'failed');
  assert.deepEqual(Array.from(env.status.missing), tags);
  assert.ok(env.warnings.length > 0); await env.advance(120000);
  assert.equal(env.imports.length, 4, 'No timer or automatic page-reload loop after exhaustion');
  await env.event(env.window, 'online'); await env.advance(120000);
  assert.equal(env.imports.length, 8, 'A later online event permits one new bounded recovery cycle');
}

async function manualRetryMountsShareCard() {
  const env = await fixture({ importer: (url, state) => state.imports.length <= 4
    ? Promise.reject(Error('initial outage')) : state.evaluateBundle(url) });
  await env.start();
  const Constructor = env.window.customElements.get('u1-share-card');
  const card = new Constructor(); card.setConfig({ entity: 'camera.u1_camera' });
  card.connectedCallback(); await env.advance(15000);
  assert.equal(env.status.phase, 'failed');
  assert.match(card._message.textContent, /Не удалось загрузить/);
  assert.equal(card._retry.hidden, false);
  card._retry.dispatchEvent(new Event('click')); await flush(); await flush();
  assert.equal(env.status.phase, 'ready'); assert.equal(env.imports.length, 5);
  assert.ok(card._card); assert.equal(card._card._config.entity, 'camera.u1_camera');
  assert.deepEqual(JSON.parse(JSON.stringify(card.getGridOptions())), { columns: 12, rows: 7, min_columns: 6, min_rows: 6 });
  card.disconnectedCallback();
}

async function lateRejectionDoesNotMultiplyRetries() {
  let rejectOld;
  const env = await fixture({ importer: (url, state) => state.imports.length === 1
    ? new Promise((resolve, reject) => { rejectOld = reject; })
    : Promise.reject(Error('permanent failure')) });
  await env.start(); await env.advance(15500);
  rejectOld(Error('late rejection after timeout')); await flush();
  await env.advance(120000);
  assert.equal(env.imports.length, 4, 'A timed-out attempt must not settle twice and create extra retries');
  assert.equal(env.status.attempts, 4); assert.equal(env.status.phase, 'failed');
}

async function reconnectionRefreshesRetry() {
  const env = await fixture({ importer: (url, state) => {
    if (state.imports.length > 4) return state.evaluateBundle(url);
    if (state.imports.length === 4) state.navigator.onLine = false;
    return Promise.reject(Error('outage during final attempt'));
  } });
  await env.start();
  const Constructor = env.window.customElements.get('u1-share-card');
  const card = new Constructor(); card.setConfig({ entity: 'camera.u1_camera' });
  card.connectedCallback(); await env.advance(15000);
  assert.equal(env.status.phase, 'failed'); assert.equal(card._retry.disabled, true);
  env.navigator.onLine = true; await env.event(env.window, 'online');
  assert.equal(env.imports.length, 4, 'Automatic cooldown remains intact');
  assert.equal(card._retry.disabled, false, 'Restored connectivity must re-enable explicit Retry during cooldown');
  card._retry.dispatchEvent(new Event('click')); await flush(); await flush();
  assert.equal(env.status.phase, 'ready'); assert.equal(env.imports.length, 5);
  card.disconnectedCallback();
}

async function slowCycleAllowsLaterRecovery() {
  const env = await fixture({ importer: () => new Promise(() => {}) });
  await env.start(); await env.advance(75000);
  assert.equal(env.status.phase, 'failed'); assert.equal(env.imports.length, 4);
  await env.event(env.window, 'online');
  assert.equal(env.imports.length, 5, 'A slow exhausted cycle is already beyond the cycle-start cooldown');
}

async function importsRespectVisibilityAndDemand() {
  const env = await fixture({ pathname: '/lovelace/0' });
  await env.start(); await env.advance(120000); assert.equal(env.imports.length, 0);
  env.window.location.pathname = '/3d-printers/overview';
  env.navigator.onLine = false; await env.event(env.window, 'location-changed');
  assert.equal(env.imports.length, 0); env.navigator.onLine = true;
  env.document.hidden = true; await env.event(env.window, 'online');
  assert.equal(env.imports.length, 0); env.document.hidden = false;
  await env.event(env.document, 'visibilitychange');
  assert.deepEqual(env.missing, []);
  assert.equal(env.imports.length, 1, 'The visibility event must load the implementations itself');
  assert.equal(env.status.phase, 'ready');
  await env.evaluateBootstrap('&duplicate=1'); await env.event(env.window, 'online');
  assert.equal(env.imports.length, 1, 'Duplicate entrypoints must share one loader');
}

async function connectedCardCreatesDemand() {
  const env = await fixture({ pathname: '/lovelace/0' });
  const legacy = { type: 'webrtc-camera', name: 'WebRTC Camera' };
  const capturedCatalog = [legacy];
  env.window.customCards = capturedCatalog;
  const standalone = await fixture({ pathname: '/lovelace/0' });
  await standalone.evaluateBundle('https://isolated.invalid/catalog-only');
  const metadata = JSON.parse(JSON.stringify(standalone.window.customCards));
  assert.deepEqual(metadata.map(card => card.type), tags);
  await env.start();
  assert.equal(env.imports.length, 0, 'An unused dashboard must not load printer components');
  assert.equal(env.window.customCards, capturedCatalog, 'HA keeps a reference to the original catalog array');
  assert.equal(capturedCatalog[0], legacy, 'Existing legacy catalog entries must be preserved');
  assert.deepEqual(JSON.parse(JSON.stringify(capturedCatalog.slice(1))), metadata,
    'The cold picker must expose the exact card metadata before any bundle import');
  await env.evaluateBootstrap('&entry=second');
  assert.equal(env.imports.length, 0, 'Publishing the catalog must not create bundle demand');
  assert.equal(capturedCatalog.length, 4, 'Both bootstrap entrypoints must share catalog entries');
  const Constructor = env.window.customElements.get('u1-share-card');
  const card = new Constructor(); card.setConfig({ entity: 'camera.u1_camera' });
  assert.equal(env.imports.length, 0, 'Configuring a disconnected card is not demand');
  card.connectedCallback(); await flush(); await flush();
  assert.equal(env.imports.length, 1, 'A connected printer card must load on a fresh other dashboard');
  assert.equal(env.status.phase, 'ready'); assert.deepEqual(env.missing, []);
  assert.ok(card._card); assert.equal(card._card._config.entity, 'camera.u1_camera');
  assert.equal(env.window.customCards, capturedCatalog);
  assert.equal(capturedCatalog[0], legacy);
  assert.deepEqual(JSON.parse(JSON.stringify(capturedCatalog.slice(1))), metadata,
    'Loading the actual implementations must not duplicate or change catalog entries');
  card.disconnectedCallback();
}

async function cardConfigsAndLegacyTagStayValid() {
  const env = await fixture(); await env.start();
  const original = class LegacyWebRTC {};
  env.window.customElements.define('webrtc-camera', original);
  await env.evaluateBundle('https://isolated.invalid/bundle?again=1');
  assert.equal(env.window.customElements.get('webrtc-camera'), original);
  assert.equal(configs.length, 4);
  for (const config of configs) {
    const Constructor = env.window.customElements.get(config.type.slice(7));
    assert.ok(Constructor, `Missing actual dashboard tag ${config.type}`);
    const card = new Constructor(); card.setConfig(config); card.setConfig(config);
  }
  const Camera = env.window.customElements.get('printer-webrtc-camera');
  assert.throws(() => new Camera().setConfig({}), /Missing/);
  const Share = env.window.customElements.get('u1-share-card');
  assert.throws(() => new Share().setConfig({ entity: 'sensor.bad' }), /camera/);
  assert.throws(() => new Share().setConfig({ public_url: 'http://invalid.test' }), /HTTPS/);
}

const scenarios = [
  ["Immediate loading shells preserve latest config/hass and mount the actual card once", loadingShellPreservesState],
  ["HA barrier and replaced registry; no Lovelace registry request required", registryBarrierUsesCurrentRegistry],
  ["Transient bundle failure recovers once, including child code in the same retry unit", transientFailureRecovers],
  ["Timed-out attempt finishing late is idempotent and cannot overwrite readiness", lateImportKeepsReadiness],
  ["Permanent failure is bounded and diagnostic; later connectivity can recover", permanentFailureIsBounded],
  ["A visible bounded-failure state provides explicit Retry and preserves the sharing card", manualRetryMountsShareCard],
  ["Late rejection after timeout cannot multiply retries or exceed the hard cap", lateRejectionDoesNotMultiplyRetries],
  ["Restored connectivity refreshes Retry during cooldown without starting an automatic request", reconnectionRefreshesRetry],
  ["A fully timed-out cycle can recover immediately on a later connectivity event", slowCycleAllowsLaterRecovery],
  ["Imports are scoped to visible printer dashboard, online state and one owner", importsRespectVisibilityAndDemand],
  ["A connected printer card loads on another dashboard without loading unused pages", connectedCardCreatesDemand],
  ["Four actual dashboard configs and validation guards preserved; legacy camera tag isolated", cardConfigsAndLegacyTagStayValid],
  ["WebRTC/MSE video and audio selection behavior preserved", cameraPriorityRegression],
  ["Inspector status, stale values, manual layer and late-response guards preserved", inspectorStatusRegression],
  ["Camera selection and external/cloud origin fallback preserved", shareSelectionRegression],
];

async function main() {
  const results = [];
  let failed = false;
  for (const [label, run] of scenarios) {
    try {
      await run();
      results.push(label);
    } catch (error) {
      failed = true;
      console.error(`FAIL ${label}\n${error?.stack || String(error)}`);
    }
  }
  console.log(JSON.stringify({ passed: results.length, results }, null, 2));
  if (failed) process.exitCode = 1;
}
main().catch(error => { console.error(error); process.exitCode = 1; });
