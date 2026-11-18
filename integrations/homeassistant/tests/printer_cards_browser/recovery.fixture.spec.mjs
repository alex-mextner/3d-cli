// CI-only, real renderer. No HA authentication, real API, printer or browser profile.
import { test, expect } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { parse } from 'yaml';
import { createServer } from 'node:http';
import { readFileSync, mkdtempSync, rmSync, appendFileSync, openSync, closeSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';

if (!process.env.CI) throw new Error('Run this real-browser fixture only in the approved CI environment');
const here = dirname(fileURLToPath(import.meta.url));
const integration = process.env.PRINTER_CARD_INTEGRATION;
if (!integration) throw new Error('Set PRINTER_CARD_INTEGRATION to the candidate integrations/homeassistant directory');
const logPath = join(tmpdir(), `printer-card-browser-${Date.now()}-${process.pid}.log`);
closeSync(openSync(logPath, 'wx', 0o600));
function log(message) {
  const line = `${new Date().toISOString()} ${message}\n`;
  appendFileSync(logPath, line);
  process.stderr.write(line);
}
log(`Isolated frontend recovery fixture; private log ${logPath}`);
const digest = value => createHash('sha256').update(value).digest('hex');
const heldResponses = new Set();
let bundleReleased = false;
let work, server, origin, nativeEntry, configs, candidateBootstrap, candidateBundle;
const beforeBootstrap = readFileSync(join(here, 'before-printer-cards-bootstrap.js'));
if (digest(beforeBootstrap) !== '65c1ccc50c7f3e016877d9bf72d8b9e3c59a628a3ea50c5f2910874b7fc2c85a') {
  throw new Error('The pre-shell baseline bootstrap changed');
}

function customCards(value) {
  if (Array.isArray(value)) return value.flatMap(customCards);
  if (!value || typeof value !== 'object') return [];
  if (String(value.type || '').startsWith('custom:')) return [value];
  return Object.values(value).flatMap(customCards);
}


function htmlDocument(mode) {
  return `<!doctype html><meta charset="utf-8"><title>Isolated frontend recovery fixture</title>
<style>body{margin:24px;font:16px system-ui;--primary-text-color:#222;--secondary-text-color:#666;--error-color:#db4437;--divider-color:#ddd;--card-background-color:white;--ha-border-radius-lg:12px}h1{font-size:22px}#scope{margin-bottom:24px;color:#555}</style>
<h1>Isolated frontend recovery fixture · ${mode === 'before' ? 'Before' : 'After'}</h1>
<p id="scope">Home Assistant 20260826.7 renderers. Local synthetic state. No live account, camera stream or printer.</p>
<div id="cards"></div><script type="module">
window.customCards = [{ type: 'fixture-existing-card', name: 'Existing fixture card' }];
await import('/${mode}/printer-cards-bootstrap.js');
await import('/ha-entry.js');
</script>`;
}

function releaseBundle(send) {
  bundleReleased = true;
  for (const pending of heldResponses) if (pending.mode === 'after') pending.send();
  return send('{}', 'application/json');
}

function serveBundle(url, response, send) {
  // The test releases the after bundle only after measuring the real >2s UI.
  const mode = url.pathname.split('/')[1];
  if (mode === 'after' && bundleReleased) return send(candidateBundle);
  const pending = { mode, send: () => send(candidateBundle) };
  heldResponses.add(pending);
  response.on('close', () => heldResponses.delete(pending));
}

test.beforeAll(async () => {
  work = mkdtempSync(join(here, '.isolated-recovery-'));
  execFileSync('python3', [join(here, 'build-native-entry.py'), '--work-dir', work],
    { cwd: here, stdio: 'inherit', timeout: 30000 });
  nativeEntry = readFileSync(join(work, 'ha-entry.js'));
  const pinned = JSON.parse(readFileSync(join(here, 'ha-frontend-fixture.json'), 'utf8'));
  candidateBootstrap = readFileSync(join(integration, 'www/printer-cards/printer-cards-bootstrap.js'));
  candidateBundle = readFileSync(join(integration, 'www/printer-cards/printer-cards.bundle.js'));
  configs = customCards(parse(readFileSync(join(integration, 'dashboards/ui-3d-printers.yaml'), 'utf8')));
  if (configs.length !== 4) throw new Error(`Expected the four current custom cards, got ${configs.length}`);
  log(`Pinned HA ${pinned.frontend_version}; bootstrap ${digest(candidateBootstrap)}; bundle ${digest(candidateBundle)}`);
  server = createServer((request, response) => {
    const url = new URL(request.url, 'http://fixture.invalid');
    const send = (body, type = 'text/javascript') => {
      if (!response.destroyed) response.writeHead(200, { 'Content-Type': type, 'Cache-Control': 'no-store' }).end(body);
    };
    if (url.pathname === '/3d-printers/overview') return send(htmlDocument(url.searchParams.get('mode') === 'before' ? 'before' : 'after'), 'text/html');
    if (url.pathname === '/ha-entry.js') return send(nativeEntry);
    if (url.pathname === '/configs.json') return send(JSON.stringify(configs), 'application/json');
    if (url.pathname === '/before/printer-cards-bootstrap.js') return send(beforeBootstrap);
    if (url.pathname === '/after/printer-cards-bootstrap.js') return send(candidateBootstrap);
    if (url.pathname === '/fixture/hold-bundle' && request.method === 'POST') {
      bundleReleased = false;
      return send('{}', 'application/json');
    }
    if (url.pathname === '/fixture/release-bundle' && request.method === 'POST') {
      return releaseBundle(send);
    }
    if (/^\/(before|after)\/printer-cards\.bundle\.js$/.test(url.pathname)) {
      return serveBundle(url, response, send);
    }
    response.writeHead(404).end('Isolated fixture: route unavailable');
  });
  await new Promise(resolveListen => server.listen(0, '127.0.0.1', resolveListen));
  origin = `http://127.0.0.1:${server.address().port}`;
  log(`Local renderer ready at ${origin}`);
});

test.afterAll(async () => {
  if (server) { server.closeAllConnections(); await new Promise(resolveClose => server.close(resolveClose)); }
  if (work) rmSync(work, { recursive: true, force: true });
  log(`Fixture complete; private log ${logPath}`);
});

async function expectMountedImplementations(page) {
  for (const [tag, count] of [['printer-webrtc-camera', 2], ['u1-inspector-card', 1], ['u1-share-card', 1]]) {
    const actual = page.locator(`${tag}-implementation`);
    await expect(actual).toHaveCount(count, { timeout: 15000 });
    await expect.poll(() => actual.evaluateAll(elements => elements.every(element => {
      const rect = element.getBoundingClientRect(); return element.isConnected && rect.width > 0 && rect.height > 0;
    }))).toBe(true);
  }
  expect(await page.evaluate(() => window.__fixtureApiCalls.filter(type => type === 'auth/sign_path').length)).toBeGreaterThanOrEqual(2);
  await expect(page.locator('hui-error-card')).toHaveCount(0);
  await expect(page.getByText('Загрузка карточки…', { exact: true })).toHaveCount(0);
}

async function expectPickerCatalog(page) {
  const observed = await page.evaluate(() => ({
    sameArray: window.__fixtureCatalog === window.customCards,
    existing: window.__fixtureCatalog.filter(card => card.type === 'fixture-existing-card'),
    counts: ['printer-webrtc-camera', 'u1-inspector-card', 'u1-share-card'].map(type =>
      window.__fixtureCatalog.filter(card => card.type === type).length),
  }));
  expect(observed).toEqual({
    sameArray: true,
    existing: [{ type: 'fixture-existing-card', name: 'Existing fixture card' }],
    counts: [1, 1, 1],
  });
}

test('slow bundle uses genuine loading UI and then mounts actual implementations', async ({ page }, testInfo) => {
  const uncaught = [];
  page.on('pageerror', error => uncaught.push(error.message));
  await page.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
  expect((await page.request.post(`${origin}/fixture/hold-bundle`)).ok()).toBe(true);
  await page.goto(`${origin}/3d-printers/overview?mode=before`, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.__fixtureMounted === true);
  await page.waitForTimeout(2300);
  await expect(page.locator('hui-error-card')).toHaveCount(4);
  await expect(page.getByText('Configuration error', { exact: true })).toHaveCount(4);
  for (const title of await page.getByText('Configuration error', { exact: true }).all()) await expect(title).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('before-native-configuration-errors.png'), fullPage: true });
  log(`${testInfo.project.name}: baseline reproduces four native HA errors beyond 2 seconds`);

  await page.goto(`${origin}/3d-printers/overview?mode=after`, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.__fixtureMounted === true);
  await page.waitForTimeout(2300);
  await expect(page.locator('hui-error-card')).toHaveCount(0);
  await expect(page.getByRole('status')).toHaveCount(4);
  await expect(page.getByText('Загрузка карточки…', { exact: true })).toHaveCount(4);
  for (const message of await page.getByText('Загрузка карточки…', { exact: true }).all()) await expect(message).toBeVisible();
  await expectPickerCatalog(page);
  await page.screenshot({ path: testInfo.outputPath('after-real-loading-shells.png'), fullPage: true });
  expect((await page.request.post(`${origin}/fixture/release-bundle`)).ok()).toBe(true);
  await expectMountedImplementations(page);
  await expectPickerCatalog(page);
  await page.screenshot({ path: testInfo.outputPath('after-actual-inner-components.png'), fullPage: true });
  expect(uncaught).toEqual([]);
  await testInfo.attach('scope', { body: 'Isolated frontend recovery fixture; native HA renderer and actual candidate cards; no production visual acceptance or live streams claimed.', contentType: 'text/plain' });
  log(`${testInfo.project.name}: real loading UI replaces red errors and all four inner components mount; no uncaught errors`);
});

test('one failed module fetch retries once and mounts all actual implementations', async ({ page }, testInfo) => {
  const uncaught = [];
  const bundleRequests = [];
  page.on('pageerror', error => uncaught.push(error.message));
  await page.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
  await page.route('**/after/printer-cards.bundle.js*', async route => {
    bundleRequests.push(route.request().url());
    if (bundleRequests.length === 1) {
      await route.fulfill({ status: 503, contentType: 'text/javascript', body: '/* Isolated fixture: one transient failure. */' });
    } else {
      await route.continue();
    }
  });
  await page.addInitScript(() => {
    window.__fixtureErrorCardsAdded = 0;
    new MutationObserver(records => {
      for (const record of records) for (const node of record.addedNodes) {
        if (node instanceof Element) {
          window.__fixtureErrorCardsAdded += Number(node.matches('hui-error-card')) + node.querySelectorAll('hui-error-card').length;
        }
      }
    }).observe(document, { childList: true, subtree: true });
  });
  // Make this test independent of execution order or --grep selection.
  expect((await page.request.post(`${origin}/fixture/release-bundle`)).ok()).toBe(true);
  await page.goto(`${origin}/3d-printers/overview?mode=after`, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.__fixtureMounted === true);
  await expect(page.locator('hui-error-card')).toHaveCount(0);
  await expectMountedImplementations(page);
  expect(bundleRequests.map(url => new URL(url).searchParams.get('attempt'))).toEqual(['1', '2']);
  expect(await page.evaluate(() => ({
    phase: window.__printerCardResources.phase,
    attempts: window.__printerCardResources.attempts,
    errorsAdded: window.__fixtureErrorCardsAdded,
  }))).toEqual({ phase: 'ready', attempts: 2, errorsAdded: 0 });
  expect(uncaught).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath('after-one-transient-failure-real-components.png'), fullPage: true });
  await testInfo.attach('scope', { body: 'Isolated frontend recovery fixture: one HTTP 503 module fetch, one retry, actual candidate components. No production account, printer, camera stream or visual acceptance claimed.', contentType: 'text/plain' });
  log(`${testInfo.project.name}: one HTTP 503 recovered with exactly two module attempts, zero native error cards, all actual components mounted`);
});
