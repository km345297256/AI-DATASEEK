import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
import { createRenderer, h, nextTick, onUnmounted, ref, watch } from 'vue';
import { createMemoryHistory, createRouter, RouterView, useRoute } from 'vue-router';

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

async function mountedDatasetRoute(t) {
  const state = harness(), loads = [];
  const watcher = ast.statements.find(node => ts.isExpressionStatement(node)
    && ts.isCallExpression(node.expression) && node.expression.expression.getText(ast) === 'watch'
    && node.expression.arguments[0]?.getText(ast).includes('route.params.datasetId'));
  assert.ok(watcher, 'dataset route watcher exists');
  const compiled = ts.transpileModule(watcher.getText(ast), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None } }).outputText;
  const DatasetPage = { setup() {
    Object.assign(state.scope, { route: useRoute(), watch, datasetViewReady: true,
      loadRouteDataset: id => { const pending = state.load(id); loads.push(pending); return pending; } });
    new Function('scope', `with (scope) { ${compiled} }`)(state.scope);
    onUnmounted(() => { state.scope.datasetViewDisposed = true; });
    return () => h('main');
  } };
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/dataset/seek/:datasetId', component: DatasetPage },
    { path: '/datasets', component: { render: () => h('section') } },
  ] });
  const renderer = createRenderer({
    createElement: () => ({ children: [] }), createText: text => ({ text }), createComment: text => ({ text }),
    insert(node, parent, anchor) {
      if (node.parent) node.parent.children = node.parent.children.filter(child => child !== node);
      const index = anchor ? parent.children.indexOf(anchor) : -1;
      if (index < 0) parent.children.push(node); else parent.children.splice(index, 0, node);
      node.parent = parent;
    },
    remove(node) { if (node.parent) node.parent.children = node.parent.children.filter(child => child !== node); },
    setElementText(node, text) { node.text = text; node.children = []; }, setText(node, text) { node.text = text; },
    parentNode: node => node.parent, nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
    patchProp() {},
  });
  await router.push('/dataset/seek/a');
  const app = renderer.createApp({ render: () => h(RouterView) });
  app.use(router); app.mount({ children: [] });
  t.after(() => app.unmount());
  const initial = state.load('a');
  state.requests[0].resolve({ dataset_id: 'a', name: 'Dataset A' });
  await initial;
  state.actions.length = 0;
  return { ...state, router, loads };
}

test('leaving dataset exploration for management does not reload the departing page with an absent ID', async (t) => {
  const state = await mountedDatasetRoute(t);
  await state.router.push('/datasets');
  await nextTick();
  assert.equal(state.router.currentRoute.value.path, '/datasets');
  assert.equal(state.scope.datasetViewDisposed, true, 'the old page is eventually unmounted');
  assert.deepEqual(state.actions, [], 'navigation must not reset the old conversation or publish a missing-parameter toast');
  assert.equal(state.loads.length, 0);
  assert.equal(state.requests.length, 1);
});

test('a dataset ID change reloads the reused page without waiting for unmount', async (t) => {
  const state = await mountedDatasetRoute(t);
  await state.router.push('/dataset/seek/b');
  assert.equal(state.scope.datasetViewDisposed, false);
  assert.deepEqual(state.actions, ['reset']);
  assert.equal(state.requests[1].id, 'b');
  state.requests[1].resolve({ dataset_id: 'b', name: 'Dataset B' });
  await state.loads[0];
  assert.equal(state.scope.dataset.value.dataset_id, 'b');
  assert.equal(state.scope.selectedDatasetId.value, 'b');
});

test('an initial dataset load without an ID still reports the invalid request', async () => {
  const state = harness();
  await state.load(undefined);
  assert.deepEqual(state.actions, ['reset', '缺少数据集参数，请重新提交']);
  assert.equal(state.requests.length, 0);
  assert.equal(state.scope.catalogLoading.value, false);
});

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
