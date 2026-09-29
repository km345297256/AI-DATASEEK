/** Actual Vue session controller -> production API -> fetch-event-source ->
 * native browser Fetch. Only HTTP responses are synthetic; no server or model.
 * This tests transport/state ownership, not the full page's visual rendering.
 */
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { browserEngine, launchBrowser } from './runtime.mjs';

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const entry = `import {createApp,h,ref,nextTick} from 'vue';
import {useAnalysisSession} from ${JSON.stringify(resolve(frontend, 'src/composables/useAnalysisSession.ts'))};
import * as api from ${JSON.stringify(resolve(frontend, 'src/api/agent.ts'))};
const draft=ref('synthetic first question');
const files=[{file_id:'synthetic-source',filename:'synthetic.csv'}];
const session=useAnalysisSession({api});session.sessionId.value='synthetic-recovery';
const send=async()=>{const text=draft.value;if(await session.send({message:text,files,skills:['synthetic-skill'],mcpServers:['synthetic-mcp'],agentProfileId:'synthetic-profile'}))if(draft.value===text)draft.value='';};
const app=createApp({render:()=>h('main',[
 h('textarea',{id:'draft',value:draft.value,onInput:event=>draft.value=event.target.value}),
 h('button',{id:'send',onClick:send,disabled:session.isLoading.value},'Send'),
 h('button',{id:'recover',onClick:()=>session.retryPendingInput(),disabled:!session.canRetryInput.value},'Recover'),
 h('p',{id:'notice'},session.connectionNotice.value),
 h('ol',session.messages.value.map(message=>h('li',{'data-type':message.type,'data-client-id':message.content.metadata?.client_message_id},message.content.content||JSON.stringify(message.content.attachments||[])))),
])});app.mount('#app');
window.harness={session,draft,restore:id=>session.restore(id),snapshot:()=>({id:session.sessionId.value,loading:session.isLoading.value,retry:session.canRetryInput.value,draft:draft.value,messages:JSON.parse(JSON.stringify(session.messages.value)),notice:session.connectionNotice.value}),unmount:()=>{session.dispose();app.unmount();},flush:()=>nextTick()};window.harnessReady=true;`;
const bundle = await build({ stdin: { contents: entry, sourcefile: 'session-recovery-entry.js', resolveDir: frontend },
  bundle: true, write: false, format: 'esm', platform: 'browser', target: 'es2022', logLevel: 'warning',
  define: { 'import.meta.env': JSON.stringify({ BASE_URL: '/', MODE: 'test', DEV: false, PROD: true }),
    '__VUE_OPTIONS_API__': 'true', '__VUE_PROD_DEVTOOLS__': 'false', '__VUE_PROD_HYDRATION_MISMATCH_DETAILS__': 'false', 'process.env.NODE_ENV': '"production"' } });
assert.equal(bundle.outputFiles.length, 1);
const output = await mkdtemp(join(tmpdir(), 'dataseek-session-recovery-'));
const report = { name: 'session-recovery', browserEngine, passed: false, errors: [], consoleErrors: [], externalAttempts: [], unexpectedRequests: [], checks: [] };
const browser = await launchBrowser();
const context = await browser.newContext({ serviceWorkers: 'block' });
const page = await context.newPage(); page.setDefaultTimeout(10000);
page.on('pageerror', error => report.errors.push(error.message));
page.on('console', message => { if (message.type() === 'error') report.consoleErrors.push(message.text()); });
const origin = 'http://localhost:7001';
let phase = 'unobserved', heldReceipt, heldHistory;
const requests = [], receipts = [], releases = [];
const envelope = data => ({ code: 0, msg: '', data });
const event = (seq, kind, data = {}) => ({ event: kind, data: { version: 1, seq, timestamp: seq, event_id: `${seq}-0`, ...data } });
const wire = events => events.map(item => `id: ${item.data.event_id}\nevent: ${item.event}\ndata: ${JSON.stringify(item.data)}\n\n`).join('');
const broken = 'event: message\ndata: {broken-json\n\n';
const history = id => envelope({ session_id: id, created_at: 1, status: 'completed', has_more: false, next_before_seq: null,
  events: [event(1, 'message', { role: 'user', content: `History of ${id}` })] });
await context.route('**/*', async route => {
  const request = route.request(), url = new URL(request.url());
  if (url.origin !== origin) { report.externalAttempts.push(url.href); return route.abort(); }
  if (url.pathname === '/__session_recovery__/') return route.fulfill({ contentType: 'text/html', body: '<!doctype html><html><head><meta charset="utf-8"></head><body><div id="app"></div><script type="module" src="/__session_recovery__/entry.js"></script></body></html>' });
  if (url.pathname === '/__session_recovery__/entry.js') return route.fulfill({ contentType: 'application/javascript', body: Buffer.from(bundle.outputFiles[0].contents) });
  if (/^\/api\/v1\/sessions\/[^/]+\/history$/.test(url.pathname)) {
    const id = url.pathname.split('/')[4];
    if (id === 'old-history') return new Promise(resolve => { heldHistory = async () => { await route.fulfill({ json: history(id) }).catch(() => {}); resolve(); }; });
    return route.fulfill({ json: history(id) });
  }
  if (url.pathname.includes('/inputs/')) {
    const id = decodeURIComponent(url.pathname.split('/').at(-1)); receipts.push({ phase, id });
    const payload = envelope({ client_message_id: id, accepted: phase === 'accepted', state: phase === 'accepted' ? 'completed' : null, event_seq: phase === 'accepted' ? 4 : null });
    if (phase === 'late-receipt') return new Promise(resolve => { heldReceipt = async () => { await route.fulfill({ json: payload }).catch(() => {}); resolve(); }; });
    return route.fulfill({ json: payload });
  }
  if (url.pathname === '/api/v1/sessions/synthetic-recovery/chat') {
    const body = request.postDataJSON(); requests.push({ phase, body });
    const count = requests.filter(item => item.phase === phase).length;
    let response = broken;
    if (phase === 'unobserved' && count === 1) response = ''; // EOF before any acknowledgement: real automatic retry.
    else if (phase === 'unobserved' && count === 3) response = wire([
      event(1, 'message', { role: 'user', content: body.message, metadata: { client_message_id: body.client_message_id }, attachments: body.attachments }),
      event(2, 'message', { role: 'assistant', content: 'Recovered synthetic answer' }), event(3, 'done'),
    ]);
    else if (phase === 'accepted' && count === 2) response = wire([
      event(4, 'message', { role: 'user', content: 'synthetic accepted question', metadata: { client_message_id: requests.find(item => item.phase === phase).body.client_message_id }, attachments: [{ file_id: 'synthetic-source', filename: 'synthetic.csv' }] }),
      event(5, 'message', { role: 'assistant', content: 'Replayed synthetic answer' }), event(6, 'done'),
    ]);
    return route.fulfill({ contentType: 'text/event-stream', body: response });
  }
  report.unexpectedRequests.push(`${request.method()} ${url.pathname}`); return route.abort();
});
const snapshot = () => page.evaluate(() => window.harness.snapshot());
const awaitRetry = () => page.waitForFunction(() => window.harness.session.canRetryInput.value && !window.harness.session.isLoading.value);
try {
  await page.goto(`${origin}/__session_recovery__/`); await page.waitForFunction(() => window.harnessReady);
  await page.locator('#send').click(); await awaitRetry();
  assert.equal(requests.length, 2, 'EOF must retry, malformed protocol must stop without uncontrolled resubmission');
  assert.deepEqual(requests[0].body, requests[1].body, 'Automatic retry must retain the complete original request');
  await page.locator('#draft').fill('newer draft retained during recovery');
  await page.locator('#recover').click();
  await page.waitForFunction(() => !window.harness.session.isLoading.value && window.harness.session.messages.value.some(row => row.content.content === 'Recovered synthetic answer'));
  assert.equal(requests.length, 3); assert.deepEqual(requests[2].body, requests[0].body, 'Explicit unobserved recovery must use the same ID, timestamp and attachments');
  let state = await snapshot();
  assert.equal(state.messages.filter(row => row.type === 'user').length, 1);
  assert.equal(state.messages.filter(row => row.type === 'attachments').length, 1);
  assert.equal(state.draft, 'newer draft retained during recovery');
  assert.equal(receipts[0].id, requests[0].body.client_message_id);
  report.checks.push({ unobservedRecovery: 'same identity and payload across automatic/manual recovery, one echo, draft preserved' });

  // Begin in a fresh controller scope so no previous admission receipt affects
  // this independent accepted-input case. Production reset owns cancellation.
  await page.evaluate(() => { window.harness.session.reset(); window.harness.session.sessionId.value = 'synthetic-recovery'; });
  phase = 'accepted';
  await page.locator('#draft').fill('synthetic accepted question'); await page.locator('#send').click(); await awaitRetry();
  await page.locator('#recover').click();
  await page.waitForFunction(() => !window.harness.session.isLoading.value && window.harness.session.messages.value.some(row => row.content.content === 'Replayed synthetic answer'));
  const accepted = requests.filter(item => item.phase === phase);
  assert.equal(accepted.length, 2); assert.equal(accepted[1].body.message, '');
  assert.equal(accepted[1].body.client_message_id, undefined, 'Accepted recovery is read-only replay, not another submission');
  assert.notEqual(accepted[0].body.client_message_id, requests[0].body.client_message_id);
  state = await snapshot(); assert.equal(state.messages.filter(row => row.type === 'user').length, 1);
  assert.deepEqual(report.consoleErrors, [], 'Recovery must not produce console errors');
  report.checks.push({ acceptedRecovery: 'owner receipt causes read-only replay and canonical echo handoff' });

  await page.evaluate(() => { window.harness.session.reset(); window.harness.session.sessionId.value = 'synthetic-recovery'; });
  phase = 'late-receipt';
  await page.locator('#draft').fill('old scope input'); await page.locator('#send').click(); await awaitRetry();
  await page.locator('#recover').click();
  await page.waitForFunction(() => window.harness.session.isLoading.value);
  // Polling a condition in the driver yields to the route callback without
  // time-based assumptions about receipt delivery.
  const receiptWait = Date.now() + 10000;
  while (!heldReceipt && Date.now() < receiptWait) await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(heldReceipt, 'The old scope receipt must actually be in flight');
  await page.evaluate(() => window.harness.restore('new-scope'));
  await heldReceipt(); releases.push('receipt');
  const beforeLate = requests.length;
  await page.evaluate(() => window.harness.flush());
  state = await snapshot(); assert.equal(state.id, 'new-scope'); assert.equal(state.retry, false);
  assert.deepEqual(state.messages.map(row => row.content.content), ['History of new-scope']);
  assert.equal(requests.length, beforeLate, 'A stale receipt must not issue another chat request');
  report.checks.push({ staleReceipt: 'real aborted fetch cannot restore an old scope or resubmit it' });

  await page.evaluate(() => { void window.harness.restore('old-history'); });
  const historyWait = Date.now() + 10000;
  while (!heldHistory && Date.now() < historyWait) await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(heldHistory, 'The old history response must actually be in flight');
  await page.evaluate(() => window.harness.restore('newer-scope'));
  await heldHistory(); releases.push('history'); await page.evaluate(() => window.harness.flush());
  state = await snapshot(); assert.equal(state.id, 'newer-scope');
  assert.deepEqual(state.messages.map(row => row.content.content), ['History of newer-scope']);
  report.checks.push({ staleHistory: 'late history response cannot overwrite the selected session' });
  assert.deepEqual(report.errors, []);
  // The existing API interceptor logs each deliberately aborted Axios read
  // as a generic network diagnostic. Account for exactly these two known
  // cancellations; additional or different console errors still fail.
  assert.deepEqual(report.consoleErrors, ['API request failed network', 'API request failed network']);
  report.expectedCancellationDiagnostics = 2;
  assert.deepEqual(report.externalAttempts, []); assert.deepEqual(report.unexpectedRequests, []);
  await page.evaluate(() => window.harness.unmount());
  assert.equal(await page.locator('#app').evaluate(element => element.childElementCount), 0);
  report.passed = true;
} catch (error) {
  report.failure = error.message; report.stack = error.stack; process.exitCode = 1;
  await page.screenshot({ path: join(output, 'failure.png'), fullPage: true }).catch(() => {});
} finally {
  if (heldReceipt && !releases.includes('receipt')) await heldReceipt();
  if (heldHistory && !releases.includes('history')) await heldHistory();
  await context.close(); await browser.close();
  report.requests = requests; report.receipts = receipts;
  await writeFile(join(output, 'results.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ name: report.name, browserEngine, passed: report.passed, checks: report.checks, failure: report.failure }));
  console.log(`BROWSER_REPORT=${output}`);
}
