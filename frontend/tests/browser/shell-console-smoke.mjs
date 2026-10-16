/** Actual ShellToolView + native DOM. Synthetic snapshots; no listening server or backend. */
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { compileScript, compileStyle, parse } from '@vue/compiler-sfc';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, readdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { browserEngine, launchBrowser } from './runtime.mjs';

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const entry = `import {createApp,h,ref,nextTick} from 'vue';
import ShellToolView from ${JSON.stringify(resolve(frontend, 'src/components/toolViews/ShellToolView.vue'))};
const tool=ref({tool_call_id:'initial',function:'shell_run',name:'shell',status:'called',args:{id:'synthetic-shell',command:'exit 0'},content:{console:[]}});
let revision=0;
const app=createApp({render:()=>h('main',{id:'console'},[h(ShellToolView,{sessionId:'synthetic-session',live:false,toolContent:tool.value})])});
app.mount('#app');
window.harness={choose:async (console,extra={})=>{tool.value={...tool.value,spill:undefined,function:'shell_run',status:'called',...extra,
  tool_call_id:'call-'+(++revision),timestamp:revision,content:{console}};await nextTick();await nextTick();},unmount:()=>app.unmount()};
window.harnessReady=true;`;
const bundle = await build({
  stdin: { contents: entry, sourcefile: 'shell-console-entry.js', resolveDir: frontend },
  bundle: true, write: false, format: 'esm', platform: 'browser', target: 'es2022', logLevel: 'warning',
  define: { 'import.meta.env': JSON.stringify({ BASE_URL: '/', MODE: 'test', DEV: false, PROD: true }),
    '__VUE_OPTIONS_API__': 'true', '__VUE_PROD_DEVTOOLS__': 'false', '__VUE_PROD_HYDRATION_MISMATCH_DETAILS__': 'false', 'process.env.NODE_ENV': '"production"' },
  plugins: [{ name: 'real-shell-view', setup(builder) {
    builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      const id = createHash('sha256').update(path).digest('hex').slice(0, 12);
      const { descriptor, errors } = parse(await readFile(path, 'utf8'), { filename: path });
      if (errors.length) throw errors[0];
      const content = compileScript(descriptor, { id, inlineTemplate: true, genDefaultAs: '__component' }).content;
      const styles = descriptor.styles.map(style => {
        const compiled = compileStyle({ filename: path, source: style.content, scoped: style.scoped, id: `data-v-${id}` });
        if (compiled.errors.length) throw compiled.errors[0];
        return compiled.code;
      }).join('\n');
      return { contents: `${content}\n__component.__scopeId='data-v-${id}';document.head.appendChild(Object.assign(document.createElement('style'),{textContent:${JSON.stringify(styles)}}));export default __component;`, loader: 'ts', resolveDir: dirname(path) };
    });
  } }],
});
assert.equal(bundle.outputFiles.length, 1);
const cssName = (await readdir(resolve(frontend, 'dist/assets'))).find(name => /^index-.*\.css$/.test(name));
assert.ok(cssName, 'Run npm run build first to use the current production styles');
const css = await readFile(resolve(frontend, 'dist/assets', cssName));
const output = await mkdtemp(join(tmpdir(), 'dataseek-shell-console-'));
const report = { name: 'shell-console', browserEngine, passed: false, checks: [], errors: [], consoleErrors: [], unexpectedRequests: [] };
const browser = await launchBrowser();
const context = await browser.newContext({ viewport: { width: 780, height: 560 }, serviceWorkers: 'block' });
const page = await context.newPage();
page.setDefaultTimeout(10000);
page.on('pageerror', error => report.errors.push(error.message));
page.on('console', message => { if (message.type() === 'error') report.consoleErrors.push(message.text()); });
await context.route('**/*', async route => {
  const url = new URL(route.request().url());
  if (url.origin === 'http://localhost:7001') {
    if (url.pathname === '/__shell_console__/') return route.fulfill({ contentType: 'text/html', body: '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/__shell_console__/project.css"><style>html,body{margin:0;font-family:Arial,sans-serif}#app{padding:24px}#console{display:flex;flex-direction:column;min-height:400px}</style></head><body><div id="app"></div><script type="module" src="/__shell_console__/entry.js"></script></body></html>' });
    if (url.pathname === '/__shell_console__/entry.js') return route.fulfill({ contentType: 'application/javascript', body: Buffer.from(bundle.outputFiles[0].contents) });
    if (url.pathname === '/__shell_console__/project.css') return route.fulfill({ contentType: 'text/css', body: css });
  }
  report.unexpectedRequests.push(`${route.request().method()} ${url.href}`);
  return route.abort();
});
const choose = (console, extra = {}) => page.evaluate(({ console, extra }) => window.harness.choose(console, extra), { console, extra });
const text = () => page.locator('#console code').innerText();
const record = (returncode, extra = {}) => ({ ps1: '$', command: 'synthetic-command', output: '', status: 'completed', returncode, ...extra });
try {
  await page.goto('http://localhost:7001/__shell_console__/', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.harnessReady);
  for (const [code, expected] of [[0, '退出码：0'], [7, '退出码：7'], [-9, '退出码：-9'], [null, '未取得退出码']]) {
    await choose([record(code)]);
    assert.ok((await text()).includes(expected), `empty output retains ${expected}`);
    assert.equal(await page.locator('#console code span').filter({ hasText: expected }).count(), 1);
    report.checks.push(`empty output: ${expected}`);
  }
  await choose([record(0), record(7), record(-9), record(null)]);
  const exitStyle = await page.locator('#console code span').filter({ hasText: '退出码：7' }).evaluate(node => ({ inline: node.getAttribute('style'), display: getComputedStyle(node).display }));
  assert.equal(exitStyle.inline, 'color: var(--text-tertiary);');
  assert.equal(exitStyle.display, 'inline');
  await page.screenshot({ path: join(output, 'shell-exit-states.png'), fullPage: true });
  report.checks.push('multiple records retain existing muted inline console presentation');

  for (const console of [[{ command: 'legacy', output: 'old output' }], [record(null, { status: 'running' })]]) {
    await choose(console);
    assert.doesNotMatch(await text(), /退出码/);
  }
  report.checks.push('legacy missing exit and still-running process do not invent results');
  for (const extra of [{ function: 'dataset_analysis_run' }, { function: 'dataset_quicklook' }, { spill: { status: 'stored' } }]) {
    await choose([record(1, { output: 'synthetic summary' })], extra);
    assert.match(await text(), /synthetic summary/);
    assert.doesNotMatch(await text(), /退出码/);
  }
  report.checks.push('legacy virtual dataset/spill summaries suppress fabricated 0/1 exits');

  await choose([record(7, { ps1: '<script>', command: 'API_KEY=synthetic-secret', output: '<img src=x> /Users/private/synthetic-data' })]);
  assert.match(await text(), /<script>/);
  assert.match(await text(), /<img src=x>/);
  assert.doesNotMatch(await text(), /synthetic-secret|\/Users\/private/);
  assert.equal(await page.locator('#console script, #console img').count(), 0);
  report.checks.push('native DOM preserves literal HTML and masks synthetic secret/host path');

  await choose({ output: 'single record', status: 'completed', returncode: 9 });
  assert.match(await text(), /single record[\s\S]*退出码：9/);
  report.checks.push('single structured record retains exit code');
  await choose([], { execution_status: 'failed' });
  assert.match(await text(), /未提供命令输出或退出信息/);
  assert.doesNotMatch(await text(), /Command completed|退出码：0/);
  report.checks.push('absent console has neutral fallback, not a success claim');
  await page.evaluate(() => window.harness.unmount());
  assert.equal(await page.locator('#console').count(), 0);
  assert.deepEqual(report.errors, []);
  assert.deepEqual(report.consoleErrors, []);
  assert.deepEqual(report.unexpectedRequests, [], 'snapshots must not fetch live shell data or external resources');
  report.checks.push('unmount is clean and every snapshot remains network-free');
  report.passed = true;
} catch (error) {
  report.failure = error.stack || String(error);
  throw error;
} finally {
  await writeFile(join(output, 'report.json'), JSON.stringify(report, null, 2));
  await browser.close();
  console.log(`BROWSER_REPORT=${join(output, 'report.json')}`);
}
