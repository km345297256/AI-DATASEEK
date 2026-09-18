import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { useAnalysisSession } from '../src/composables/useAnalysisSession.ts';

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const event = (seq, name, data) => ({ event: name, data: { seq, timestamp: seq, ...data } });
const history = (id = 'session-a', extra = {}) => ({ session_id: id, status: 'completed', events: [], has_more: false, next_before_seq: null, ...extra });
const source = file => readFileSync(new URL(file, import.meta.url), 'utf8');

function harness(extraApi = {}) {
  const stopRequest = deferred();
  const requests = [], cancels = [];
  const api = {
    chatWithSession: async (...args) => { requests.push(args); return () => cancels.push(args[0]); },
    stopSession: () => stopRequest.promise,
    getSessionHistory: async id => history(id),
    createSession: async () => ({ session_id: 'created', created_at: 5 }),
    createClientMessageId: () => 'message-id', ...extraApi,
  };
  const session = useAnalysisSession({ api });
  session.sessionId.value = 'session-a';
  return { session, requests, cancels, stopRequest };
}

for (const [entry, sourceRequest] of [
  ['upload', { files: [{ file_id: 'upload-a', filename: 'sample.csv' }], inputFileIds: ['previous-upload'] }],
  ['dataset', { datasetIds: ['dataset-a'] }],
  ['mixed', { datasetIds: ['dataset-a'], files: [{ file_id: 'upload-a', filename: 'sample.csv' }], inputFileIds: ['previous-upload'] }],
]) {
  test(`${entry}: shared send preserves opaque sources; omitted follow-up inputs inherit and [] clears`, async () => {
    const { session, requests } = harness();
    await session.send({ message: '分析资料', ...sourceRequest });
    assert.deepEqual(requests[0][4], sourceRequest.files || []);
    assert.deepEqual(requests[0][9], sourceRequest.datasetIds);
    assert.deepEqual(requests[0][12], sourceRequest.inputFileIds);
    requests[0][8].onMessage(event(1, 'done', {})); requests[0][8].onClose();
    await session.send({ message: '继续解释结果' });
    assert.equal(requests[1][9], undefined); assert.equal(requests[1][12], undefined);
    requests[1][8].onMessage(event(2, 'done', {})); requests[1][8].onClose();
    await session.send({ message: '不使用这些输入', inputFileIds: [] });
    assert.deepEqual(requests[2][12], []);
  });

  test(`${entry}: stop cancels transport immediately; only confirmation fails running steps`, async () => {
    const { session, requests, cancels, stopRequest } = harness();
    await session.send({ message: '分析', ...sourceRequest });
    requests[0][8].onMessage(event(1, 'step', { id: 's', status: 'running', description: '分析' }));
    const pending = session.stop();
    assert.deepEqual(cancels, ['session-a']);
    assert.equal(session.messages.value.find(message => message.type === 'step').content.status, 'running');
    stopRequest.resolve(); await pending;
    assert.equal(session.messages.value.find(message => message.type === 'step').content.status, 'failed');
  });

  test(`${entry}: reconnect keeps steps, deduplicates replay and ignores superseded callbacks`, async () => {
    const { session, requests } = harness();
    await session.send({ message: '分析', ...sourceRequest });
    const callbacks = requests[0][8];
    const step = event(1, 'step', { id: 's', status: 'running', description: '分析' });
    callbacks.onMessage(step); callbacks.onMessage(step);
    assert.equal(session.messages.value.filter(message => message.type === 'step').length, 1);
    callbacks.onRetry({ attempt: 1, maxAttempts: 6 });
    assert.match(session.connectionNotice.value, /正在恢复/);
    callbacks.onError(new Error('offline'));
    assert.equal(session.messages.value.find(message => message.type === 'step').content.status, 'running');
    session.reset(); session.sessionId.value = 'session-b'; session.connectionNotice.value = 'new view';
    callbacks.onRetry({ attempt: 2, maxAttempts: 6 }); callbacks.onMessage(event(3, 'error', { error: 'obsolete' })); callbacks.onClose();
    assert.equal(session.connectionNotice.value, 'new view'); assert.equal(session.messages.value.length, 0);
  });
}

test('failed stop preserves uncertainty; late acknowledgement cannot affect a new conversation', async () => {
  for (const reset of [false, true]) {
    const { session, requests, stopRequest } = harness();
    await session.send({ message: 'analysis' });
    requests[0][8].onMessage(event(1, 'step', { id: 's', status: 'running' }));
    const pending = session.stop();
    if (reset) { session.reset(); session.sessionId.value = 'other'; session.connectionNotice.value = 'new'; }
    stopRequest.reject(new Error('offline')); await pending;
    if (reset) assert.equal(session.connectionNotice.value, 'new');
    else { assert.match(session.connectionNotice.value, /停止请求未能确认/); assert.equal(session.messages.value[1].content.status, 'running'); }
  }
});

test('stop keeps the task locked until acknowledgement so it cannot cancel a newly submitted analysis', async () => {
  let stopCalls = 0;
  const stopRequest = deferred();
  const { session, requests } = harness({ stopSession: () => { stopCalls++; return stopRequest.promise; } });
  await session.send({ message: 'original analysis' });
  const stopping = session.stop();
  assert.equal(session.isLoading.value, true);
  assert.match(session.loadingStatus.value, /停止/);
  assert.equal(await session.send({ message: 'next analysis' }), false);
  await session.stop();
  assert.equal(stopCalls, 1);
  assert.equal(requests.length, 1);
  stopRequest.resolve(); await stopping;
  assert.equal(session.isLoading.value, false);
  assert.equal(await session.send({ message: 'next analysis' }), true);
  assert.equal(requests.length, 2);
});

test('a late stop acknowledgement cannot unlock a running analysis in the next conversation', async () => {
  const { session, stopRequest } = harness();
  await session.send({ message: 'original analysis' });
  const stopping = session.stop();
  session.reset(); session.sessionId.value = 'next-session';
  await session.send({ message: 'next analysis' });
  stopRequest.resolve(); await stopping;
  assert.equal(session.isLoading.value, true);
  assert.equal(session.sessionId.value, 'next-session');
});

test('late history and restore cannot enter another conversation even if cancellation is ignored', async () => {
  for (const mode of ['restore', 'earlier']) {
    const request = deferred(); let signal;
    const { session } = harness({ getSessionHistory: (_id, _before, currentSignal) => { signal = currentSignal; return request.promise; } });
    session.historyBeforeSeq.value = 10;
    const pending = mode === 'restore' ? session.restore('session-a') : session.loadEarlierHistory();
    session.reset(); session.sessionId.value = 'session-b';
    request.resolve(history('session-a', { events: [event(1, 'message', { role: 'user', content: 'obsolete' })] }));
    await pending;
    assert.equal(signal.aborted, true); assert.equal(session.sessionId.value, 'session-b'); assert.equal(session.messages.value.length, 0);
  }
});

test('restoring an active task reconnects read-only without resubmitting sources or objective', async () => {
  const { session, requests } = harness({ getSessionHistory: async id => history(id, {
    status: 'running', events: [event(4, 'message', { role: 'user', content: 'original', metadata: { skills: ['s'], mcp_servers: ['m'] } }), event(5, 'step', { id: 's', status: 'running' })],
  }) });
  await session.restore('session-a');
  assert.equal(requests[0][1], ''); assert.equal(requests[0][3], 5);
  assert.equal(requests[0][9], undefined); assert.equal(requests[0][12], undefined);
  assert.deepEqual(session.selectedSkills.value, ['s']); assert.deepEqual(session.selectedMcpServers.value, ['m']);
  requests[0][8].onMessage(event(5, 'step', { id: 's', status: 'running' }));
  assert.equal(session.messages.value.length, 2);
});

test('synchronous close does not leave a cancel handle disabling continuation', async () => {
  const { session } = harness({ chatWithSession: async (...args) => { args[8].onClose(); return () => {}; } });
  await session.send({ message: 'analysis' });
  session.messages.value.push({ type: 'assistant', content: { metadata: { analysis_outcome: { status: 'partial', reason_code: 'artifacts_missing', missing: [{ kind: 'image', min_count: 1, label: '图表' }], can_resume: true, resume_from: 'a'.repeat(32) } } } });
  assert.equal(session.canResumeAnalysis(1), true);
});

test('send snapshots all source selections before asynchronous session creation', async () => {
  const creation = deferred();
  const { session, requests } = harness({ createSession: () => creation.promise });
  session.sessionId.value = undefined;
  const request = { message: 'analysis', files: [{ file_id: 'upload-a', filename: 'a.csv' }], skills: ['s'], mcpServers: ['m'], datasetIds: ['d'], inputFileIds: ['u'] };
  const pending = session.send(request);
  request.files[0].file_id = 'changed'; request.skills.push('changed'); request.mcpServers.push('changed'); request.datasetIds.push('changed'); request.inputFileIds.push('changed');
  creation.resolve({ session_id: 'created', created_at: 1 }); await pending;
  assert.equal(requests[0][4][0].file_id, 'upload-a');
  assert.deepEqual(requests[0][5], ['s']); assert.deepEqual(requests[0][6], ['m']);
  assert.deepEqual(requests[0][9], ['d']); assert.deepEqual(requests[0][12], ['u']);
});

test('both page shells delegate lifecycle operations to the same controller', () => {
  for (const page of ['ChatPage', 'DatasetSeekPage']) {
    const code = source(`../src/pages/${page}.vue`);
    assert.match(code, /useAnalysisSession\(/); assert.match(code, /analysisSession\.send\(/);
    assert.match(code, /analysisSession\.restore\(/); assert.match(code, /analysisSession\.dispose\(/);
    assert.doesNotMatch(code, /(?:function|const) (?:handleEvent|handleMessageEvent|handleStepEvent|conversationCallbacks)\b/);
  }
});

test('a late session creation after reset or disposal cannot send an analysis or bind its session', async () => {
  for (const dispose of [false, true]) {
    const creation = deferred();
    const { session, requests } = harness({ createSession: () => creation.promise });
    session.sessionId.value = undefined;
    const pending = session.send({ message: 'analysis' });
    if (dispose) session.dispose(); else session.reset();
    session.sessionId.value = 'new-view';
    creation.resolve({ session_id: 'obsolete', created_at: 1 });
    await pending;
    assert.equal(session.sessionId.value, 'new-view');
    assert.equal(requests.length, 0);
  }
});

test('older history prepends to live messages without rolling the live event cursor back', async () => {
  const request = deferred();
  const { session } = harness({ getSessionHistory: () => request.promise });
  session.handleEvent(event(10, 'message', { role: 'user', content: 'current' }));
  session.historyBeforeSeq.value = 10;
  const pending = session.loadEarlierHistory();
  session.handleEvent(event(11, 'message', { role: 'assistant', content: 'live result' }));
  request.resolve(history('session-a', { events: [event(1, 'message', { role: 'user', content: 'older' }), event(2, 'message', { role: 'assistant', content: 'old answer' })] }));
  await pending;
  assert.deepEqual(session.messages.value.map(message => message.content.content), ['older', 'old answer', 'current', 'live result']);
  assert.equal(session.lastEventSeq.value, 11);
  session.handleEvent(event(11, 'message', { role: 'assistant', content: 'duplicate' }));
  assert.equal(session.messages.value.length, 4);
});

test('late step/plan updates cannot regress completed status and tools merge by opaque call identity', () => {
  const { session } = harness();
  session.handleEvent(event(1, 'message', { role: 'user', content: 'analysis' }));
  session.handleEvent(event(2, 'plan', { steps: [{ id: 's', status: 'running' }] }));
  session.handleEvent(event(3, 'step', { id: 's', status: 'running' }));
  session.handleEvent(event(4, 'tool', { tool_call_id: 'tool-1', name: 'shell', status: 'calling' }));
  session.handleEvent(event(5, 'tool', { tool_call_id: 'tool-1', name: 'shell', status: 'called' }));
  session.handleEvent(event(6, 'step', { id: 's', status: 'completed' }));
  session.handleEvent(event(7, 'step', { id: 's', status: 'running' }));
  session.handleEvent(event(8, 'plan', { steps: [{ id: 's', status: 'running' }] }));
  const step = session.messages.value.find(message => message.type === 'step').content;
  assert.equal(step.status, 'completed');
  assert.equal(session.plan.value.steps[0].status, 'completed');
  assert.equal(step.tools.length, 1);
  assert.equal(step.tools[0].status, 'called');
});
