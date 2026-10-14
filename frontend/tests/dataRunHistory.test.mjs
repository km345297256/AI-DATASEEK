import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import ts from 'typescript';

const source = readFileSync(new URL('../src/composables/useDataRuns.ts', import.meta.url), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
function harness() {
  const requests = [];
  let unmount;
  const module = { exports: {} };
  const scope = vue.effectScope();
  new Function('require', 'module', 'exports', code)((name) => {
    if (name === 'vue') return { ...vue, onUnmounted: (callback) => { unmount = callback; } };
    if (name === '@/api/agent') return { getDataRuns: (sessionId, before, signal) => new Promise((resolve, reject) => requests.push({ sessionId, before, signal, resolve, reject })) };
    assert.fail(name);
  }, module, module.exports);
  const props = vue.reactive({ sessionId: 'first', revision: 1 });
  const state = scope.run(() => module.exports.useDataRuns(vue.toRef(props, 'sessionId'), vue.toRef(props, 'revision')));
  return { state, props, requests, close: () => { unmount(); scope.stop(); } };
}
const flush = async () => { await Promise.resolve(); await vue.nextTick(); };
const page = (id, next = null) => ({ runs: [{ run_id: id, status: 'unknown', steps: [], skills: [] }], next_before_seq: next });

test('task status loads immediately and stale responses cannot cross session boundaries', async () => {
  const h = harness();
  try {
    assert.equal(h.requests.length, 1);
    await flush();
    assert.equal(h.requests[0].sessionId, 'first');
    h.props.sessionId = 'second';
    await flush();
    assert.equal(h.requests[0].signal.aborted, true);
    h.requests[1].resolve(page('second-run'));
    await flush();
    h.requests[0].resolve(page('first-run'));
    await flush();
    assert.deepEqual(h.state.runs.value.map(run => run.run_id), ['second-run']);
  } finally { h.close(); }
});

test('pagination deduplicates run identities and loading failures do not become empty success', async () => {
  const h = harness();
  try {
    await flush();
    h.requests[0].resolve(page('one', 10));
    await flush();
    const pending = h.state.load(true);
    assert.equal(h.requests[1].before, 10);
    h.requests[1].resolve({ runs: [...page('one').runs, ...page('two').runs], next_before_seq: null });
    await pending;
    assert.deepEqual(h.state.runs.value.map(run => run.run_id), ['one', 'two']);
    const failed = h.state.load();
    h.requests[2].reject(new Error('/private/secret'));
    await failed;
    assert.match(h.state.error.value, /暂时无法读取/);
    assert.doesNotMatch(h.state.error.value, /private/);
    assert.equal(h.state.runs.value.length, 2);
  } finally { h.close(); }
});

test('unmount aborts requests and prevents a late update', async () => {
  const h = harness();
  await flush();
  h.close();
  assert.equal(h.requests[0].signal.aborted, true);
  h.requests[0].resolve(page('late'));
  await flush();
  assert.deepEqual(h.state.runs.value, []);
});
