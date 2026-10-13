import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { parse, compileScript } from '@vue/compiler-sfc';
import ts from 'typescript';
const source = readFileSync(new URL('../src/components/DataSourceCandidates.vue', import.meta.url), 'utf8');
const script = compileScript(parse(source).descriptor, { id: 'candidate-test' });
const code = ts.transpileModule(script.content, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
function harness() {
  const requests = [];
  let unmount;
  const module = { exports: {} }, scope = vue.effectScope();
  new Function('require', 'module', 'exports', code)((name) => {
    if (name === 'vue') return { ...vue, onUnmounted: callback => { unmount = callback; } };
    if (name === '../api/dataCandidate') return Object.fromEntries(['listCandidates', 'createCandidate', 'listVersions', 'registerVersion'].map(method =>
      [method, (...args) => new Promise((resolve, reject) => requests.push({ method, args, resolve, reject }))]));
    assert.fail(name);
  }, module, module.exports);
  const props = vue.reactive({ sessionId: 'first', files: [] });
  const state = scope.run(() => module.exports.default.setup(props, { expose() {} }));
  return { state, props, requests, close: () => { unmount(); scope.stop(); } };
}
const flush = async () => { await Promise.resolve(); await vue.nextTick(); };
const candidate = { candidate_id: 'one', title: 'Data', source_url: 'https://example.org/', external_id: 'DS-1' };

test('source panel is lazy, session changes abort and isolate late responses', async () => {
  const h = harness();
  try {
    assert.equal(h.requests.length, 0);
    h.state.open.value = true; await flush();
    h.props.sessionId = 'second'; await flush();
    assert.equal(h.requests[0].args.at(-1).aborted, true);
    h.requests[1].resolve({ candidates: [], next_cursor: null }); await flush();
    h.requests[0].resolve({ candidates: [candidate], next_cursor: null }); await flush();
    assert.deepEqual(h.state.candidates.value, []);
    assert.equal(h.state.busy.value, false);
  } finally { h.close(); }
});

test('failed registration preserves selection and never invents a successful receipt', async () => {
  const h = harness();
  try {
    h.state.selected.value = candidate; h.state.fileIds.value = ['file1'];
    const pending = h.state.register();
    assert.equal(h.requests[0].method, 'registerVersion');
    h.requests[0].reject(new Error('/private/secret'));
    await pending;
    assert.deepEqual(h.state.versions.value, []);
    assert.deepEqual(h.state.fileIds.value, ['file1']);
    assert.match(h.state.error.value, /未确认成功/);
    assert.doesNotMatch(h.state.error.value, /private/);
    assert.equal(h.state.notice.value, '');
  } finally { h.close(); }
});

test('late mutation response does not populate another session or clear its draft', async () => {
  const h = harness();
  try {
    h.state.draft.value = { title: 'Old', source_url: candidate.source_url, external_id: 'one' };
    const pending = h.state.save();
    h.props.sessionId = 'second'; await flush();
    h.state.draft.value.title = 'New';
    h.requests[0].resolve(candidate); await pending;
    assert.deepEqual(h.state.candidates.value, []);
    assert.equal(h.state.draft.value.title, 'New');
    assert.equal(h.state.notice.value, '');
  } finally { h.close(); }
});

test('unmount aborts an in-flight read and suppresses late updates', async () => {
  const h = harness();
  const pending = h.state.load(); h.close();
  assert.equal(h.requests[0].args.at(-1).aborted, true);
  h.requests[0].resolve({ candidates: [candidate], next_cursor: null }); await pending;
  assert.deepEqual(h.state.candidates.value, []);
});
