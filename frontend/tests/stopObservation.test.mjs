import assert from 'node:assert/strict';
import test from 'node:test';
import { useAnalysisSession } from '../src/composables/useAnalysisSession.ts';

const event = (seq, name, data = {}) => ({ event: name, data: { seq, event_id: `${seq}-0`, timestamp: seq, ...data } });
const outcome = event(4, 'message', { role: 'assistant', content: '本轮请求已取消，已有结果保留。',
  metadata: { analysis_outcome: { status: 'partial', reason_code: 'request_cancelled', missing: [], can_resume: false } },
  attachments: [{ file_id: 'delivered', filename: 'result.csv' }],
});
const done = event(5, 'done');
const callbacks = request => request[8];

async function harness(extra = {}) {
  const requests = [], cancelled = [], historyReads = [];
  let count = 0;
  const api = {
    createClientMessageId: () => `input-${++count}`,
    createSession: async () => ({ session_id: 'session', created_at: 1 }),
    getSessionHistory: async (...args) => { historyReads.push(args); throw Error('stop must use cursor replay, not one-shot history'); },
    getInputReceipt: async () => { throw Error('stop recovery must not retry submission based on an absent receipt'); },
    stopSession: async () => {},
    chatWithSession: async (...args) => { requests.push(args); return () => cancelled.push(args); },
    ...extra,
  };
  const session = useAnalysisSession({ api });
  session.sessionId.value = 'session';
  await session.send({ message: '分析', files: [{ file_id: 'source', filename: 'source.csv' }],
    datasetIds: ['dataset'], skills: ['skill'], mcpServers: ['server'], agentProfileId: 'profile' });
  callbacks(requests[0]).onMessage(event(1, 'step', { id: 'step', status: 'running', description: '分析' }));
  return { session, api, requests, cancelled, historyReads };
}

function assertReadOnly(request, seq = 1) {
  assert.equal(request[1], '');
  assert.equal(request[2], `${seq}-0`);
  assert.equal(request[3], seq);
  assert.deepEqual(request[4], []);
  assert.deepEqual(request[5], []);
  assert.deepEqual(request[6], []);
  assert.equal(request[7], null);
  assert.equal(request[9], undefined);
  assert.equal(request[10], undefined);
  assert.equal(request[11], undefined);
  assert.equal(request[12], undefined);
  assert.equal(request[14], 'input-1', 'identity selects observation only, not submission');
}

test('HTTP stop confirmation is not terminal; cursor replay keeps results and settles exactly once', async t => {
  const h = await harness(); t.after(() => h.session.dispose());
  const original = callbacks(h.requests[0]);
  await h.session.stop();
  assert.equal(h.cancelled.length, 1);
  assertReadOnly(h.requests[1]);
  assert.equal(h.session.isLoading.value, true);
  assert.equal(await h.session.send({ message: 'new draft' }), false);
  original.onMessage(outcome); original.onMessage(done); original.onClose();
  assert.equal(h.session.messages.value.some(message => message.type === 'assistant'), false);
  const replay = callbacks(h.requests[1]);
  replay.onMessage(event(2, 'tool', { tool_call_id: 'call', name: 'shell', function: 'shell_run', status: 'called',
    execution_status: 'succeeded', args: {}, content: { console: [{ command: 'echo ok', output: 'ok', returncode: 0 }] } }));
  replay.onMessage(outcome); replay.onMessage(outcome);
  assert.equal(h.session.isLoading.value, true, 'outcome alone precedes the durable terminal');
  replay.onMessage(done); replay.onClose();
  assert.equal(h.session.messages.value.filter(message => message.type === 'assistant').length, 1);
  assert.equal(h.session.messages.value.filter(message => message.type === 'attachments' && message.content.role === 'assistant').length, 1);
  assert.equal(h.session.messages.value.find(message => message.type === 'step').content.status, 'failed');
  assert.equal(h.session.isLoading.value, false);
  assert.equal(h.session.canRetryInput.value, false);
  assert.equal(h.historyReads.length, 0);
  assert.equal(await h.session.send({ message: 'new draft' }), true);
  assert.equal(h.requests[2][10], 'input-2');
});

for (const failure of ['stop-http', 'replay-error', 'replay-eof', 'replay-throw']) {
  test(`${failure}: recovery remains read-only and preserves the cursor/input owner`, async t => {
    const h = await harness(failure === 'stop-http' ? { stopSession: async () => { throw Error('offline'); } } : {});
    t.after(() => h.session.dispose());
    const connect = h.api.chatWithSession;
    if (failure === 'replay-throw') h.api.chatWithSession = async () => { throw Error('cannot connect'); };
    await h.session.stop();
    h.api.chatWithSession = connect;
    if (failure === 'replay-error') {
      callbacks(h.requests[1]).onMessage(event(2, 'tool', { tool_call_id: 'call', name: 'shell', function: 'shell_run', status: 'called', args: {} }));
      callbacks(h.requests[1]).onError(Error('connection lost'));
    } else if (failure === 'replay-eof') callbacks(h.requests[1]).onClose();
    assert.equal(h.session.canRetryInput.value, true);
    assert.match(h.session.connectionNotice.value, /不会重新/);
    const before = h.requests.length;
    assert.equal(await h.session.send({ message: 'new draft' }), false);
    assert.equal(h.requests.length, before);
    await h.session.retryPendingInput();
    assertReadOnly(h.requests.at(-1), failure === 'replay-error' ? 2 : 1);
    callbacks(h.requests.at(-1)).onMessage(outcome);
    callbacks(h.requests.at(-1)).onMessage(done);
    callbacks(h.requests.at(-1)).onClose();
    assert.equal(h.session.canRetryInput.value, false);
    assert.equal(await h.session.send({ message: 'new draft' }), true);
    assert.equal(h.requests.at(-1)[10], 'input-2');
  });
}

test('late stop replay cannot alter a reset session, even when the same session ID is reused', async t => {
  const h = await harness(); t.after(() => h.session.dispose());
  await h.session.stop();
  const stale = callbacks(h.requests[1]);
  h.session.reset(); h.session.sessionId.value = 'session';
  await h.session.send({ message: 'new turn' });
  stale.onMessage(outcome); stale.onMessage(done); stale.onError(Error('stale')); stale.onClose();
  assert.equal(h.session.isLoading.value, true);
  assert.equal(h.session.messages.value.length, 1);
  assert.equal(h.session.messages.value[0].content.content, 'new turn');
  assert.equal(h.session.canRetryInput.value, false);
});

test('a naturally completed outcome racing stop remains successful, not a fabricated cancellation', async t => {
  const h = await harness(); t.after(() => h.session.dispose());
  await h.session.stop();
  callbacks(h.requests[1]).onMessage(event(4, 'message', { role: 'assistant', content: '已完成', metadata: {
    analysis_outcome: { status: 'succeeded', reason_code: 'completed', missing: [], can_resume: false },
  } }));
  callbacks(h.requests[1]).onMessage(done);
  assert.equal(h.session.messages.value.find(message => message.type === 'step').content.status, 'completed');
  assert.equal(h.session.isLoading.value, false);
});

test('a late stop click after an observed terminal neither cancels nor waits for an impossible new terminal', async t => {
  let stopCalls = 0;
  const h = await harness({ stopSession: async () => { stopCalls++; } }); t.after(() => h.session.dispose());
  callbacks(h.requests[0]).onMessage(done);
  await h.session.stop();
  assert.equal(stopCalls, 0);
  assert.equal(h.requests.length, 1);
  assert.equal(h.session.isLoading.value, false);
});

test('stop during an unacknowledged submission observes that new identity, not the previous turn', async t => {
  const h = await harness(); t.after(() => h.session.dispose());
  callbacks(h.requests[0]).onMessage(done); callbacks(h.requests[0]).onClose();
  // This adapter's receipt is intentionally unavailable: finish the first turn
  // with a reset, as a restored view would, then submit before any SSE arrives.
  h.session.reset(); h.session.sessionId.value = 'session';
  await h.session.send({ message: 'not yet accepted' });
  await h.session.stop();
  assert.equal(h.requests.at(-1)[14], 'input-2');
  assert.equal(h.requests.at(-1)[1], '');
  callbacks(h.requests.at(-1)).onError(Error('unobserved after bounded retry'));
  assert.match(h.session.connectionNotice.value, /新建对话/);
  assert.equal(await h.session.send({ message: 'must not overtake' }), false);
  h.session.reset();
  assert.equal(await h.session.send({ message: 'explicit new conversation' }), true);
});

test('stopping a continuation observes its fresh input identity, not the original question', async t => {
  const h = await harness(); t.after(() => h.session.dispose());
  callbacks(h.requests[0]).onMessage(event(3, 'message', { role: 'assistant', content: '待继续', metadata: {
    analysis_outcome: { status: 'partial', reason_code: 'artifacts_missing', missing: [], can_resume: true, resume_from: 'a'.repeat(32) },
  } }));
  callbacks(h.requests[0]).onMessage(done); callbacks(h.requests[0]).onClose();
  const index = h.session.messages.value.findIndex(message => message.type === 'assistant');
  await h.session.resumeAnalysis(index);
  assert.equal(h.requests[1][10], 'input-2');
  assert.equal(h.requests[1][11], 'a'.repeat(32));
  await h.session.stop();
  assert.equal(h.requests[2][14], 'input-2');
  assert.equal(h.requests[2][1], '');
  assert.equal(h.requests[2][11], undefined);
});
