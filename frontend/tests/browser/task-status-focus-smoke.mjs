/** Real PlanPanel/SimpleBar SFCs, built utility CSS and native browser input.
 * Synthetic local fixtures only: no listening server, model call or real API.
 * Run after npm run build: node tests/browser/task-status-focus-smoke.mjs
 */
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { compileScript, compileStyle, compileTemplate, parse } from '@vue/compiler-sfc';
import { createHash } from 'node:crypto';
import { existsSync } from 'node:fs';
import { mkdtemp, readFile, readdir, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { homedir, tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const require = createRequire(resolve(frontend, 'package.json'));
let playwrightEntry = process.env.VISUALIZATION_PLAYWRIGHT_MODULE;
if (!playwrightEntry) {
  try { playwrightEntry = require.resolve('playwright'); }
  catch { playwrightEntry = join(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs'); }
}
const { chromium } = await import(pathToFileURL(resolve(playwrightEntry)).href);
const outcome = (status, reason_code, missing = []) => ({ status, reason_code, missing, can_resume: false });
const step = (id, status, description = `合成步骤 ${id}`) => ({ id, status, description, timestamp: 1 });
const messages = (stepId, result) => [
  { type: 'user', content: { content: '合成分析请求', timestamp: 1 } },
  { type: 'assistant', content: { content: '合成结果，不参与状态推断', timestamp: 2, metadata: { step_id: stepId, analysis_outcome: result } } },
];
const cases = [
  { id: 'completed', steps: [step('done', 'completed')], expected: 'completed', title: '任务已完成', completed: 1, icon: 'lucide-check', stepStatuses: ['completed'] },
  { id: 'failed', steps: [step('failed', 'failed')], expected: 'failed', title: '任务失败', completed: 0, icon: 'lucide-circle-x', stepStatuses: ['failed'] },
  { id: 'mixed', steps: [step('done', 'completed'), step('failed', 'failed')], expected: 'partial', title: '任务部分完成', completed: 1, icon: 'lucide-circle-alert', stepStatuses: ['completed', 'failed'] },
  { id: 'partial-result', steps: [step('partial', 'failed')], messages: messages('partial', outcome('partial', 'artifacts_missing', [{ kind: 'image', min_count: 1, label: '图表' }])), expected: 'partial', title: '任务部分完成', completed: 0, icon: 'lucide-circle-alert', stepStatuses: ['partial'] },
  { id: 'stopped', steps: [step('stopped', 'failed')], messages: messages('stopped', outcome('failed', 'user_cancelled')), expected: 'stopped', title: '任务已停止', completed: 0, icon: 'lucide-circle-pause', stepStatuses: ['stopped'] },
  { id: 'running-after-failure', steps: [step('failed', 'failed'), step('running', 'running', '正在生成合成图表'), step('next', 'pending')], expected: 'running', title: '正在生成合成图表', completed: 0, icon: 'lucide-clock', stepStatuses: ['failed', 'running', 'pending'] },
  { id: 'pending', steps: [step('next', 'pending', '等待合成数据分析')], expected: 'pending', title: '等待合成数据分析', completed: 0, icon: 'lucide-clock', stepStatuses: ['pending'] },
  { id: 'empty', steps: [], expected: 'pending', completed: 0, icon: 'lucide-clock', stepStatuses: [] },
];

const entry = `import {createApp,h,nextTick} from 'vue';
import {createI18n} from 'vue-i18n';
import PlanPanel from ${JSON.stringify(resolve(frontend, 'src/components/PlanPanel.vue'))};
import SimpleBar from ${JSON.stringify(resolve(frontend, 'src/components/SimpleBar.vue'))};
import zh from ${JSON.stringify(resolve(frontend, 'src/locales/zh.ts'))};
const cases=${JSON.stringify(cases)};
const app=createApp({render:()=>h('main',[
  h('section',{id:'focus-fixture'},[
    h('button',{id:'before-region',type:'button'},'滚动区域前的按钮'),
    h('div',{id:'scroll-fixture'},[h(SimpleBar,{},()=>h('div',{id:'scroll-body'},[
      h('p',{id:'pointer-target'},'点击这里检查滚动容器焦点，不显示整页蓝框。'),
      h('button',{id:'inner-button',type:'button'},'区域内键盘按钮'),
      h('div',{id:'scroll-filler'},Array.from({length:35},(_,index)=>h('p','合成滚动内容 '+index))),
    ]))]),
    h('button',{id:'after-region',type:'button'},'滚动区域后的按钮'),
  ]),
  h('section',{id:'status-fixtures'},cases.map(item=>h('section',{id:'case-'+item.id,class:'status-fixture'},[
    h('h2',item.id),h(PlanPanel,{plan:{steps:item.steps,timestamp:1},messages:item.messages||[]}),
  ]))),
])});
app.use(createI18n({legacy:false,locale:'zh',messages:{zh},missingWarn:false,fallbackWarn:false}));
app.mount('#app');
window.harness={cases,flush:()=>nextTick(),unmount:()=>app.unmount()};
window.harnessReady=true;`;

const bundle = await build({
  stdin: { contents: entry, sourcefile: 'task-status-focus-entry.js', resolveDir: frontend },
  bundle: true, write: false, format: 'esm', platform: 'browser', target: 'es2022', logLevel: 'warning',
  define: {
    'import.meta.env': JSON.stringify({ BASE_URL: '/', MODE: 'test', DEV: false, PROD: true }),
    '__VUE_OPTIONS_API__': 'true', '__VUE_PROD_DEVTOOLS__': 'false',
    '__VUE_PROD_HYDRATION_MISMATCH_DETAILS__': 'false', 'process.env.NODE_ENV': '"production"',
  },
  plugins: [{ name: 'real-task-status-focus-sfcs', setup(builder) {
    builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      const source = await readFile(path, 'utf8');
      const id = createHash('sha256').update(path).digest('hex').slice(0, 12);
      const { descriptor, errors } = parse(source, { filename: path });
      if (errors.length) throw errors[0];
      let content;
      if (descriptor.script || descriptor.scriptSetup) {
        content = compileScript(descriptor, { id, inlineTemplate: true, genDefaultAs: '__component' }).content;
      } else {
        const template = compileTemplate({ source: descriptor.template.content, filename: path, id });
        if (template.errors.length) throw template.errors[0];
        content = `${template.code}\nconst __component={render};`;
      }
      const styles = descriptor.styles.map(style => {
        const compiled = compileStyle({ filename: path, source: style.content, scoped: style.scoped, id: `data-v-${id}` });
        if (compiled.errors.length) throw compiled.errors[0];
        return compiled.code;
      }).join('\n');
      return { contents: `${content}\n__component.__scopeId='data-v-${id}';document.head.appendChild(Object.assign(document.createElement('style'),{textContent:${JSON.stringify(styles)}}));export default __component;`, loader: 'ts', resolveDir: dirname(path) };
    });
  } }],
});
assert.equal(bundle.outputFiles.length, 1, 'The fixture must stay a single in-memory module');
const cssName = (await readdir(resolve(frontend, 'dist/assets'))).find(name => /^index-.*\.css$/.test(name));
assert.ok(cssName, 'Run npm run build before this smoke test');
const projectCss = await readFile(resolve(frontend, 'dist/assets', cssName));
const output = await mkdtemp(join(tmpdir(), 'dataseek-task-status-focus-'));
const report = {
  name: 'task-status-focus', passed: false, cssName,
  cssSha256: createHash('sha256').update(projectCss).digest('hex'),
  errors: [], consoleErrors: [], externalAttempts: [], unexpectedRequests: [], checks: [],
};
const systemChrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const browser = await chromium.launch({
  executablePath: process.env.VISUALIZATION_BROWSER_EXECUTABLE || (existsSync(systemChrome) ? systemChrome : undefined),
  headless: true, args: ['--disable-background-networking', '--disable-component-update'],
});
const context = await browser.newContext({ viewport: { width: 1200, height: 900 }, serviceWorkers: 'block' });
const page = await context.newPage();
page.setDefaultTimeout(10000);
page.on('pageerror', error => report.errors.push(error.message));
page.on('console', message => { if (message.type() === 'error') report.consoleErrors.push(message.text()); });
await context.route('**/*', async route => {
  const request = route.request(), url = new URL(request.url());
  if (url.origin !== 'http://localhost:7001') { report.externalAttempts.push(url.href); return route.abort(); }
  if (url.pathname === '/__task_status_focus__/') return route.fulfill({ contentType: 'text/html', body: `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/__task_status_focus__/project.css"><style>
html,body{margin:0;min-height:100%;font-family:Arial,sans-serif}*{box-sizing:border-box}
#app{width:100%;padding:16px 12px}main{max-width:1100px;margin:auto;min-width:0}
#focus-fixture{margin-bottom:24px}#focus-fixture button{padding:6px 8px;border:1px solid #888;border-radius:4px;background:white}
#scroll-fixture{height:260px;width:100%;margin:10px 0;border:1px solid #ddd}
#scroll-body{width:100%;min-width:0;padding:16px}#pointer-target{margin:0 0 12px;min-height:30px}
#scroll-filler p{padding:18px 0;margin:0}.status-fixture{margin:16px 0;min-width:0}.status-fixture h2{margin:0 0 4px;font-size:12px;color:#777}
</style></head><body><div id="app"></div><script type="module" src="/__task_status_focus__/entry.js"></script></body></html>` });
  if (url.pathname === '/__task_status_focus__/entry.js') return route.fulfill({ contentType: 'application/javascript', body: Buffer.from(bundle.outputFiles[0].contents) });
  if (url.pathname === '/__task_status_focus__/project.css') return route.fulfill({ contentType: 'text/css', body: projectCss });
  report.unexpectedRequests.push(`${request.method()} ${url.pathname}`);
  return route.abort();
});

const region = page.getByRole('region', { name: 'scrollable content', exact: true });
const readFocus = locator => locator.evaluate(element => {
  const style = getComputedStyle(element);
  return { active: document.activeElement === element, visible: element.matches(':focus-visible'),
    outlineStyle: style.outlineStyle, outlineWidth: style.outlineWidth, outlineColor: style.outlineColor,
    boxShadow: style.boxShadow, scrollTop: element.scrollTop, scrollHeight: element.scrollHeight, clientHeight: element.clientHeight };
});

try {
  await page.goto('http://localhost:7001/__task_status_focus__/', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.harnessReady);
  for (const width of [320, 390, 768, 1200]) {
    await page.setViewportSize({ width, height: 900 });
    await region.evaluate(element => { element.scrollTop = 0; });
    await page.getByRole('button', { name: '滚动区域前的按钮', exact: true }).click();
    await page.locator('#pointer-target').click();
    const pointer = await readFocus(region);
    assert.equal(pointer.active, true, `${width}px: pointer focus must actually reach the scroll region`);
    assert.equal(pointer.outlineStyle, 'none', `${width}px: pointer click must not show browser-default blue outline`);
    assert.equal(pointer.outlineWidth, '0px', `${width}px: the outer focus frame must have no width`);
    assert.ok(pointer.scrollHeight > pointer.clientHeight, `${width}px: fixture must genuinely overflow vertically`);

    await page.getByRole('button', { name: '滚动区域前的按钮', exact: true }).click();
    await page.keyboard.press('Tab');
    const keyboard = await readFocus(region);
    assert.equal(keyboard.active, true, `${width}px: Tab must reach the scroll region`);
    assert.equal(keyboard.visible, true, `${width}px: native keyboard focus must remain distinguishable`);
    assert.equal(keyboard.outlineStyle, 'none', `${width}px: keyboard focus must not restore the outer blue frame`);
    assert.ok(keyboard.boxShadow.includes('inset'), `${width}px: keyboard focus needs its scoped inset marker`);
    assert.ok(keyboard.boxShadow.includes('1px'), `${width}px: the inset marker should be a quiet 1px line`);
    await page.keyboard.press('PageDown');
    await page.waitForFunction(() => document.querySelector('.simplebar-content-wrapper').scrollTop > 0);
    const paged = await readFocus(region);
    assert.equal(paged.active, true, `${width}px: PageDown must retain scroll-region focus`);
    assert.ok(paged.scrollTop > keyboard.scrollTop, `${width}px: PageDown must scroll actual contents`);
    await page.keyboard.press('Tab');
    const child = await readFocus(page.locator('#inner-button'));
    assert.equal(child.active, true, `${width}px: child buttons must remain in the keyboard tab order`);
    assert.equal(child.visible, true, `${width}px: child buttons must retain native keyboard focus visibility`);
    assert.ok((child.outlineStyle !== 'none' && parseFloat(child.outlineWidth) > 0) || child.boxShadow !== 'none', `${width}px: removing the container frame must not remove child-button focus indicators`);
    const geometry = await page.evaluate(() => ({ viewportWidth: innerWidth, pageWidth: document.documentElement.scrollWidth,
      regionWidth: document.querySelector('.simplebar-content-wrapper').clientWidth,
      regionScrollWidth: document.querySelector('.simplebar-content-wrapper').scrollWidth }));
    assert.ok(geometry.pageWidth <= geometry.viewportWidth + 1, `${width}px: no horizontal viewport overflow`);
    assert.ok(geometry.regionScrollWidth <= geometry.regionWidth + 1, `${width}px: scroll region must not overflow horizontally`);
    report.checks.push({ width, pointer, keyboard, pageDownScrollTop: paged.scrollTop, child, geometry });
  }

  await page.setViewportSize({ width: 1200, height: 900 });
  for (const item of cases) {
    const fixture = page.locator(`#case-${item.id}`);
    const summary = fixture.locator('[data-plan-status]');
    assert.equal(await summary.getAttribute('data-plan-status'), item.expected, `${item.id}: summary status`);
    const text = (await summary.innerText()).replace(/\s+/g, ' ').trim();
    if (item.title) assert.ok(text.includes(item.title), `${item.id}: expected ${item.title}, received ${text}`);
    if (item.expected !== 'completed') assert.ok(!text.includes('任务已完成'), `${item.id}: a non-success must not claim task completion`);
    assert.ok(text.includes(`已完成 ${item.completed} / ${item.steps.length}`), `${item.id}: only succeeded steps count as completed; received ${text}`);
    assert.equal(await summary.locator(`svg.${item.icon}`).count(), 1, `${item.id}: icon and status must agree`);
    if (item.expected === 'completed') assert.equal(await summary.locator('svg.lucide-circle-x').count(), 0, `${item.id}: completed cannot retain a failure icon`);
    if (item.expected === 'running') assert.equal(await summary.locator('svg.lucide-circle-x').count(), 0, `${item.id}: earlier failure cannot replace the active icon`);
    await summary.click();
    const renderedSteps = await fixture.locator('[data-plan-step-status]').evaluateAll(elements => elements.map(element => element.dataset.planStepStatus));
    assert.deepEqual(renderedSteps, item.stepStatuses, `${item.id}: expanded rows must use the same status semantics`);
    report.checks.push({ id: item.id, summary: text, status: item.expected, icon: item.icon, expandedSteps: renderedSteps });
  }

  await page.screenshot({ path: join(output, 'statuses-expanded-1200.png'), fullPage: true });
  await page.setViewportSize({ width: 320, height: 900 });
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Expanded task rows must not overflow a narrow viewport');
  await page.screenshot({ path: join(output, 'statuses-expanded-320.png'), fullPage: true });
  assert.deepEqual(await page.evaluate(() => window.harness.cases), cases, 'Rendering and input must not mutate synthetic source history');
  assert.deepEqual(report.errors, []);
  assert.deepEqual(report.consoleErrors, []);
  assert.deepEqual(report.externalAttempts, []);
  assert.deepEqual(report.unexpectedRequests, []);
  await page.evaluate(() => window.harness.unmount());
  assert.equal(await page.locator('#app').evaluate(element => element.childElementCount), 0);
  report.passed = true;
} catch (error) {
  report.failure = error.message;
  report.stack = error.stack;
  await page.screenshot({ path: join(output, 'failure.png'), fullPage: true }).catch(() => {});
  process.exitCode = 1;
} finally {
  await context.close();
  await browser.close();
  await writeFile(join(output, 'results.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
  console.log(`BROWSER_REPORT=${output}`);
}
