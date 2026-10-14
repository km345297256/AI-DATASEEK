import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
import { ref } from 'vue';
import { shouldRecoverSession } from '../src/utils/sessionRecovery.ts';

const user = { event: 'message', data: { role: 'user', metadata: { data_connectors: ['ncbi-datasets-v2'] } } };
const done = { event: 'done', data: {} };
test('new and metadata-only pending sessions never open a chat stream', () => {
  assert.equal(shouldRecoverSession('pending', []), false);
  assert.equal(shouldRecoverSession('pending', [{ event: 'title', data: { title: 'API sample' } }]), false);
  assert.equal(shouldRecoverSession('pending', [user, done]), false);
  assert.equal(shouldRecoverSession('pending', [user]), true);
  assert.equal(shouldRecoverSession('pending', [user, done, user]), true);
  assert.equal(shouldRecoverSession('running', []), true);
  assert.equal(shouldRecoverSession('completed', [user]), false);
  assert.equal(shouldRecoverSession('waiting', [user]), false);
});

const source = readFileSync(new URL('../src/pages/ChatPage.vue', import.meta.url), 'utf8');
const script = source.match(/<script setup[^>]*>([\s\S]*?)<\/script>/)[1];
const ast = ts.createSourceFile('ChatPage', script, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
function handler(name, scope) {
  const statement = ast.statements.find(node => ts.isVariableStatement(node) && node.declarationList.declarations.some(item => item.name.getText(ast) === name));
  const code = ts.transpileModule(statement.getText(ast), {compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.None}}).outputText;
  return new Function('scope', `with(scope) { ${code}; return ${name}; }`)(scope);
}
test('actual history restoration leaves an empty API sample idle but resumes submitted pending work', async () => {
  for (const events of [[], [user]]) {
    let streams = 0;
    const scope = {
      sessionId: ref('sample'), chatGeneration: 1, historyRequest: undefined,
      isRestoringHistory: ref(false), hasMoreHistory: ref(false), historyBeforeSeq: ref(),
      shareMode: ref('private'), titleManuallySet: ref(false), realTime: ref(true), title: ref(''),
      selectedSkills: ref([]), selectedMcpServers: ref([]), selectedConnectors: ref([]), isLoading: ref(false),
      connectionNotice: ref('old failure'), clearAnalysisProgress() {}, handleEvent() {},
      isCurrentSession: id => id === 'sample', shouldRecoverSession, t: x => x,
      agentApi: { getSessionHistory: async () => ({ events, status: 'pending', title: 'API sample', has_more: false }), clearUnreadMessageCount() {} },
      chat: async () => { streams++; },
    };
    await handler('restoreSession', scope)();
    assert.equal(streams, events.length ? 1 : 0);
    assert.equal(scope.isRestoringHistory.value, false);
    assert.deepEqual(scope.selectedConnectors.value, events.length ? ['ncbi-datasets-v2'] : []);
    if (!events.length) {
      assert.equal(scope.isLoading.value, false);
      assert.equal(scope.connectionNotice.value, '');
    }
  }
});

test('stopping recovery cancels local retries and invalidates late callbacks without stopping the backend', () => {
  let cancelled = 0;
  const scope = { chatGeneration: 3, cancelCurrentChat: ref(() => cancelled++), isLoading: ref(true),
    taskStartedAtMs: ref(1), connectionNotice: ref('retry'), clearAnalysisProgress() {} };
  handler('stopReconnecting', scope)();
  assert.equal(cancelled, 1);
  assert.equal(scope.chatGeneration, 4);
  assert.equal(scope.isLoading.value, false);
  assert.equal(scope.cancelCurrentChat.value, null);
  assert.match(scope.connectionNotice.value, /已停止自动重连/);
});
