import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
import * as vue from 'vue';

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function harness(overrides = {}) {
  const source = readFileSync(new URL('../src/components/ChatBoxFiles.vue', import.meta.url), 'utf8');
  const script = source.match(/<script setup[^>]*>([\s\S]*?)<\/script>/)[1];
  const compiled = ts.transpileModule(script, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
  const props = vue.reactive({ attachments: [] });
  const requests = [], parts = [], completions = [], unmounted = [];
  const dependencies = {
    vue: { ...vue, onMounted: () => {}, onUnmounted: callback => unmounted.push(callback) },
    'lucide-vue-next': {}, 'vue-i18n': { useI18n: () => ({ t: value => value }) },
    '../utils/fileType': {}, './icons/LoadingSpinnerIcon.vue': {},
    '../composables/useFilePanel': { useFilePanel: () => ({ showFilePanel() {} }) },
    '../api/file': {
      uploadFile: file => { const request = { file, ...deferred() }; requests.push(request); return request.promise; },
      initLargeUpload: file => { const request = { file, ...deferred() }; requests.push(request); return request.promise; },
      uploadLargeFilePart: (...args) => { parts.push(args); return Promise.resolve({ etag: 'etag', size: args[2].size }); },
      completeLargeUpload: (...args) => { completions.push(args); return Promise.resolve({ file_id: 'large-result', filename: 'a.bin' }); },
      ...overrides,
    },
  };
  const scope = vue.effectScope();
  const state = scope.run(() => new Function('require', 'exports', 'defineProps', 'defineExpose',
    `${compiled}\nreturn { files, handleFileSelect, handleLargeFileSelect, processLargeFileUpload, removeFile };`,
  )(name => {
    assert.ok(name in dependencies, `Unexpected dependency ${name}`);
    return dependencies[name];
  }, {}, () => props, () => {}));
  return { state, props, requests, parts, completions, dispose: () => { unmounted.forEach(callback => callback()); scope.stop(); } };
}

const selectedFiles = () => [new File(['first'], 'first.csv'), new File(['second'], 'second.csv')];

for (const mode of ['normal', 'large']) {
  for (const transition of ['session-change', 'unmount']) {
    test(`${mode} multi-file selection stops scheduling files after ${transition}`, async () => {
      const h = harness();
      const input = { files: selectedFiles(), value: 'selected-files' };
      const pending = h.state[mode === 'normal' ? 'handleFileSelect' : 'handleLargeFileSelect']({ target: input });
      assert.equal(h.requests.length, 1);
      if (transition === 'session-change') { h.props.attachments = []; await vue.nextTick(); }
      else h.dispose();
      h.requests[0].resolve(mode === 'normal'
        ? { file_id: 'first-upload', filename: 'first.csv' }
        : { upload_id: 'upload-one', part_size: 2 });
      // Several microtasks allow a wrongly scheduled second upload to surface.
      for (let turn = 0; turn < 10; turn++) await vue.nextTick();
      assert.equal(h.requests.length, 1, 'files from the abandoned selection must not enter another composer');
      await pending;
      if (transition === 'session-change') assert.deepEqual(h.state.files.value, []);
      assert.equal(h.parts.length, 0, 'abandoned multipart upload must not start new parts');
      assert.equal(h.completions.length, 0);
      assert.equal(input.value, '');
      h.dispose();
    });
  }
}

test('removing a multipart placeholder while initialization is pending stops its remaining work', async () => {
  const h = harness();
  const pending = h.state.processLargeFileUpload(selectedFiles()[0]);
  h.state.removeFile(h.state.files.value[0].file_id);
  h.requests[0].resolve({ upload_id: 'upload-one', part_size: 2 });
  await pending;
  assert.equal(h.parts.length, 0);
  assert.equal(h.completions.length, 0);
  assert.deepEqual(h.state.files.value, []);
  h.dispose();
});

test('a normal multi-file selection still uploads every file into its original composer', async () => {
  const h = harness();
  const pending = h.state.handleFileSelect({ target: { files: selectedFiles(), value: 'selected' } });
  h.requests[0].resolve({ file_id: 'one', filename: 'first.csv' });
  for (let turn = 0; turn < 3; turn++) await vue.nextTick();
  assert.equal(h.requests.length, 2);
  h.requests[1].resolve({ file_id: 'two', filename: 'second.csv' });
  await pending;
  assert.deepEqual(h.state.files.value.map(file => file.file_id), ['one', 'two']);
  assert.deepEqual(h.state.files.value.map(file => file.status), ['success', 'success']);
  h.dispose();
});

test('multipart upload preserves the bounded worker pool and completes every part for the active composer', async () => {
  const parts = [];
  const h = harness({ uploadLargeFilePart: (id, number, blob) => {
    const request = { id, number, blob, ...deferred() }; parts.push(request); return request.promise;
  } });
  const pending = h.state.processLargeFileUpload(new File(['1234567890'], 'data.csv'));
  h.requests[0].resolve({ upload_id: 'upload-one', part_size: 2 });
  await vue.nextTick();
  assert.equal(parts.length, 3);
  parts[1].resolve({ etag: 'two', size: 2 });
  for (let turn = 0; turn < 3; turn++) await vue.nextTick();
  assert.equal(parts.length, 4);
  parts[0].resolve({ etag: 'one', size: 2 });
  parts[2].resolve({ etag: 'three', size: 2 });
  for (let turn = 0; turn < 3; turn++) await vue.nextTick();
  assert.equal(parts.length, 5);
  parts[3].resolve({ etag: 'four', size: 2 });
  parts[4].resolve({ etag: 'five', size: 2 });
  await pending;
  assert.equal(h.completions.length, 1);
  assert.deepEqual(h.completions[0][1].map(part => part.part_number).sort(), [1, 2, 3, 4, 5]);
  assert.equal(h.state.files.value[0].status, 'success');
  h.dispose();
});

for (const interruption of ['remove', 'session-change', 'part-failure']) {
  test(`multipart ${interruption} stops queued parts and cannot finalize an abandoned upload`, async t => {
    t.mock.method(console, 'error', () => {});
    const parts = [];
    const h = harness({ uploadLargeFilePart: () => {
      const request = deferred(); parts.push(request); return request.promise;
    } });
    const pending = h.state.processLargeFileUpload(new File(['1234567890'], 'data.csv'));
    h.requests[0].resolve({ upload_id: 'upload-one', part_size: 2 });
    await vue.nextTick();
    assert.equal(parts.length, 3);
    if (interruption === 'remove') h.state.removeFile(h.state.files.value[0].file_id);
    if (interruption === 'session-change') h.props.attachments = [];
    if (interruption === 'part-failure') {
      parts[0].reject(Error('network failure'));
      for (let turn = 0; turn < 3; turn++) await vue.nextTick();
    }
    parts.forEach(part => part.resolve({ etag: 'complete', size: 2 }));
    await pending;
    for (let turn = 0; turn < 3; turn++) await vue.nextTick();
    assert.equal(parts.length, 3);
    assert.equal(h.completions.length, 0);
    if (interruption === 'part-failure') assert.equal(h.state.files.value[0].status, 'failed');
    else assert.deepEqual(h.state.files.value, []);
    h.dispose();
  });
}
