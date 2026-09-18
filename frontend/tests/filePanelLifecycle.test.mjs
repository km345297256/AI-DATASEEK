import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { afterEach, beforeEach, test } from 'node:test';
import { effectScope } from 'vue';
import { createMemoryHistory, createRouter, isNavigationFailure, NavigationFailureType } from 'vue-router';
import { installFilePanelRouteLifecycle, useFilePanel } from '../src/composables/useFilePanel.ts';
import { eventBus } from '../src/utils/eventBus.ts';
import { EVENT_SHOW_FILE_PANEL } from '../src/constants/event.ts';

const panel = useFilePanel();
const fileA = { file_id: 'file-a', filename: 'analysis-a.tif', upload_date: '2026-09-07' };
const fileB = { file_id: 'file-b', filename: 'analysis-b.shp', upload_date: '2026-09-07' };
const files = [fileA, fileB];
const source = (file) => readFileSync(new URL(file, import.meta.url), 'utf8');

beforeEach(() => panel.hideFilePanel());
afterEach(() => panel.hideFilePanel());

async function testRouter(t, initialPath) {
  const MainLayout = {};
  const ShareLayout = {};
  const DatasetSeekPage = {};
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/dataset/seek/:datasetId', component: DatasetSeekPage },
      {
        path: '/chat',
        component: MainLayout,
        children: [
          { path: 'datasets', alias: '/datasets', component: {} },
          { path: ':sessionId', component: {} },
        ],
      },
      { path: '/share', component: ShareLayout, children: [{ path: ':sessionId', component: {} }] },
    ],
  });
  const removeLifecycle = installFilePanelRouteLifecycle(router);
  t.after(removeLifecycle);
  await router.push(initialPath);
  return router;
}

function assertClosed() {
  assert.equal(panel.isShow.value, false);
  assert.equal(panel.fileInfo.value, undefined);
  assert.deepEqual(panel.relatedFiles.value, []);
  assert.equal(panel.visible.value, true);
}

function deferred() {
  let resolve;
  const promise = new Promise((complete) => { resolve = complete; });
  return { promise, resolve };
}

test('dataset exploration to newly mounted management layout clears shared preview state', async (t) => {
  const router = await testRouter(t, '/dataset/seek/dataset-a');
  const previousLayout = router.currentRoute.value.matched[0].components.default;
  panel.showFilePanel(fileA, files);
  await router.push('/datasets');
  assert.notEqual(router.currentRoute.value.matched[0].components.default, previousLayout);
  assertClosed();
});

test('switching tasks inside a reused main layout clears the previous task file', async (t) => {
  const router = await testRouter(t, '/chat/task-a');
  const previousLayout = router.currentRoute.value.matched[0].components.default;
  panel.showFilePanel(fileA, files);
  await router.push('/chat/task-b');
  assert.equal(router.currentRoute.value.matched[0].components.default, previousLayout);
  assertClosed();
  assert.equal(panel.showFilePanel(fileB), true);
  assert.equal(panel.fileInfo.value.file_id, fileB.file_id);
  assert.deepEqual(panel.relatedFiles.value, []);
});

test('leaving a shared analysis for management clears the shared layout preview', async (t) => {
  const router = await testRouter(t, '/share/task-a');
  panel.showFilePanel(fileA, files);
  await router.push('/datasets');
  assertClosed();
});

test('an aborted navigation retains the current file and pending preparation', async (t) => {
  const router = await testRouter(t, '/chat/task-a');
  router.beforeEach((to) => to.path === '/datasets' ? false : undefined);
  panel.showFilePanel(fileA, files);
  const request = panel.beginFilePreview();
  const failure = await router.push('/datasets');
  assert.equal(isNavigationFailure(failure, NavigationFailureType.aborted), true);
  assert.equal(panel.isShow.value, true);
  assert.equal(panel.fileInfo.value.file_id, fileA.file_id);
  assert.deepEqual(panel.relatedFiles.value, files);
  assert.equal(request.isCurrent(), true);
  assert.equal(request.show(fileB), true);
});

test('a duplicate navigation retains the current preview', async (t) => {
  const router = await testRouter(t, '/chat/task-a');
  panel.showFilePanel(fileA, files);
  const request = panel.beginFilePreview();
  const failure = await router.push('/chat/task-a');
  assert.equal(isNavigationFailure(failure, NavigationFailureType.duplicated), true);
  assert.equal(panel.isShow.value, true);
  assert.equal(panel.fileInfo.value.file_id, fileA.file_id);
  assert.deepEqual(panel.relatedFiles.value, files);
  assert.equal(request.isCurrent(), true);
});

test('closing clears file references and restores visibility for subsequent previews', () => {
  panel.showFilePanel(fileA, files);
  panel.visible.value = false;
  panel.hideFilePanel();
  assertClosed();
  panel.showFilePanel(fileB);
  assert.equal(panel.visible.value, true);
  assert.equal(panel.isShow.value, true);
  assert.equal(panel.fileInfo.value.file_id, fileB.file_id);
  assert.deepEqual(panel.relatedFiles.value, []);
});

test('an async preparation completing after navigation cannot reopen the panel', async (t) => {
  const router = await testRouter(t, '/dataset/seek/dataset-a');
  const request = panel.beginFilePreview();
  const preparation = deferred();
  const completion = preparation.promise.then((file) => request.show(file, files));
  await router.push('/datasets');
  preparation.resolve(fileA);
  assert.equal(await completion, false);
  assert.equal(request.isCurrent(), false);
  assertClosed();
});

test('closing while preparation is pending invalidates its late response', () => {
  panel.showFilePanel(fileA, files);
  const request = panel.beginFilePreview();
  panel.hideFilePanel();
  assert.equal(request.isCurrent(), false);
  assert.equal(request.show(fileB, files), false);
  assertClosed();
});

test('opening another file invalidates pending preparation without replacing the new file', () => {
  const request = panel.beginFilePreview();
  panel.showFilePanel(fileB, [fileB]);
  assert.equal(request.show(fileA, files), false);
  assert.equal(panel.fileInfo.value.file_id, fileB.file_id);
  assert.deepEqual(panel.relatedFiles.value, [fileB]);
});

test('a newer preparation invalidates an older one even before either completes', () => {
  const oldRequest = panel.beginFilePreview();
  const newRequest = panel.beginFilePreview();
  assert.equal(oldRequest.isCurrent(), false);
  assert.equal(oldRequest.show(fileA, files), false);
  assert.equal(panel.isShow.value, false);
  assert.equal(newRequest.isCurrent(), true);
  assert.equal(newRequest.show(fileB, [fileB]), true);
  assert.equal(panel.fileInfo.value.file_id, fileB.file_id);
  assert.equal(oldRequest.show(fileA, files), false);
  assert.equal(panel.fileInfo.value.file_id, fileB.file_id);
});

test('resetting a conversation without URL changes invalidates a pending preparation', async (t) => {
  const router = await testRouter(t, '/dataset/seek/dataset-a');
  const path = router.currentRoute.value.fullPath;
  const request = panel.beginFilePreview();
  panel.hideFilePanel();
  assert.equal(router.currentRoute.value.fullPath, path);
  assert.equal(request.show(fileA, files), false);
  assertClosed();
});

test('a disposed component scope cannot open a preview or complete pending preparation', () => {
  const scope = effectScope();
  const scopedPanel = scope.run(() => useFilePanel());
  const request = scopedPanel.beginFilePreview();
  scope.stop();
  assert.equal(scopedPanel.showFilePanel(fileA, files), false);
  assert.equal(request.isCurrent(), false);
  assert.equal(request.show(fileA, files), false);
  assert.equal(scopedPanel.beginFilePreview().show(fileA, files), false);
  assertClosed();
});

test('disposing an old owner cannot replace another active owner preview', () => {
  const oldScope = effectScope();
  const oldPanel = oldScope.run(() => useFilePanel());
  oldScope.stop();
  panel.showFilePanel(fileB, [fileB]);
  assert.equal(oldPanel.showFilePanel(fileA, files), false);
  assert.equal(panel.fileInfo.value.file_id, fileB.file_id);
});

test('a disposed owner cannot invalidate an active owner pending preparation', () => {
  const oldScope = effectScope();
  const oldPanel = oldScope.run(() => useFilePanel());
  oldScope.stop();
  const currentRequest = panel.beginFilePreview();
  assert.equal(oldPanel.beginFilePreview().isCurrent(), false);
  assert.equal(currentRequest.isCurrent(), true);
  assert.equal(currentRequest.show(fileB, [fileB]), true);
  assert.equal(panel.fileInfo.value.file_id, fileB.file_id);
});

test('a disposed owner cannot close another owner preview or invalidate its preparation', () => {
  const oldScope = effectScope();
  const oldPanel = oldScope.run(() => useFilePanel());
  oldScope.stop();
  panel.showFilePanel(fileB, [fileB]);
  const currentRequest = panel.beginFilePreview();
  oldPanel.hideFilePanel();
  assert.equal(panel.isShow.value, true);
  assert.equal(panel.fileInfo.value.file_id, fileB.file_id);
  assert.equal(currentRequest.isCurrent(), true);
});

test('current preparation opens normally and can only be consumed once', () => {
  const request = panel.beginFilePreview();
  assert.equal(request.isCurrent(), true);
  assert.equal(request.show(fileA, files), true);
  assert.equal(panel.isShow.value, true);
  assert.equal(panel.fileInfo.value.file_id, fileA.file_id);
  assert.deepEqual(panel.relatedFiles.value, files);
  assert.equal(request.isCurrent(), false);
  assert.equal(request.show(fileB), false);
  assert.equal(panel.fileInfo.value.file_id, fileA.file_id);
});

test('obsolete requests do not emit panel-opening events', (t) => {
  let opened = 0;
  const onShow = () => { opened += 1; };
  eventBus.on(EVENT_SHOW_FILE_PANEL, onShow);
  t.after(() => eventBus.off(EVENT_SHOW_FILE_PANEL, onShow));
  const request = panel.beginFilePreview();
  panel.hideFilePanel();
  assert.equal(request.show(fileA, files), false);
  assert.equal(opened, 0);
  assert.equal(panel.showFilePanel(fileB), true);
  assert.equal(opened, 1);
});

test('application router installs the shared lifecycle independently of layout mounting', () => {
  const router = source('../src/router/index.ts');
  assert.match(router, /import\s*\{\s*installFilePanelRouteLifecycle\s*\}\s*from\s*['"]@\/composables\/useFilePanel['"]/);
  assert.match(router, /installFilePanelRouteLifecycle\(router\)/);
});

test('same-page conversation reset closes previews before clearing the previous task', () => {
  const page = source('../src/pages/DatasetSeekPage.vue');
  const controller = source('../src/composables/useAnalysisSession.ts');
  assert.match(page, /onReset: \(\) => \{ hideFilePanel\(\)/);
  const reset = controller.slice(controller.indexOf('function reset()'), controller.indexOf('async function restore('));
  assert.ok(reset.indexOf('options.onReset?.()') < reset.indexOf('messages.value = []'));
  const load = page.slice(page.indexOf('async function loadConversation('), page.indexOf('async function restoreConversation('));
  assert.match(load, /analysisSession\.restore\(targetSessionId\)/);
  const fresh = page.slice(page.indexOf('function newConversationFromHistory()'), page.indexOf('onMounted(async'));
  assert.match(fresh, /analysisSession\.reset\(\)/);
});

test('async shapefile preparation captures its request before awaiting and uses guarded completion', () => {
  const page = source('../src/components/AnalysisConversation.vue');
  const start = page.indexOf('async function openShapefilePreview(');
  assert.ok(start >= 0);
  const preview = page.slice(start, page.indexOf('function openMolecularPreview(', start));
  const capture = preview.indexOf('const request = beginFilePreview()');
  const preparation = preview.indexOf('await prepareShapefilePreview(');
  assert.ok(capture >= 0 && preparation > capture);
  assert.match(preview.slice(preparation), /if \(selected\) request\.show\(selected, layer!\.components\)/);
  assert.match(preview, /if \(request\.isCurrent\(\) && currentSession === props\.sessionId\) showErrorToast/);
  assert.doesNotMatch(preview, /\bshowFilePanel\(/);
});

test('switching to the tool panel resets previews and unregisters only its own handler', () => {
  const component = source('../src/components/FilePanel.vue');
  assert.match(component, /const handleToolPanelShown = \(\) =>\s*\{\s*hideFilePanel\(\)\s*visible\.value = false/);
  assert.match(component, /eventBus\.on\(EVENT_SHOW_TOOL_PANEL, handleToolPanelShown\)/);
  assert.match(component, /eventBus\.off\(EVENT_SHOW_TOOL_PANEL, handleToolPanelShown\)/);
});
