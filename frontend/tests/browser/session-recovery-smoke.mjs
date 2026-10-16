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
 h('button',{id:'stop',onClick:()=>session.stop(),disabled:!session.isLoading.value},'Stop'),
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
const requests = [], receipts = [], releases = [], stopRequests = [];
const heldStopInputs = new Map(), heldStopObservations = new Map();
const envelope = data => ({ code: 0, msg: '', data });
const event = (seq, kind, data = {}) => ({ event: kind, data: { version: 1, seq, timestamp: seq, event_id: `${seq}-0`, ...data } });
const wire = events => events.map(item => `id: ${item.data.event_id}\nevent: ${item.event}\ndata: ${JSON.stringify(item.data)}\n\n`).join('');
const broken = 'event: message\ndata: {broken-json\n\n';
const history = id => envelope({ session_id: id, created_at: 1, status: 'completed', has_more: false, next_before_seq: null,
  events: [event(1, 'message', { role: 'user', content: `History of ${id}` })] });
const stoppedOutcome = currentPhase => [
  event(10, 'message', { role: 'user', content: `Input for ${currentPhase}`,
    metadata: { client_message_id: requests.find(item => item.phase === currentPhase).body.client_message_id },
    attachments: [{ file_id: 'synthetic-source', filename: 'synthetic.csv' }] }),
  event(11, 'message', { role: 'assistant', content: `Late persisted outcome for ${currentPhase}`,
    metadata: { analysis_outcome: { status: 'partial', reason_code: 'request_cancelled', missing: [], can_resume: false } },
    attachments: [{ file_id: 'synthetic-delivered-result', filename: 'result.csv' }] }),
];
const hold = (map, key, route) => new Promise(resolve => {
  map.set(key, async body => {
    map.delete(key);
    await route.fulfill({ contentType: 'text/event-stream', body }).catch(() => {});
    resolve();
  });
});
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
  if (url.pathname === '/api/v1/sessions/synthetic-recovery/stop') {
    stopRequests.push({ phase, method: request.method() });
    // Release the aborted original request without an acknowledgement. Only
    // the observation below can deliver this input's durable terminal suffix.
    const release = heldStopInputs.get(phase);
    if (release) await release('');
    return route.fulfill({ json: phase === 'stop-failed'
      ? { code: 1, msg: 'Synthetic stop confirmation unavailable', data: null }
      : envelope(null) });
  }
  if (url.pathname === '/api/v1/sessions/synthetic-recovery/chat') {
    const body = request.postDataJSON(); requests.push({ phase, body });
    const count = requests.filter(item => item.phase === phase).length;
    if (phase.startsWith('stop-')) {
      if (count === 1) return hold(heldStopInputs, phase, route);
      const observationCount = count - 1;
      if (phase === 'stop-late') return hold(heldStopObservations, observationCount, route);
      const response = phase === 'stop-eof' && observationCount === 1 ? ''
        : phase === 'stop-eof' && observationCount === 2 ? broken
        : wire([...stoppedOutcome(phase), event(12, 'done')]);
      return route.fulfill({ contentType: 'text/event-stream', body: response });
    }
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
async function waitForHeld(map, key, label) {
  const deadline = Date.now() + 10000;
  while (!map.has(key) && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(map.has(key), `${label} must actually be in flight`);
}
function assertStopObservationBodies(currentPhase) {
  const submitted = requests.filter(item => item.phase === currentPhase);
  assert.equal(submitted.filter(item => item.body.message).length, 1, 'Stopping must not submit the original input again or send a newer task');
  for (const { body } of submitted.slice(1)) {
    assert.equal(body.message, '');
    assert.equal(body.observe_client_message_id, submitted[0].body.client_message_id);
    for (const field of ['client_message_id', 'resume_from', 'attachments', 'skills', 'mcp_servers', 'agent_profile_id', 'dataset_ids', 'input_file_ids']) {
      assert.equal(body[field], undefined, `Read-only observation must not send ${field}`);
    }
  }
  assert.equal(receipts.filter(item => item.phase === currentPhase).length, 0, 'Stopping must not use an absent receipt as permission to resubmit');
}
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

  // The actual controller, API serializer and Fetch transport recover a
  // durable cancellation suffix that arrives after the stop acknowledgement.
  await page.evaluate(() => { window.harness.session.reset(); window.harness.session.sessionId.value = 'synthetic-recovery'; });
  phase = 'stop-late';
  await page.locator('#draft').fill(`Input for ${phase}`); await page.locator('#send').click();
  await waitForHeld(heldStopInputs, phase, 'Original input');
  await page.locator('#draft').fill('new draft while old analysis stops');
  await page.locator('#stop').click();
  await waitForHeld(heldStopObservations, 1, 'Stop observation');
  assert.equal(await page.locator('#send').isDisabled(), true);
  assert.equal(await page.evaluate(() => window.harness.session.send({ message: 'must not start during stop' })), false);
  await heldStopObservations.get(1)(wire(stoppedOutcome(phase)));
  await page.waitForFunction(() => window.harness.session.messages.value.some(row => row.content.content === 'Late persisted outcome for stop-late'));
  await waitForHeld(heldStopObservations, 2, 'Observation retry after outcome-only EOF');
  state = await snapshot();
  assert.equal(state.loading, true, 'A persisted outcome without terminal must not unlock the composer');
  assert.equal(state.draft, 'new draft while old analysis stops');
  assert.equal(await page.evaluate(() => window.harness.session.send({ message: 'must not start before terminal' })), false);
  const lateRequests = requests.filter(item => item.phase === phase);
  assert.equal(lateRequests.length, 3, 'One input plus two read-only observations');
  assert.equal(lateRequests[2].body.event_seq, 11, 'EOF recovery advances from the actual persisted outcome');
  assert.equal(lateRequests[2].body.event_id, '11-0');
  await heldStopObservations.get(2)(wire([event(12, 'done')]));
  await page.waitForFunction(() => !window.harness.session.isLoading.value && !window.harness.session.canRetryInput.value);
  state = await snapshot();
  assert.equal(state.messages.filter(row => row.type === 'user').length, 1);
  const lateOutcome = state.messages.filter(row => row.type === 'assistant');
  assert.equal(lateOutcome.length, 1);
  assert.equal(lateOutcome[0].content.metadata.analysis_outcome.reason_code, 'request_cancelled');
  assert.equal(state.messages.filter(row => row.type === 'attachments' && row.content.role === 'assistant').length, 1);
  assert.equal(state.draft, 'new draft while old analysis stops');
  assert.equal(state.notice, '');
  assertStopObservationBodies(phase);
  report.checks.push({ stopLateTerminal: 'late persisted outcome and attachments survive; outcome-only EOF stays locked until cursor-only terminal replay; draft preserved' });

  for (const failurePhase of ['stop-failed', 'stop-eof']) {
    await page.evaluate(() => { window.harness.session.reset(); window.harness.session.sessionId.value = 'synthetic-recovery'; });
    phase = failurePhase;
    await page.locator('#draft').fill(`Input for ${phase}`); await page.locator('#send').click();
    await waitForHeld(heldStopInputs, phase, 'Original input before failure');
    await page.locator('#draft').fill(`preserved new draft for ${phase}`);
    await page.locator('#stop').click(); await awaitRetry();
    state = await snapshot();
    assert.match(state.notice, /不会重新/);
    const beforeBlockedSend = requests.length;
    await page.locator('#send').click(); // Enabled UI still cannot overtake the unresolved owner.
    await page.evaluate(() => window.harness.flush());
    assert.equal(requests.length, beforeBlockedSend);
    assert.equal((await snapshot()).draft, `preserved new draft for ${phase}`);
    const beforeManual = requests.filter(item => item.phase === phase);
    assert.equal(beforeManual.length, phase === 'stop-eof' ? 3 : 1,
      'Failed stop is not resubmitted; EOF has only its read-only bounded retry before protocol refusal');
    await page.locator('#recover').click();
    await page.waitForFunction(expected => !window.harness.session.isLoading.value && !window.harness.session.canRetryInput.value
      && window.harness.session.messages.value.some(row => row.content.content === expected), `Late persisted outcome for ${phase}`);
    state = await snapshot();
    assert.equal(state.draft, `preserved new draft for ${phase}`);
    assert.equal(state.messages.filter(row => row.type === 'user').length, 1);
    assert.equal(state.messages.filter(row => row.type === 'assistant').length, 1);
    assert.equal(stopRequests.filter(item => item.phase === phase).length, 1, 'Read-only retry must not issue another stop command');
    assertStopObservationBodies(phase);
    report.checks.push({ [phase]: 'uncertain stop blocks newer sends; explicit recovery only observes the original identity and preserves draft' });
  }
  assert.deepEqual(report.errors, []);
  assert.deepEqual(report.consoleErrors, ['API request failed network', 'API request failed network']);
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
  for (const release of [...heldStopInputs.values(), ...heldStopObservations.values()]) await release('');
  await context.close(); await browser.close();
  report.requests = requests; report.receipts = receipts; report.stopRequests = stopRequests;
  await writeFile(join(output, 'results.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ name: report.name, browserEngine, passed: report.passed, checks: report.checks, failure: report.failure }));
  console.log(`BROWSER_REPORT=${output}`);
}
