/** Real chat SFCs + built CSS + native browser events. Synthetic data, no listening server. */
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
const fixtureFile = { file_id: 'synthetic-chat-input', filename: 'synthetic-table.csv', size: 42, upload_date: '' };
const fixtureSkill = { id: 'synthetic-chat-skill', name: 'Synthetic table skill', description: 'Synthetic layout fixture', triggers: [], scope: 'user', installed: true, source: 'personal' };
const messages = [
  { type: 'user', content: { content: 'Compare the synthetic table', timestamp: 1 } },
  { type: 'task-summary', content: { duration_ms: 1234, has_steps: true, timestamp: 2 } },
  { type: 'attachments', content: { role: 'user', attachments: [fixtureFile], timestamp: 3 } },
  { type: 'step', content: { id: 'synthetic-step', description: 'Read the table', status: 'completed', tools: [], timestamp: 4 } },
  { type: 'assistant', content: { content: 'The synthetic result', timestamp: 5 } },
];

// Dialogs are outside this test's scope. The message stub exposes the real
// conversation's event bindings and fold/resume props without mounting editors.
const stubs = new Map([
  ['SkillDialog.vue', 'export default { render: () => null };'],
  ['MCPDialog.vue', 'export default { render: () => null };'],
  ['ChatMessage.vue', `import {defineComponent,h} from 'vue';
export default defineComponent({
  props:['message','allowAnalysisResume','taskSummaryExpanded','showAssistantActions'],
  emits:['resume-analysis','task-summary-toggle','tool-click'],
  setup(props,{emit}) { return () => {
    const message=props.message;
    return h('article',{'data-type':message.type,'data-role':message.content.role,
      'data-source-timestamp':message.content.timestamp},[
      h('span',message.type==='attachments'?message.content.attachments.map(file=>file.filename).join(', '):message.content.content||message.type),
      message.type==='task-summary'?h('button',{'data-testid':'summary-toggle','aria-expanded':props.taskSummaryExpanded,
        onClick:()=>emit('task-summary-toggle')},'Toggle thought summary'):null,
      message.type==='step'?h('button',{'data-testid':'tool-click',onClick:()=>emit('tool-click',{tool_call_id:'synthetic-tool'})},'Open tool'):null,
      message.type==='assistant'&&props.allowAnalysisResume?h('button',{'data-testid':'resume-analysis',onClick:()=>emit('resume-analysis')},'Resume analysis'):null,
    ]);
  }; }
});`],
]);
const entry = `import {createApp,h,reactive,nextTick} from 'vue';
import {createI18n} from 'vue-i18n';
import ChatBox from ${JSON.stringify(resolve(frontend, 'src/components/ChatBox.vue'))};
import AnalysisConversation from ${JSON.stringify(resolve(frontend, 'src/components/AnalysisConversation.vue'))};
import {ConversationViewport} from ${JSON.stringify(resolve(frontend, 'src/utils/conversationViewport.ts'))};
import en from ${JSON.stringify(resolve(frontend, 'src/locales/en.ts'))};
const messages=${JSON.stringify(messages)};
const state=reactive({modelValue:'',rows:1,isRunning:false,attachments:[],selectedSkills:[],selectedMcpServers:[],
  disabled:false,showFileActions:true,showMcpActions:true,submitOnEnter:true,compactComposer:true,placeholder:'输入分析问题…'});
const events={submissions:[],stops:0,resumes:[],tools:[],resumeChecks:[]};
const app=createApp({render:()=>h('main',[
  h('section',{id:'composer'},[h(ChatBox,{...state,
    'onUpdate:modelValue':value=>state.modelValue=value,
    'onUpdate:selectedSkills':value=>state.selectedSkills=value,
    'onUpdate:selectedMcpServers':value=>state.selectedMcpServers=value,
    onSubmit:()=>events.submissions.push(state.modelValue),onStop:()=>events.stops++})]),
  h('section',{id:'conversation'},[h(AnalysisConversation,{messages,sessionId:'synthetic-chat-session',isLoading:false,
    messageKey:message=>message.content.timestamp,
    canResumeAnalysis:index=>{events.resumeChecks.push(index);return index===4;},
    onResumeAnalysis:index=>events.resumes.push(index),onToolClick:tool=>events.tools.push(tool)})]),
])});
app.use(createI18n({legacy:false,locale:'en',messages:{en},missingWarn:false,fallbackWarn:false}));
app.mount('#app');
window.harness={state,events,messages,set:async patch=>{Object.assign(state,patch);await nextTick();},unmount:()=>app.unmount()};
window.ConversationViewport=ConversationViewport;
window.harnessReady=true;`;
const bundle = await build({
  stdin: { contents: entry, sourcefile: 'chat-layout-entry.js', resolveDir: frontend },
  bundle: true, write: false, format: 'esm', platform: 'browser', target: 'es2022', logLevel: 'warning',
  define: { 'import.meta.env': JSON.stringify({ BASE_URL: '/', MODE: 'test', DEV: false, PROD: true }),
    '__VUE_OPTIONS_API__': 'true', '__VUE_PROD_DEVTOOLS__': 'false', '__VUE_PROD_HYDRATION_MISMATCH_DETAILS__': 'false', 'process.env.NODE_ENV': '"production"' },
  plugins: [{ name: 'real-chat-sfcs', setup(builder) {
    builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      const stub = stubs.get(path.split('/').pop());
      if (stub) return { contents: stub, loader: 'js', resolveDir: frontend };
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
assert.equal(bundle.outputFiles.length, 1, 'The isolated chat harness must be one in-memory module');
const projectCssName = (await readdir(resolve(frontend, 'dist/assets'))).find(name => /^index-.*\.css$/.test(name));
assert.ok(projectCssName, 'Run npm run build before this check so the real utility CSS is current');
const projectCss = await readFile(resolve(frontend, 'dist/assets', projectCssName));
const output = await mkdtemp(join(tmpdir(), 'dataseek-chat-layout-'));
const report = { name: 'chat-layout', passed: false, errors: [], consoleErrors: [], externalAttempts: [], unexpectedRequests: [], mockedRequests: [], checks: [] };
const systemChrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const browser = await chromium.launch({
  executablePath: process.env.VISUALIZATION_BROWSER_EXECUTABLE || (existsSync(systemChrome) ? systemChrome : undefined),
  headless: true, args: ['--disable-background-networking', '--disable-component-update'],
});
const context = await browser.newContext({ viewport: { width: 1200, height: 900 }, serviceWorkers: 'block' });
const page = await context.newPage();
page.setDefaultTimeout(10000);
page.setDefaultNavigationTimeout(30000);
page.on('pageerror', error => report.errors.push(error.message));
page.on('console', message => { if (message.type() === 'error') report.consoleErrors.push(message.text()); });
await context.route('**/*', async route => {
  const request = route.request(), url = new URL(request.url());
  if (url.origin !== 'http://localhost:7001') { report.externalAttempts.push(url.href); return route.abort(); }
  if (url.pathname === '/__chat_layout__/') return route.fulfill({ contentType: 'text/html', body: '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/__chat_layout__/project.css"><style>html,body{margin:0;min-height:100%;font-family:Arial,sans-serif}#app{width:100%;padding:16px 12px}#conversation{margin-top:32px}*{box-sizing:border-box}</style></head><body><div id="app"></div><script type="module" src="/__chat_layout__/entry.js"></script></body></html>' });
  if (url.pathname === '/__chat_layout__/entry.js') return route.fulfill({ contentType: 'application/javascript', body: Buffer.from(bundle.outputFiles[0].contents) });
  if (url.pathname === '/__chat_layout__/project.css') return route.fulfill({ contentType: 'text/css', body: projectCss });
  if (request.method() === 'GET' && ['/api/v1/skills/preferences', '/api/v1/skills'].includes(url.pathname)) {
    report.mockedRequests.push(url.pathname);
    return route.fulfill({ json: { code: 0, msg: '', data: url.pathname.endsWith('/preferences') ? { auto_enabled_skills: [] } : { skills: [fixtureSkill] } } });
  }
  report.unexpectedRequests.push(`${request.method()} ${url.pathname}`);
  return route.abort();
});

const textarea = page.locator('#composer textarea');
const setState = patch => page.evaluate(patch => window.harness.set(patch), patch);
const readEvents = () => page.evaluate(() => JSON.parse(JSON.stringify(window.harness.events)));
async function checkCompactGeometry(label) {
  const geometry = await page.locator('#composer').evaluate(composer => {
    const entry = composer.querySelector('.chat-box-entry');
    const editor = composer.querySelector('.chat-box-editor');
    const textarea = editor.querySelector('textarea');
    const start = composer.querySelector('.chat-box-actions-start');
    const end = composer.querySelector('.chat-box-actions-end');
    const rect = element => {
      const bounds = element.getBoundingClientRect();
      return { x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height, right: bounds.right, bottom: bounds.bottom };
    };
    const style = getComputedStyle(textarea);
    return { entry: rect(entry), editor: rect(editor), textarea: rect(textarea), start: rect(start), end: rect(end),
      entryDisplay: getComputedStyle(entry).display, footerDisplay: getComputedStyle(composer.querySelector('footer')).display,
      contentLeft: textarea.getBoundingClientRect().left + parseFloat(style.paddingLeft),
      contentRight: textarea.getBoundingClientRect().right - parseFloat(style.paddingRight),
      pageWidth: document.documentElement.scrollWidth, viewportWidth: innerWidth,
      placeholder: textarea.placeholder, placeholderOpacity: getComputedStyle(textarea, '::placeholder').opacity,
      scrollWidth: textarea.scrollWidth, clientWidth: textarea.clientWidth };
  });
  assert.equal(geometry.entryDisplay, 'grid', `${label}: compact controls must participate in grid layout`);
  assert.equal(geometry.footerDisplay, 'contents', `${label}: footer must let both button groups size the grid`);
  assert.ok(geometry.start.right + 4 <= geometry.textarea.x, `${label}: add/MCP controls overlap the input`);
  assert.ok(geometry.textarea.right + 4 <= geometry.end.x, `${label}: send/stop control overlaps the input`);
  assert.ok(geometry.contentRight - geometry.contentLeft >= 100, `${label}: placeholder/text region is too narrow`);
  assert.ok(geometry.end.right <= geometry.entry.right + 1, `${label}: right control exceeds composer`);
  assert.ok(geometry.start.x >= geometry.entry.x - 1, `${label}: left controls exceed composer`);
  assert.ok(Math.abs(geometry.start.y + geometry.start.height / 2 - geometry.textarea.y - geometry.textarea.height / 2) <= 1, `${label}: left controls are not centered beside input`);
  assert.ok(Math.abs(geometry.end.y + geometry.end.height / 2 - geometry.textarea.y - geometry.textarea.height / 2) <= 1, `${label}: right control is not centered beside input`);
  assert.ok(geometry.pageWidth <= geometry.viewportWidth + 1, `${label}: page overflows horizontally`);
  assert.ok(geometry.scrollWidth <= geometry.clientWidth + 1, `${label}: input content overflows horizontally`);
  return geometry;
}

try {
  await page.goto('http://localhost:7001/__chat_layout__/', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.harnessReady);
  await textarea.waitFor();
  for (const width of [320, 390, 768, 1200]) {
    await page.setViewportSize({ width, height: 900 });
    for (const showMcpActions of [false, true]) {
      const label = `${width}px MCP ${showMcpActions ? 'on' : 'off'}`;
      await setState({ modelValue: '', disabled: false, isRunning: false, showMcpActions, attachments: [], selectedSkills: [] });
      assert.equal(await page.getByTitle('MCP Tools', { exact: true }).count(), Number(showMcpActions));
      assert.equal(await page.getByRole('button', { name: 'Send message', exact: true }).isDisabled(), true);
      const placeholder = await checkCompactGeometry(`${label} placeholder`);
      assert.equal(placeholder.placeholder, '输入分析问题…');
      assert.equal(Number(placeholder.placeholderOpacity), 1);
      await textarea.fill('请比较合成数据。 ' + 'A-long-analysis-question '.repeat(15));
      const filled = await checkCompactGeometry(`${label} filled`);
      assert.equal(filled.textarea.height, placeholder.textarea.height, `${label}: typing must retain compact height`);
      const beforeSend = (await readEvents()).submissions.length;
      await page.getByRole('button', { name: 'Send message', exact: true }).click();
      assert.equal((await readEvents()).submissions.length, beforeSend + 1);

      await setState({ modelValue: '', isRunning: true, disabled: true });
      assert.equal(await textarea.isDisabled(), true);
      assert.equal(await page.getByTitle('Add content', { exact: true }).isDisabled(), true);
      if (showMcpActions) assert.equal(await page.getByTitle('MCP Tools', { exact: true }).isDisabled(), true);
      const stop = page.getByRole('button', { name: 'Stop task', exact: true });
      assert.equal(await stop.isEnabled(), true, `${label}: busy composer must retain an enabled stop button`);
      const beforeStop = (await readEvents()).stops;
      await stop.click();
      assert.equal((await readEvents()).stops, beforeStop + 1, `${label}: stop must receive a real pointer click`);
      await checkCompactGeometry(`${label} disabled running`);
      await setState({ isRunning: false });
      assert.equal(await page.getByRole('button', { name: 'Send message', exact: true }).isDisabled(), true);
      report.checks.push({ label, placeholder, filled, clickableStopWhileDisabled: true });
    }
    await setState({ disabled: false, modelValue: '', attachments: [fixtureFile], selectedSkills: [fixtureSkill.name] });
    const attachment = await page.locator('#composer').getByText(fixtureFile.filename, { exact: true }).boundingBox();
    const skill = await page.getByRole('button', { name: `Remove skill ${fixtureSkill.name}`, exact: true }).boundingBox();
    const input = await textarea.boundingBox();
    assert.ok(attachment.y + attachment.height <= input.y, `${width}px: attachment must appear above text`);
    assert.ok(skill.y + skill.height <= input.y, `${width}px: skill chip must appear above text`);
    await checkCompactGeometry(`${width}px with attachment and skill`);
    await page.screenshot({ path: join(output, `composer-${width}.png`), fullPage: true });
  }

  await page.setViewportSize({ width: 390, height: 900 });
  await setState({ attachments: [], selectedSkills: [], modelValue: '', disabled: false, isRunning: false });
  const beforeKeyboard = (await readEvents()).submissions.length;
  await textarea.fill('First line');
  await textarea.press('Shift+Enter');
  await textarea.pressSequentially('Second line');
  assert.equal(await textarea.inputValue(), 'First line\nSecond line');
  assert.equal((await readEvents()).submissions.length, beforeKeyboard);
  await textarea.press('Enter');
  assert.deepEqual((await readEvents()).submissions.slice(beforeKeyboard), ['First line\nSecond line']);
  assert.equal(await textarea.inputValue(), 'First line\nSecond line', 'Submit-on-Enter must not append another newline');
  await textarea.fill('');
  await textarea.press('Enter');
  assert.equal((await readEvents()).submissions.length, beforeKeyboard + 1, 'Blank Enter must not submit');
  report.checks.push({ keyboard: { shiftEnterAddsNewline: true, enterSubmits: true, blankEnterIgnored: true } });

  for (const width of [320, 1200]) {
    await page.setViewportSize({ width, height: 900 });
    await setState({ compactComposer: false, submitOnEnter: false, rows: 3, modelValue: '' });
    const editor = await page.locator('.chat-box-editor').boundingBox();
    const footer = await page.locator('.chat-box-footer').boundingBox();
    assert.ok(editor.y + editor.height <= footer.y, `${width}px: standard composer retains its separate toolbar row`);
    const count = (await readEvents()).submissions.length;
    await textarea.fill('Standard composer');
    await textarea.press('Enter');
    assert.equal(await textarea.inputValue(), 'Standard composer\n');
    assert.equal((await readEvents()).submissions.length, count);
    await textarea.press('Control+Enter');
    assert.equal((await readEvents()).submissions.length, count + 1);
    report.checks.push({ standardComposerWidth: width, plainEnterAddsNewline: true, controlEnterSubmits: true });
  }

  const visibleTypes = () => page.locator('#conversation article').evaluateAll(elements => elements.map(element => element.dataset.type));
  assert.deepEqual(await visibleTypes(), ['attachments', 'user', 'task-summary', 'assistant']);
  assert.deepEqual((await readEvents()).resumeChecks.slice(0, 4), [2, 0, 1, 4], 'Display ordering must preserve canResumeAnalysis source indices');
  const summary = page.getByTestId('summary-toggle');
  assert.equal(await summary.getAttribute('aria-expanded'), 'false');
  await summary.click();
  assert.equal(await summary.getAttribute('aria-expanded'), 'true');
  assert.deepEqual(await visibleTypes(), ['attachments', 'user', 'task-summary', 'step', 'assistant']);
  await page.getByTestId('tool-click').click();
  assert.deepEqual((await readEvents()).tools, [{ tool_call_id: 'synthetic-tool' }]);
  await page.getByTestId('resume-analysis').click();
  assert.deepEqual((await readEvents()).resumes, [4], 'Resume must use original assistant index, not its display position');
  await summary.click();
  assert.deepEqual(await visibleTypes(), ['attachments', 'user', 'task-summary', 'assistant']);
  assert.deepEqual(await page.evaluate(() => window.harness.messages), messages, 'Presentation and clicks must not mutate source history');
  report.checks.push({ conversation: { collapsedOrder: await visibleTypes(), expandedOrder: ['attachments', 'user', 'task-summary', 'step', 'assistant'], resumeSourceIndex: 4, sourceHistoryUnchanged: true } });
  const nativeAnchor = await page.evaluate(async () => {
    const frames = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const viewport = document.createElement('section');
    viewport.style.cssText = 'height:240px;max-width:700px;overflow:auto;overflow-anchor:none;border:1px solid #999;margin:24px auto';
    const content = document.createElement('div'); content.className = 'analysis-conversation';
    for (let index = 0; index < 30; index++) {
      const row = document.createElement('div'); row.dataset.messageKey = `fixture-${index}`;
      row.style.cssText = 'height:80px;padding:18px;border-bottom:1px solid #ccc;box-sizing:border-box';
      row.textContent = `Synthetic history row ${index}`; content.append(row);
    }
    viewport.append(content); document.body.append(viewport);
    const controller = new window.ConversationViewport(() => false);
    viewport.scrollTop = 430;
    controller.capture(viewport);
    const row = content.children[5];
    const offset = () => row.getBoundingClientRect().top - viewport.getBoundingClientRect().top;
    const before = offset();
    const older = document.createElement('div'); older.style.height = '200px'; older.textContent = 'Older history';
    content.prepend(older); controller.layout(); await frames();
    const afterPrepend = offset();
    const image = document.createElement('img'); image.style.cssText = 'display:block;width:100%;height:auto';
    older.style.height = 'auto';
    const loaded = new Promise(resolve => { image.onload = resolve; image.onerror = resolve; });
    image.src = 'data:image/svg+xml,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="600" height="300"><rect width="600" height="300" fill="#dff3e8"/><text x="30" y="150" font-size="24">Delayed synthetic image</text></svg>');
    older.append(image); await loaded; await frames(); await frames();
    const afterImage = offset();
    const top = viewport.scrollTop;
    // Native event dispatch releases the anchor; no artificial observer call.
    viewport.dispatchEvent(new WheelEvent('wheel', { deltaY: -20, bubbles: true }));
    const extra = document.createElement('div'); extra.style.height = '75px'; content.prepend(extra);
    await frames(); await frames();
    const afterIntent = viewport.scrollTop;
    controller.dispose();
    content.prepend(Object.assign(document.createElement('div'), { style: 'height:90px' }));
    await frames();
    const afterDispose = viewport.scrollTop;
    return { before, afterPrepend, afterImage, top, afterIntent, afterDispose };
  });
  assert.ok(Math.abs(nativeAnchor.afterPrepend - nativeAnchor.before) <= 1, 'Native prepend must retain the same reading row');
  assert.ok(Math.abs(nativeAnchor.afterImage - nativeAnchor.before) <= 1, 'A native ResizeObserver must retain the reading row after image decode');
  assert.equal(nativeAnchor.afterIntent, nativeAnchor.top, 'Reader intent must release compensation');
  assert.equal(nativeAnchor.afterDispose, nativeAnchor.afterIntent, 'Disposal must stop layout writes');
  report.checks.push({ nativeAnchor });
  await page.screenshot({ path: join(output, 'history-anchor.png'), fullPage: true });
  assert.deepEqual(report.errors, []); assert.deepEqual(report.consoleErrors, []);
  assert.deepEqual(report.externalAttempts, []); assert.deepEqual(report.unexpectedRequests, []);
  await page.evaluate(() => window.harness.unmount());
  assert.equal(await page.locator('#app').evaluate(element => element.childElementCount), 0);
  report.passed = true;
} catch (error) {
  report.failure = error.message; report.stack = error.stack;
  await page.screenshot({ path: join(output, 'failure.png'), fullPage: true }).catch(() => {});
  process.exitCode = 1;
} finally {
  await context.close(); await browser.close();
  await writeFile(join(output, 'results.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
  console.log(`BROWSER_REPORT=${output}`);
}
