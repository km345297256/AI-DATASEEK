import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as vue from 'vue';
import { compileScript, parse } from '@vue/compiler-sfc';
import ts from 'typescript';
import { shellConsoleExitText } from '../src/utils/shellConsole.ts';
import { sanitizeToolDisplayText } from '../src/utils/toolDisplay.ts';

for (const [name, record, expected] of [
  ['zero', { status: 'completed', returncode: 0 }, '退出码：0'],
  ['nonzero', { status: 'completed', returncode: 7 }, '退出码：7'],
  ['negative', { status: 'completed', returncode: -9 }, '退出码：-9'],
  ['unknown', { status: 'completed', returncode: null }, '未取得退出码'],
  ['missing', { status: 'completed' }, ''],
  ['old history', { command: 'old' }, ''],
  ['running', { status: 'running', returncode: null }, ''],
  ['boolean', { status: 'completed', returncode: false }, '未取得退出码'],
  ['numeric string', { status: 'completed', returncode: '0' }, '未取得退出码'],
  ['fraction', { status: 'completed', returncode: 0.5 }, '未取得退出码'],
  ['infinity', { status: 'completed', returncode: Infinity }, '未取得退出码'],
  ['unsafe integer', { status: 'completed', returncode: 2 ** 54 }, '未取得退出码'],
  ['output is not proof', { status: 'completed', output: 'Process exited with code 0' }, ''],
]) test(`${name} exit presentation never coerces or infers a process result`, () => {
  assert.equal(shellConsoleExitText(record), expected);
});

const source = readFileSync(new URL('../src/components/toolViews/ShellToolView.vue', import.meta.url), 'utf8');
const code = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'ShellToolView' }).content,
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
function mount(t, console, extra = {}) {
  const mounted = [], unmounted = [];
  const module = { exports: {} };
  const modules = { vue: { ...vue, onMounted: fn => mounted.push(fn), onUnmounted: fn => unmounted.push(fn) },
    '@/api/agent': { viewShellSession: async () => { assert.fail('a snapshot must not request live output'); } },
    '@/utils/toolDisplay': { sanitizeToolDisplayText }, '@/utils/shellConsole': { shellConsoleExitText } };
  new Function('require', 'module', 'exports', code)(id => { assert.ok(id in modules, id); return modules[id]; }, module, module.exports);
  const scope = vue.effectScope();
  const props = vue.reactive({ sessionId: 'session', live: false, toolContent: {
    tool_call_id: 'call', function: 'shell_run', name: 'shell', status: 'called',
    args: { command: 'exit 7' }, content: { console }, ...extra,
  } });
  const state = scope.run(() => module.exports.default.setup(props, { expose() {} }));
  for (const fn of mounted) fn();
  t.after(() => { for (const fn of unmounted) fn(); scope.stop(); });
  return { props, state };
}

test('actual shell details show each process exit even when output is empty', t => {
  const { state } = mount(t, [
    { ps1: '$', command: 'exit 0', output: '', status: 'completed', returncode: 0 },
    { ps1: '$', command: 'exit 7', output: '', status: 'completed', returncode: 7 },
    { ps1: '$', command: 'killed', output: '', status: 'completed', returncode: -9 },
    { ps1: '$', command: 'unknown', output: '', status: 'completed', returncode: null },
  ]);
  for (const label of ['退出码：0', '退出码：7', '退出码：-9', '未取得退出码']) assert.ok(state.shell.value.includes(label));
  assert.doesNotMatch(state.shell.value, /Command completed/);
});

for (const extra of [{ function: 'dataset_analysis_run' }, { function: 'dataset_quicklook' }, { spill: { status: 'stored' } }]) {
  test(`old synthetic ${extra.function ?? 'spill'} summaries do not turn 0/1 into process exits`, t => {
    const { state } = mount(t, [{ ps1: '$', command: 'synthetic', output: 'summary', status: 'completed', returncode: 1 }], extra);
    assert.match(state.shell.value, /summary/);
    assert.doesNotMatch(state.shell.value, /退出码/);
  });
}

test('shell detail status preserves HTML escaping and sensitive-data masking', t => {
  const { state } = mount(t, [{ ps1: '<script>', command: 'API_KEY=secret', output: '<img src=x> /Users/private/data',
    status: 'completed', returncode: 7 }]);
  assert.match(state.shell.value, /&lt;script&gt;/);
  assert.match(state.shell.value, /&lt;img src=x&gt;/);
  assert.doesNotMatch(state.shell.value, /API_KEY=secret|\/Users\/private/);
  assert.match(state.shell.value, /退出码：7/);
});

test('called tool with no console does not claim the command completed successfully', t => {
  const { state } = mount(t, [], { execution_status: 'failed' });
  assert.match(state.shell.value, /未提供命令输出或退出信息/);
  assert.doesNotMatch(state.shell.value, /Command completed|退出码：0/);
});

test('a single structured console record retains its exit without assuming an array', t => {
  const { state } = mount(t, { output: 'result', status: 'completed', returncode: 9 });
  assert.match(state.shell.value, /result/);
  assert.match(state.shell.value, /退出码：9/);
});
