import assert from 'node:assert/strict';
import test from 'node:test';
import { fetchEventSource } from '@microsoft/fetch-event-source';

import { createAgentStreamRecovery } from '../src/utils/agentStreamRecovery.ts';
import { startSSEConnection, SSEConnectionError } from '../src/utils/sseConnection.ts';

function frame(seq, event = 'message', extra = {}) {
  return { event, data: { seq, event_id: `${seq}-0`, version: 1, timestamp: 1, ...extra } };
}

function emit(options, event) {
  options.onmessage({ id: event.data.event_id, event: event.event, data: JSON.stringify(event.data) });
}

const response = () => new Response(null, { headers: { 'content-type': 'text/event-stream; charset=utf-8' } });

function run(attempts, initial = {}, overrides = {}) {
  const recovery = createAgentStreamRecovery({ message: 'Analyze', client_message_id: 'stable-input', ...initial });
  const requests = [];
  const events = [];
  const errors = [];
  const retries = [];
  let closed = 0;
  const connection = startSSEConnection('/chat', {
    method: 'POST', retryOnError: true, getBody: recovery.getBody, isComplete: recovery.isComplete,
  }, {
    onMessage(event) { if (recovery.accept(event)) events.push(event); },
    onError: (error) => errors.push(error),
    onRetry: (state) => retries.push(state),
    onClose: () => { closed += 1; },
  }, {
    connect: async (_url, options) => {
      const index = requests.length;
      requests.push(JSON.parse(options.body));
      await attempts[index](options);
    },
    wait: async () => {},
    ...overrides,
  });
  return { ...connection, requests, events, errors, retries, get closed() { return closed; } };
}

test('loss before response and after HTTP 200 retries the same input ID and payload', async () => {
  const session = run([
    async () => { throw new TypeError('Failed to fetch'); },
    async (options) => { await options.onopen(response()); throw new TypeError('Connection lost'); },
    async (options) => { await options.onopen(response()); emit(options, frame(2, 'done')); },
  ], { timestamp: 100, attachments: [{ file_id: 'f1' }], dataset_ids: ['d1'] });
  await session.finished;
  assert.equal(session.requests.length, 3);
  assert.deepEqual(session.requests[1], session.requests[0]);
  assert.deepEqual(session.requests[2], session.requests[0]);
  assert.equal(session.requests[0].event_seq, 0);
  assert.equal(session.errors.length, 0);
  assert.equal(session.closed, 1);
});

test('midstream loss resumes with an empty message and newest durable cursor, deduplicating overlap', async () => {
  const session = run([
    async (options) => {
      emit(options, frame(12, 'step', { id: 'step-1', status: 'running' }));
      throw new TypeError('Connection lost');
    },
    async (options) => {
      emit(options, frame(12, 'step', { id: 'step-1', status: 'running' }));
      emit(options, frame(13, 'done'));
    },
  ], { event_id: '10-0', event_seq: 10 });
  await session.finished;
  assert.equal(session.requests[1].message, '');
  assert.equal(session.requests[1].client_message_id, undefined);
  assert.equal(session.requests[1].event_seq, 12);
  assert.equal(session.requests[1].event_id, '12-0');
  assert.deepEqual(session.events.map((event) => event.data.seq), [12, 13]);
  assert.equal(session.errors.length, 0);
});

test('clean EOF without a terminal event is recovered and a task finished offline is replayed once', async () => {
  const session = run([
    async (options) => { emit(options, frame(4, 'plan')); },
    async (options) => { emit(options, frame(5, 'message')); emit(options, frame(6, 'done')); },
  ]);
  await session.finished;
  assert.equal(session.retries.length, 1);
  assert.equal(session.requests[1].message, '');
  assert.deepEqual(session.events.map((event) => event.event), ['plan', 'message', 'done']);
  assert.equal(session.closed, 1);
});

for (const terminal of ['done', 'wait', 'error']) {
  test(`${terminal} stops the stream without retries or duplicate terminal delivery`, async () => {
    const session = run([async (options) => {
      emit(options, frame(2, terminal));
      assert.equal(options.signal.aborted, true);
      emit(options, frame(2, terminal));
      emit(options, frame(3, 'message'));
    }]);
    await session.finished;
    assert.equal(session.events.length, 1);
    assert.equal(session.retries.length, 0);
    assert.equal(session.closed, 1);
  });
}

test('HTTP rejection, JSON errors and unsupported event versions fail closed without replay', async () => {
  const failures = [
    async (options) => options.onopen(new Response(null, { status: 403 })),
    async (options) => options.onopen(new Response('{}', { headers: { 'content-type': 'application/json' } })),
    async (options) => options.onmessage({ event: 'message', data: '{broken-json' }),
    async (options) => emit(options, frame(1, 'message', { version: 2 })),
    async (options) => emit(options, frame(1, 'message', { seq: Number.MAX_SAFE_INTEGER + 1 })),
  ];
  for (const failure of failures) {
    const session = run([failure]);
    await session.finished;
    assert.equal(session.requests.length, 1);
    assert.equal(session.retries.length, 0);
    assert.equal(session.errors.length, 1);
    assert.ok(session.errors[0] instanceof SSEConnectionError);
  }
});

test('transport retries are bounded and report failure once after exponential backoff', async () => {
  const session = run(Array.from({ length: 7 }, () => async () => { throw new TypeError('offline'); }));
  await session.finished;
  assert.equal(session.requests.length, 7);
  assert.deepEqual(session.retries.map((retry) => retry.delayMs), [500, 1000, 2000, 4000, 8000, 8000]);
  assert.equal(session.errors.length, 1);
  assert.equal(session.errors[0].kind, 'transport');
  assert.equal(session.closed, 0);
});

test('cancel during backoff (stop/navigation) prevents reconnect and callbacks', async () => {
  let enteredWait;
  const waiting = new Promise((resolve) => { enteredWait = resolve; });
  const session = run([async () => { throw new TypeError('offline'); }], {}, {
    wait: (_delay, signal) => new Promise((resolve) => {
      signal.addEventListener('abort', resolve, { once: true });
      enteredWait();
    }),
  });
  await waiting;
  session.cancel();
  await session.finished;
  assert.equal(session.requests.length, 1);
  assert.equal(session.errors.length, 0);
  assert.equal(session.closed, 0);
});

test('cancel during an active connection ignores already-buffered frames and never retries', async () => {
  let opened;
  let finishAttempt;
  let activeOptions;
  const ready = new Promise((resolve) => { opened = resolve; });
  const session = run([async (options) => {
    activeOptions = options;
    opened();
    await new Promise((resolve) => { finishAttempt = resolve; });
    emit(options, frame(2, 'done'));
  }]);
  await ready;
  session.cancel();
  assert.equal(activeOptions.signal.aborted, true);
  finishAttempt();
  await session.finished;
  assert.equal(session.events.length, 0);
  assert.equal(session.closed, 0);
  assert.equal(session.retries.length, 0);
});

test('non-idempotent generic POST requests never opt into transport retries implicitly', async () => {
  let requests = 0;
  let errors = 0;
  const session = startSSEConnection('/unsafe', { method: 'POST', body: { action: 'write' } }, {
    onError: () => { errors += 1; },
  }, { connect: async () => { requests += 1; throw new TypeError('offline'); } });
  await session.finished;
  assert.equal(requests, 1);
  assert.equal(errors, 1);
});

test('legacy event IDs remain supported and versioned replay upgrades the durable cursor', () => {
  const recovery = createAgentStreamRecovery({ message: '', event_id: 'legacy-0' });
  const legacy = { event: 'message', data: { timestamp: 1, event_id: 'legacy-1' } };
  assert.equal(recovery.getBody().event_seq, undefined);
  assert.equal(recovery.accept(legacy), true);
  assert.equal(recovery.accept(legacy), false);
  assert.equal(recovery.getBody().event_id, 'legacy-1');
  assert.equal(recovery.accept(frame(10, 'message')), true);
  assert.equal(recovery.getBody().event_seq, 10);
});

test('UI selection changes cannot mutate the payload paired with an ambiguous input ID', () => {
  const request = { message: 'Analyze', client_message_id: 'stable', skills: ['geo'], dataset_ids: ['d1'] };
  const recovery = createAgentStreamRecovery(request);
  request.skills.push('other');
  request.dataset_ids[0] = 'd2';
  assert.deepEqual(recovery.getBody().skills, ['geo']);
  assert.deepEqual(recovery.getBody().dataset_ids, ['d1']);
  assert.equal(recovery.getBody().client_message_id, 'stable');
});

test('continuation command retries an ambiguous submission with the same token and input ID', async () => {
  const token = '0123456789abcdef0123456789abcdef';
  const session = run([
    async () => { throw new TypeError('offline'); },
    async (options) => { await options.onopen(response()); throw new TypeError('lost after headers'); },
    async (options) => { emit(options, frame(11, 'message', { role: 'user' })); emit(options, frame(12, 'done')); },
  ], { message: '', resume_from: token, client_message_id: 'continuation-input', event_seq: 10 });
  await session.finished;
  assert.equal(session.requests.length, 3);
  for (const request of session.requests) {
    assert.equal(request.message, '');
    assert.equal(request.resume_from, token);
    assert.equal(request.client_message_id, 'continuation-input');
  }
  assert.equal(session.errors.length, 0);
});

test('accepted continuation reconnects read-only without replaying the resume command', async () => {
  const session = run([
    async (options) => {
      emit(options, frame(11, 'message', { role: 'user' }));
      throw new TypeError('lost');
    },
    async (options) => { emit(options, frame(12, 'done')); },
  ], { message: '', resume_from: '0123456789abcdef0123456789abcdef', client_message_id: 'continuation-input', event_seq: 10 });
  await session.finished;
  assert.equal(session.requests[1].resume_from, undefined);
  assert.equal(session.requests[1].client_message_id, undefined);
  assert.equal(session.requests[1].message, '');
  assert.equal(session.requests[1].event_seq, 11);
  assert.equal(session.requests[1].event_id, '11-0');
  assert.equal(session.requests.length, 2);
});

test('continuation selection cannot change while a stable input is waiting to be accepted', () => {
  const request = { message: '', resume_from: '0123456789abcdef0123456789abcdef', client_message_id: 'continuation-input' };
  const recovery = createAgentStreamRecovery(request);
  request.resume_from = 'abcdef0123456789abcdef0123456789';
  assert.equal(recovery.getBody().resume_from, '0123456789abcdef0123456789abcdef');
  assert.equal(recovery.getBody().client_message_id, 'continuation-input');
});

test('real SSE parser reconnects after a broken byte stream and drains offline terminal replay', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  globalThis.window = { clearTimeout, setTimeout };
  globalThis.document = { removeEventListener() {} };
  const requests = [];
  try {
    const fakeFetch = async (_url, options) => {
      requests.push(JSON.parse(options.body));
      const first = requests.length === 1;
      let pulled = false;
      const stream = new ReadableStream({
        pull(controller) {
          if (pulled) {
            if (first) controller.error(new TypeError('socket disconnected'));
            else controller.close();
            return;
          }
          pulled = true;
          const events = first ? [frame(3, 'step')] : [frame(3, 'step'), frame(4, 'message'), frame(5, 'done')];
          controller.enqueue(new TextEncoder().encode(events.map((event) =>
            `id: ${event.data.event_id}\nevent: ${event.event}\ndata: ${JSON.stringify(event.data)}\n\n`,
          ).join('')));
        },
      });
      return new Response(stream, { headers: { 'content-type': 'text/event-stream' } });
    };
    const session = run([], {}, {
      connect: (url, options) => fetchEventSource(url, { ...options, fetch: fakeFetch }),
    });
    await session.finished;
    assert.equal(requests.length, 2);
    assert.equal(requests[0].message, 'Analyze');
    assert.equal(requests[1].message, '');
    assert.equal(requests[1].event_seq, 3);
    assert.equal(requests[1].event_id, '3-0');
    assert.deepEqual(session.events.map((event) => event.data.seq), [3, 4, 5]);
    assert.equal(session.errors.length, 0);
    assert.equal(session.closed, 1);
  } finally {
    globalThis.window = previousWindow;
    globalThis.document = previousDocument;
  }
});
