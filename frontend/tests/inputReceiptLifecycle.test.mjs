import assert from 'node:assert/strict';
import test from 'node:test';
import { useAnalysisSession } from '../src/composables/useAnalysisSession.ts';
import { SSEConnectionError } from '../src/utils/sseConnection.ts';

function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
function harness(extra = {}) {
  const requests = [], receipts = [], rejected = [];
  let count = 0, observation = { accepted: false, event_seq: null, state: null };
  const session = useAnalysisSession({ onInputRejected: request => rejected.push(request), api: {
    createClientMessageId: () => `input-${++count}`,
    createSession: async () => ({ session_id: 'session', created_at: 1 }),
    chatWithSession: async (...args) => { requests.push(args); return () => {}; },
    getInputReceipt: async (id, key, signal) => { receipts.push({ id, key, signal }); return { client_message_id: key, ...observation }; },
    getSessionHistory: async id => ({ session_id: id, status: 'completed', events: [], has_more: false }),
    stopSession: async () => {}, ...extra,
  } });
  session.sessionId.value = 'session';
  return { session, requests, receipts, rejected, observe: value => { observation = value; } };
}
const callbacks = request => request[8];
const terminal = request => { callbacks(request).onMessage({ event: 'done', data: { seq: 8, timestamp: 8 } }); callbacks(request).onClose(); };

test('unknown submission recovery keeps identity, timestamp, payload and one optimistic turn', async () => {
  const h = harness();
  const request = { message: 'same question', files: [{ file_id: 'file', filename: 'sample.csv' }], skills: ['skill'], mcpServers: ['mcp'], datasetIds: ['dataset'], inputFileIds: ['input'], agentProfileId: 'profile' };
  await h.session.send(request);
  const first = h.requests[0];
  callbacks(first).onError(new SSEConnectionError('transport', 'offline'));
  request.files[0].file_id = 'changed'; request.datasetIds.push('changed'); request.skills.push('changed'); request.message = 'changed';
  assert.equal(h.session.canRetryInput.value, true);
  await h.session.retryPendingInput();
  assert.equal(h.requests.length, 2);
  assert.deepEqual(h.requests[1].slice(1, 8), first.slice(1, 8));
  assert.deepEqual(h.requests[1].slice(9), first.slice(9));
  assert.equal(h.session.messages.value.filter(row => row.type === 'user').length, 1);
  assert.equal(h.session.messages.value.filter(row => row.type === 'attachments').length, 1);
  assert.equal(h.receipts[0].key, 'input-1');
  h.session.dispose();
});

test('accepted input reconnects read-only and canonical user echo hands off by ID exactly once', async () => {
  const h = harness();
  await h.session.send({ message: 'local text' });
  const echo = h.session.messages.value[0];
  callbacks(h.requests[0]).onError(new Error('offline'));
  h.observe({ accepted: true, event_seq: 1, state: 'running' });
  await h.session.retryPendingInput();
  assert.equal(h.requests[1][1], '');
  callbacks(h.requests[1]).onMessage({ event: 'message', data: { seq: 1, timestamp: 1, role: 'user', content: 'canonical text', metadata: { client_message_id: 'input-1' } } });
  assert.equal(h.session.messages.value.length, 1);
  assert.equal(h.session.messages.value[0], echo);
  assert.equal(echo.content.content, 'canonical text');
  terminal(h.requests[1]);
  h.observe({ accepted: true, event_seq: 1, state: 'completed' });
  await h.session.send({ message: 'local text' });
  assert.equal(h.requests[2][10], 'input-2');
  assert.equal(h.session.messages.value.filter(row => row.type === 'user').length, 2);
  h.session.dispose();
});

test('an intended repeated question gets a new ID after completion, never text-based deduplication', async () => {
  const h = harness();
  await h.session.send({ message: 'identical' }); terminal(h.requests[0]);
  h.observe({ accepted: true, state: 'completed', event_seq: 1 });
  assert.equal(await h.session.send({ message: 'identical' }), true);
  assert.notEqual(h.requests[0][10], h.requests[1][10]);
  h.session.dispose();
});

test('canonical input attachments update the same visible echo or retire only the unaccepted local attachment', async () => {
  for (const canonical of [[{ file_id: 'durable', filename: 'canonical.csv' }], []]) {
    const h = harness();
    await h.session.send({ message: 'question', files: [{ file_id: 'local', filename: 'local.csv' }] });
    const user = h.session.messages.value[0], attachment = h.session.messages.value[1];
    callbacks(h.requests[0]).onMessage({ event: 'message', data: { seq: 1, timestamp: 1, role: 'user', content: 'question', attachments: canonical, metadata: { client_message_id: 'input-1' } } });
    assert.equal(h.session.messages.value[0], user);
    assert.equal(h.session.messages.value.filter(row => row.type === 'user').length, 1);
    if (canonical.length) {
      assert.equal(h.session.messages.value[1], attachment);
      assert.deepEqual(attachment.content.attachments, canonical);
    } else assert.equal(h.session.messages.value.length, 1);
    h.session.dispose();
  }
});

test('new sends cannot overwrite unobserved input ownership; new drafts are not replayed as the old request', async () => {
  const h = harness();
  await h.session.send({ message: 'first' }); callbacks(h.requests[0]).onError(new Error('lost'));
  assert.equal(await h.session.send({ message: 'new intentional draft' }), false);
  assert.equal(h.requests.length, 1);
  await h.session.retryPendingInput();
  assert.equal(h.requests[1][1], 'first');
  assert.equal(h.requests[1][10], 'input-1');
  h.session.dispose();
});

test('confirmed terminal receipt after transport loss replays missing result before accepting a new turn', async () => {
  const h = harness();
  await h.session.send({ message: 'first' }); callbacks(h.requests[0]).onError(new Error('lost'));
  h.observe({ accepted: true, state: 'completed', event_seq: 1 });
  assert.equal(await h.session.send({ message: 'next draft' }), false);
  assert.equal(h.requests[1][1], '');
  terminal(h.requests[1]);
  assert.equal(await h.session.send({ message: 'next draft' }), true);
  assert.equal(h.requests[2][10], 'input-2');
  h.session.dispose();
});

for (const kind of ['http-rejected', 'creation-rejected']) test(`${kind} retires only the local echo and restores the original draft`, async () => {
  const h = harness(kind === 'creation-rejected' ? { createSession: async () => { throw new Error('no session'); } } : {});
  if (kind === 'creation-rejected') h.session.sessionId.value = undefined;
  await h.session.send({ message: 'draft', files: [{ file_id: 'f', filename: 'f.csv' }] });
  if (kind === 'http-rejected') callbacks(h.requests[0]).onError(new SSEConnectionError('http', 'denied', 422));
  assert.equal(h.session.messages.value.length, 0);
  assert.equal(h.session.canRetryInput.value, false);
  assert.equal(h.rejected.length, 1);
  assert.equal(h.rejected[0].message, 'draft');
  h.session.dispose();
});

test('a rejection after ambiguous reconnect cannot declare the original input unaccepted', async () => {
  const h = harness();
  await h.session.send({ message: 'draft' });
  callbacks(h.requests[0]).onRetry({ attempt: 1, maxAttempts: 6 });
  callbacks(h.requests[0]).onError(new SSEConnectionError('http', 'denied', 403));
  assert.equal(h.session.messages.value.length, 1);
  assert.equal(h.rejected.length, 0);
  assert.equal(h.session.canRetryInput.value, true);
  h.session.dispose();
});

test('double recovery and late receipts are generation-owned and cancel on navigation', async () => {
  const read = deferred(); let signal;
  const h = harness({ getInputReceipt: (_id, _key, currentSignal) => { signal = currentSignal; return read.promise; } });
  await h.session.send({ message: 'old' }); callbacks(h.requests[0]).onError(new Error('lost'));
  const pending = h.session.retryPendingInput();
  assert.equal(await h.session.retryPendingInput(), false);
  h.session.reset(); h.session.sessionId.value = 'other';
  read.resolve({ client_message_id: 'input-1', accepted: false });
  await pending;
  assert.equal(signal.aborted, true);
  assert.equal(h.requests.length, 1);
  assert.equal(h.session.messages.value.length, 0);
  assert.equal(h.session.canRetryInput.value, false);
  callbacks(h.requests[0]).onRetry({ attempt: 3, maxAttempts: 6 });
  assert.equal(h.session.connectionNotice.value, '');
  h.session.dispose();
});

test('failed or mismatched receipt never resubmits, and late close cannot hide recovery', async () => {
  for (const getInputReceipt of [async () => { throw Error('offline'); }, async () => ({ client_message_id: 'foreign', accepted: true })]) {
    const h = harness({ getInputReceipt });
    await h.session.send({ message: 'draft' }); callbacks(h.requests[0]).onError(new Error('lost'));
    callbacks(h.requests[0]).onClose();
    assert.equal(h.session.canRetryInput.value, true);
    await h.session.retryPendingInput();
    assert.equal(h.requests.length, 1);
    assert.equal(h.session.canRetryInput.value, true);
    h.session.dispose();
  }
});

test('an unconfirmed stop retains identity and replays missing results before a new input', async () => {
  const h = harness({ stopSession: async () => { throw new Error('lost stop response'); } });
  await h.session.send({ message: 'first' });
  await h.session.stop();
  assert.equal(h.session.canRetryInput.value, true);
  assert.equal(h.session.messages.value[0].content.content, 'first');
  h.observe({ accepted: true, state: 'completed', event_seq: 1 });
  assert.equal(await h.session.send({ message: 'next draft' }), false);
  assert.equal(h.requests[1][1], '');
  terminal(h.requests[1]);
  assert.equal(await h.session.send({ message: 'next draft' }), true);
  assert.equal(h.requests[2][10], 'input-2');
  h.session.dispose();
});
