import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { mkdtemp, mkdir, readFile, realpath, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { build } from 'vite';
import ts from 'typescript';
import { CLIENT_PROTOCOL, createReleaseMonitor, isChunkLoadError, parseClientContract, parseRelease, RELEASE_MESSAGES } from '../src/utils/releaseCompatibility.ts';
import { releaseManifest } from '../release-manifest.ts';

const oldBuild = 'a'.repeat(64), newBuild = 'b'.repeat(64);
const asset = (build = oldBuild, protocol = CLIENT_PROTOCOL) => ({ schema_version: 1, build_id: build, client_protocol: protocol });
const contract = (protocols = [CLIENT_PROTOCOL]) => ({ schema_version: 1, supported_client_protocols: protocols });
const json = data => new Response(JSON.stringify(data), { headers: { 'Content-Type': 'application/json' } });
function harness() {
  const notices = [], calls = [];
  const state = { now: 0, asset: asset(), contract: contract() };
  const monitor = createReleaseMonitor({
    bootBuildId: oldBuild, releaseUrl: '/release.json', contractUrl: '/api/v1/config/client-contract',
    now: () => state.now, notify: value => notices.push(value),
    fetcher: async (url, options) => {
      calls.push({ url, options });
      const value = url === '/release.json' ? state.asset : state.contract;
      if (value instanceof Error) throw value;
      return value instanceof Response ? value : json(value);
    },
  });
  return { monitor, notices, calls, state, next: async () => { state.now += 30_000; await monitor.check(); } };
}

test('same release is quiet and compatible updates never interrupt or reload', async () => {
  const h = harness(); await h.monitor.check(); assert.deepEqual(h.notices, []);
  h.state.asset = asset(newBuild); await h.next(); assert.deepEqual(h.notices, ['update']);
  await h.next(); assert.deepEqual(h.notices, ['update']);
  h.state.asset = asset(oldBuild); await h.next();
  h.state.asset = asset(newBuild); await h.next(); assert.deepEqual(h.notices, ['update', 'update']);
  assert.ok(h.calls.every(({ options }) => options.cache === 'no-store' && options.credentials === 'same-origin' && !options.body && !options.method));
});

test('backend compatibility, not different image/build identities, determines incompatibility', async () => {
  const h = harness(); h.state.asset = asset(newBuild, 2); h.state.contract = contract([1, 2]);
  await h.monitor.check(); assert.deepEqual(h.notices, ['update']);
  h.state.contract = contract([2]); await h.next(); assert.deepEqual(h.notices, ['update', 'incompatible']);
  h.state.contract = contract([1]); await h.next(); assert.equal(h.notices.at(-1), 'update');
});

test('old backend, malformed manifests and network failures are not incompatibility', async () => {
  for (const invalid of [null, {}, [], 'secret server body', { schema_version: 2 }, new Response('legacy', { status: 404 }), new Response('<html>error</html>'), Error('private URL')]) {
    const h = harness(); h.state.asset = invalid; h.state.contract = invalid;
    await h.monitor.check(); assert.deepEqual(h.notices, []);
  }
});

test('a later failed check preserves earlier evidence without repeating its notification', async () => {
  const h = harness(); h.state.contract = contract([2]); await h.monitor.check();
  h.state.contract = Error('offline'); h.state.asset = undefined; await h.next();
  assert.deepEqual(h.notices, ['incompatible']);
});

test('valid JSON metadata is strictly bounded and sensitive extra fields are never returned', () => {
  assert.deepEqual(parseRelease({ ...asset(), secret: 'hidden' }), { buildId: oldBuild, clientProtocol: 1 });
  for (const value of [{ ...asset(), build_id: '/Users/private' }, { ...asset(), client_protocol: true }, { ...asset(), client_protocol: 1.5 }]) assert.equal(parseRelease(value), undefined);
  for (const values of [[], [true], ['1'], [0], [NaN], Array(17).fill(1)]) assert.equal(parseClientContract(contract(values)), undefined);
  assert.deepEqual(parseClientContract(contract([1, 2])), [1, 2]);
  assert.deepEqual(parseClientContract(contract([2, 1, 2])), [1, 2]);
});

test('oversized and invalid UTF8 bodies are cancelled without parsing or exposing them', async () => {
  for (const headers of [{ 'Content-Type': 'application/json' }, { 'Content-Type': 'application/json', 'Content-Length': '9000' }]) {
    const h = harness(); h.state.asset = new Response('x'.repeat(9000), { headers });
    h.state.contract = new Response(new Uint8Array([0xff]), { headers: { 'Content-Type': 'application/json' } });
    await h.monitor.check(); assert.deepEqual(h.notices, []);
  }
});

test('concurrent triggers share one request and focus checks are throttled', async () => {
  let resolve; const gate = new Promise(r => { resolve = r; }); let calls = 0;
  const monitor = createReleaseMonitor({ bootBuildId: oldBuild, releaseUrl: '/release', contractUrl: '/contract', notify() {}, now: () => 0,
    fetcher: async url => { calls++; await gate; return json(url === '/release' ? asset() : contract()); } });
  const first = monitor.check(); const second = monitor.check(); assert.equal(first, second); assert.equal(calls, 2);
  resolve(); await first; await monitor.check(); assert.equal(calls, 2); monitor.dispose();
});

test('disposal aborts owned reads and late responses never notify', async () => {
  let resolve; const gate = new Promise(r => { resolve = r; }); const signals = [], notices = [];
  const monitor = createReleaseMonitor({ bootBuildId: oldBuild, releaseUrl: '/release', contractUrl: '/contract', notify: n => notices.push(n),
    fetcher: async (_url, options) => { signals.push(options.signal); await gate; return json(asset(newBuild)); } });
  const pending = monitor.check(); monitor.dispose(); assert.ok(signals.every(s => s.aborted));
  resolve(); await pending; monitor.assetFailed(); await monitor.check(); assert.deepEqual(notices, []);
});

test('diagnostic timeout aborts only its reads and permits a later check', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  let now = 0, hanging = true; const signals = [], notices = [];
  const monitor = createReleaseMonitor({ bootBuildId: oldBuild, releaseUrl: '/release', contractUrl: '/contract',
    now: () => now, notify: n => notices.push(n), fetcher: async (url, options) => {
      signals.push(options.signal);
      if (hanging) await new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true }));
      return json(url === '/release' ? asset(newBuild) : contract());
    } });
  const pending = monitor.check(); t.mock.timers.tick(5000); await pending;
  assert.ok(signals.every(signal => signal.aborted)); assert.deepEqual(notices, []);
  hanging = false; now = 30_000; await monitor.check();
  assert.deepEqual(notices, ['update']); monitor.dispose();
});

test('lazy asset failure is explicit and never cleared by a successful metadata check', async () => {
  const h = harness(); h.monitor.assetFailed(); h.monitor.assetFailed(); await h.monitor.check();
  h.state.asset = asset(newBuild); await h.next();
  assert.deepEqual(h.notices, ['asset_error']);
  assert.ok(isChunkLoadError(Error('Failed to fetch dynamically imported module: private-url')));
  assert.ok(isChunkLoadError(Error('Importing a module script failed.')));
  assert.equal(isChunkLoadError(Error('ordinary application failure')), false);
  assert.doesNotMatch(RELEASE_MESSAGES.asset_error, /private-url/);
});

test('actual build identity includes late offline assets and workers, deterministically', async () => {
  const root = await realpath(await mkdtemp(join(tmpdir(), 'dataseek-release-test-')));
  const output = join(root, 'dist');
  await writeFile(join(root, 'index.html'), '<html><head></head><body>fixture</body></html>');
  let offlineAsset = 'first SVG', worker = 'first worker';
  const run = async () => {
    await build({ configFile: false, root, logLevel: 'silent', plugins: [
      { name: 'late-assets-fixture', async closeBundle() {
        await new Promise(resolve => setTimeout(resolve, 10));
        await mkdir(join(output, 'visualization-assets'), { recursive: true });
        await writeFile(join(output, 'visualization-assets/domain.svg'), offlineAsset);
      }, async writeBundle() { await writeFile(join(output, 'worker.js'), worker); } },
      releaseManifest(),
    ] });
    return { manifest: JSON.parse(await readFile(join(output, 'release.json'), 'utf8')), html: await readFile(join(output, 'index.html'), 'utf8') };
  };
  const a = await run(); const b = await run(); offlineAsset = 'changed SVG'; const c = await run();
  assert.match(a.manifest.build_id, /^[a-f0-9]{64}$/); assert.deepEqual(a.manifest, b.manifest);
  assert.notEqual(a.manifest.build_id, c.manifest.build_id);
  worker = 'changed worker'; const d = await run(); assert.notEqual(c.manifest.build_id, d.manifest.build_id);
  assert.ok(a.html.includes(`name="dataseek-release" content="${a.manifest.build_id}"`));
  assert.deepEqual(Object.keys(a.manifest).sort(), ['build_id', 'client_protocol', 'schema_version']);
});

test('actual installer owns listeners/hooks; uses existing toast and never auto reloads or writes storage', async () => {
  const source = readFileSync(new URL('../src/utils/installReleaseCompatibility.ts', import.meta.url), 'utf8');
  const ast = ts.createSourceFile('installer.ts', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const fn = ast.statements.find(node => ts.isFunctionDeclaration(node));
  const compiled = ts.transpileModule(fn.getText(ast).replace(/^export /, '').replaceAll('import.meta.env.BASE_URL', "'/'"), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None } }).outputText;
  const window = new EventTarget(), document = new EventTarget(); document.visibilityState = 'visible'; document.querySelector = () => ({ getAttribute: () => oldBuild });
  let checks = 0, disposed = 0, assetFailures = 0, after, error, config, removed = 0;
  const router = { afterEach(fn) { after = fn; return () => removed++; }, onError(fn) { error = fn; return () => removed++; } };
  const scope = { window, document, BASE_URL: '/api/v1', createReleaseMonitor(options) { config = options; return { check() { checks++; }, dispose() { disposed++; }, assetFailed() { assetFailures++; } }; }, isChunkLoadError, RELEASE_MESSAGES, showInfoToast() {} };
  const install = new Function('scope', `with(scope) { ${compiled}; return installReleaseCompatibility; }`)(scope);
  const stop = install(router); assert.equal(checks, 1); assert.equal(config.releaseUrl, '/release.json');
  window.dispatchEvent(new Event('focus')); after(); assert.equal(checks, 3);
  window.dispatchEvent(new Event('vite:preloadError')); error(Error('ordinary')); error(Error('Importing a module script failed.')); assert.equal(assetFailures, 2);
  document.visibilityState = 'hidden'; window.dispatchEvent(new Event('focus')); assert.equal(checks, 3);
  stop(); assert.equal(disposed, 1); assert.equal(removed, 2);
  document.visibilityState = 'visible'; window.dispatchEvent(new Event('focus')); assert.equal(checks, 3);
  assert.doesNotMatch(source, /location\.reload|localStorage|sessionStorage|preventDefault/);
  assert.match(readFileSync(new URL('../src/main.ts', import.meta.url), 'utf8'), /installReleaseCompatibility\(router\)/);
});

test('metadata endpoint bypasses SPA fallback/cache and missing chunks return 404', () => {
  const nginx = readFileSync(new URL('../nginx.conf', import.meta.url), 'utf8');
  assert.match(nginx, /location = \/release\.json\s*\{[^}]*try_files \$uri =404;[^}]*Cache-Control "no-store"/);
  assert.match(nginx, /location \/assets\/\s*\{[^}]*try_files \$uri =404;/);
});
