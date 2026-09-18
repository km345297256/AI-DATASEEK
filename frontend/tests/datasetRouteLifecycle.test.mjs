import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
import { ref } from 'vue';

const source = readFileSync(new URL('../src/pages/DatasetSeekPage.vue', import.meta.url), 'utf8');
const script = source.match(/<script setup[^>]*>([\s\S]*?)<\/script>/)[1];
const ast = ts.createSourceFile('DatasetSeekPage', script, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
function handler(name, scope) {
  const declaration = ast.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === name);
  assert.ok(declaration, `${name} exists`);
  const compiled = ts.transpileModule(declaration.getText(ast), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None } }).outputText;
  return new Function('scope', `with (scope) { ${compiled}; return ${name}; }`)(scope);
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function harness() {
  const requests = [], actions = [];
  const scope = { datasetLoadGeneration: 0, datasetViewDisposed: false,
    analysisSession: { reset: () => actions.push('reset') },
    getDataCenterDataset: id => { const request = { id, ...deferred() }; requests.push(request); return request.promise; },
    showErrorToast: message => actions.push(message),
    loadDataProducts: async () => {}, loadSuggestedQuestions: async () => {},
    refreshHistory: async () => {}, restoreConversation: async () => actions.push('restore'),
  };
  for (const field of ['dataset', 'selectedDatasetId', 'dataProducts', 'historySessions', 'historyOpen', 'historyLoading', 'inputMessage',
    'suggestedQuestions', 'suggestedQuestionsLoading', 'suggestedQuestionsError', 'productDialogVisible', 'productFileMoveVisible',
    'movingProduct', 'movingProductFile', 'editingProductId', 'expandedProductIds', 'expandedProductDirectories', 'ncViewVisible', 'catalogLoading']) {
    scope[field] = ref();
  }
  scope.isCurrentDatasetLoad = handler('isCurrentDatasetLoad', scope);
  return { scope, requests, actions, load: handler('loadRouteDataset', scope) };
}

test('dataset route reuse resets conversation/preview owner before async loading and ignores older same-ID responses', async () => {
  const h = harness();
  h.scope.dataset.value = { dataset_id: 'old' }; h.scope.inputMessage.value = 'old draft';
  const first = h.load('a');
  assert.deepEqual(h.actions, ['reset']);
  assert.equal(h.scope.dataset.value, undefined); assert.equal(h.scope.inputMessage.value, '');
  const second = h.load('b');
  const third = h.load('a');
  h.requests[2].resolve({ dataset_id: 'a', name: 'latest' }); await third;
  h.requests[1].resolve({ dataset_id: 'b', name: 'obsolete' }); await second;
  h.requests[0].resolve({ dataset_id: 'a', name: 'older same ID' }); await first;
  assert.equal(h.scope.dataset.value.name, 'latest');
  assert.equal(h.scope.selectedDatasetId.value, 'a');
  assert.equal(h.actions.filter(action => action === 'restore').length, 1);
  assert.equal(h.scope.catalogLoading.value, false);
  assert.match(source, /watch\(\(\) => route\.params\.datasetId,[\s\S]*?loadRouteDataset\(id\)[\s\S]*?flush: 'sync'/);
});

test('dataset load finishing after disposal cannot restore a conversation or publish errors', async () => {
  for (const reject of [false, true]) {
    const h = harness(); const pending = h.load('a');
    h.scope.datasetViewDisposed = true;
    if (reject) h.requests[0].reject(Error('obsolete error'));
    else h.requests[0].resolve({ dataset_id: 'a' });
    await pending;
    assert.deepEqual(h.actions, ['reset']);
    assert.equal(h.scope.dataset.value, undefined);
  }
});

test('dataset submission waits for initial catalog and history restoration before accepting a new task', async () => {
  const sent = [];
  const scope = {
    inputMessage: ref('analyse current dataset'), dataset: ref({ dataset_id: 'dataset-a' }),
    catalogLoading: ref(true), isLoading: ref(false), isRestoringHistory: ref(false),
    selectedSkills: ref([]), selectedProfileId: ref('profile'),
    buildDatasetChatCapabilities: id => ({ datasetIds: [id], skills: [], attachments: [], mcpServers: [] }),
    analysisSession: { send: async request => sent.push(request) },
  };
  const submit = handler('submit', scope);
  await submit();
  assert.equal(sent.length, 0);
  assert.equal(scope.inputMessage.value, 'analyse current dataset');
  scope.catalogLoading.value = false;
  await submit();
  assert.equal(sent.length, 1);
  assert.deepEqual(sent[0].datasetIds, ['dataset-a']);
  assert.match(source, /:disabled="catalogLoading \|\| isLoading \|\| isRestoringHistory \|\| !dataset"/);
});

for (const [name, apiName, result, field] of [
  ['loadDataProducts', 'listDatasetDataProducts', [{ product_id: 'old-product' }], 'dataProducts'],
  ['loadSuggestedQuestions', 'generateDatasetSuggestedQuestions', ['a', 'b', 'c', 'd'], 'suggestedQuestions'],
  ['refreshHistory', 'listDatasetChatSessions', [{ session_id: 'old-session' }], 'historySessions'],
]) {
  test(`${name}: stale catalog response cannot overwrite the next dataset's state`, async () => {
    const h = harness(), request = deferred();
    h.scope.selectedDatasetId.value = 'a'; h.scope[apiName] = () => request.promise;
    const pending = handler(name, h.scope)();
    h.scope.datasetLoadGeneration += 1; h.scope.selectedDatasetId.value = 'b';
    h.scope[field].value = ['current'];
    request.resolve(result); await pending;
    assert.deepEqual(h.scope[field].value, ['current']);
  });
}
