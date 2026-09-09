import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
import { nextTick, ref } from 'vue';

import { failRunningSteps } from '../src/utils/chatTimeline.ts';
import { prependHistoricalMessages, projectHistoryMessages } from '../src/utils/sessionHistory.ts';
import { useAnalysisProgress } from '../src/composables/useAnalysisProgress.ts';

// Run the actual SFC handlers with real Vue refs and a controlled pending API.
// Unrelated visual components are deliberately not mounted in these race tests.
const compiledHandlers = new Map();
function pageHandler(page, name, scope) {
  const cacheKey = `${page}:${name}`;
  if (!compiledHandlers.has(cacheKey)) {
    const source = readFileSync(new URL(`../src/pages/${page}.vue`, import.meta.url), 'utf8');
    const script = source.match(/<script setup[^>]*>([\s\S]*?)<\/script>/)[1];
    const ast = ts.createSourceFile(page, script, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
    const statement = ast.statements.find((node) =>
      (ts.isFunctionDeclaration(node) && node.name?.text === name)
      || (ts.isVariableStatement(node) && node.declarationList.declarations.some((item) => item.name.getText(ast) === name)));
    assert.ok(statement, `Actual ${page}.${name} handler exists`);
    const code = ts.transpileModule(statement.getText(ast), {
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
    }).outputText;
    compiledHandlers.set(cacheKey, new Function('scope', `with (scope) { ${code}; return ${name}; }`));
  }
  return compiledHandlers.get(cacheKey)(scope);
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function harness(page) {
  const api = deferred();
  const steps = [{ type: 'user', content: { timestamp: 1, content: 'question' } },
    { type: 'step', content: { timestamp: 2, id: 'step', status: 'running', tools: [] } }];
  const calls = [];
  const scope = {
    chatGeneration: 1, conversationGeneration: 1, viewDisposed: false,
    historyRequest: { abort: () => calls.push('abort-history') },
    sessionId: ref('session-a'), messages: ref(steps),
    isLoading: ref(true), isLoadingHistory: ref(false), isLoadingEarlierHistory: ref(false), isRestoringHistory: ref(false),
    loadingStatus: ref('working'), connectionNotice: ref(''), taskStartedAtMs: ref(10),
    cancelCurrentChat: ref(() => calls.push('cancel-chat')), cancelChat: () => calls.push('cancel-chat'),
    agentApi: { stopSession: () => api.promise }, stopSession: () => api.promise,
    eventBus: { emit: () => calls.push('refresh') }, EVENT_REFRESH_SESSION_LIST: 'refresh',
    failRunningSteps,
    ...useAnalysisProgress(),
  };
  scope.isCurrentSession = (id) => id === scope.sessionId.value;
  scope.failActiveSteps = () => failRunningSteps(scope.messages.value);
  const stop = pageHandler(page, page === 'ChatPage' ? 'handleStop' : 'stop', scope);
  return { scope, api, calls, stop };
}

for (const page of ['ChatPage', 'DatasetSeekPage']) {
  test(`${page}: confirmed stop cancels transport immediately and closes the current running steps`, async () => {
    const { scope, api, calls, stop } = harness(page);
    const pending = stop();
    assert.deepEqual(calls, ['abort-history', 'cancel-chat']);
    assert.equal(scope.messages.value[1].content.status, 'running');
    api.resolve();
    await pending;
    assert.equal(scope.messages.value[1].content.status, 'failed');
    assert.equal(scope.taskStartedAtMs.value, undefined);
  });

  test(`${page}: failed stop request reports uncertainty rather than claiming task failure`, async () => {
    const { scope, api, stop } = harness(page);
    const pending = stop();
    api.reject(new TypeError('offline'));
    await pending;
    assert.equal(scope.messages.value[1].content.status, 'running');
    assert.match(scope.connectionNotice.value, /停止请求未能确认/);
  });

  test(`${page}: late stop acknowledgement cannot end the newly selected conversation`, async () => {
    const { scope, api, stop } = harness(page);
    const pending = stop();
    scope.chatGeneration += 1;
    scope.conversationGeneration += 1;
    scope.sessionId.value = 'session-b';
    scope.taskStartedAtMs.value = 99;
    api.resolve();
    await pending;
    assert.equal(scope.messages.value[1].content.status, 'running');
    assert.equal(scope.taskStartedAtMs.value, 99);
  });

  test(`${page}: late older-history response cannot enter another conversation even if abort is ignored`, async () => {
    const { scope } = harness(page);
    const request = deferred();
    let signal;
    const getHistory = (_id, _seq, requestSignal) => { signal = requestSignal; return request.promise; };
    Object.assign(scope, {
      historyBeforeSeq: ref(10), hasMoreHistory: ref(true), follow: ref(false), shouldFollowTimeline: ref(false),
      chatContainerRef: ref(null), timelineRef: ref(null), nextTick, projectHistoryMessages, prependHistoricalMessages,
      getSessionHistory: getHistory, showErrorToast: () => assert.fail('cancelled history must not display an error'),
    });
    scope.agentApi.getSessionHistory = getHistory;
    const load = pageHandler(page, 'loadEarlierHistory', scope);
    const pending = load();
    scope.historyRequest.abort();
    scope.chatGeneration += 1;
    scope.conversationGeneration += 1;
    scope.sessionId.value = 'session-b';
    request.resolve({ has_more: false, next_before_seq: null, events: [
      { event: 'message', data: { seq: 1, event_id: '1-0', timestamp: 1, role: 'user', content: 'obsolete question' } },
    ] });
    await pending;
    assert.equal(signal.aborted, true);
    assert.equal(scope.messages.value.length, 2);
    assert.equal(scope.messages.value[0].content.content, 'question');
  });
}

test('dataset reconnect callbacks preserve active steps and ignore a superseded stream', () => {
  const { scope } = harness('DatasetSeekPage');
  scope.handleEvent = () => assert.fail('old stream must not dispatch');
  const callbacks = pageHandler('DatasetSeekPage', 'conversationCallbacks', scope)(1);
  callbacks.onRetry({ attempt: 1, maxAttempts: 6 });
  assert.equal(scope.isLoading.value, true);
  assert.match(scope.connectionNotice.value, /正在恢复/);
  assert.equal(scope.messages.value[1].content.status, 'running');
  callbacks.onError(new Error('connection unavailable'));
  assert.equal(scope.messages.value[1].content.status, 'running');
  assert.equal(scope.analysisProgress.value, '');
  scope.conversationGeneration += 1;
  scope.connectionNotice.value = 'new conversation';
  scope.analysisProgress.value = 'new progress';
  callbacks.onRetry({ attempt: 2, maxAttempts: 6 });
  callbacks.onMessage({ event: 'done', data: {} });
  callbacks.onClose();
  assert.equal(scope.connectionNotice.value, 'new conversation');
  assert.equal(scope.analysisProgress.value, 'new progress');
});
