import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
import { ref } from 'vue';
import { useAnalysisProgress } from '../src/composables/useAnalysisProgress.ts';
import { isAnalysisProgressEvent, isAnalysisProgressMessage } from '../src/utils/analysisProgress.ts';
import { acceptAgentEvent, createAgentEventCursor } from '../src/utils/agentEventCursor.ts';
import { projectHistoryMessages, prependHistoricalMessages } from '../src/utils/sessionHistory.ts';

const progress = (seq = 1, stage = 'verifying_execution', extra = {}) => ({
  event: 'message', data: { seq, timestamp: seq, role: 'assistant', content: 'private diagnostic must not be shown',
    metadata: { analysis_progress: { stage } }, ...extra },
});
const user = (seq = 1) => ({ event: 'message', data: { seq, timestamp: seq, role: 'user', content: '分析数据' } });
const outcome = { status: 'succeeded', reason_code: 'completed', missing: [], can_resume: false };
const final = (seq = 3) => ({ event: 'message', data: { seq, timestamp: seq, role: 'assistant', content: '已完成的真实结论', metadata: { analysis_outcome: outcome } } });

function pageHandler(page, name, scope) {
  const source = readFileSync(new URL(`../src/pages/${page}.vue`, import.meta.url), 'utf8');
  const script = source.match(/<script setup[^>]*>([\s\S]*?)<\/script>/)[1];
  const ast = ts.createSourceFile(page, script, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const statement = ast.statements.find((node) =>
    (ts.isFunctionDeclaration(node) && node.name?.text === name)
    || (ts.isVariableStatement(node) && node.declarationList.declarations.some((item) => item.name.getText(ast) === name)));
  assert.ok(statement, `${page}.${name} exists`);
  const code = ts.transpileModule(statement.getText(ast), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
  }).outputText;
  return new Function('scope', `with (scope) { ${code}; return ${name}; }`)(scope);
}

function harness(page) {
  const scope = {
    ...useAnalysisProgress(), isAnalysisProgressEvent, isAnalysisProgressMessage,
    acceptAgentEvent, eventCursor: createAgentEventCursor(), messages: ref([]), timelineRevision: ref(0),
    lastEventId: ref(), lastEventSeq: ref(), isLoading: ref(true), taskStartedAtMs: ref(),
    completionAdvice: ref(), currentPlan: ref(),
    isPlaceholderAssistantMessage: () => false, isLegacyPlanProgressMessage: () => false,
    completeRunningSteps() {}, failRunningSteps() {}, insertTaskExecutionSummary() {},
    eventBus: { emit() {} }, EVENT_REFRESH_SESSION_LIST: 'refresh', refreshHistory() {},
    handleToolEvent() {}, handleTool() {}, handleStepEvent() {}, handleStep() {},
    handlePlanEvent() {}, handleTitleEvent() {}, handleErrorEvent() {},
  };
  scope.startUserTurn = scope.beginAnalysisProgress;
  const messageHandler = pageHandler(page, page === 'DatasetSeekPage' ? 'handleMessage' : 'handleMessageEvent', scope);
  scope.handleMessage = scope.handleMessageEvent = messageHandler;
  return { scope, receive: pageHandler(page, 'handleEvent', scope) };
}

for (const page of ['ChatPage', 'DatasetSeekPage', 'SharePage']) {
  test(`${page}: repeated progress changes one transient status without appending or scrolling chat`, () => {
    const { scope, receive } = harness(page);
    for (let seq = 1; seq <= 100; seq++) receive(progress(seq));
    assert.equal(scope.messages.value.length, 0);
    assert.equal(scope.timelineRevision.value, 0);
    assert.equal(scope.lastEventSeq.value, 100);
    assert.equal(scope.analysisProgress.value, '正在核验执行状态…');
    receive(progress(101, 'completing_results'));
    assert.equal(scope.analysisProgress.value, '正在补全分析结果…');
    receive(progress(50)); // Transport replay must not roll back the live stage.
    assert.equal(scope.analysisProgress.value, '正在补全分析结果…');
  });

  test(`${page}: final outcome remains a genuine answer and closes late progress`, () => {
    const { scope, receive } = harness(page);
    receive(progress(1));
    receive(final(2));
    receive(progress(3));
    assert.equal(scope.analysisProgress.value, '');
    assert.equal(scope.messages.value.length, 1);
    assert.equal(scope.messages.value[0].content.content, '已完成的真实结论');
    assert.deepEqual(scope.messages.value[0].content.metadata.analysis_outcome, outcome);
    receive(user(4));
    receive(progress(5));
    assert.equal(scope.analysisProgress.value, '正在核验执行状态…');
  });

  test(`${page}: done, failure and waiting clear status and reject late hints`, () => {
    for (const event of ['done', 'error', 'wait']) {
      const { scope, receive } = harness(page);
      receive(progress(1));
      receive({ event, data: { seq: 2, timestamp: 2, error: 'execution failed' } });
      receive(progress(3));
      assert.equal(scope.analysisProgress.value, '');
      assert.equal(scope.messages.value.filter((message) => message.content.metadata?.analysis_progress).length, 0);
    }
  });
}

test('cancel/disconnect clears progress until a new turn explicitly begins', () => {
  const state = useAnalysisProgress();
  state.updateAnalysisProgress(progress());
  state.clearAnalysisProgress();
  state.updateAnalysisProgress(progress(2));
  assert.equal(state.analysisProgress.value, '');
  state.beginAnalysisProgress();
  state.updateAnalysisProgress(progress(3, 'completing_results'));
  assert.equal(state.analysisProgress.value, '正在补全分析结果…');
});

test('progress stays stable while verification tools report their own lifecycle', () => {
  const state = useAnalysisProgress();
  state.updateAnalysisProgress(progress());
  state.updateAnalysisProgress({ event: 'tool', data: { status: 'calling' } });
  state.updateAnalysisProgress({ event: 'tool', data: { status: 'called' } });
  assert.equal(state.analysisProgress.value, '正在核验执行状态…');
  state.updateAnalysisProgress(final());
  assert.equal(state.analysisProgress.value, '');
});

test('progress labels are controlled even for unknown or malformed metadata', () => {
  const state = useAnalysisProgress();
  for (const value of [null, {}, { stage: '/Users/private/key' }, { stage: '__proto__' }, 'invalid']) {
    state.updateAnalysisProgress(progress(1, '', { metadata: { analysis_progress: value } }));
    assert.equal(state.analysisProgress.value, '正在处理分析…');
  }
});

test('typed final outcomes, real attachments and unmarked/user text are never swallowed as progress', () => {
  for (const data of [
    { ...progress().data, metadata: { analysis_progress: { stage: 'verifying_execution' }, analysis_outcome: outcome } },
    { ...progress().data, attachments: [{ file_id: 'actual-upload' }] },
    { ...progress().data, role: 'user' },
    { role: 'assistant', content: '正在核验执行状态…', timestamp: 1 },
  ]) {
    assert.equal(isAnalysisProgressMessage(data), false);
    assert.ok(projectHistoryMessages([{ event: 'message', data }]).length >= 1);
  }
});

test('eager and paginated history consistently hide all progress without mutating server events', () => {
  const old = [user(1), progress(2), progress(3), final(4), { event: 'done', data: { seq: 5, timestamp: 5 } }];
  const latest = [user(6), progress(7), final(8)];
  const snapshot = JSON.stringify([...old, ...latest]);
  for (const datasetMode of [false, true]) {
    const eager = projectHistoryMessages([...old, ...latest], datasetMode);
    const paged = prependHistoricalMessages(projectHistoryMessages(old, datasetMode), projectHistoryMessages(latest, datasetMode));
    assert.deepEqual(eager, paged);
    assert.equal(eager.filter((message) => message.type === 'assistant').length, 2);
    assert.ok(eager.every((message) => !message.content.metadata?.analysis_progress));
  }
  assert.equal(JSON.stringify([...old, ...latest]), snapshot);
});

test('each page renders the shared transient state outside ChatMessage and clears it on unmount', () => {
  for (const page of ['ChatPage', 'DatasetSeekPage', 'SharePage']) {
    const source = readFileSync(new URL(`../src/pages/${page}.vue`, import.meta.url), 'utf8');
    assert.match(source, /analysisProgress \|\|/);
    assert.match(source, /onUnmounted\(\(\) => \{[\s\S]*?clearAnalysisProgress\(\)/);
  }
});

test('shared replay ignores a late response after switching away and after disposal', async () => {
  for (const dispose of [false, true]) {
    let resolve;
    const request = new Promise((done) => { resolve = done; });
    const scope = {
      ...useAnalysisProgress(), sessionId: ref('old-session'), replayGeneration: 1, viewDisposed: false,
      loadReplaySession: () => request, handleEvent: () => assert.fail('obsolete replay must not render'),
      realTime: ref(false), follow: ref(true), isLoading: ref(false), showErrorToast() {}, t: (value) => value,
    };
    const restore = pageHandler('SharePage', 'restoreSession', scope);
    const pending = restore();
    if (dispose) scope.viewDisposed = true;
    else scope.replayGeneration += 1;
    scope.analysisProgress.value = 'new view';
    resolve({ events: [progress()] });
    await pending;
    assert.equal(scope.analysisProgress.value, 'new view');
  }
});

test('shared timed replay cannot append or clear a new view after its timer resumes', async () => {
  let wake;
  const scope = {
    ...useAnalysisProgress(), sessionId: ref('old-session'), replayGeneration: 1, viewDisposed: false,
    router: { currentRoute: { value: { params: { sessionId: 'old-session' } } } },
    loadReplaySession: async () => ({ events: [progress()] }),
    handleEvent: () => assert.fail('obsolete timer must not render'),
    realTime: ref(false), isLoading: ref(false), jumpToEnd: ref(false), replayCompleted: ref(false),
    hideFilePanel() {}, toolPanel: ref(null), showErrorToast() {}, t: (value) => value,
    setTimeout: (callback) => { wake = callback; return 1; },
  };
  scope.resetState = () => { scope.replayGeneration += 1; scope.beginAnalysisProgress(); };
  const replay = pageHandler('SharePage', 'replay', scope);
  const pending = replay();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(typeof wake, 'function');
  scope.replayGeneration += 1;
  scope.analysisProgress.value = 'new view';
  wake();
  await pending;
  assert.equal(scope.analysisProgress.value, 'new view');
  assert.equal(scope.replayCompleted.value, false);
});
