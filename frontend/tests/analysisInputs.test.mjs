import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import ts from 'typescript';
import { toggleInputFile } from '../src/utils/analysisInputs.ts';

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function harness() {
  const requests = [];
  const source = readFileSync(new URL('../src/composables/useAnalysisInputs.ts', import.meta.url), 'utf8');
  const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', compiled)(name => {
    if (name === 'vue') return vue;
    if (name === '../utils/analysisInputs') return { toggleInputFile };
    if (name === '../api/agent') return { getSessionAnalysisInputs(id, signal) {
      const request = { id, signal, ...deferred() }; requests.push(request); return request.promise;
    } };
    assert.fail(`Unexpected dependency ${name}`);
  }, module, module.exports);
  const scope = vue.effectScope();
  const id = vue.ref('a');
  const running = vue.ref(false);
  const state = scope.run(() => module.exports.useAnalysisInputs(id, running));
  return { id, running, state, requests, dispose: () => scope.stop() };
}
const files = [{ file_id: 'one', filename: 'same.csv' }, { file_id: 'two', filename: 'same.csv' }];

test('inputs initially inherit server scope; explicit clearing is distinct from unknown', async () => {
  const h = harness();
  assert.equal(h.state.selectedIds.value, undefined);
  h.requests[0].resolve({ files, selected_file_ids: ['one'] });
  await vue.nextTick();
  assert.deepEqual(h.state.selectedIds.value, ['one']);
  h.state.toggle('one');
  assert.deepEqual(h.state.selectedIds.value, []);
  h.state.toggle('two');
  assert.deepEqual(h.state.selectedIds.value, ['two']);
  h.dispose();
});

test('late response cannot display files from a previously selected conversation', async () => {
  const h = harness();
  h.id.value = 'b'; await vue.nextTick();
  assert.equal(h.requests[0].signal.aborted, true);
  h.requests[1].resolve({ files: [files[1]], selected_file_ids: ['two'] });
  await vue.nextTick();
  h.requests[0].resolve({ files: [files[0]], selected_file_ids: ['one'] });
  await vue.nextTick();
  assert.deepEqual(h.state.selectedIds.value, ['two']);
  assert.deepEqual(h.state.files.value, [files[1]]);
  h.dispose();
});

test('changing session immediately clears previous choices before a pending first submission', async () => {
  const h = harness();
  h.requests[0].resolve({ files, selected_file_ids: ['one'] }); await vue.nextTick();
  h.state.toggle('two');
  assert.deepEqual(h.state.selectedIds.value, ['one', 'two']);
  h.id.value = 'b';
  // No nextTick: the new request must inherit its own server scope, not send
  // IDs selected in the conversation the user just left.
  assert.equal(h.state.selectedIds.value, undefined);
  assert.deepEqual(h.state.files.value, []);
  assert.equal(h.state.loading.value, true);
  assert.equal(h.requests[0].signal.aborted, true);
  h.dispose();
});

test('in-flight task locks scope selection; completion reloads authoritative input inventory', async () => {
  const h = harness();
  h.requests[0].resolve({ files, selected_file_ids: ['one'] }); await vue.nextTick();
  h.running.value = true; await vue.nextTick();
  h.state.toggle('two');
  assert.deepEqual(h.state.selectedIds.value, ['one']);
  h.running.value = false; await vue.nextTick();
  assert.equal(h.requests.length, 2);
  h.requests[1].resolve({ files, selected_file_ids: ['one', 'two'] }); await vue.nextTick();
  assert.deepEqual(h.state.selectedIds.value, ['one', 'two']);
  h.dispose();
});

test('failed discovery is not a fabricated empty selection and disposal ignores late responses', async () => {
  const h = harness();
  h.requests[0].reject(Error('offline')); await vue.nextTick();
  assert.match(h.state.error.value, /无法确认分析资料/);
  assert.equal(h.state.selectedIds.value, undefined);
  const reload = h.state.refresh();
  h.dispose();
  h.requests[1].resolve({ files, selected_file_ids: ['one'] });
  await reload;
  assert.deepEqual(h.state.files.value, []);
});
