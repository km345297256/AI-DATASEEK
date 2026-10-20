import assert from 'node:assert/strict';
import test from 'node:test';
import { useAnalysisSession } from '../src/composables/useAnalysisSession.ts';
import { createAgentStreamRecovery } from '../src/utils/agentStreamRecovery.ts';
import { startSSEConnection } from '../src/utils/sseConnection.ts';

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function emit(options, seq, event, data = {}) {
  options.onmessage({ id: `${seq}-0`, event,
    data: JSON.stringify({ seq, event_id: `${seq}-0`, version: 1, timestamp: seq, ...data }) });
}

function harness(connect, wait) {
  const requests = [];
  const streams = [];
  const handlers = [];
  let stops = 0;
  let ids = 0;
  let terminals = 0;
  const api = {
    createClientMessageId: () => `synthetic-input-${++ids}`,
    createSession: async () => ({ session_id: 'synthetic-session', created_at: 1 }),
    getSessionHistory: async () => { throw Error('stop must observe the stream, not reread history'); },
    stopSession: async () => { stops += 1; },
    chatWithSession: async (...args) => {
      const callback = args[8];
      handlers.push(callback);
      const recovery = createAgentStreamRecovery({
        message: args[1], event_id: args[2], event_seq: args[3],
        ...(args[14] ? { observe_client_message_id: args[14] } : {
          attachments: args[4], skills: args[5], mcp_servers: args[6],
          agent_profile_id: args[7], dataset_ids: args[9], input_file_ids: args[12],
        }),
        client_message_id: args[1] || args[11] ? args[10] : undefined,
        ...(args[11] ? { resume_from: args[11] } : {}),
      });
      const stream = startSSEConnection('/synthetic/chat', {
        method: 'POST', retryOnError: true, getBody: recovery.getBody, isComplete: recovery.isComplete,
      }, {
        ...callback,
        onMessage(event) { if (recovery.accept(event)) callback.onMessage(event); },
      }, {
        connect: async (_url, options) => {
          const request = JSON.parse(options.body);
          requests.push(request);
          await options.onopen(new Response(null, { headers: { 'content-type': 'text/event-stream' } }));
          await connect(options, request, requests.length);
        },
        wait,
      });
      streams.push(stream);
      return stream.cancel;
    },
  };
  const session = useAnalysisSession({ api, onTerminal: () => { terminals += 1; } });
  return { session, requests, streams, handlers, get stops() { return stops; }, get terminals() { return terminals; } };
}

const savedResult = {
  role: 'assistant', content: 'Synthetic report already saved.',
  attachments: [{ file_id: 'synthetic-result', filename: 'synthetic-report.md' }],
};

test('an error terminal plus a late socket rejection cannot restore loading or lose the delivered result', async t => {
  const deliver = deferred();
  const h = harness(async options => {
    await deliver.promise;
    emit(options, 1, 'step', { id: 'synthetic-step', status: 'running', description: 'Analyze synthetic data' });
    emit(options, 2, 'message', savedResult);
    emit(options, 3, 'error', { error: 'invalid_execution_result: synthetic terminal failure' });
    throw new TypeError('late socket failure');
  }, async () => { assert.fail('a terminal must not enter retry backoff'); });
  t.after(() => h.session.dispose());
  await h.session.send({ message: 'Analyze synthetic data' });
  deliver.resolve();
  await h.streams[0].finished;
  // Also exercise callbacks already queued outside the transport's promise.
  h.handlers[0].onRetry({ attempt: 4, maxAttempts: 6, delayMs: 4000 });
  h.handlers[0].onOpen();
  h.handlers[0].onError(new TypeError('late queued error'));
  h.handlers[0].onClose();
  assert.equal(h.requests.length, 1);
  assert.equal(h.session.isLoading.value, false);
  assert.equal(h.session.connectionNotice.value, '');
  assert.equal(h.session.canRetryInput.value, false);
  assert.equal(h.terminals, 1);
  assert.equal(h.session.messages.value.filter(message => message.type === 'assistant' && message.content.content === savedResult.content).length, 1);
  assert.equal(h.session.messages.value.filter(message => message.type === 'attachments').length, 1);
  await h.session.stop();
  assert.equal(h.stops, 0, 'a stale stop click must not cancel another session-wide task');
});

test('stop during real transport backoff aborts the retry and opens only a read-only result observation', async t => {
  const backoff = deferred();
  const observation = deferred();
  let retrySignal;
  let finishQueuedRetry;
  const h = harness(async (options, request) => {
    if (request.message) {
      emit(options, 1, 'step', { id: 'synthetic-step', status: 'running', description: 'Analyze synthetic data' });
      throw new TypeError('connection dropped before stop');
    }
    await observation.promise;
    emit(options, 2, 'message', { ...savedResult, metadata: { analysis_outcome: {
      status: 'partial', reason_code: 'request_cancelled', missing: [], can_resume: false,
    } } });
    emit(options, 3, 'done');
    throw new DOMException('reader aborted after terminal', 'AbortError');
  }, (_delay, signal) => new Promise(resolve => {
    retrySignal = signal;
    finishQueuedRetry = resolve;
    signal.addEventListener('abort', resolve, { once: true });
    backoff.resolve();
  }));
  t.after(() => h.session.dispose());
  await h.session.send({ message: 'Analyze synthetic data', datasetIds: ['synthetic-data'] });
  await backoff.promise;
  assert.match(h.session.connectionNotice.value, /连接中断/);
  await h.session.stop();
  assert.equal(retrySignal.aborted, true);
  finishQueuedRetry();
  await h.streams[0].finished;
  assert.equal(h.stops, 1);
  assert.equal(h.requests.length, 2);
  assert.equal(h.requests.filter(request => request.message).length, 1);
  assert.deepEqual(h.requests[1], {
    message: '', event_id: '1-0', event_seq: 1, observe_client_message_id: 'synthetic-input-1',
  });
  assert.equal(h.session.isLoading.value, true, 'the stop acknowledgement is not the result delivery');
  observation.resolve();
  await h.streams[1].finished;
  assert.equal(h.requests.length, 2);
  assert.equal(h.session.isLoading.value, false);
  assert.equal(h.session.connectionNotice.value, '');
  assert.equal(h.session.canRetryInput.value, false);
  assert.equal(h.session.messages.value.filter(message => message.type === 'assistant' && message.content.content === savedResult.content).length, 1);
  assert.equal(h.session.messages.value.filter(message => message.type === 'attachments').length, 1);
  assert.equal(h.session.messages.value.find(message => message.type === 'step').content.status, 'failed');
  assert.equal(h.terminals, 1);
});
